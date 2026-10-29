"""Changed role chapters only, with fixed output keys and typed review actions."""

from ..contracts import canonical, sha
from ..generation import Generation
from ..generation_contracts import Draft, DraftSection
from ..markdown_checks import section_headings
from ..planning import QualityFailure
from ..structured_quality import (
    REVIEW_RULES,
    ChapterAssessment,
    QualityFinding,
    essential_review,
    finding_protocol_issues,
    fixed_chapters,
    scientific_content,
    validate_findings,
)
from .entity_contracts import Chapter
from .entity_writing import asset_packet, bind_images, checked_images


def write_group(store, calls, specs):
    from ..resilience import REVIEW_POLICY, SevereQualityFailure, enabled, grounded_severe, readable
    resilient = enabled(calls)
    packet = []
    for spec in specs:
        unique = {f["sha256"]: f for f in spec["fragments"]}
        evidence = [dict(f) for f in unique.values()]
        packet.append({**{k: v for k, v in spec.items() if k not in {"fragments","retained_images"}},
                       "fragments": evidence, "images": {**spec.get('retained_images',{}),**asset_packet(store, evidence)}})
    concise = any(s.get('content_policy') == 'concise-foundation-v1' for s in specs)
    if concise:
        from ..publication_assets import page_assets, without_images
        blocked = page_assets(store)
        for spec in packet:
            for fragment in spec['fragments']:
                fragment['text'] = without_images(fragment['text'], blocked)
            spec['images'] = {key: value for key, value in spec['images'].items()
                              if value['asset_ref'].removeprefix('../') not in blocked}
            if spec.get('accepted_core'):
                spec['accepted_core'] = {**spec['accepted_core'], 'markdown': without_images(spec['accepted_core']['markdown'], blocked)}
    projection = None
    if getattr(calls, 'compact_evidence', False):
        from ..model_packets import EvidencePacket
        projection = EvidencePacket(packet)
        if calls.journal:
            receipt = projection.receipt(packet)
            calls.journal.compute('evidence-map.' + projection.input_hash, {'input_sha256': projection.input_hash}, lambda: receipt)
    calls.begin_unit(10 if getattr(calls, "review_policy", None) in {"essential-v2", "essential-v3"} else 8, key="entity-patch-v2:" + sha(canonical(packet)))
    remaining, findings, accepted = packet, {s['key']:s['repair_findings'] for s in packet if s.get('repair_findings')}, {}
    for attempt in range(2):
        keys = [s["key"] for s in remaining]
        instruction = (
            '根据输入写一个高度浓缩、中文为主的Concept知识框架，不再生成一份详细来源笔记。'
            'chapters必须使用required_chapter_keys。通常包含一句话定义、要素与联系、核心公式及成立条件、变体对照、适用边界；'
            '按对象选择必要部分，不强填空节。一般约600至1500中文字，复杂对象可更长，优先简明。'
            'fragments是新来源证据，accepted_core是仍有有效来源支持且已审查的旧核心，允许直接保留旧核心知识，'
            '只把净新增的关键条件或变体合入对应位置。重复例子、代码、完整推导、长实验不写入核心，它们保留在Atomic深入阅读。'
            '不按新Atomic逐篇添加章节，不用每个fragment标题重新展开；不要求每个片段细节在摘要出现。'
            '仅凝练来源已有知识，关键公式条件不能因压缩改变。新增来源没有净新增时保持原核心。'
            'foundation_rebase=true时，按当前有效教材证据重新建立基础框架，同时保留补充来源仍有支持的净新增；不把旧正文当成独立事实依据。'
            '同一符号须保持概率、期望值或计数的语义一致；梯度写明针对哪个目标及优化方向。'
            '在最小化负对数似然与最大化对数似然之间切换时，梯度、海森、矩阵界和更新方向须显式对应各自目标；来源特例若与一般式矛盾，可保留有明确条件的一般式并说明歧义。'
            '若有repair_findings，对照实际fragments修正被引用位置及其直接依赖，其他正确内容保留，不照抄指控。'
            '允许采用帮助理解框架的关键模型架构、流程、几何或机制图，通常少量即可，不强制配图或数量。'
            '只能使用images登记的真实资源；整页截图、大段原文截图、装饰、重复图和长算例过程图不要写入核心。'
            '为每个候选图返回USE/OMIT，USE可给caption和after_heading选择实际存在的小标题，程序会将决定绑定为真实图片引用；也可直接引用已登记asset_ref。图注简洁中文，不复制整页OCR。不要在正文重复来源元数据和编辑过程。'
            '正文小标题使用真实的###层级；数学表达式用LaTeX的$或$$排版，不用反引号代码替代数学公式。'
            '小标题用中文，可保留必要缩写；输出章节内Markdown，不含H1/H2。findings只修已定位错误。'
        ) if concise else (
            '仅融合给定Concept角色章节。chapters必须恰好使用required_chapter_keys，不可用fragment的section_id、来源小节标题或新发明key替换。'
            '每章只用所属fragments与已列图片，保留重要定义、公式条件、步骤、完整例子、代码与图文。旧正文用于差异比较，未变化章节不重写。'
            'entity给出共同知识对象，topics给出内部主题；合并重复定义，保留不同条件与变体，不逐篇来源重复。'
            '每幅图在images给USE/OMIT及理由。findings只修当前候选的已定位问题，不修改来源fragment。'
        )
        raw = calls.run("next.entity_repair" if attempt else "next.entity_write",
            instruction,
            {"required_chapter_keys": keys, "chapters": projection.project(remaining) if projection else remaining, "findings": projection.remap(findings) if projection else findings}, fixed_chapters(keys, Chapter),
            use_pro_model=bool(attempt), check=lambda value, keys=keys: exact(value, keys))
        from ..source_quality import code_findings

        normalized, program_findings = {}, {}
        for spec in remaining:
            key = spec["key"]
            chapter = bind_images(raw.chapters[key], spec['images'])
            checked_images(chapter, spec["images"])
            ids = {f["fragment_id"] for f in spec["fragments"]}
            converted = Draft(title=spec["title"], summary=spec["question"], sections=[DraftSection(
                key=key, title=spec["title"], question=spec["question"], scope=spec["scope"],
                markdown=section_headings(chapter.markdown), units=sorted(ids))])
            normalized[key] = Generation.checked_draft(converted, ids, {a["asset_ref"] for a in spec["images"].values()},
                content_checks=not (resilient and attempt)).sections[0].markdown
            if concise:
                from .core_state import compact_markdown
                normalized[key] = compact_markdown(normalized[key], spec['entity']['title'])
            if resilient:
                readable(normalized[key])
            program_findings[key] = code_findings([{"key": key, "draft": {"sections": [{"key": key, "markdown": normalized[key]}]}}])

        if attempt and essential_review(calls):
            risky = []
            for spec in remaining:
                key = spec["key"]
                if getattr(calls, "review_policy", None) not in {"essential-v2", "essential-v3"} and scientific_content(normalized[key] + "\n" + "\n".join(f["text"] for f in spec["fragments"])):
                    risky.append(spec)
                else:
                    if program_findings[key] and not resilient:
                        raise QualityFailure("Repaired Concept still fails program validation: " + canonical([f.model_dump() for f in program_findings[key]]))
                    accepted[key] = (normalized[key], {"writer_called": True, "policy": "entity-patch-v2",
                        "review": {"review_mode": "PROGRAM_ACCEPTANCE_AFTER_SINGLE_REPAIR", "prior_findings": findings.get(key, []), "post_repair_semantic_review": False,
                                   "nonblocking_code_findings": [f.model_dump() for f in program_findings[key]]},
                        "image_choices": {k: v.model_dump() for k, v in raw.chapters[key].images.items()}, "assets": spec["images"]})
            remaining = risky
            if not remaining:
                return accepted
            keys = [s["key"] for s in remaining]

        def checked(value, remaining=remaining, normalized=normalized, keys=keys):
            if projection:
                projection.restore_review(value)
            exact(value, keys)
            for spec in remaining:
                result = value.chapters[spec["key"]]
                sources = {f["fragment_id"]: f["text"] for f in spec["fragments"]}
                if not resilient and (set(result.supported_fragment_ids) != set(sources) or len(result.supported_fragment_ids) != len(sources) or set(result.checked_image_ids) != set(spec["images"]) or len(result.checked_image_ids) != len(spec["images"])):
                    raise QualityFailure("Review must cover exactly every fragment and candidate image")
                if getattr(calls, "review_policy", None) not in {"essential-v2", "essential-v3"}:
                    validate_findings(result.findings, normalized[spec["key"]], sources)

        review_warning = None
        try:
            review = calls.run("next.entity_review", REVIEW_RULES + (REVIEW_POLICY if resilient else "")
                + ('当前是浓缩知识框架，检查新断言与新fragments、accepted_core是否一致，accepted_core仍有有效旧来源支持。'
                   '不因摘要未复制完整例子、推导、代码、图片、实验细节判遗漏；只检查核心定义/条件因压缩被实质改变。'
                   '完整来源保留在Atomic；不要要求核心扩写成详尽笔记。' if concise else '')
                + "每章完整返回supported_fragment_ids与checked_image_ids。按required_chapter_keys逐章输出findings，不返回自由文本issues或passed。",
                {"required_chapter_keys": keys, "chapters": [{**s, "markdown": normalized[s["key"]], "image_choices": {k: v.model_dump() for k, v in raw.chapters[s["key"]].images.items()}} for s in (projection.project(remaining) if projection else remaining)]},
                fixed_chapters(keys, ChapterAssessment), check=checked, use_pro_model=True, **({"max_attempts": 1} if resilient else {}))
        except QualityFailure as exc:
            if not resilient or isinstance(exc, SevereQualityFailure):
                raise
            review_warning = str(exc)
            review = fixed_chapters(keys, ChapterAssessment).model_validate({"chapters": {key: {"supported_fragment_ids": []} for key in keys}})
        findings, upstream = {}, []
        for spec in remaining:
            key = spec["key"]
            check = review.chapters[key]
            if resilient:
                sources = {f["fragment_id"]: f["text"] for f in spec["fragments"]}
                for finding in check.findings:
                    if grounded_severe(finding, normalized[key], sources):
                        raise SevereQualityFailure("Severe Concept content failure: " + canonical(finding.model_dump()))
            for computation in check.data_checks:
                if not computation.passed:
                    check.findings.append(QualityFinding(category="CANDIDATE_ERROR", issue="数据复算不一致：" + computation.recomputation,
                        candidate_quote=computation.candidate_quote, source_ids=computation.source_ids))
            active = [f for f in check.findings if f.category != "ADVISORY"]
            protocol = finding_protocol_issues(check.findings, normalized[key], {f["fragment_id"]: f["text"] for f in spec["fragments"]})
            adjudications, guard_hash = [], None
            if active and getattr(calls, "review_policy", None) == "essential-v2":
                from ..guard_review import adjudicate_chapter

                confirmed, adjudications, guard_hash = adjudicate_chapter(store, calls, spec["title"], normalized[key],
                    spec["fragments"], [f.issue for f in active])
                active = [f for f in active if f.issue in confirmed]
            blockers = [f for f in active if f.category == "CANDIDATE_ERROR"] + [QualityFinding(category="CANDIDATE_ERROR", issue=f.issue) for f in program_findings[key]]
            if not resilient:
                upstream.extend(f.issue for f in active if f.category == "SOURCE_ISSUE")
            if blockers:
                findings[key] = [f.model_dump() for f in blockers]
            else:
                accepted[key] = (normalized[key], {"writer_called": True, "policy": "entity-patch-v2", "review": check.model_dump(),
                    "guard_adjudications": adjudications, "guard_markdown_sha256": guard_hash,
                    "review_protocol_issues": protocol,
                    **({"warnings": ([{"code": "REVIEW_RESPONSE_INVALID", "detail": review_warning}] if review_warning else [])
                        + [f.model_dump() for f in active if f.category == "SOURCE_ISSUE"], "post_repair_semantic_review": False,
                        "review_mode": "SINGLE_REVIEW_UNAVAILABLE" if review_warning else "SINGLE_LLM_REVIEW"} if resilient else {}),
                    "image_choices": {k: v.model_dump() for k, v in raw.chapters[key].images.items()}, "assets": spec["images"]})
        if upstream:
            from ..quality_repair import UpstreamRepairPending, request_upstream_repair
            fragments = [f for spec in remaining for f in spec["fragments"]]
            if request_upstream_repair(store, calls, fragments, upstream):
                raise UpstreamRepairPending("Confirmed source issue; bounded Atomic repair requested")
        if not findings:
            return accepted
        if attempt:
            raise QualityFailure("Typed entity review still has candidate blockers: " + canonical(findings))
        remaining = [{**s, "previous": normalized[s["key"]]} for s in remaining if s["key"] in findings]
    raise AssertionError("Unreachable review state")


def exact(value, keys):
    if set(value.chapters) != set(keys):
        raise QualityFailure("Output must cover exact required Concept role chapter keys: " + canonical(keys))
