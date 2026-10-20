"""Whole-page relation checks for notes containing several cropped panels."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import fitz

from .contracts import canonical, sha
from .planning import QualityFailure
from .repository import one


def page_contexts(store, journal, vision, revision_id, units):
    pages = [
        page for page, count in Counter(u.page for u in units if u.asset_ref).items() if count >= 3
    ]
    if not pages:
        return []
    if vision is None or not vision.enabled:
        raise QualityFailure("Multi-panel notes require an enabled whole-page visual check")
    with store.db.engine.connect() as c:
        source = one(c, "SELECT * FROM source_revisions WHERE id=:id", id=revision_id)
    original = store.read(source["blob_ref"], source["sha256"])
    contexts = []
    with fitz.open(stream=original, filetype="pdf") as pdf:
        for page in sorted(pages):
            content = pdf[page - 1].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).tobytes("png")
            asset_ref = f"assets/{sha(content)}.png"
            store.put(asset_ref, content)
            inputs = {
                "revision_id": revision_id,
                "page": page,
                "image_sha256": sha(content),
                "policy": "whole-page-relations-v1",
                "model": getattr(vision, "model_name", "injected"),
            }
            stage = f"page-context.{page}.{sha(content)[:16]}"

            def build(page=page, asset_ref=asset_ref, inputs=inputs):
                key = f"page-{page}"
                visual = vision.analyze(
                    store.path(asset_ref),
                    external_block_id=key,
                    context={
                        "page": page,
                        "task": "核对整页的多面板关系。区分不同演示的输入、核、输出。结合箭头、标题和通道标签解释数值对应，不能把不同例子的矩阵合算。明确记录图中各通道乘加、偏置及输出；无法辨认处说明不确定。",
                    },
                ).model_dump(mode="json")
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
                    revision_id, "whole-page-relations-v1", inputs, artifact, lease=journal.lease
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

            contexts.append(journal.compute(stage, inputs, build))
    return contexts


def contextual_panels(store, journal, vision, revision_id, units, contexts):
    """Replace isolated crop guesses with page-grounded, separately anchored evidence."""
    batches = []
    for context in contexts:
        panels = [u for u in units if u.page == context["page"] and u.asset_ref]
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
