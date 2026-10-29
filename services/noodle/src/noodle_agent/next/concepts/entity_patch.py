"""One bounded update/check for changed entity chapters; old checked chapters are reused."""

from ..contracts import canonical, sha
from ..generation import Generation
from ..generation_contracts import Draft, DraftSection
from ..markdown_checks import section_headings
from ..planning import QualityFailure
from ..segmentation import tokens
from .entity_contracts import EntityPatch, EntityPatchCheck
from .entity_writing import asset_packet, checked_images


def write_entity_sections(store, calls, specs):
    groups, current = [], []
    for spec in specs:
        if current and tokens(canonical(current + [spec])) > 9000:
            groups.append(current)
            current = []
        current.append(spec)
        # 9k is a grouping target, not a failure threshold for one complete
        # evidence closure. Never reject a valid chapter because of cost alone.
    if current:
        groups.append(current)
    result = {}
    for group in groups:
        result.update(write_group(store, calls, group))
    return result


def write_group(store, calls, specs):
    if getattr(calls, "policy", None) == "structured-check-v2":
        from .entity_patch_v2 import write_group as structured_group
        return structured_group(store, calls, specs)
    packet = []
    for spec in specs:
        unique = {f["sha256"]: f for f in spec["fragments"]}
        evidence = list(unique.values())
        packet.append({**{k: v for k, v in spec.items() if k != "fragments"},
                       "fragments": evidence, "images": asset_packet(store, evidence)})
    calls.begin_unit(5, key="entity-patch:" + sha(canonical(packet)))
    remaining, findings, accepted = packet, {}, {}
    for attempt in range(2):
        raw = calls.run(
            "next.entity_repair" if attempt else "next.entity_write",
            "融合同一知识实体中这些受影响章节，返回chapters按输入key组织的最小章节更新。仅以当前有效fragments作为事实依据，旧正文用于差异比较。"
            "保留重要定义、公式条件、步骤、完整例子和图文，不机械追加，不改无关章节，不扩展来源没有的答案。重复内容保持原叙述。"
            "区分来源事实、推导与不确定性；来源错误或矛盾不能无说明复制，也不凭记忆新增事实。章节内不写H1/H2，数学使用$或$$。"
            "每个候选图片都在images选择USE/OMIT并给理由，引用只能使用已有asset_ref，保留有价值的图和条件。"
            "有findings时只修对应章节的问题，替换错误位置，不能在错误断言后追加相反说法。",
            {"chapters": remaining, "findings": findings}, EntityPatch,
            check=lambda v, remaining=remaining: complete_keys(v, remaining),
        )
        normalized = {}
        for spec in remaining:
            key = spec["key"]
            chapter = raw.chapters[key]
            checked_images(chapter, spec["images"])
            ids = {f["fragment_id"] for f in spec["fragments"]}
            converted = Draft(title=spec["title"], summary=spec["question"], sections=[DraftSection(
                key=key, title=spec["title"], question=spec["question"], scope=spec["scope"],
                markdown=section_headings(chapter.markdown), units=sorted(ids))])
            normalized[key] = Generation.checked_draft(converted, ids, {a["asset_ref"] for a in spec["images"].values()}).sections[0].markdown
        review = calls.run(
            "next.entity_review",
            "独立逐章节对照有效来源检查更新正文的重要事实、定义、公式条件、数值、推导、代码、图片和范围。"
            "核对新增内容与旧章节是否冲突，忠实复制原文并不自动代表科学正确；不确定或来源矛盾应准确标明。"
            "只报告阻断发布的错误或重要遗漏，不因文风偏好重写。每章节返回全部supported_fragment_ids和checked_image_ids，passed仅在无问题时为true。",
            {"chapters": [{**s, "markdown": normalized[s["key"]], "image_choices": {k: v.model_dump() for k, v in raw.chapters[s["key"]].images.items()}} for s in remaining]},
            EntityPatchCheck, check=lambda v, remaining=remaining: complete_keys(v, remaining),
        )
        findings = {}
        for spec in remaining:
            key = spec["key"]
            check = review.chapters[key]
            expected = {f["fragment_id"] for f in spec["fragments"]}
            if not check.passed or check.issues or set(check.supported_fragment_ids) != expected or set(check.checked_image_ids) != set(spec["images"]):
                findings[key] = check.issues or ["Check must cover every fragment and candidate image"]
            else:
                accepted[key] = (normalized[key], {"writer_called": True, "policy": "entity-patch-v1", "review": check.model_dump(),
                    "image_choices": {k: v.model_dump() for k, v in raw.chapters[key].images.items()}, "assets": spec["images"]})
        if not findings:
            return accepted
        remaining = [{**s, "previous": normalized[s["key"]]} for s in remaining if s["key"] in findings]
    raise QualityFailure("Entity update failed bounded independent checks: " + canonical(findings))


def complete_keys(value, specs):
    if set(value.chapters) != {s["key"] for s in specs}:
        raise QualityFailure("Entity update/check must cover the exact changed chapter keys")
