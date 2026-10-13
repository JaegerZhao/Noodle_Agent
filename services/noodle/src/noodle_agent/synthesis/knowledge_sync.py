"""Durable source-event reconciliation of shared knowledge releases."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update

from ..db import (
    AtomicNote,
    BackboneCorpus,
    Claim,
    ClaimEvidence,
    Concept,
    ConceptAlias,
    ConceptContribution,
    ConceptEmbedding,
    ConceptModule,
    ConceptModuleVersion,
    ConceptReleaseModule,
    ConceptUpdateJob,
    ConceptVersion,
    Job,
    KnowledgeContributionSelection,
    KnowledgeModuleState,
    KnowledgeSyncEvent,
    KnowledgeSyncProposal,
    NoteBlock,
    NoteVersion,
    Source,
    SourceNote,
    SourceNoteUnitPlacement,
    SourceNoteVersion,
    SourceVersion,
    Synthesis,
    SynthesisNode,
    SynthesisReferenceSource,
    SynthesisVersion,
    delete_fts_for_note,
    insert_fts,
    utcnow,
)
from .curation import normalize_alias
from .semantic_builder import ModuleReviewRequired, _canonical_module_role

POLICY_VERSION = "knowledge-sync-v1"


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(value, default=None):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {} if default is None else default


def digest(value) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def blocked_concepts(session) -> set[str]:
    # A rejected, unpinned new module must not hide an existing valid release.
    return set(
        session.scalars(
            select(Concept.id)
            .join(
                ConceptReleaseModule,
                ConceptReleaseModule.concept_version_id == Concept.active_version_id,
            )
            .join(
                KnowledgeModuleState,
                KnowledgeModuleState.module_id == ConceptReleaseModule.module_id,
            )
            .where(KnowledgeModuleState.dirty.is_(True))
        )
    )


def eligible_note(session, version: NoteVersion) -> bool:
    note = session.get(AtomicNote, version.note_id)
    source = session.get(Source, note.source_id) if note else None
    return bool(
        note
        and source
        and note.visibility == "ACTIVE"
        and source.visibility == "ACTIVE"
        and note.active_version_id == version.id
        and source.current_version_id == version.source_version_id
    )


class KnowledgeSyncService:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.builder = service.semantic_synthesis

    def enqueue(self, session, source_id, kind, payload, *, key):
        existing = session.scalar(
            select(KnowledgeSyncEvent).where(KnowledgeSyncEvent.event_key == key)
        )
        if existing:
            return existing.id
        event = KnowledgeSyncEvent(
            event_key=key, source_id=source_id, event_type=kind, payload_json=dumps(payload)
        )
        session.add(event)
        session.flush()
        return event.id

    def invalidate(self, session, version_ids, event_id):
        module_ids = set(
            session.scalars(
                select(ConceptContribution.module_id).where(
                    ConceptContribution.atomic_note_version_id.in_(version_ids),
                    ConceptContribution.decision_status.in_(
                        ["ACCEPTED", "APPROVED", "RETRACT_PENDING", "SUPERSEDE_PENDING"]
                    ),
                )
            )
        )
        for module_id in module_ids:
            self._dirty(session, module_id, event_id)
        return sorted(module_ids)

    @staticmethod
    def _dirty(session, module_id, event_id):
        state = session.get(KnowledgeModuleState, module_id)
        if state is None:
            state = KnowledgeModuleState(module_id=module_id, revision=0)
            session.add(state)
        state.revision += 1
        state.dirty = True
        state.event_id = event_id

    def delete_source(self, source_id, actor):
        with self.db.session() as session:
            source = session.get(Source, source_id)
            if not source:
                raise ValueError(f"Unknown source_id: {source_id}")
            if source.visibility == "DELETED":
                return {"status": "DELETED", "source_id": source_id, "reused": True}
            source.visibility = "DELETED"
            notes = session.scalars(
                select(AtomicNote).where(AtomicNote.source_id == source_id)
            ).all()
            versions = session.scalars(
                select(NoteVersion.id).where(NoteVersion.note_id.in_([n.id for n in notes]))
            ).all()
            contribution_ids = session.scalars(
                select(ConceptContribution.id).where(
                    ConceptContribution.atomic_note_version_id.in_(versions),
                    ConceptContribution.decision_status.in_(["ACCEPTED", "APPROVED"]),
                )
            ).all()
            event_id = self.enqueue(
                session,
                source_id,
                "SOURCE_DELETED",
                {"actor": actor, "retired_contributions": contribution_ids},
                key=f"delete:{source_id}:{uuid.uuid4().hex}",
            )
            modules = self.invalidate(session, versions, event_id)
            for note in notes:
                if note.visibility == "ACTIVE":
                    note.visibility = "SOURCE_DELETED"
                delete_fts_for_note(session, note.id)
            for release in session.scalars(
                select(SourceNote).where(SourceNote.source_id == source_id)
            ):
                release.visibility = "SOURCE_DELETED"
            # Keep historical foreign keys. Active generation is cancelled and fenced at persistence.
            for job in session.scalars(
                select(Job).where(
                    Job.source_version_id.in_(
                        session.scalars(
                            select(SourceVersion.id).where(SourceVersion.source_id == source_id)
                        ).all()
                    ),
                    Job.status.notin_(
                        ["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "CANCELLED"]
                    ),
                )
            ):
                job.status = "CANCELLED"
                job.lease_owner = None
                job.lease_expires_at = None
            return {
                "status": "DELETED",
                "source_id": source_id,
                "sync_event_id": event_id,
                "affected_modules": len(modules),
                "deletion_mode": "LOGICAL",
                "source_deleted": True,
                "external_original_untouched": True,
                "filesystem_warnings": [],
                "deleted": {"sources": 1, "atomic_notes": len(notes)},
            }

    def restore_source(self, source_id):
        with self.db.session() as session:
            source = session.get(Source, source_id)
            if not source or source.visibility != "DELETED":
                raise ValueError("Unknown deleted source")
            source.visibility = "ACTIVE"
            for note in session.scalars(
                select(AtomicNote).where(
                    AtomicNote.source_id == source_id, AtomicNote.visibility == "SOURCE_DELETED"
                )
            ):
                note.visibility = "ACTIVE"
            for release in session.scalars(
                select(SourceNote).where(
                    SourceNote.source_id == source_id, SourceNote.visibility == "SOURCE_DELETED"
                )
            ):
                release.visibility = "ACTIVE"
            event_id = self.enqueue(
                session,
                source_id,
                "SOURCE_RESTORED",
                {},
                key=f"restore:{source_id}:{uuid.uuid4().hex}",
            )
            deletion = session.scalar(
                select(KnowledgeSyncEvent)
                .where(
                    KnowledgeSyncEvent.source_id == source_id,
                    KnowledgeSyncEvent.event_type == "SOURCE_DELETED",
                )
                .order_by(KnowledgeSyncEvent.created_at.desc())
            )
            for contribution_id in (
                loads(deletion.payload_json).get("retired_contributions", []) if deletion else []
            ):
                contribution = session.get(ConceptContribution, contribution_id)
                version = (
                    session.get(NoteVersion, contribution.atomic_note_version_id)
                    if contribution
                    else None
                )
                if (
                    contribution
                    and version
                    and eligible_note(session, version)
                    and contribution.decision_status == "RETRACTED"
                ):
                    contribution.decision_status = "APPROVED"
                    self._dirty(session, contribution.module_id, event_id)
            for note in session.scalars(
                select(AtomicNote).where(
                    AtomicNote.source_id == source_id, AtomicNote.visibility == "ACTIVE"
                )
            ):
                version = session.get(NoteVersion, note.active_version_id)
                blocks = session.scalars(
                    select(NoteBlock).where(NoteBlock.note_version_id == version.id)
                ).all()
                insert_fts(
                    session,
                    note_version_id=version.id,
                    note_id=note.id,
                    title=version.title,
                    thesis=version.thesis,
                    body="\n\n".join(b.content_text for b in blocks),
                    tags="",
                )
            return {"status": "RESTORED", "sync_event_id": event_id}

    def delete_note(self, note_id):
        with self.db.session() as session:
            note = session.get(AtomicNote, note_id)
            if not note:
                raise ValueError("Unknown AtomicNote")
            versions = session.scalars(
                select(NoteVersion.id).where(NoteVersion.note_id == note_id)
            ).all()
            note.visibility = "DELETED"
            event_id = self.enqueue(
                session,
                note.source_id,
                "NOTE_DELETED",
                {"note_id": note_id},
                key=f"note-delete:{note_id}:{uuid.uuid4().hex}",
            )
            self.invalidate(session, versions, event_id)
            delete_fts_for_note(session, note_id)
            release_owner = session.scalar(
                select(SourceNote).where(SourceNote.source_id == note.source_id)
            )
            parent = (
                session.get(SourceNoteVersion, release_owner.active_version_id)
                if release_owner
                else None
            )
            if parent:
                new = self.service._clone_source_note_release(
                    session,
                    source_note=release_owner,
                    parent=parent,
                    actor="knowledge-sync",
                    edit_signature={"operation": "DELETE_NOTE", "note_id": note_id},
                )
                for placement in session.scalars(
                    select(SourceNoteUnitPlacement).where(
                        SourceNoteUnitPlacement.source_note_version_id == new.id,
                        SourceNoteUnitPlacement.note_id == note_id,
                    )
                ):
                    session.delete(placement)
            return {"status": "DELETED", "sync_event_id": event_id}

    def events(self, source_id=None):
        with self.db.session() as session:
            query = (
                select(KnowledgeSyncEvent).order_by(KnowledgeSyncEvent.created_at.desc()).limit(50)
            )
            if source_id:
                query = query.where(KnowledgeSyncEvent.source_id == source_id)
            return [
                {
                    "id": e.id,
                    "source_id": e.source_id,
                    "event_type": e.event_type,
                    "source_title": session.get(Source, e.source_id).title
                    if session.get(Source, e.source_id)
                    else "已删除资料",
                    "status": e.status,
                    "attempt_count": e.attempt_count,
                    "error_detail": e.error_detail,
                    "result": loads(e.result_json),
                    "updated_at": e.updated_at.isoformat() + "Z",
                }
                for e in session.scalars(query)
            ]

    def proposals(self, event_id):
        with self.db.session() as session:
            return [
                {
                    "id": p.id,
                    "note_version_id": p.note_version_id,
                    "decision": p.decision,
                    "status": p.status,
                    "error_detail": p.error_detail,
                    "proposal": loads(p.payload_json),
                }
                for p in session.scalars(
                    select(KnowledgeSyncProposal).where(KnowledgeSyncProposal.event_id == event_id)
                )
            ]

    def retry(self, event_id, *, module_ids=None):
        with self.db.session() as session:
            event = session.get(KnowledgeSyncEvent, event_id)
            if not event or event.status not in {
                "FAILED",
                "COMPLETED_WITH_ERRORS",
                "COMPLETED_WITH_REVIEW",
            }:
                raise ValueError("Only terminal failed or review sync events can be retried")
            selected_modules = set(module_ids or [])
            if selected_modules:
                known = set(
                    session.scalars(
                        select(KnowledgeModuleState.module_id).where(
                            KnowledgeModuleState.event_id == event_id
                        )
                    )
                )
                if not selected_modules <= known:
                    raise ValueError("Retry requested a module outside this sync event")
            retry_reviews = event.status == "COMPLETED_WITH_REVIEW" or bool(selected_modules)
            history = loads(event.payload_json)
            history.setdefault("attempt_history", []).append(
                {
                    "attempt": event.attempt_count,
                    "status": event.status,
                    "result": loads(event.result_json),
                    "error": event.error_detail,
                }
            )
            event.payload_json = dumps(history)
            event.status = "QUEUED"
            for proposal in session.scalars(
                select(KnowledgeSyncProposal).where(
                    KnowledgeSyncProposal.event_id == event_id,
                    KnowledgeSyncProposal.status == "NEEDS_REVIEW",
                )
            ):
                if (
                    "Unknown existing module" in proposal.error_detail
                    or "Equivalent identity" in proposal.error_detail
                ):
                    proposal.status = "RETRY_ANALYSIS"
                elif (
                    retry_reviews
                    and "Concept candidate rejected by independent Module Judge"
                    in proposal.error_detail
                ):
                    data = loads(proposal.payload_json)
                    mid = data.get("module_id")
                    if mid and (not selected_modules or mid in selected_modules):
                        for contribution in session.scalars(
                            select(ConceptContribution)
                            .join(
                                KnowledgeContributionSelection,
                                KnowledgeContributionSelection.contribution_id
                                == ConceptContribution.id,
                            )
                            .where(
                                ConceptContribution.module_id == mid,
                                ConceptContribution.decision_status == "REVIEW_REQUIRED",
                                KnowledgeContributionSelection.event_id == event_id,
                            )
                        ):
                            version = session.get(NoteVersion, contribution.atomic_note_version_id)
                            if version and eligible_note(session, version):
                                contribution.decision_status = "APPROVED"
                        self._dirty(session, mid, event_id)
                        proposal.status = "APPLIED"
                        proposal.error_detail = ""
            event.error_detail = ""
            event.lease_owner = None
            event.lease_expires_at = None
            return {"status": "QUEUED", "event_id": event_id}

    def claim(self, owner):
        with self.db.session() as session:
            now = utcnow()
            ids = session.scalars(
                select(KnowledgeSyncEvent.id)
                .where(
                    KnowledgeSyncEvent.status.in_(["QUEUED", "RUNNING"]),
                    (KnowledgeSyncEvent.lease_expires_at.is_(None))
                    | (KnowledgeSyncEvent.lease_expires_at <= now),
                )
                .order_by(KnowledgeSyncEvent.created_at)
                .limit(8)
            ).all()
            for event_id in ids:
                result = session.execute(
                    update(KnowledgeSyncEvent)
                    .where(
                        KnowledgeSyncEvent.id == event_id,
                        (KnowledgeSyncEvent.lease_expires_at.is_(None))
                        | (KnowledgeSyncEvent.lease_expires_at <= now),
                    )
                    .values(
                        status="RUNNING",
                        lease_owner=owner,
                        lease_expires_at=now + timedelta(seconds=180),
                        attempt_count=KnowledgeSyncEvent.attempt_count + 1,
                    )
                )
                if result.rowcount == 1:
                    return event_id
        return None

    def heartbeat(self, event_id, owner):
        with self.db.session() as session:
            result = session.execute(
                update(KnowledgeSyncEvent)
                .where(
                    KnowledgeSyncEvent.id == event_id,
                    KnowledgeSyncEvent.lease_owner == owner,
                    KnowledgeSyncEvent.status == "RUNNING",
                )
                .values(lease_expires_at=utcnow() + timedelta(seconds=180))
            )
            if result.rowcount == 1:
                session.execute(
                    update(ConceptUpdateJob)
                    .where(ConceptUpdateJob.lease_owner == f"sync:{event_id}:{owner}")
                    .values(lease_expires_at=utcnow() + timedelta(seconds=180))
                )
            return result.rowcount == 1

    @staticmethod
    def fence(session, event_id, owner):
        event = session.get(KnowledgeSyncEvent, event_id)
        if (
            not event
            or event.status != "RUNNING"
            or event.lease_owner != owner
            or not event.lease_expires_at
            or event.lease_expires_at <= utcnow()
        ):
            raise ValueError("Sync execution lease lost; late publication rejected")

    def _snapshot(self, session, module_id):
        result = []
        for contribution in session.scalars(
            select(ConceptContribution)
            .where(
                ConceptContribution.module_id == module_id,
                ConceptContribution.decision_status.in_(["ACCEPTED", "APPROVED"]),
            )
            .order_by(ConceptContribution.id)
        ):
            version = session.get(NoteVersion, contribution.atomic_note_version_id)
            if not version or not eligible_note(session, version):
                continue
            selection = session.get(KnowledgeContributionSelection, contribution.id)
            result.append(
                {
                    "contribution_id": contribution.id,
                    "note_version_id": version.id,
                    "sha256": version.content_sha256,
                    "blocks": loads(selection.block_ids_json, []) if selection else [],
                }
            )
        return result

    def _classify(self, version_id):
        with self.db.session() as session:
            version = session.get(NoteVersion, version_id)
            if not version or not eligible_note(session, version):
                return []
            blocks = session.scalars(
                select(NoteBlock)
                .where(NoteBlock.note_version_id == version_id)
                .order_by(NoteBlock.order_index)
            ).all()
            supported = set(
                session.scalars(
                    select(Claim.note_block_id)
                    .join(ClaimEvidence, ClaimEvidence.claim_id == Claim.id)
                    .where(Claim.note_version_id == version_id)
                )
            )
            note = {
                "note_version_id": version.id,
                "title": version.title,
                "thesis": version.thesis,
                "blocks": [
                    {
                        "id": b.id,
                        "key": b.block_key,
                        "text": b.content_text[:2500],
                        "supported": b.id in supported,
                    }
                    for b in blocks
                ],
            }
        hits = self.service.knowledge_curation.search_concepts(note["title"], limit=6)
        candidates = []
        with self.db.session() as session:
            version = session.get(NoteVersion, version_id)
            atomic = session.get(AtomicNote, version.note_id)
            related_ids = set(
                session.scalars(
                    select(ConceptModule.concept_id)
                    .join(ConceptContribution, ConceptContribution.module_id == ConceptModule.id)
                    .join(NoteVersion, NoteVersion.id == ConceptContribution.atomic_note_version_id)
                    .join(AtomicNote, AtomicNote.id == NoteVersion.note_id)
                    .where(AtomicNote.source_id == atomic.source_id)
                )
            )
            related_ids.update(hit.concept_id for hit in hits)
            registry = self.service.knowledge_curation.list_concepts(note["title"], limit=6)
            related_ids.update(r["concept_id"] for r in registry)
            identity_text = normalize_alias(note["title"] + " " + note["thesis"])
            for candidate in session.scalars(
                select(Concept).where(Concept.visibility.in_(["ACTIVE", "WITHDRAWN"]))
            ):
                name = normalize_alias(candidate.canonical_name)
                if len(name) >= 3 and name in identity_text:
                    related_ids.add(candidate.id)
            concepts = [session.get(Concept, c) for c in related_ids]
            concepts = [c for c in concepts if c and c.visibility in {"ACTIVE", "WITHDRAWN"}]
            concepts.sort(
                key=lambda c: self.service.knowledge_curation._lexical_similarity(
                    note["title"], c.canonical_name
                ),
                reverse=True,
            )
            for concept in concepts[:8]:
                release = session.get(ConceptVersion, concept.active_version_id)
                modules = []
                for pin in session.scalars(
                    select(ConceptReleaseModule).where(
                        ConceptReleaseModule.concept_version_id == release.id
                    )
                ):
                    module = session.get(ConceptModule, pin.module_id)
                    mv = session.get(ConceptModuleVersion, pin.module_version_id)
                    if module and mv:
                        modules.append({"key": module.module_key, "text": mv.plain_text[:1800]})
                if not modules:
                    modules = [
                        {"key": m.module_key, "text": "当前没有有效正文。"}
                        for m in session.scalars(
                            select(ConceptModule).where(ConceptModule.concept_id == concept.id)
                        )
                    ]
                if modules:
                    candidates.append(
                        {
                            "id": concept.id,
                            "version_id": release.id,
                            "title": release.title,
                            "scope": loads(release.scope),
                            "modules": modules,
                        }
                    )
            groups = []
            for synthesis in session.scalars(
                select(Synthesis).where(Synthesis.visibility == "ACTIVE")
            ):
                for node in session.scalars(
                    select(SynthesisNode).where(
                        SynthesisNode.synthesis_version_id == synthesis.active_version_id,
                        SynthesisNode.node_kind == "GROUP",
                    )
                ):
                    groups.append(
                        {
                            "synthesis_id": synthesis.id,
                            "system": synthesis.title,
                            "group_key": node.stable_key,
                            "title": node.title,
                        }
                    )
        raw = self.service.agent.structured_json(
            system=(
                "你是个人知识库增量分析器。比较新笔记与候选 Concept，决定是否有独立且被证据支持的增量。"
                "同义重复 NO_CHANGE；仅新来源 EVIDENCE_ONLY；互补定义/机制/公式/例子 ADD_DETAIL；"
                "同一 Concept 的新侧面 NEW_MODULE；确认全库候选无等价主题且能独立解释才 NEW_CONCEPT；"
                "矛盾或科学条件不足 CONFLICT；无实质知识或证据不足 INVALID。相关不等于值得更新。"
                "不要把任意有界函数、足够训练样本等课件简化话术扩写为无条件理论。"
                "输出 JSON {updates:[{decision,concept_id,module_key,title,domain_key,scope:{includes:[],excludes:[],prerequisites:[],aliases:[]},"
                "reason,new_knowledge:[],must_preserve:[],selected_block_ids:[],placements:[{synthesis_id,group_key}]}]}。"
                "每篇最多3个明确主题；只选 supported=true 的实际块 ID；已有 Concept 只能用候选 ID。"
                "新增模块使用简短英文语义 key；NEW_CONCEPT title 为简洁中文概念名，不能照抄整篇复合笔记标题。"
                "placements 只能选择给定 GROUP；不确定则空数组。明确重复应 NO_CHANGE，禁止为了更新而更新。"
            ),
            user={"note": note, "candidates": candidates, "groups": groups},
            operation="knowledge_sync.classify_v1",
            prompt_version=POLICY_VERSION,
            use_pro_model=False,
        )
        updates = raw.get("updates") if isinstance(raw, dict) else None
        if not isinstance(updates, list) or not 1 <= len(updates) <= 3:
            raise ValueError("Invalid update proposal schema")
        candidate_ids = {c["id"]: c for c in candidates}
        allowed = {b["id"] for b in note["blocks"] if b["supported"]}
        valid_decisions = {
            "NO_CHANGE",
            "EVIDENCE_ONLY",
            "ADD_DETAIL",
            "NEW_MODULE",
            "NEW_CONCEPT",
            "CONFLICT",
            "INVALID",
        }
        for index, item in enumerate(updates):
            if isinstance(item, dict) and item.get("decision") == "NEW_CONCEPT":
                with self.db.session() as session:
                    name = normalize_alias(str(item.get("title") or ""))
                    equivalents = [
                        c
                        for c in session.scalars(select(Concept))
                        if normalize_alias(c.canonical_name) == name
                    ]
                    resolved = []
                    for c in equivalents:
                        release = session.get(ConceptVersion, c.active_version_id)
                        if not release:
                            continue
                        pins = session.scalars(
                            select(ConceptReleaseModule).where(
                                ConceptReleaseModule.concept_version_id == release.id
                            )
                        ).all()
                        modules = [
                            {
                                "key": session.get(ConceptModule, pin.module_id).module_key,
                                "text": session.get(
                                    ConceptModuleVersion, pin.module_version_id
                                ).plain_text[:1800],
                            }
                            for pin in pins
                        ]
                        if not modules and c.visibility == "WITHDRAWN":
                            modules = [
                                {"key": m.module_key, "text": "暂无有效正文"}
                                for m in session.scalars(
                                    select(ConceptModule).where(ConceptModule.concept_id == c.id)
                                )
                            ]
                        if modules:
                            resolved.append(
                                {
                                    "id": c.id,
                                    "version_id": release.id,
                                    "title": release.title,
                                    "scope": loads(release.scope),
                                    "modules": modules,
                                }
                            )
                if resolved:
                    repaired = self.service.agent.structured_json(
                        system="全库存在同名知识身份。比较范围并复用合适的给定 Concept；禁止 NEW_CONCEPT。返回 JSON {update:{decision,concept_id,module_key,reason,new_knowledge:[],must_preserve:[],selected_block_ids:[],placements:[]}}。已有模块补充 ADD_DETAIL，新语义模块 NEW_MODULE，冲突或范围不一致 CONFLICT，重复 NO_CHANGE。",
                        user={
                            "note": note,
                            "existing_identities": resolved,
                            "previous_proposal": item,
                        },
                        operation="knowledge_sync.resolve_identity_v1",
                        prompt_version=POLICY_VERSION,
                        use_pro_model=False,
                    )
                    item = repaired.get("update", {})
                    if item.get("decision") == "NEW_CONCEPT":
                        raise ValueError(
                            "Identity resolution cannot duplicate a registered Concept"
                        )
                    updates[index] = item
                    candidate_ids.update({c["id"]: c for c in resolved})
            if not isinstance(item, dict) or item.get("decision") not in valid_decisions:
                raise ValueError("Invalid proposal decision")
            chosen = set(item.get("selected_block_ids") or [])
            if item["decision"] not in {"NO_CHANGE", "CONFLICT", "INVALID"} and (
                not chosen or not chosen <= allowed
            ):
                raise ValueError("Proposal selected unsupported or invented source blocks")
            if item["decision"] in {"ADD_DETAIL", "EVIDENCE_ONLY", "NEW_MODULE"}:
                candidate = candidate_ids.get(item.get("concept_id"))
                if not candidate:
                    raise ValueError("Proposal referenced a Concept outside candidate scope")
                item["expected_version_id"] = candidate["version_id"]
                if item["decision"] == "ADD_DETAIL" and item.get("module_key") not in {
                    m["key"] for m in candidate["modules"]
                }:
                    # A new module is a structural extension, never an overwrite of an invented existing key.
                    if not item.get("new_knowledge"):
                        raise ValueError("Unknown module without a declared knowledge increment")
                    item["original_decision"] = "ADD_DETAIL"
                    item["decision"] = "NEW_MODULE"
            item["note_version_id"] = version_id
            item["placements"] = [
                p
                for p in item.get("placements", [])
                if p
                in [
                    {"synthesis_id": g["synthesis_id"], "group_key": g["group_key"]} for g in groups
                ]
            ]
        return updates

    def _install(self, session, proposal, event_id):
        version = session.get(NoteVersion, proposal["note_version_id"])
        if not version or not eligible_note(session, version):
            raise ValueError("Source changed before proposal installation")
        decision = proposal["decision"]
        concept = (
            session.get(Concept, proposal.get("concept_id")) if proposal.get("concept_id") else None
        )
        if decision == "NEW_CONCEPT":
            title = str(proposal.get("title") or "").strip()
            scope = proposal.get("scope")
            if not title or not isinstance(scope, dict) or not scope.get("includes"):
                raise ValueError("New Concept requires an explicit independent title and scope")
            alias = normalize_alias(title)
            registered = session.scalar(
                select(ConceptAlias).where(ConceptAlias.normalized_alias == alias)
            )
            concept = session.get(Concept, registered.concept_id) if registered else None
            if not concept:
                concept = next(
                    (
                        c
                        for c in session.scalars(select(Concept))
                        if normalize_alias(c.canonical_name) == alias
                    ),
                    None,
                )
            if concept and concept.visibility not in {"WITHDRAWN", "DRAFT"}:
                raise ValueError(
                    "Equivalent identity already registered; reclassify instead of duplicate creation"
                )
            if not concept:
                key = "auto:" + hashlib.sha256(alias.encode()).hexdigest()[:24]
                concept = session.scalar(select(Concept).where(Concept.stable_key == key))
                if not concept:
                    concept = Concept(
                        stable_key=key,
                        domain_key=proposal.get("domain_key") or "personal-learning",
                        canonical_name=title,
                        original_name=title,
                        status="DRAFT",
                        visibility="DRAFT",
                    )
                    session.add(concept)
                    session.flush()
                    session.add(
                        ConceptAlias(
                            concept_id=concept.id,
                            alias=title,
                            normalized_alias=alias,
                            alias_type="AUTO",
                            created_by="knowledge-sync",
                        )
                    )
            proposal["concept_id"] = concept.id
        if not concept:
            raise ValueError("Proposal has no target Concept")
        if decision != "NEW_CONCEPT" and concept.active_version_id != proposal.get(
            "expected_version_id"
        ):
            raise ValueError("Concept changed during classification; reclassify")
        key = str(proposal.get("module_key") or "overview").strip()
        if (
            not key
            or len(key) > 170
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in key)
        ):
            raise ValueError("Invalid semantic module key")
        module = session.scalar(
            select(ConceptModule).where(
                ConceptModule.concept_id == concept.id, ConceptModule.module_key == key
            )
        )
        if not module:
            if decision not in {"NEW_MODULE", "NEW_CONCEPT"}:
                raise ValueError("Unknown existing module")
            module = ConceptModule(
                concept_id=concept.id, module_key=key, semantic_role=_canonical_module_role(key)
            )
            session.add(module)
            session.flush()
        if not concept.active_version_id:
            base = ConceptVersion(
                concept_id=concept.id,
                version_no=1,
                title=concept.canonical_name,
                scope=dumps(proposal.get("scope") or {}),
                summary=version.thesis,
                content_sha256=digest({"draft": concept.id}),
                schema_version="concept-v3",
                prompt_version=POLICY_VERSION,
                status="DRAFT",
            )
            session.add(base)
            session.flush()
            concept.active_version_id = base.id
        contribution = session.scalar(
            select(ConceptContribution).where(
                ConceptContribution.module_id == module.id,
                ConceptContribution.atomic_note_version_id == version.id,
                ConceptContribution.contribution_role == "SUPPORT",
            )
        )
        if not contribution:
            contribution = ConceptContribution(
                module_id=module.id,
                atomic_note_version_id=version.id,
                source_role="ATOMIC_NOTE",
                source_ref=f"AtomicNote:{version.note_id}:{version.id}",
                contribution_role="SUPPORT",
                decision_status="APPROVED",
                rationale=str(proposal.get("reason") or "自动增量"),
                assigned_by="knowledge-sync",
            )
            session.add(contribution)
            session.flush()
        else:
            contribution.decision_status = (
                "APPROVED" if contribution.decision_status != "ACCEPTED" else "ACCEPTED"
            )
        selection = session.get(KnowledgeContributionSelection, contribution.id)
        if not selection:
            selection = KnowledgeContributionSelection(
                contribution_id=contribution.id, event_id=event_id
            )
            session.add(selection)
        selection.block_ids_json = dumps(proposal["selected_block_ids"])
        self._dirty(session, module.id, event_id)
        proposal["module_id"] = module.id
        return module.id

    def _retire_invalid(self, session, source_id, event_id):
        affected = set()
        rows = session.execute(
            select(ConceptContribution, NoteVersion, AtomicNote)
            .join(NoteVersion, NoteVersion.id == ConceptContribution.atomic_note_version_id)
            .join(AtomicNote, AtomicNote.id == NoteVersion.note_id)
            .where(
                AtomicNote.source_id == source_id,
                ConceptContribution.decision_status.in_(["ACCEPTED", "APPROVED"]),
            )
        ).all()
        for contribution, version, note in rows:
            if eligible_note(session, version):
                continue
            affected.add(contribution.module_id)
            contribution.decision_status = (
                "SUPERSEDED" if note.visibility == "ACTIVE" else "RETRACTED"
            )
            contribution.decided_at = utcnow()
            self._dirty(session, contribution.module_id, event_id)
        affected.update(
            session.scalars(
                select(KnowledgeModuleState.module_id).where(
                    KnowledgeModuleState.event_id == event_id, KnowledgeModuleState.dirty.is_(True)
                )
            )
        )
        return affected

    def _historic(self, session, module_id, snapshot):
        wanted = {x["note_version_id"] for x in snapshot}
        for mv in session.scalars(
            select(ConceptModuleVersion)
            .where(ConceptModuleVersion.module_id == module_id)
            .order_by(ConceptModuleVersion.version_no.desc())
        ):
            payload = loads(mv.payload_json)
            source_ids = {x.get("note_version_id") for x in payload.get("_contributions", [])}
            old_selection = {
                x.get("note_version_id"): sorted(x.get("selected_block_ids", []))
                for x in payload.get("_contributions", [])
            }
            desired_selection = {x["note_version_id"]: sorted(x["blocks"]) for x in snapshot}
            if (
                source_ids == wanted
                and wanted
                and (old_selection == desired_selection or not any(x["blocks"] for x in snapshot))
            ):
                return mv
        return None

    def _repin(self, session, concept, version, placements):
        refreshed = []
        for synthesis in session.scalars(select(Synthesis).where(Synthesis.visibility == "ACTIVE")):
            current = session.get(SynthesisVersion, synthesis.active_version_id)
            if not current:
                continue
            nodes = session.scalars(
                select(SynthesisNode)
                .where(SynthesisNode.synthesis_version_id == current.id)
                .order_by(SynthesisNode.sort_order)
            ).all()
            matches = [
                n
                for n in nodes
                if n.concept_id == concept.id
                or loads(n.source_locator_json).get("withdrawn_concept_id") == concept.id
            ]
            placement = next((p for p in placements if p.get("synthesis_id") == synthesis.id), None)
            parent = next(
                (
                    n
                    for n in nodes
                    if placement
                    and n.stable_key == placement.get("group_key")
                    and n.node_kind == "GROUP"
                ),
                None,
            )
            corpus = session.get(BackboneCorpus, current.backbone_corpus_id)
            curated = bool(corpus and loads(corpus.manifest_json).get("origin") == "CURATED")
            if not matches and (not parent or curated or concept.visibility != "ACTIVE"):
                continue
            new = SynthesisVersion(
                synthesis_id=synthesis.id,
                backbone_corpus_id=current.backbone_corpus_id,
                version_no=(
                    session.scalar(
                        select(func.max(SynthesisVersion.version_no)).where(
                            SynthesisVersion.synthesis_id == synthesis.id
                        )
                    )
                    or 0
                )
                + 1,
                title=current.title,
                summary=current.summary,
                status="PUBLISHED",
                coverage_json=current.coverage_json,
                content_sha256=digest(
                    {"parent": current.id, "concept": concept.id, "release": version.id}
                ),
            )
            session.add(new)
            session.flush()
            ids = {}
            for node in sorted(nodes, key=lambda n: (n.level, n.sort_order)):
                if node.parent_id and node.parent_id not in ids:
                    raise ValueError("Synthesis parent ordering invalid; refuse publication")
                matched = node in matches
                withdrawn = matched and concept.visibility != "ACTIVE"
                locator = loads(node.source_locator_json)
                if matched:
                    locator = (
                        {**locator, "withdrawn_concept_id": concept.id}
                        if withdrawn
                        else {k: v for k, v in locator.items() if k != "withdrawn_concept_id"}
                    )
                clone = SynthesisNode(
                    synthesis_version_id=new.id,
                    stable_key=node.stable_key,
                    parent_id=ids.get(node.parent_id),
                    title=node.title,
                    original_title=node.original_title,
                    level=node.level,
                    sort_order=node.sort_order,
                    node_kind="GROUP" if withdrawn else "CONCEPT" if matched else node.node_kind,
                    concept_id=None if withdrawn else concept.id if matched else node.concept_id,
                    concept_version_id=None
                    if withdrawn
                    else version.id
                    if matched
                    else node.concept_version_id,
                    source_locator_json=dumps(locator),
                )
                session.add(clone)
                session.flush()
                ids[node.id] = clone.id
            if not matches and parent:
                session.add(
                    SynthesisNode(
                        synthesis_version_id=new.id,
                        stable_key=f"auto-{concept.id}",
                        parent_id=ids[parent.id],
                        title=concept.canonical_name,
                        original_title=concept.canonical_name,
                        level=parent.level + 1,
                        sort_order=max([n.sort_order for n in nodes], default=-1) + 1,
                        node_kind="CONCEPT",
                        concept_id=concept.id,
                        concept_version_id=version.id,
                        source_locator_json=dumps({"origin": "AUTO"}),
                    )
                )
            references = {}
            for ref in session.scalars(
                select(SynthesisReferenceSource).where(
                    SynthesisReferenceSource.synthesis_version_id == current.id
                )
            ):
                source_version = session.get(SourceVersion, ref.source_version_id)
                source = session.get(Source, source_version.source_id) if source_version else None
                if (
                    source
                    and source.visibility == "ACTIVE"
                    and source.current_version_id == source_version.id
                ):
                    references[source_version.id] = ref.source_role
            for pin in session.scalars(
                select(ConceptReleaseModule).where(
                    ConceptReleaseModule.concept_version_id == version.id
                )
            ):
                mv = session.get(ConceptModuleVersion, pin.module_version_id)
                for item in loads(mv.payload_json).get("_contributions", []):
                    note_version = session.get(NoteVersion, item.get("note_version_id"))
                    if note_version and eligible_note(session, note_version):
                        references.setdefault(note_version.source_version_id, "SUPPORT")
            for source_version_id, role in references.items():
                session.add(
                    SynthesisReferenceSource(
                        synthesis_version_id=new.id,
                        source_version_id=source_version_id,
                        source_role=role,
                    )
                )
            session.flush()
            current_nodes = session.scalars(
                select(SynthesisNode).where(SynthesisNode.synthesis_version_id == new.id)
            ).all()
            node_keys = {n.id: n.stable_key for n in current_nodes}
            count = sum(n.node_kind == "CONCEPT" for n in current_nodes)
            coverage = loads(current.coverage_json)
            coverage.update(
                concept_nodes=count,
                published_concepts=count,
                groups=sum(n.node_kind == "GROUP" for n in current_nodes),
                withdrawn_concepts=sum(
                    bool(loads(n.source_locator_json).get("withdrawn_concept_id"))
                    for n in current_nodes
                ),
            )
            new.coverage_json = dumps(coverage)
            new.content_sha256 = digest(
                [
                    (
                        n.stable_key,
                        node_keys.get(n.parent_id),
                        n.node_kind,
                        n.concept_id,
                        n.concept_version_id,
                    )
                    for n in current_nodes
                ]
            )
            changed = session.execute(
                update(Synthesis)
                .where(Synthesis.id == synthesis.id, Synthesis.active_version_id == current.id)
                .values(active_version_id=new.id)
            )
            if changed.rowcount != 1:
                raise ValueError("Synthesis head changed; retry publication")
            refreshed.append(new.id)
        return refreshed

    def _rebuild(self, module_id, event_id, owner, proposals):
        with self.db.session() as session:
            self.fence(session, event_id, owner)
            module = session.get(ConceptModule, module_id)
            concept = session.get(Concept, module.concept_id)
            base_id = concept.active_version_id
            state = session.get(KnowledgeModuleState, module_id)
            revision = state.revision
            snapshot = self._snapshot(session, module_id)
            manifest_hash = digest(snapshot)
            historical = self._historic(session, module_id, snapshot) if snapshot else None
            historical_id = historical.id if historical else None
            if snapshot and not historical:
                job = ConceptUpdateJob(
                    concept_id=concept.id,
                    module_id=module.id,
                    atomic_note_version_id=snapshot[0]["note_version_id"],
                    contribution_id=snapshot[0]["contribution_id"],
                    operation="RECONCILE",
                    expected_concept_version_id=base_id,
                    status="RUNNING",
                    actor="knowledge-sync",
                    rationale=dumps(
                        {"event_id": event_id, "policy": POLICY_VERSION, "updates": proposals}
                    ),
                    lease_owner=f"sync:{event_id}:{owner}",
                    lease_expires_at=utcnow() + timedelta(seconds=180),
                )
                session.add(job)
                session.flush()
                job_id = job.id
            else:
                job_id = None
            spec = {
                "key": concept.stable_key,
                "stable_key": concept.stable_key,
                "title": concept.canonical_name,
                "kind": "COMPOSITE",
                "summary_hint": concept.canonical_name,
                "source_prefixes": [],
                "parent_key": "",
            }
        candidate_id = None
        if job_id:
            prepared = self.builder._prepared_from_contributions(
                spec, include_contribution_ids={x["contribution_id"] for x in snapshot}
            )
            prepared["modules"] = [
                m for m in prepared["modules"] if m["block_key"] == module.module_key
            ]
            compiled = self.builder._compile_module_update_candidate(
                prepared, concept_id=concept.id, base_version_id=base_id, job_id=job_id
            )
            candidate_id = compiled["concept_version_id"]
        with self.db.session() as session:
            self.fence(session, event_id, owner)
            state = session.get(KnowledgeModuleState, module_id)
            if (
                state.revision != revision
                or digest(self._snapshot(session, module_id)) != manifest_hash
            ):
                raise ValueError("Source manifest changed; stale candidate cannot publish")
            concept = session.get(Concept, concept.id)
            if concept.active_version_id != base_id:
                raise ValueError("Concept head changed; stale candidate cannot publish")
            base = session.get(ConceptVersion, base_id)
            pins = session.scalars(
                select(ConceptReleaseModule)
                .where(ConceptReleaseModule.concept_version_id == base_id)
                .order_by(ConceptReleaseModule.sort_order)
            ).all()
            if candidate_id:
                candidate = session.get(ConceptVersion, candidate_id)
            else:
                release_modules = [
                    (
                        p.module_id,
                        historical_id if p.module_id == module_id else p.module_version_id,
                        p.sort_order,
                    )
                    for p in pins
                    if p.module_id != module_id or historical_id
                ]
                if historical_id and not any(p.module_id == module_id for p in pins):
                    release_modules.append((module_id, historical_id, len(release_modules)))
                candidate = ConceptVersion(
                    concept_id=concept.id,
                    version_no=(
                        session.scalar(
                            select(func.max(ConceptVersion.version_no)).where(
                                ConceptVersion.concept_id == concept.id
                            )
                        )
                        or 0
                    )
                    + 1,
                    title=base.title,
                    original_title=base.original_title,
                    scope=base.scope,
                    summary=base.summary,
                    content_sha256=digest(
                        {"base": base_id, "modules": release_modules, "manifest": manifest_hash}
                    ),
                    schema_version="concept-v3",
                    prompt_version=POLICY_VERSION,
                    status="READY",
                )
                session.add(candidate)
                session.flush()
                for mid, vid, order in release_modules:
                    session.add(
                        ConceptReleaseModule(
                            concept_version_id=candidate.id,
                            module_id=mid,
                            module_version_id=vid,
                            sort_order=order,
                        )
                    )
                session.flush()
                candidate_id = candidate.id
            active_pins = session.scalars(
                select(ConceptReleaseModule).where(
                    ConceptReleaseModule.concept_version_id == candidate.id
                )
            ).all()
            active = bool(active_pins)
            texts = [
                session.get(ConceptModuleVersion, p.module_version_id).plain_text
                for p in active_pins
            ]
            candidate.summary = (
                "\n".join(t.split("\n\n")[0] for t in texts if t)[:400]
                if active
                else "暂无有效来源。"
            )
            candidate.status = "PUBLISHED" if active else "WITHDRAWN"
            changed = session.execute(
                update(Concept)
                .where(Concept.id == concept.id, Concept.active_version_id == base_id)
                .values(
                    active_version_id=candidate.id,
                    status=candidate.status,
                    visibility="ACTIVE" if active else "WITHDRAWN",
                )
            )
            if changed.rowcount != 1:
                raise ValueError("Concept conditional publication failed")
            concept.visibility = "ACTIVE" if active else "WITHDRAWN"
            concept.active_version_id = candidate.id
            for item in snapshot:
                contribution = session.get(ConceptContribution, item["contribution_id"])
                contribution.decision_status = "ACCEPTED"
                contribution.decided_at = utcnow()
            placements = [p for proposal in proposals for p in proposal.get("placements", [])]
            refreshed = self._repin(session, concept, candidate, placements)
            state.dirty = False
            state.manifest_sha256 = manifest_hash
            state.error_detail = ""
            if job_id:
                job = session.get(ConceptUpdateJob, job_id)
                job.status = "PUBLISHED"
                job.completed_at = utcnow()
                job.lease_owner = None
                job.lease_expires_at = None
                job.result_json = dumps(
                    {
                        "status": "UPDATED",
                        "concept_id": concept.id,
                        "concept_version_id": candidate.id,
                        "synthesis_versions": refreshed,
                    }
                )
            return {
                "module_id": module_id,
                "concept_id": concept.id,
                "version_id": candidate.id,
                "status": candidate.status,
                "reused_content": bool(historical_id),
                "sources": len(snapshot),
                "synthesis_versions": refreshed,
            }

    def _quarantine_review(self, module_id, event_id, owner, detail):
        with self.db.session() as session:
            self.fence(session, event_id, owner)
            for contribution in session.scalars(
                select(ConceptContribution).where(
                    ConceptContribution.module_id == module_id,
                    ConceptContribution.decision_status == "APPROVED",
                )
            ):
                contribution.decision_status = "REVIEW_REQUIRED"
            for row in session.scalars(
                select(KnowledgeSyncProposal).where(
                    KnowledgeSyncProposal.event_id == event_id,
                    KnowledgeSyncProposal.status == "APPLIED",
                )
            ):
                if loads(row.payload_json).get("module_id") == module_id:
                    row.status = "NEEDS_REVIEW"
                    row.error_detail = detail[:4000]
            module = session.get(ConceptModule, module_id)
            concept = session.get(Concept, module.concept_id)
            pin = session.scalar(
                select(ConceptReleaseModule).where(
                    ConceptReleaseModule.concept_version_id == concept.active_version_id,
                    ConceptReleaseModule.module_id == module_id,
                )
            )
            # Clear invalidation only if the old materialized sources remain current.
            inputs = (
                loads(session.get(ConceptModuleVersion, pin.module_version_id).payload_json).get(
                    "_contributions", []
                )
                if pin
                else []
            )
            valid = (
                not pin
                or bool(inputs)
                and all(
                    (v := session.get(NoteVersion, item.get("note_version_id"))) is not None
                    and eligible_note(session, v)
                    for item in inputs
                )
            )
            state = session.get(KnowledgeModuleState, module_id)
            state.dirty = not valid
            state.error_detail = detail[:4000]
            if valid:
                state.manifest_sha256 = digest(self._snapshot(session, module_id))
            for job in session.scalars(
                select(ConceptUpdateJob).where(
                    ConceptUpdateJob.module_id == module_id,
                    ConceptUpdateJob.lease_owner == f"sync:{event_id}:{owner}",
                )
            ):
                job.status = "NEEDS_REVIEW"
                job.error_code = "SEMANTIC_REVIEW_REQUIRED"
                job.error_detail = detail[:4000]
                job.completed_at = utcnow()
                job.lease_owner = None
                job.lease_expires_at = None
            return {"module_id": module_id, "reason": detail, "previous_release_available": valid}

    def _index_releases(self, releases):
        prepared = []
        with self.db.session() as session:
            blocked = blocked_concepts(session)
            for concept_id in sorted({r["concept_id"] for r in releases}):
                concept = session.get(Concept, concept_id)
                if not concept or concept.visibility != "ACTIVE" or concept_id in blocked:
                    continue
                version = session.get(ConceptVersion, concept.active_version_id)
                body = self.service.knowledge_curation._concept_body(session, concept, version)
                aliases = [
                    a.alias
                    for a in session.scalars(
                        select(ConceptAlias).where(ConceptAlias.concept_id == concept_id)
                    )
                ]
                scope = loads(version.scope)
                aliases += scope.get("aliases", []) if isinstance(scope, dict) else []
                alias_text = " ".join(sorted(set(aliases)))
                index_text = f"{version.title}\n{version.summary}\n{body}\n{alias_text}"
                content_hash = hashlib.sha256(index_text.encode()).hexdigest()
                exists = session.scalar(
                    select(ConceptEmbedding.id).where(
                        ConceptEmbedding.concept_version_id == version.id,
                        ConceptEmbedding.model_name == self.service.embedding.model_name,
                        ConceptEmbedding.content_sha256 == content_hash,
                    )
                )
                if not exists:
                    prepared.append((version.id, index_text, content_hash))
        try:
            vectors = self.service.embedding.embed([p[1] for p in prepared]) if prepared else []
            if len(vectors) != len(prepared):
                raise ValueError("Unexpected Concept vector count")
            with self.db.session() as session:
                for (version_id, _text, content_hash), vector in zip(
                    prepared, vectors, strict=True
                ):
                    exists = session.scalar(
                        select(ConceptEmbedding.id).where(
                            ConceptEmbedding.concept_version_id == version_id,
                            ConceptEmbedding.model_name == self.service.embedding.model_name,
                        )
                    )
                    if not exists:
                        session.add(
                            ConceptEmbedding(
                                concept_version_id=version_id,
                                model_name=self.service.embedding.model_name,
                                dimensions=len(vector),
                                vector_json=dumps(vector),
                                content_sha256=content_hash,
                            )
                        )
            return "READY"
        except Exception:  # noqa: BLE001 - explicit lexical fallback; source validity is still enforced.
            return "LEXICAL_FALLBACK"

    def run(self, event_id, owner):
        results, errors, review, review_modules = [], [], 0, []
        try:
            with self.db.session() as session:
                self.fence(session, event_id, owner)
                event = session.get(KnowledgeSyncEvent, event_id)
                source_id = event.source_id
                affected = self._retire_invalid(session, source_id, event_id)
                source = session.get(Source, source_id)
                notes = (
                    session.scalars(
                        select(NoteVersion)
                        .join(AtomicNote, AtomicNote.active_version_id == NoteVersion.id)
                        .where(
                            AtomicNote.source_id == source_id,
                            AtomicNote.visibility == "ACTIVE",
                            NoteVersion.source_version_id
                            == (source.current_version_id if source else ""),
                        )
                    ).all()
                    if source and source.visibility == "ACTIVE"
                    else []
                )
                note_ids = [n.id for n in notes]
            for version_id in note_ids:
                with self.db.session() as session:
                    already = session.scalar(
                        select(ConceptContribution.id).where(
                            ConceptContribution.atomic_note_version_id == version_id,
                            ConceptContribution.decision_status.in_(["ACCEPTED", "APPROVED"]),
                        )
                    )
                    previous = session.scalars(
                        select(KnowledgeSyncProposal).where(
                            KnowledgeSyncProposal.event_id == event_id,
                            KnowledgeSyncProposal.note_version_id == version_id,
                        )
                    ).all()
                    if previous and all(
                        p.status in {"APPLIED", "SKIPPED", "NEEDS_REVIEW"} for p in previous
                    ):
                        continue
                if already and not any(p.status == "RETRY_ANALYSIS" for p in previous):
                    continue
                try:
                    updates = self._classify(version_id)
                except Exception as exc:  # noqa: BLE001 - durable worker boundary records and quarantines failures.
                    updates = [
                        {"decision": "CONFLICT", "reason": str(exc), "note_version_id": version_id}
                    ]
                for index, proposal in enumerate(updates):
                    with self.db.session() as session:
                        self.fence(session, event_id, owner)
                        target_key = (
                            proposal.get("concept_id") or proposal.get("title") or str(index)
                        )
                        row = session.scalar(
                            select(KnowledgeSyncProposal).where(
                                KnowledgeSyncProposal.event_id == event_id,
                                KnowledgeSyncProposal.note_version_id == version_id,
                                KnowledgeSyncProposal.target_key == target_key,
                            )
                        )
                        if not row:
                            row = KnowledgeSyncProposal(
                                event_id=event_id,
                                note_version_id=version_id,
                                target_key=target_key,
                                decision=proposal["decision"],
                            )
                            session.add(row)
                            session.flush()
                        if row.status in {"APPLIED", "SKIPPED"}:
                            continue
                        row.payload_json = dumps(proposal)
                        if proposal["decision"] in {"NO_CHANGE", "INVALID"}:
                            row.status = "SKIPPED"
                        elif proposal["decision"] == "CONFLICT":
                            row.status = "NEEDS_REVIEW"
                            review += 1
                        else:
                            try:
                                with session.begin_nested():
                                    affected.add(self._install(session, proposal, event_id))
                                row.status = "APPLIED"
                                row.payload_json = dumps(proposal)
                            except ValueError as exc:
                                row.status = "NEEDS_REVIEW"
                                row.error_detail = str(exc)
                                review += 1
                with self.db.session() as session:
                    for previous_row in session.scalars(
                        select(KnowledgeSyncProposal).where(
                            KnowledgeSyncProposal.event_id == event_id,
                            KnowledgeSyncProposal.note_version_id == version_id,
                            KnowledgeSyncProposal.status == "RETRY_ANALYSIS",
                        )
                    ):
                        previous_row.status = "REANALYZED"
            with self.db.session() as session:
                all_proposals = [
                    loads(p.payload_json)
                    for p in session.scalars(
                        select(KnowledgeSyncProposal).where(
                            KnowledgeSyncProposal.event_id == event_id,
                            KnowledgeSyncProposal.status == "APPLIED",
                        )
                    )
                ]
                affected.update(p.get("module_id") for p in all_proposals if p.get("module_id"))
            for module_id in sorted(affected):
                with self.db.session() as session:
                    state = session.get(KnowledgeModuleState, module_id)
                    if not state or not state.dirty:
                        continue
                try:
                    results.append(
                        self._rebuild(
                            module_id,
                            event_id,
                            owner,
                            [p for p in all_proposals if p.get("module_id") == module_id],
                        )
                    )
                except ModuleReviewRequired as exc:
                    review_modules.append(
                        self._quarantine_review(module_id, event_id, owner, str(exc))
                    )
                except Exception as exc:  # noqa: BLE001 - durable worker boundary records and quarantines failures.
                    errors.append({"module_id": module_id, "error": str(exc)})
                    with self.db.session() as session:
                        state = session.get(KnowledgeModuleState, module_id)
                        if state:
                            state.error_detail = str(exc)[:4000]
                        for job in session.scalars(
                            select(ConceptUpdateJob).where(
                                ConceptUpdateJob.module_id == module_id,
                                ConceptUpdateJob.lease_owner == f"sync:{event_id}:{owner}",
                            )
                        ):
                            job.status = "FAILED"
                            job.error_code = "SYNC_BUILD_FAILED"
                            job.error_detail = str(exc)[:4000]
                            job.lease_owner = None
                            job.lease_expires_at = None
            self.service.knowledge_curation.ensure_concept_index()
            index_mode = self._index_releases(results)
            with self.db.session() as session:
                review = (
                    session.scalar(
                        select(func.count(KnowledgeSyncProposal.id)).where(
                            KnowledgeSyncProposal.event_id == event_id,
                            KnowledgeSyncProposal.status == "NEEDS_REVIEW",
                        )
                    )
                    or 0
                )
                self.fence(session, event_id, owner)
                event = session.get(KnowledgeSyncEvent, event_id)
                event.status = (
                    "COMPLETED_WITH_ERRORS"
                    if errors
                    else "COMPLETED_WITH_REVIEW"
                    if review or review_modules
                    else "COMPLETED"
                )
                event.result_json = dumps(
                    {
                        "processed_notes": len(note_ids),
                        "releases": results,
                        "needs_review": review,
                        "review_modules": review_modules,
                        "errors": errors,
                        "index_mode": index_mode,
                    }
                )
                event.error_detail = dumps(errors) if errors else ""
                event.lease_owner = None
                event.lease_expires_at = None
            return {"event_id": event_id, "releases": results, "errors": errors}
        except Exception as exc:  # noqa: BLE001 - durable worker boundary records and quarantines failures.
            with self.db.session() as session:
                event = session.get(KnowledgeSyncEvent, event_id)
                if event and event.lease_owner == owner:
                    event.status = "FAILED"
                    event.error_detail = str(exc)[:8000]
                    event.lease_owner = None
                    event.lease_expires_at = None
            return {"event_id": event_id, "error": str(exc)}
