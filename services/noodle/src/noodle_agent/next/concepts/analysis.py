"""Checked fragment decisions; no model writes directly to the content store."""

import json

from sqlalchemy import text

from ..contracts import canonical, sha
from ..eligibility import eligible
from ..jobs import require_lease
from ..planning import QualityFailure
from ..repository import Conflict, identifier, insert, now, one, rows
from .catalogue import audit_identity, candidates, comparison
from .contracts import Check, Decision, Identity
from .coordinator import POLICY, schedule
from .fragments import fragments


def catalogue(store, connection):
    result = []
    valid_atomics = {n["revision_id"] for n in eligible(connection) if n["kind"] == "ATOMIC"}
    supported_targets = {r['concept_id'] for r in rows(connection,
        "SELECT DISTINCT concept_id,atomic_revision_id FROM contribution_decisions WHERE status='ACCEPTED' AND concept_id IS NOT NULL")
        if r['atomic_revision_id'] in valid_atomics}
    for row in rows(
        connection,
        "SELECT n.*,c.definition_json FROM notes n LEFT JOIN concept_identity_claims c ON c.concept_id=n.id WHERE n.kind='CONCEPT' AND (n.current_revision_id IS NOT NULL OR c.concept_id IS NOT NULL) ORDER BY n.id",
    ):
        if row['visibility'] != 'ACTIVE':
            continue
        valid = bool(row['current_revision_id'] and store.content.valid_revision(connection, row['current_revision_id']))
        if not valid and row['id'] not in supported_targets:
            continue  # Retired/empty claims are history, not live object candidates.
        view = {
            "id": row["id"],
            "title": row["title"],
            "domain": row["domain"],
            "revision_id": row["current_revision_id"],
            "visibility": row["visibility"],
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
                summary=rev["summary"],
                scope=json.loads(rev["scope_json"]),
                valid=store.content.valid_revision(connection, rev["id"]),
                markdown_sha256=rev['markdown_sha256'],
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
        view['internal_topics'] = [{'key': s['section_id'], 'title': s['title']} for s in view['sections'] if s['section_id'].startswith('entity_topic_')]
        result.append(view)
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


def refresh_unpublished_scopes(store):
    """Refine source-section identity metadata before its first publication."""
    with store.repository.transaction() as c:
        claims = rows(
            c,
            "SELECT ci.concept_id,ci.definition_json FROM concept_identity_claims ci JOIN notes n ON n.id=ci.concept_id WHERE n.current_revision_id IS NULL AND json_extract(ci.definition_json,'$._entity_policy') IS NULL AND EXISTS(SELECT 1 FROM contribution_decisions d JOIN note_revisions r ON r.id=d.atomic_revision_id JOIN notes a ON a.id=r.note_id WHERE d.concept_id=n.id AND a.creation_key LIKE :pattern)",
            pattern="%:source-section:%",
        )
        for claim in claims:
            old = json.loads(claim["definition_json"])
            revised = Identity.model_validate(old).model_dump()
            if old == revised:
                continue
            receipt = canonical(
                {
                    "policy": "open-auto-identity-scope-v1",
                    "concept_id": claim["concept_id"],
                    "before": old,
                    "after": revised,
                }
            )
            store.put(f"artifacts/identity-refinements/{sha(receipt)}.json", receipt.encode())
            c.execute(
                text(
                    "UPDATE concept_identity_claims SET definition_json=:value WHERE concept_id=:id"
                ),
                {"value": canonical(revised), "id": claim["concept_id"]},
            )


def analyze(store, lease, calls, retriever=None):
    batch = lease.input.get("payload", {}).get("routing_batch")
    if batch:
        from dataclasses import replace
        outcomes = []
        for atomic in batch["atomics"]:
            child = replace(lease, input={**lease.input, "entity_id": atomic["note_id"], "revision_id": atomic["revision_id"], "payload": {**lease.input["payload"], "routing_batch": None, "closed_routing_batch": batch}})
            outcome = analyze(store, child, calls, retriever)
            outcomes.append({**atomic, **outcome})
        # Commit one desired input set per Concept, even if a previous attempt
        # saved contributions and lost its lease before scheduling.
        with store.repository.transaction() as c:
            require_lease(c, lease)
            for target in sorted({target for o in outcomes for target in o.get('concept_ids', [o['concept_id']] if o.get('concept_id') else [])}):
                schedule(store, c, target, lease.job_id)
        return {"status": "PARTIAL_REVIEW" if any(o["status"] == "REVIEW" or o.get("unresolved_relations") or o.get("protocol_issues") for o in outcomes) else "BATCH_ANALYZED", "outcomes": outcomes, "input_hash": batch["input_hash"]}
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
    from ..navigation import frozen_candidate
    if frozen_candidate(store, note["revision"]["id"]).entity_hints:
        from .entity_routing import analyze_entity
        return analyze_entity(store, lease, calls, note, retriever)
    manifest = fragments(store, note)
    precomputed = {}
    if ":source-section:" in (note.get("creation_key") or ""):
        from .batching import batch_decisions

        refresh_unpublished_scopes(store)
        with store.db.engine.connect() as c:
            pending = [
                f
                for f in manifest
                if not one(
                    c,
                    "SELECT id FROM contribution_decisions WHERE atomic_revision_id=:r AND fragment_id=:f AND policy=:p",
                    r=f["atomic_revision_id"],
                    f=f["fragment_id"],
                    p=POLICY,
                )
            ]
            current_catalog = catalogue(store, c)
        precomputed = batch_decisions(store, pending[:4], current_catalog, calls, retriever)
    processed = 0
    for fragment in manifest:
        calls.begin_unit()
        with store.db.engine.connect() as c:
            existing = one(
                c,
                "SELECT * FROM contribution_decisions WHERE atomic_revision_id=:r AND fragment_id=:f AND policy=:p",
                r=fragment["atomic_revision_id"],
                f=fragment["fragment_id"],
                p=POLICY,
            )
            complete_catalog = catalogue(store, c)
        if existing:
            if existing["status"] == "REVIEW":
                raise QualityFailure("Contribution needs review")
            continue
        catalog, catalog_complete = candidates(complete_catalog, fragment, retriever=retriever)
        feedback = []
        identity_audit = None
        batch = precomputed.get(fragment["fragment_id"])
        for attempt in range(3):
            decision = (
                batch["decision"]
                if batch and attempt == 0
                else calls.run(
                    "next.concept_decide",
                    "根据当前章节的核心问题、领域、scope和正文，为片段选择知识维护动作。title是来源笔记标题，不要求每个片段覆盖全篇标题；section_title/section_question是当前片段语境。"
                    "候选只有身份和章节卡，不能凭空猜测正文。重复定义可暂选ADD_EVIDENCE，再由独立审查比较实际目标章节；补充解释条件选UPDATE_SECTION，同一身份的独立问题选CREATE_SECTION。"
                    "代码、具体输出表、长案例及对比若不适合作为独立概念，可LINK_ONLY到最相关Concept，保留独立原子笔记。不要为每个API调用或数据表新建Concept。跨多个子问题的比较段也可LINK_ONLY到相关父Concept，保留完整Atomic，不以一个片段必须同时写进多个章节作为前提。"
                    "新建只用于有独立组织价值且检索未发现等价身份的核心知识，提供identity_key、title、domain、question、scope及第一章问题。应用会追加混合身份检索核查，无需全库分页LLM扫描。"
                    "Concept的scope用于界定对象与条件，不能将来源笔记临时不展开的实现细节固化成该身份永远不允许引用的内容。若确实超出目标身份范围，使用LINK_ONLY而非强行覆盖。"
                    "catalogue_complete=false表示有限检索候选，不能视为全库不存在的证明，也不能据此否认每个合理新建。planned=true或valid=false身份仍可复用，表示未发布或旧正文失效，不能把旧正文当事实支持。"
                    "已有目标必须复制精确UUID和其section_id。存在冲突选CONFLICT，确无独立贡献选INSUFFICIENT；不能用NO_CHANGE丢掉新证据。",
                    {
                        "fragment": fragment,
                        "catalogue": catalog,
                        "catalogue_complete": catalog_complete,
                        "identity_audit": identity_audit,
                        "feedback": feedback,
                    },
                    Decision,
                )
            )
            try:
                if decision.action == "CREATE_CONCEPT" and complete_catalog:
                    if not decision.proposal:
                        raise QualityFailure("New Concept requires a proposal")
                    match, identity_audit = audit_identity(
                        calls, decision.proposal, complete_catalog, retriever=retriever
                    )
                    if match:
                        target_detail = next(
                            item for item in complete_catalog if item["id"] == match
                        )
                        from .catalogue import card

                        catalog = [card(target_detail)] + [
                            item for item in catalog if item["id"] != match
                        ][:5]
                        feedback.append(
                            "Whole-library identity audit found an equivalent Concept. Reuse its exact ID: "
                            + match
                        )
                        continue
                validate(decision, catalog)
                review_catalog = (
                    comparison(complete_catalog, decision, fragment)
                    if decision.concept_id
                    else catalog
                )
                target_digest = sha(
                    canonical(
                        next((i for i in complete_catalog if i["id"] == decision.concept_id), None)
                    )
                )
                checked_batch = batch and attempt == 0 and batch["target_sha256"] == target_digest
                check = (
                    batch["check"]
                    if checked_batch
                    else calls.run(
                        "next.concept_decision_review",
                        "独立核对当前片段、选定动作与实际目标章节，检查对象、条件、重复或真实增量；只对实质错误判不通过。只列最多3条具体问题，reason简短。"
                        "fragment的title是来源全篇标题，不要求局部章节片段覆盖整个标题；fragment.section_id是来源位置，不会被当成目标Concept章节ID。"
                        "LINK_ONLY只建立到原子笔记的阅读关系，适合代码、输出表、长案例、超出目标展开范围的实例；它不将片段正文塞入目标章节，不能因缺少可重写的新知识而否决。"
                        "planned=true或valid=false不能用于事实支持，但不妨碍复用身份、引用已预留章节或建立导航。不要把不存在已发布正文误判为没有有效原子证据。"
                        "catalogue_complete=false只说明是检索候选；identity_audit已做独立候选身份核查，不再要求全库分页扫描。新Concept需要独立组织价值，不要求发明超出片段的新知识或实验。"
                        "scope.excludes按实际文字理解，不能一会儿要求遵守、一会儿反向解释。比较实际目标正文，不要求片段包含所有领域知识或补写来源未提供的数值与机制。",
                        {
                            "fragment": fragment,
                            "catalogue": review_catalog,
                            "catalogue_complete": catalog_complete,
                            "identity_audit": identity_audit,
                            "decision": decision.model_dump(),
                        },
                        Check,
                    )
                )
                if not check.passed or check.issues:
                    raise QualityFailure(str(check.model_dump()))
                break
            except ValueError as exc:
                feedback.append(str(exc)[:1800])
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
            if sha(canonical(catalogue(store, c))) != sha(canonical(complete_catalog)):
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
        processed += 1
        if processed >= (len(precomputed) if precomputed else 4):
            with store.db.engine.connect() as c:
                pending = any(
                    not one(
                        c,
                        "SELECT id FROM contribution_decisions WHERE atomic_revision_id=:r AND fragment_id=:f AND policy=:p",
                        r=f["atomic_revision_id"],
                        f=f["fragment_id"],
                        p=POLICY,
                    )
                    for f in manifest
                )
            if pending:
                return {
                    "status": "PARTIAL",
                    "processed_fragments": processed,
                    "atomic_revision_id": note["revision"]["id"],
                }
    return {"processed_fragments": len(manifest), "atomic_revision_id": note["revision"]["id"]}
