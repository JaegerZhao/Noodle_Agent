"""Changed entity chapters include explicit image dispositions and one independent check."""

import re

from ..contracts import canonical, sha
from ..generation import Generation
from ..generation_contracts import Draft, DraftSection
from ..markdown_checks import section_headings
from ..planning import QualityFailure
from .entity_contracts import Chapter, ChapterCheck

IMAGE = re.compile(r"!\[([^\]]*)\]\((\.\./assets/[a-f0-9]{64}\.(?:png|jpg|jpeg|webp))\)")


def asset_packet(store, fragments):
    result = {}
    for fragment in fragments:
        for match in IMAGE.finditer(fragment["text"]):
            caption, ref = match.groups()
            path = store.path(ref.removeprefix("../"))
            if not path.exists() or sha(path.read_bytes()) != path.stem:
                raise QualityFailure("Image is missing or its content hash differs: " + ref)
            key = "image_" + sha(ref)[:20]
            item = result.setdefault(
                key, {"asset_ref": ref, "caption": caption, "fragment_ids": [], "evidence_ids": []}
            )
            item["fragment_ids"].append(fragment["fragment_id"])
            item["evidence_ids"] = sorted(set(item["evidence_ids"]) | set(fragment["evidence_ids"]))
    return result


def retained_assets(store, fragments, markdown):
    refs={ref for _,ref in IMAGE.findall(markdown)}
    return {key:value for key,value in asset_packet(store,fragments).items() if value['asset_ref'] in refs}


def checked_images(draft, assets):
    if set(draft.images) != set(assets):
        raise QualityFailure("Every candidate image needs an explicit USE/OMIT disposition")
    actual = {ref for _, ref in IMAGE.findall(draft.markdown)}
    expected = {
        assets[key]["asset_ref"] for key, value in draft.images.items() if value.action == "USE"
    }
    if actual != expected:
        raise QualityFailure("Image decisions and actual Markdown image references differ")


def bind_images(draft, assets):
    """Compile explicit choices to exact assets; never guess an unknown image."""
    if set(draft.images) != set(assets):
        raise QualityFailure('Image choices must cover exactly the registered candidates')
    by_ref = {value['asset_ref']: key for key, value in assets.items()}
    def existing(match):
        ref = match[2]
        if ref in assets:
            ref = assets[ref]['asset_ref']
        if ref not in by_ref:
            raise QualityFailure('Unregistered Concept image: ' + ref)
        return f'![{match[1]}]({ref})' if draft.images[by_ref[ref]].action == 'USE' else ''
    markdown = re.sub(r'!\[([^\]]*)\]\(([^)\s]+)\)', existing, draft.markdown)
    used = {ref for _, ref in IMAGE.findall(markdown)}
    additions = {}
    for key, choice in draft.images.items():
        ref = assets[key]['asset_ref']
        if choice.action != 'USE' or ref in used:
            continue
        caption = re.sub(r'\s+', ' ', choice.caption or assets[key]['caption'] or '关键结构图')
        caption = caption.replace('[', r'\[').replace(']', r'\]')
        additions.setdefault(choice.after_heading or '', []).append(f'![{caption}]({ref})')
    lines, result = markdown.splitlines(), []
    for line in lines:
        result.append(line)
        heading = re.match(r'^#{1,6}\s+(.+)$', line)
        if heading and heading[1] in additions:
            result.extend(['', *additions.pop(heading[1]), ''])
    if additions:
        result.extend(['', '### 关键图示', '', *[image for images in additions.values() for image in images]])
    return draft.model_copy(update={'markdown': '\n'.join(result).strip()})


def write_entity_chapter(
    store, calls, key, title, question, scope, fragments, previous=None, links=(), repair_findings=()
):
    # Deduplication never removes a formula sign or changes case-sensitive code.
    unique = {}
    for f in fragments:
        unique.setdefault(f["sha256"], f)
    evidence = list(unique.values())
    assets = asset_packet(store, evidence)
    if len(canonical(evidence)) > 36000:
        raise QualityFailure(
            "Entity chapter support exceeds budget; partition rather than truncate"
        )
    data = {
        "question": question,
        "scope": scope.model_dump(),
        "fragments": evidence,
        "old_reference": previous,
        "images": assets,
        "relations": list(links),
    }
    issues = list(repair_findings)
    ids = {f["fragment_id"] for f in evidence}
    for _ in range(2):
        draft = calls.run(
            "next.entity_write",
            "综合当前有效Atomic片段写一个完整可读中文章节，不能机械拼接。保留公式条件、数值约定与关键例子；"
            "旧正文仅供差异对照，事实必须由fragments支持。新材料已被旧正文覆盖时保持原叙述，不反复改写或尾部追加。"
            "不得扩大为无条件结论，不虚构图或资料未讲的比较。章节正文不含H1/H2，数学用$或$$。"
            "images中每个ID必须选USE或OMIT并写具体理由；重要原理图和有解释价值的示例图应保留在合适段落并给中文图注，"
            "图文角色、矩阵方向和参数必须一致。重复代码截图可省略，但正文重复不等于新增示例图无价值。"
            "只引用images已有asset_ref。不能以看过OCR当作亲自看过原图。relations是待核对关系，正文可介绍有来源支持的联系但不能新增事实。",
            {**data, "issues": issues},
            Chapter,
        )
        try:
            checked_images(draft, assets)
            converted = Draft(
                title=title,
                summary=question,
                sections=[
                    DraftSection(
                        key=key,
                        title=title,
                        question=question,
                        scope=scope,
                        markdown=section_headings(draft.markdown),
                        units=sorted(ids),
                    )
                ],
            )
            checked = Generation.checked_draft(
                converted, ids, {a["asset_ref"] for a in assets.values()}
            )
            markdown = checked.sections[0].markdown
            from ..critical_quality import critical_problems

            problems = critical_problems(title, markdown)
            adjudications = []
            guard_hash = None
            if problems:
                from ..guard_review import adjudicate_chapter
                problems, adjudications, guard_hash = adjudicate_chapter(store,calls,title,markdown,evidence,problems)
            if problems:
                raise QualityFailure(canonical(problems))
            review = calls.run(
                "next.entity_review",
                "独立核对章节忠于全部有效输入，重要条件、公式、数值、例子不漏不误。不是逐字拼接要求；"
                "逐一覆盖fragment_id，重复片段可由同一叙述支持。核对图注和图像识别依据，不能根据孤立裁图猜图意。"
                "images必须逐ID核对：必要原理/示例图不得无理由省略，代码重复截图可省略，若图像语境缺失导致关键解释不可靠则失败。"
                "检查relations是否确有来源支持、是否仅是阅读组织而非严格子类；无支持或错误上级必须报告。"
                "仅在无实质问题时passed=true，返回全部supported_fragment_ids及checked_image_ids。",
                {
                    **data,
                    "markdown": markdown,
                    "heuristic_guard_adjudications": adjudications,
                    "image_choices": {k: v.model_dump() for k, v in draft.images.items()},
                },
                ChapterCheck,
            )
            if (
                not review.passed
                or review.issues
                or set(review.supported_fragment_ids) != ids
                or set(review.checked_image_ids) != set(assets)
            ):
                from ..quality_repair import UpstreamRepairPending, request_upstream_repair
                if request_upstream_repair(store, calls, evidence, review.issues):
                    raise UpstreamRepairPending("Concept paused until its faulty Atomic input is repaired")
                raise QualityFailure(canonical(review.model_dump()))
            return markdown, {
                "writer_called": True,
                "policy": "entity-concepts-v2",
                "review": review.model_dump(),
                "image_choices": {k: v.model_dump() for k, v in draft.images.items()},
                "assets": assets,
                "guard_adjudications": adjudications,
                "guard_markdown_sha256": guard_hash,
            }
        except ValueError as exc:
            issues.append(str(exc))
    raise QualityFailure("Entity chapter failed bounded checks: " + canonical(issues))
