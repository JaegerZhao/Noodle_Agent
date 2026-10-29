"""Whole-page relation checks for notes containing several cropped panels."""

import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import fitz

from .contracts import canonical, sha
from .planning import QualityFailure
from .repository import one
from .visual_policy import asset_needs_vision, complete_native_text, native_text


def needed_pages(units, source_aligned=False):
    by_page = defaultdict(list)
    for unit in units:
        by_page[unit.page].append(unit)
    native = {p: native_text(values) for p, values in by_page.items()}
    images = [u for u in units if asset_needs_vision(u, native[u.page])]
    image_ids = {u.unit_id for u in images}
    pages = [
        page for page, count in Counter(u.page for u in images).items() if count >= 3
    ]
    if source_aligned:
        code = re.compile(
            r"^\s*(?:import\s+[a-zA-Z_]|from\s+[a-zA-Z_]\w*(?:\.\w+)*\s+import|def\s+\w+\s*\(|class\s+\w+|[a-zA-Z_]\w*\s*=\s*[\w.]+\()",
            re.MULTILINE,
        )
        image_pages = {u.page for u in images}
        critical = {
            u.page
            for u in units
            if (not u.asset_ref or u.unit_id in image_ids) and (code.search(u.text)
            or (
                u.kind.upper() == "EQUATION"
                and re.search(r"\\(?:begin\{(?:array|[bp]?matrix)\}|boxed)", u.text)
            )
            or (u.page in image_pages and re.search(r"\\(?:lambda|frac|sum|begin)", u.text)))
        }
        critical.update(
            u.page
            for u in images
            if (
                (u.visual or {}).get("group_type") in {"MULTI_PANEL", "FRAGMENT"}
                or (u.visual or {}).get("essential")
                or float((u.visual or {}).get("confidence", 1)) < 0.9
            )
        )
        pages = sorted(set(pages) | critical)
    return pages


def native_page_complete(units):
    """Bypass readable native pages only when no image evidence remains missing."""
    native = native_text(units)
    if any(asset_needs_vision(u, native) for u in units):
        return False
    return complete_native_text(native)


def cached_page_visual(store, journal, vision, source_sha256, page, image_path, context):
    """Exact content/prompt/request cache shared by jobs in this library."""
    from ..domain import VisualUnderstanding
    from .visual_policy import page_prompt_version, prompt_hashes

    version = page_prompt_version(context)
    inputs = {'source_sha256': source_sha256, 'page': page, 'image_sha256': sha(image_path.read_bytes()),
              'model': getattr(vision, 'model_name', 'injected'), 'prompt_version': version,
              'prompt_sha256': prompt_hashes()[version], 'context_sha256': sha(canonical(context))}
    stage = 'visual-page-cache.' + sha(canonical(inputs))

    def build():
        with store.db.engine.connect() as c:
            record = one(c, "SELECT job_id,artifact_ref,artifact_sha256 FROM job_checkpoints WHERE stage=:stage "
                         "AND input_sha256=:digest AND state='DONE' ORDER BY rowid DESC LIMIT 1",
                         stage=stage, digest=sha(canonical(inputs)))
        if record:
            value = VisualUnderstanding.model_validate_json(store.read(record['artifact_ref'], record['artifact_sha256']))
            if value.model != inputs['model'] or value.prompt_version != version:
                record = None
        if not record:
            value = vision.analyze(image_path, external_block_id=f'page-{page}', context=context)
        if value.image_sha256 != inputs['image_sha256'] or value.external_block_id != f'page-{page}':
            raise QualityFailure('Cached page visual image/page binding mismatch')
        reused = bool(record)
        journal.compute(stage+('.reuse' if reused else '.fresh'),inputs,lambda:{
            'policy':'exact-visual-reuse-v1','cache_hit':reused,'origin_job_id':record['job_id'] if record else journal.lease.job_id,
            'origin_ref':record['artifact_ref'] if record else None,'binding':inputs,
            'new_provider_call':not reused,'evidence_owner':'CURRENT_SOURCE_REVISION'})
        return value.model_dump(mode='json')

    # The existing page-context checkpoint still owns anchors. Only model output
    # is shared, so a re-import receives evidence bound to its own source revision.
    return journal.compute(stage, inputs, build)


def page_contexts(store, journal, vision, revision_id, units, *, source_aligned=False, selected_pages=None):
    pages = needed_pages(units, source_aligned) if selected_pages is None else selected_pages
    if not pages:
        return []
    if vision is None or not vision.enabled:
        raise QualityFailure("Multi-panel notes require an enabled whole-page visual check")
    with store.db.engine.connect() as c:
        source = one(c, "SELECT * FROM source_revisions WHERE id=:id", id=revision_id)
    original = store.read(source["blob_ref"], source["sha256"])

    def check_page(page):
        with fitz.open(stream=original, filetype="pdf") as pdf:
            content = pdf[page - 1].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).tobytes("png")
            asset_ref = f"assets/{sha(content)}.png"
            store.put(asset_ref, content)
            inputs = {
                "revision_id": revision_id,
                "page": page,
                "image_sha256": sha(content),
                "policy": "whole-page-source-v3" if source_aligned else "whole-page-relations-v1",
                "model": getattr(vision, "model_name", "injected"),
            }
            stage = (
                f"page-context.{'source-v3.' if source_aligned else ''}{page}.{sha(content)[:16]}"
            )
            # Reuse already completed v3 recognition before selecting a newer
            # prompt. A cost change must not buy the same page again.
            cached = journal.load(stage, inputs)
            if cached is not None:
                return cached
            from .cost_policy import enabled
            compact = source_aligned and enabled()
            native = native_text([u for u in units if u.page == page])
            if compact:
                inputs = {**inputs, 'policy': 'whole-page-source-v4', 'native_sha256': sha(native)}
                stage = f'page-context.source-v4.{page}.{sha(content)[:16]}'
                if journal.exists(stage):
                    # Frozen non-throughput jobs used the selected multi-page
                    # native packet. Load it unchanged before the per-page fix.
                    legacy = {**inputs, 'native_sha256': sha(native_text(units))}
                    with store.db.engine.connect() as c:
                        saved = one(c, 'SELECT input_sha256 FROM job_checkpoints WHERE job_id=:job AND stage=:stage',
                                    job=journal.lease.job_id, stage=stage)
                    return journal.load(stage, legacy if saved['input_sha256'] == sha(canonical(legacy)) else inputs)

            def build(page=page, asset_ref=asset_ref, inputs=inputs):
                key = f"page-{page}"
                context = {
                        "page": page,
                        "extraction_mode": "SOURCE_PAGE" if source_aligned else "RELATIONS",
                        **({'native_text': native, 'evidence_policy': 'native-delta-v1'} if compact else {}),
                        "task": (
                            "核对原PDF整页，优先识别代码、公式和矩阵维度。代码截图须在ocr_text逐行完整转写可辨认代码，保持缩进与函数调用，不遗漏截图下半部分或被分块漏掉的函数；不添加未显示代码。公式图按本页标注抄写m/n/k维度和行列含义，区分本页与相邻页采用的约定，不能拿另一页的尺寸替代。识别乘号与上标符号，图表保留标题、必要图例与实际数值。无法辨认处明确不确定，禁止猜测。"
                            if source_aligned
                            else "核对整页的多面板关系。区分不同演示的输入、核、输出。结合箭头、标题和通道标签解释数值对应，不能把不同例子的矩阵合算。明确记录图中各通道乘加、偏置及输出；无法辨认处说明不确定。"
                        ),
                    }
                visual = (cached_page_visual(store, journal, vision, source['sha256'], page,
                                             store.path(asset_ref), context)
                          if source_aligned else
                          vision.analyze(store.path(asset_ref), external_block_id=key, context=context).model_dump(mode='json'))
                if visual["image_sha256"] != inputs["image_sha256"]:
                    raise QualityFailure("Whole-page image hash mismatch")
                text = canonical(visual)
                artifact = {
                    "blocks": [
                        {
                            "id": key,
                            "page": page,
                            "text": text,
                            "asset_ref": asset_ref,
                            "evidence_origin": "DERIVED_WHOLE_PAGE_VISION",
                        }
                    ],
                    "source_sha256": source["sha256"],
                }
                parse_id = store.sources.record_parse(
                    revision_id, inputs["policy"], inputs, artifact, lease=journal.lease
                )
                evidence = store.sources.add_evidence(
                    revision_id,
                    text,
                    page=page,
                    block_id=key,
                    parse_id=parse_id,
                    lease=journal.lease,
                )
                return {
                    "page": page,
                    "text": text,
                    "evidence_id": evidence,
                    "asset_ref": asset_ref,
                    "parse_id": parse_id,
                    "input": inputs,
                }

            return journal.compute(stage, inputs, build)

    with ThreadPoolExecutor(max_workers=2 if source_aligned else 1) as pool:
        return list(pool.map(check_page, sorted(pages)))


def contextual_panels(store, journal, vision, revision_id, units, contexts):
    """Replace isolated crop guesses with page-grounded, separately anchored evidence."""
    batches = []
    for context in contexts:
        page_units = [u for u in units if u.page == context['page']]
        panels = [u for u in page_units if asset_needs_vision(u, native_text(page_units))]
        for start in range(0, len(panels), 4):
            batches.append((context, panels[start : start + 4]))
    if not batches:
        return []

    def check(batch):
        context, panels = batch
        inputs = {
            "revision_id": revision_id,
            "page": context["page"],
            "whole_page_asset": context["asset_ref"],
            "whole_page_evidence": context["evidence_id"],
            "units": [u.model_dump(mode="json") for u in panels],
            "policy": "page-grounded-panels-v1",
            "model": getattr(vision, "model_name", "injected"),
        }
        stage = f"page-panels.{context['page']}.{sha(canonical(inputs))[:20]}"

        def build():
            visuals = vision.analyze_batch(
                [
                    {
                        "image_path": store.path(unit.asset_ref),
                        "external_block_id": unit.unit_id,
                        "page_context_path": store.path(context["asset_ref"]),
                        "context": {
                            "page": unit.page,
                            "whole_page_summary": context["text"],
                            "task": "先核对整页的面板标题、箭头和通道标签，再解释这张裁切图。明确它是输入、卷积核还是输出，属于哪个独立演示。不要把输入矩阵当成输出，不要混用不同演示的数值。无法辨认时明确不确定。",
                        },
                    }
                    for unit in panels
                ]
            )
            if set(visuals) != {u.unit_id for u in panels}:
                raise QualityFailure("Page-grounded panel coverage mismatch")
            blocks = []
            for unit in panels:
                visual = visuals[unit.unit_id].model_dump(mode="json")
                if visual["image_sha256"] != sha(store.path(unit.asset_ref).read_bytes()):
                    raise QualityFailure("Page-grounded crop hash mismatch")
                original = unit.text.split("\n[视觉识别，需结合原图核对]\n", 1)[0]
                text = original + "\n[结合原PDF整页的面板核对]\n" + canonical(visual)
                blocks.append(
                    {
                        "id": unit.unit_id,
                        "page": unit.page,
                        "text": text,
                        "asset_ref": unit.asset_ref,
                        "visual": visual,
                        "whole_page_asset": context["asset_ref"],
                        "evidence_origin": "PAGE_GROUNDED_PANEL",
                    }
                )
            artifact = {"blocks": blocks, "source_revision_id": revision_id}
            parse_id = store.sources.record_parse(
                revision_id, "page-grounded-panels-v1", inputs, artifact, lease=journal.lease
            )
            result = []
            for block in blocks:
                evidence_id = store.sources.add_evidence(
                    revision_id,
                    block["text"],
                    page=block["page"],
                    block_id=block["id"],
                    parse_id=parse_id,
                    lease=journal.lease,
                )
                result.append(
                    {
                        "unit_id": block["id"],
                        "page": block["page"],
                        "text": block["text"],
                        "visual": block["visual"],
                        "parse_id": parse_id,
                        "evidence_id": evidence_id,
                    }
                )
            return result

        return journal.compute(stage, inputs, build)

    with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
        return [panel for result in pool.map(check, batches) for panel in result]
