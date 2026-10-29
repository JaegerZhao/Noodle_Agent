"""Downstream quality findings trigger bounded, source-grounded Atomic revisions."""

import json

from pydantic import Field

from .contracts import Contract, Review, canonical, sha
from .jobs import require_lease
from .navigation import frozen_candidate
from .planning import QualityFailure
from .repository import Conflict, one
from .section_contracts import SectionPatch


class UpstreamProblem(Contract):
    fragment_id: str
    issue: str = Field(min_length=1)


class QualityTriage(Contract):
    problems: list[UpstreamProblem] = Field(default_factory=list)


class AtomicPatch(Contract):
    sections: list[SectionPatch]
    summary: str | None = None


class AtomicCheck(Contract):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    checked_section_ids: list[str]


class UpstreamRepairPending(Exception):
    pass


def evidence_packet(store, ids):
    result = []
    with store.db.engine.connect() as c:
        for identity in dict.fromkeys(ids):
            row = one(
                c,
                "SELECT id,source_revision_id,page_number,quoted_text,sha256 FROM evidence_anchors WHERE id=:id",
                id=identity,
            )
            if (
                not row
                or sha(row["quoted_text"]) != row["sha256"]
                or not store.sources.source_valid(c, row["source_revision_id"])
            ):
                raise Conflict("Repair evidence is missing or no longer current")
            result.append(dict(row))
    return result


def request_upstream_repair(store, calls, fragments, issues):
    if not issues or not getattr(calls, "journal", None):
        return False
    by_id = {f["fragment_id"]: f for f in fragments}
    proof = evidence_packet(store, [e for f in fragments for e in f["evidence_ids"]])
    triage = calls.run(
        "next.quality_triage",
        "对照Atomic片段与原始解析/冻结视觉证据，裁定下游Concept或问答审查问题是否确实源于上游Atomic。只返回必须先修正Atomic的实质错误，引用精确fragment_id和具体issue。若Atomic本身正确而只是下游答案/Concept草稿写错，不报上游问题。审查意见本身也可能错，不能无条件服从；先独立对照实际计算或代码操作，辨明对象的语义角色与算法输入角色，检查是否混淆或遗漏使正文产生错误推论；原文自身矛盾时，Atomic应明确说明不一致而非把矛盾解读当正确事实。禁止要求来源无依据的新知识，禁止修改资料或直接输出替换正文。",
        {"fragments": fragments, "evidence": proof, "concept_findings": issues},
        QualityTriage,
        use_pro_model=True,
    )
    grouped = {}
    for problem in triage.problems:
        if problem.fragment_id not in by_id:
            raise QualityFailure("Quality triage references unknown fragment")
        f = by_id[problem.fragment_id]
        grouped.setdefault(f["atomic_revision_id"], []).append(
            {"section_id": f["section_id"], "issue": problem.issue}
        )
    for revision, findings in grouped.items():
        with store.repository.transaction() as c:
            require_lease(c, calls.journal.lease)
            note = one(
                c,
                "SELECT n.id,n.current_revision_id FROM notes n JOIN note_revisions r ON r.note_id=n.id WHERE r.id=:r",
                r=revision,
            )
            if note["current_revision_id"] != revision:
                raise Conflict("Atomic changed during quality triage")
            pending = one(c, "SELECT status FROM jobs WHERE kind='REPAIR_ATOMIC' AND json_extract(input_json,'$.revision_id')=:r ORDER BY created_at DESC LIMIT 1", r=revision)
            if pending and pending["status"] == "REVIEW":
                raise QualityFailure("Upstream Atomic repair is already in REVIEW; do not loop on the same failed source")
            if pending and pending["status"] in {"PENDING", "RUNNING", "RETRY"}:
                continue
            store.repository.event(
                c,
                "ATOMIC_REPAIR_REQUESTED",
                note["id"],
                revision,
                {
                    "expected_revision": revision,
                    "findings": findings,
                    "cause": calls.journal.lease.job_id,
                },
                key="atomic-quality:" + revision + ":" + sha(canonical(findings)),
            )
    return bool(grouped)


def request_retrieval_repair(store, calls, hits, issues):
    """Adjudicate rejected answers against real Atomic sources, never trust answer feedback alone."""
    fragments = []
    seen = set()
    for hit in hits:
        key = (hit["revision_id"], hit["section_id"])
        if hit["kind"] != "ATOMIC" or key in seen:
            continue
        seen.add(key)
        candidate = frozen_candidate(store, hit["revision_id"])
        section = next((s for s in candidate.sections if s.section_id == hit["section_id"]), None)
        if section and not section.protected:
            fragments.append(
                {
                    "fragment_id": "R" + str(len(fragments)),
                    "atomic_revision_id": hit["revision_id"],
                    "section_id": section.section_id,
                    "text": section.markdown,
                    "evidence_ids": section.evidence_ids,
                }
            )
    return bool(fragments) and request_upstream_repair(store, calls, fragments, issues)


def repair_atomic(store, lease, calls):
    expected = lease.input["payload"]["expected_revision"]
    identity = lease.input["entity_id"]
    with store.db.engine.connect() as c:
        head = one(c, "SELECT current_revision_id FROM notes WHERE id=:id", id=identity)
        proof = one(
            c,
            "SELECT * FROM build_artifacts WHERE storage_ref=:r",
            r=f"artifacts/builds/{expected}.json",
        )
    if not head or head["current_revision_id"] != expected:
        return {"status": "SUPERSEDED"}
    candidate = frozen_candidate(store, expected)
    payload = json.loads(store.read(proof["storage_ref"], proof["sha256"]))
    depth = payload["review"]["report"].get("automatic_repair_depth", 0)
    if depth >= 2:
        raise QualityFailure("Atomic automatic repair lineage exhausted; retain review checkpoint")
    findings = lease.input["payload"]["findings"]
    ids = {f["section_id"] for f in findings}
    selected = [s for s in candidate.sections if s.section_id in ids]
    if len(selected) != len(ids) or any(s.protected for s in selected):
        raise QualityFailure("Repair scope unknown or protected")
    evidence = evidence_packet(store, [i for s in selected for i in s.evidence_ids])
    data = {
        "title": candidate.title,
        "summary": candidate.summary,
        "sections": [s.model_dump() for s in selected],
        "findings": findings,
        "evidence": evidence,
    }
    feedback = []
    for attempt in range(2):

        def scope(patch):
            keys = [s.key for s in patch.sections]
            if len(keys) != len(set(keys)) or not set(keys) <= ids or not keys:
                raise QualityFailure("Atomic repair patch exceeded exact section scope")

        patch = calls.run(
            "next.atomic_quality_repair",
            "仅修复已定位的Atomic知识错误，直接在原错误位置替换；保留未受影响正文、公式、图片、范围和来源。原文有矛盾时如实注明，不无条件复述错误，也不另加外部参数。不能只在末尾追加相反说明而保留原错误。sections返回稳定section_id作为key及完整替换markdown；简介若需修正则介绍正文内容，不写编辑日志。",
            {**data, "previous_check": feedback},
            AtomicPatch,
            check=scope,
            use_pro_model=True,
        )
        changes = {s.key: s.markdown for s in patch.sections}
        revised = candidate.model_copy(
            update={
                "summary": candidate.summary if patch.summary is None else patch.summary,
                "sections": [
                    s.model_copy(update={"markdown": changes.get(s.section_id, s.markdown)})
                    for s in candidate.sections
                ],
            }
        )
        if revised.fingerprint == candidate.fingerprint:
            raise QualityFailure(
                "REPAIR_NO_PROGRESS: Atomic unchanged after source-grounded repair"
            )
        checked = calls.run(
            "next.atomic_quality_review",
            "独立核对修订是否解决每个明确问题、是否引入新的事实/条件错误，逐项对照原始证据。原文自身矛盾允许明确客观标注，不要求武断补全。检查全文简介与受检章节一致、代码/数值/矩阵约定无冲突；重要图与公式不得无理由删除。checked_section_ids完整列出本次受检章节。",
            {
                "before": data,
                "after": {
                    "summary": revised.summary,
                    "sections": [s.model_dump() for s in revised.sections if s.section_id in ids],
                },
            },
            AtomicCheck,
            use_pro_model=True,
        )
        if checked.passed and not checked.issues and set(checked.checked_section_ids) == ids:
            break
        feedback = checked.issues or ["Incomplete independent section check"]
    else:
        raise QualityFailure(
            "Atomic quality repair failed independent check: " + canonical(feedback)
        )
    # Keep source-unit bookkeeping, but do not claim the old assembly review checked this new body.
    previous = payload["review"]["report"]
    report = {
        k: previous[k]
        for k in ["units", "omissions", "plan_key", "source_revision", "whole_page_contexts"]
        if k in previous
    }
    report.update(
        {
            "automatic_repair_depth": depth + 1,
            "parent_revision": expected,
            "findings": findings,
            "independent_check": checked.model_dump(),
            "source_guard_adjudications": [],
        }
    )
    result = store.publish(
        revised,
        expected_revision=expected,
        lease=lease,
        review=Review(
            candidate_sha256=revised.fingerprint,
            policy_version=revised.policy_version,
            reviewer="next.atomic_quality_review",
            passed=True,
            report=report,
        ),
    )
    return {"status": "REPAIRED", **result, "repaired_section_ids": sorted(ids)}
