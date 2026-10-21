"""Checked fragment decisions; no model writes directly to the content store."""

import json

from sqlalchemy import text

from ..contracts import canonical, sha
from ..eligibility import eligible
from ..jobs import require_lease
from ..planning import QualityFailure
from ..repository import Conflict, identifier, insert, now, one, rows
from .contracts import Check, Decision
from .coordinator import POLICY, schedule
from .fragments import fragments


def catalogue(store, connection):
    result = []
    valid_atomics = {n["revision_id"] for n in eligible(connection) if n["kind"] == "ATOMIC"}
    for row in rows(
        connection,
        "SELECT n.*,c.definition_json FROM notes n LEFT JOIN concept_identity_claims c ON c.concept_id=n.id WHERE n.kind='CONCEPT' AND (n.current_revision_id IS NOT NULL OR c.concept_id IS NOT NULL) ORDER BY n.id",
    ):
        view = {
            "id": row["id"],
            "title": row["title"],
            "domain": row["domain"],
            "aliases": json.loads(row["aliases_json"]),
            "sections": [],
        }
        if row["definition_json"]:
            view["definition"] = json.loads(row["definition_json"])
        if row["current_revision_id"]:
            rev = one(
                connection,
                "SELECT * FROM note_revisions WHERE id=:id",
                id=row["current_revision_id"],
            )
            body = store.read(rev["markdown_ref"], rev["markdown_sha256"]).decode()
            view.update(
                scope=json.loads(rev["scope_json"]),
                valid=store.content.valid_revision(connection, rev["id"]),
            )
            view["sections"] = [
                {**dict(s), "markdown": body[s["start_offset"] : s["end_offset"]]}
                for s in rows(
                    connection,
                    "SELECT section_id,title,question,start_offset,end_offset FROM sections WHERE revision_id=:id ORDER BY position",
                    id=rev["id"],
                )
            ]
        # Expose reserved chapters before their first publication. Later fragments must
        # address these stable IDs rather than inventing a source-note section ID.
        pending = {}
        published_ids = {s["section_id"] for s in view["sections"]}
        for contribution in rows(
            connection,
            "SELECT * FROM contribution_decisions WHERE concept_id=:id AND status='ACCEPTED' AND section_id IS NOT NULL ORDER BY created_at,id",
            id=row["id"],
        ):
            if (
                contribution["atomic_revision_id"] not in valid_atomics
                or contribution["section_id"] in published_ids
            ):
                continue
            decision = json.loads(contribution["decision_json"])
            fragment = json.loads(contribution["fragment_json"])
            chapter = pending.setdefault(
                contribution["section_id"],
                {
                    "section_id": contribution["section_id"],
                    "title": decision.get("section_title") or fragment["title"],
                    "question": decision.get("section_question") or fragment["title"],
                    "markdown": "",
                    "planned": True,
                },
            )
            chapter["markdown"] += fragment["text"] + "\n\n"
        view["sections"].extend(pending.values())
        result.append(view)
    if len(canonical(result)) > 80000:
        raise QualityFailure(
            "Complete concept catalogue exceeds context budget; no identities were dropped"
        )
    return result


def validate(decision, catalog):
    if decision.action == "CREATE_CONCEPT":
        if decision.concept_id or not decision.proposal:
            raise QualityFailure("New concept requires proposal and no existing target")
        return
    if (
        decision.action == "CONFLICT"
        and decision.concept_id
        and not any(c["id"] == decision.concept_id for c in catalog)
    ):
        raise QualityFailure("Unknown conflict target")
    if decision.action in {"NO_CHANGE", "INSUFFICIENT", "CONFLICT"}:
        if decision.action == "NO_CHANGE":
            raise QualityFailure("New support cannot be discarded using NO_CHANGE")
        return
    target = next((c for c in catalog if c["id"] == decision.concept_id), None)
    if not target:
        raise QualityFailure(
            "concept_id must be an exact catalogue id (UUID), never identity_key or title; choose from "
            + canonical([{"concept_id": c["id"], "title": c["title"]} for c in catalog])
        )
    ids = {s["section_id"] for s in target["sections"]}
    if decision.action in {"ADD_EVIDENCE", "UPDATE_SECTION"} and decision.section_id not in ids:
        if not target["sections"] and decision.section_question:
            return
        raise QualityFailure(
            "Unknown section target; choose an exact Concept section_id from "
            + str(sorted(ids))
            + "; source fragment section IDs are not Concept section IDs"
        )
    if decision.action == "CREATE_SECTION" and not decision.section_question.strip():
        raise QualityFailure("New section needs a concrete question")
    if decision.action == "CREATE_SECTION" and decision.section_id in ids:
        raise QualityFailure("New section collides with existing section")


def analyze(store, lease, calls):
    with store.db.engine.connect() as c:
        note = one(c, "SELECT * FROM notes WHERE id=:id", id=lease.input["entity_id"])
        if (
            not note
            or note["kind"] != "ATOMIC"
            or note["current_revision_id"] != lease.input["revision_id"]
            or not store.content.valid_revision(c, note["current_revision_id"])
        ):
            return {"status": "SUPERSEDED_OR_INELIGIBLE"}
    note = store.get_note(note["id"])
    manifest = fragments(store, note)
    for fragment in manifest:
        with store.db.engine.connect() as c:
            existing = one(
                c,
                "SELECT * FROM contribution_decisions WHERE atomic_revision_id=:r AND fragment_id=:f AND policy=:p",
                r=fragment["atomic_revision_id"],
                f=fragment["fragment_id"],
                p=POLICY,
            )
            catalog = catalogue(store, c)
        if existing:
            if existing["status"] == "REVIEW":
                raise QualityFailure("Contribution needs review")
            continue
        feedback = []
        for attempt in range(3):
            decision = calls.run(
                "next.concept_decide",
                "逐片段决定知识维护。先核对领域、对象、scope和条件，再匹配章节和增量。重复定义ADD_EVIDENCE，新解释/条件UPDATE_SECTION，同主题独立问题CREATE_SECTION，独立长案例LINK_ONLY。仅当完整目录没有等价身份且有独立组织价值才CREATE_CONCEPT；不能每篇笔记复制同义Concept。新建提供ASCII identity_key、中文title、domain、question、scope与第一章section_title/section_question；已有目标的concept_id必须逐字复制catalogue条目的id（UUID），不能使用definition.identity_key或标题；section_id复制该Concept的sections[].section_id，不能使用来源原子片段的section_id。planned=true是已预留、待发布章节，也可引用。矛盾CONFLICT，证据不足INSUFFICIENT。新片段不能用NO_CHANGE丢掉新支持。",
                {
                    "fragment": fragment,
                    "catalogue": catalog,
                    "catalogue_complete": True,
                    "feedback": feedback,
                },
                Decision,
            )
            try:
                validate(decision, catalog)
                check = calls.run(
                    "next.concept_decision_review",
                    "独立核对身份、章节、范围和增量。检查新建是否重复、补证据是否确无新知识、单位条件符号是否相容。valid=false旧正文只用于比较，不作事实支持。重要教学内容不能误判无价值。",
                    {"fragment": fragment, "catalogue": catalog, "decision": decision.model_dump()},
                    Check,
                )
                if not check.passed or check.issues:
                    raise QualityFailure(str(check.model_dump()))
                break
            except ValueError as exc:
                feedback.append(str(exc))
        else:
            raise QualityFailure("Decision review failed: " + str(feedback))
        target = decision.concept_id
        if decision.action == "CREATE_CONCEPT":
            proposal = decision.proposal
            target = store.content.create_note(
                "CONCEPT",
                proposal.title,
                domain=proposal.domain,
                creation_key=f"concept:{proposal.domain}:{proposal.identity_key}",
                lease=lease,
            )
        status = (
            "REVIEW"
            if decision.action == "CONFLICT"
            else "SKIPPED"
            if decision.action in {"NO_CHANGE", "INSUFFICIENT"}
            else "ACCEPTED"
        )
        section = decision.section_id
        if decision.action in {"CREATE_CONCEPT", "CREATE_SECTION"} or (
            not section and decision.section_question
        ):
            question = decision.section_question or (
                decision.proposal.question if decision.proposal else ""
            )
            if not question:
                raise QualityFailure("New chapter needs a concrete question")
            section = "section_" + sha(question)[:20]
        with store.repository.transaction() as c:
            require_lease(c, lease)
            if not store.content.valid_revision(c, fragment["atomic_revision_id"]):
                raise Conflict("Atomic changed during analysis")
            if sha(canonical(catalogue(store, c))) != sha(canonical(catalog)):
                raise Conflict("Identity catalogue changed during analysis")
            if decision.action == "CREATE_CONCEPT":
                c.execute(
                    text(
                        "INSERT OR IGNORE INTO concept_identity_claims(domain,identity_key,concept_id,definition_json) VALUES(:d,:k,:id,:v)"
                    ),
                    {
                        "d": proposal.domain,
                        "k": proposal.identity_key,
                        "id": target,
                        "v": canonical(proposal.model_dump()),
                    },
                )
                claim = one(
                    c,
                    "SELECT concept_id FROM concept_identity_claims WHERE domain=:d AND identity_key=:k",
                    d=proposal.domain,
                    k=proposal.identity_key,
                )
                if claim["concept_id"] != target:
                    raise Conflict("Identity reservation changed")
            insert(
                c,
                "contribution_decisions",
                id=identifier(),
                atomic_revision_id=fragment["atomic_revision_id"],
                fragment_id=fragment["fragment_id"],
                policy=POLICY,
                concept_id=target,
                section_id=section,
                action=decision.action,
                status=status,
                fragment_json=canonical(fragment),
                decision_json=canonical(decision.model_dump()),
                applied_revision_id=None,
                created_at=now(),
            )
            if status == "ACCEPTED":
                schedule(store, c, target, lease.job_id)
            if status == "REVIEW" and target:
                store.repository.advance_epoch(c)
                store.repository.event(
                    c,
                    "CONCEPT_CONFLICT_DETECTED",
                    target,
                    None,
                    {"atomic_revision_id": fragment["atomic_revision_id"]},
                    key="conflict:" + fragment["fragment_id"],
                )
        if status == "REVIEW":
            raise QualityFailure("Source conflict: " + decision.reason)
    return {"processed_fragments": len(manifest), "atomic_revision_id": note["revision"]["id"]}
