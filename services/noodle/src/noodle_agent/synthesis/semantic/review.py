from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from ...composition import Component
from ...db import (
    ConceptModuleJudgeRecord,
    ConceptModuleVersion,
    ConceptReleaseModule,
    ConceptUpdateJob,
)
from ..domain import (
    ConceptModuleJudgeResult,
    SemanticConceptDraft,
    SemanticConceptModuleDraft,
)
from ..machine_learning_blueprint import ConceptSpec
from .common import (
    JUDGE_PROMPT_VERSION,
    REPAIR_PROMPT_VERSION,
    _json,
    _loads,
    _sha256_text,
)


class ReviewModule(Component):
    @staticmethod
    def _release_contains_contribution(
        session,
        concept_version_id: str | None,
        module_id: str,
        contribution_id: str,
    ) -> bool:
        if not concept_version_id:
            return False
        placement = session.scalar(
            select(ConceptReleaseModule).where(
                ConceptReleaseModule.concept_version_id == concept_version_id,
                ConceptReleaseModule.module_id == module_id,
            )
        )
        if placement is None:
            return False
        module_version = session.get(ConceptModuleVersion, placement.module_version_id)
        if module_version is None:
            return False
        payload = _loads(module_version.payload_json, {})
        return any(
            isinstance(item, dict) and item.get("contribution_id") == contribution_id
            for item in payload.get("_contributions", [])
        )

    @staticmethod
    def _validate_update_draft(
        draft: SemanticConceptDraft,
        module_seed: dict[str, Any],
    ) -> None:
        if len(draft.modules) != 1 or draft.modules[0].block_key != module_seed["block_key"]:
            raise ValueError("Concept candidate must contain exactly the requested module")
        allowed = {
            str(source.get("contribution_id") or "")
            for source in module_seed["sources"]
            if source.get("contribution_id")
        }
        allowed_note_blocks: set[str] = set()
        allowed_spans: dict[str, str] = {}
        allowed_assets: dict[str, str] = {}
        for source in module_seed["sources"]:
            for source_block in source.get("blocks", []):
                note_block_id = str(source_block.get("note_block_id") or "")
                if not note_block_id:
                    continue
                allowed_note_blocks.add(note_block_id)
                for evidence in source_block.get("evidence", []):
                    span_id = str(evidence.get("evidence_span_id") or "")
                    if span_id:
                        allowed_spans[span_id] = note_block_id
                for asset_id in source_block.get("asset_ids", []):
                    allowed_assets[str(asset_id)] = note_block_id
        blocks = draft.modules[0].payload.blocks
        if not blocks:
            raise ValueError("Concept candidate module has no content blocks")
        for block in blocks:
            evidence = set(block.evidence_refs)
            if not evidence:
                raise ValueError(f"Concept candidate block has no evidence_refs: {block.block_key}")
            if not evidence <= allowed:
                raise ValueError(
                    f"Concept candidate block has unknown evidence_refs: {block.block_key}"
                )
            note_block_ids = set(block.note_block_ids)
            span_ids = set(block.evidence_span_ids)
            if not note_block_ids or not note_block_ids <= allowed_note_blocks:
                raise ValueError(
                    f"Concept candidate block has invalid note_block_ids: {block.block_key}"
                )
            if not span_ids or not span_ids <= set(allowed_spans):
                raise ValueError(
                    f"Concept candidate block has invalid evidence_span_ids: {block.block_key}"
                )
            if any(allowed_spans[span_id] not in note_block_ids for span_id in span_ids):
                raise ValueError(
                    f"Concept candidate evidence does not belong to selected NoteBlocks: {block.block_key}"
                )
            if not set(block.asset_ids) <= set(allowed_assets):
                raise ValueError(
                    f"Concept candidate block has unknown asset_ids: {block.block_key}"
                )
            if any(allowed_assets[asset_id] not in note_block_ids for asset_id in block.asset_ids):
                raise ValueError(
                    f"Concept candidate asset does not belong to selected NoteBlocks: {block.block_key}"
                )

    def _update_context(self, job_id: str) -> dict[str, Any]:
        with self.owner.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            pin = session.scalar(
                select(ConceptReleaseModule).where(
                    ConceptReleaseModule.concept_version_id == job.expected_concept_version_id,
                    ConceptReleaseModule.module_id == job.module_id,
                )
            )
            mv = session.get(ConceptModuleVersion, pin.module_version_id) if pin else None
            return {
                "request": job.rationale,
                "previous_module": mv.plain_text[:12000] if mv else "",
                "rule": "保留仍由有效来源支持的重要知识；移除失效来源独有内容；检查适用条件与冲突。",
            }

    def _judge_module_candidate(
        self,
        *,
        spec: ConceptSpec,
        module_seed: dict[str, Any],
        module_draft: SemanticConceptModuleDraft,
        job_id: str,
        attempt_no: int,
    ) -> ConceptModuleJudgeResult:
        """Review only the changed module against the evidence visible to its writer."""

        allowed_evidence = {
            str(source.get("contribution_id") or "")
            for source in module_seed["sources"]
            if source.get("contribution_id")
        }
        block_keys = {item.block_key for item in module_draft.payload.blocks}
        span_owners = {
            e["evidence_span_id"]: source["contribution_id"]
            for source in module_seed["sources"]
            for b in source.get("blocks", [])
            for e in b.get("evidence", [])
        }
        judge_input = {
            "concept": {
                "canonical_key": self.owner.blueprint.stable_concept_key(spec),
                "title": spec["title"],
            },
            "module": module_draft.model_dump(),
            "allowed_block_keys": sorted(block_keys),
            "allowed_contribution_ids": sorted(allowed_evidence),
            "allowed_evidence_span_ids": sorted(span_owners),
            "update_context": self.owner._update_context(job_id),
            "evidence": [
                {
                    "contribution_id": source.get("contribution_id", ""),
                    "pages": source.get("pages", []),
                    "text": self.owner._source_text(source, max_chars=3_000),
                    "blocks": source.get("blocks", []),
                }
                for source in module_seed["sources"]
            ],
        }
        input_hash = _sha256_text(_json(judge_input))
        with self.owner.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            if job is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            job.status = "JUDGING" if attempt_no == 1 else "REJUDGING"

        raw = self.owner.agent.structured_json(
            system=(
                "你是独立的 Concept Module 质量审校员，不参与正文撰写。只根据给出的 evidence 审核候选 Module。"
                "重点检查：每个事实是否被引用证据支持；术语是否准确；公式和符号是否正确；是否把平移等变性误写为"
                "平移不变性；不同来源冲突是否显式保留；是否重复、冗长或难读。不得补充外部知识来替候选辩护。"
                "输出严格 JSON：{decision,summary,issues,coverage}。decision 只能为 PASS、REPAIR、REJECT。"
                "issues 每项为 {issue_id,severity,category,block_key,claim,message,evidence_refs,suggested_action}；"
                "severity 只能为 BLOCKER、MAJOR、MINOR；category 只能为 EVIDENCE_GAP、FACTUAL_ERROR、"
                "TERMINOLOGY、FORMULA、SOURCE_CONFLICT、READABILITY、DUPLICATION。"
                "BLOCKER 为严重事实错配、不可支持或隐去冲突；MAJOR 为错误公式、错误术语、实质遗漏或无效引用；MINOR 仅为措辞、轻微重复和排版建议。"
                "只有 MINOR 或无问题时用 PASS 并保留建议；有 MAJOR 用 REPAIR；有不可修复 BLOCKER 用 REJECT。"
                "审查 note_block_ids 应对照 evidence.blocks.note_block_id，不应对照 contribution IDs。"
                "引用粒度为内容 Block：evidence_refs 是本块来源的并集，每项事实至少有一条所选来源支持，每条引用至少支持本块一项事实；不要求每条来源支持本块每一句话。"
                "来源明确支持的等价公式与计数重表达不算事实错误；存在单位歧义时要求说明口径。"
                "已有模块在当前有效来源支持下保留事实是必要行为，不将与 previous_module 重复本身判为错误；不能引入外部教材知识。"
            ),
            user=judge_input,
            operation="semantic_synthesis.judge_concept_module_v1",
            prompt_version=JUDGE_PROMPT_VERSION,
            use_pro_model=False,
        )

        def validate_review(value):
            if isinstance(value, dict):
                for issue in value.get("issues", []):
                    refs = issue.get("evidence_refs", [])
                    spans = list(issue.get("evidence_span_ids", []))
                    spans.extend(ref for ref in refs if ref in span_owners)
                    issue["evidence_span_ids"] = list(dict.fromkeys(spans))
                    issue["evidence_refs"] = list(
                        dict.fromkeys(span_owners.get(ref, ref) for ref in refs)
                    )
            checked = ConceptModuleJudgeResult.model_validate(value)
            for issue in checked.issues:
                if issue.block_key and issue.block_key not in block_keys:
                    raise ValueError(f"Judge referenced unknown block_key: {issue.block_key}")
                if not set(issue.evidence_refs) <= allowed_evidence or not set(
                    issue.evidence_span_ids
                ) <= set(span_owners):
                    raise ValueError(f"Judge referenced unknown evidence: {issue.issue_id}")
            return checked

        try:
            try:
                result = validate_review(raw)
            except (ValidationError, ValueError) as first_error:
                raw = self.owner.agent.structured_json(
                    system="修正审校 JSON 的引用或枚举结构，不改变质量标准。block_key 仅使用 allowed_block_keys，evidence_refs 用 contribution IDs，evidence_span_ids 用 span IDs。decision/严重级别必须与问题一致。输出合法审校 JSON。",
                    user={**judge_input, "previous_review": raw, "schema_error": str(first_error)},
                    operation="semantic_synthesis.judge_concept_module_schema_repair",
                    prompt_version=JUDGE_PROMPT_VERSION,
                    use_pro_model=False,
                )
                result = validate_review(raw)
        except (ValidationError, ValueError) as exc:
            result = ConceptModuleJudgeResult(
                decision="REJECT",
                summary="Judge 输出无法通过结构化校验，按 fail-closed 策略拒绝发布。",
                issues=[
                    {
                        "issue_id": "judge-output-invalid",
                        "severity": "BLOCKER",
                        "category": "FACTUAL_ERROR",
                        "message": str(exc)[:2_000],
                        "suggested_action": "修复 Judge 输出或重试。",
                    }
                ],
                coverage={"validated": False},
            )
        output_json = _json(result.model_dump())
        with self.owner.db.session() as session:
            session.add(
                ConceptModuleJudgeRecord(
                    concept_update_job_id=job_id,
                    attempt_no=attempt_no,
                    judge_model=self.owner.agent.name,
                    prompt_version=JUDGE_PROMPT_VERSION,
                    input_sha256=input_hash,
                    output_sha256=_sha256_text(output_json),
                    decision=result.decision,
                    summary=result.summary,
                    issues_json=_json([item.model_dump() for item in result.issues]),
                )
            )
        return result

    def _repair_module_candidate(
        self,
        *,
        spec: ConceptSpec,
        module_seed: dict[str, Any],
        module_draft: SemanticConceptModuleDraft,
        judge: ConceptModuleJudgeResult,
        job_id: str,
    ) -> SemanticConceptModuleDraft:
        """Repair one changed module once; never regenerate the complete Concept."""

        with self.owner.db.session() as session:
            job = session.get(ConceptUpdateJob, job_id)
            if job is None:
                raise ValueError(f"Unknown Concept update job: {job_id}")
            job.status = "REPAIRING"
        raw = self.owner.agent.structured_json(
            system=(
                "你是 Concept Module 局部修复器。只修改 Judge 指出的 Block，保持 module block_key 不变，"
                "不得新增输入证据之外的事实。输出严格 JSON：{module:{block_key,title,render_type,semantic_role,"
                "payload:{blocks:[...]}}}。每个内容 Block 必须保留合法 evidence_refs。"
            ),
            user={
                "concept": {
                    "canonical_key": self.owner.blueprint.stable_concept_key(spec),
                    "title": spec["title"],
                },
                "module": module_draft.model_dump(),
                "issues": [item.model_dump() for item in judge.issues],
                "evidence": [
                    {
                        "contribution_id": source.get("contribution_id", ""),
                        "pages": source.get("pages", []),
                        "text": self.owner._source_text(source, max_chars=3_000),
                        "blocks": source.get("blocks", []),
                    }
                    for source in module_seed["sources"]
                ],
            },
            operation="semantic_synthesis.repair_concept_module_v1",
            prompt_version=REPAIR_PROMPT_VERSION,
            use_pro_model=True,
        )
        candidate = raw.get("module", raw) if isinstance(raw, dict) else raw
        try:
            repaired = SemanticConceptModuleDraft.model_validate(candidate)
        except ValidationError as exc:
            candidate = self.owner._repair_module_schema(candidate, module_seed, str(exc))
            repaired = SemanticConceptModuleDraft.model_validate(candidate)
        repaired.block_key = module_draft.block_key
        repaired.semantic_role = module_draft.semantic_role
        return repaired

    def _repair_module_schema(self, candidate, module_seed, detail):
        raw = self.owner.agent.structured_json(
            system="仅纠正候选模块 JSON 的结构与引用字段，不增补事实，不改变模块 key。evidence_refs 只能使用 contribution_id；note_block_ids 只能使用对应 note_block_id；evidence_span_ids 必须属于所选 note_block；asset_ids 必须属于所选块，ASSET 必须包含合法 asset_ids。删除没有真实支持的内容，不能编造 ID。输出 {module:完整模块}。",
            user={
                "module": candidate,
                "validation_error": detail,
                "sources": module_seed["sources"],
            },
            operation="semantic_synthesis.module_schema_repair",
            prompt_version="module-schema-repair-v1",
            use_pro_model=False,
        )
        return raw.get("module", raw) if isinstance(raw, dict) else raw

    def _align_declared_provenance(self, draft, module_seed):
        # Follow declared, real span/asset IDs to their authoritative parents.
        # Unknown IDs remain untouched and fail strict validation.
        spans, assets, blocks = {}, {}, {}
        for source in module_seed["sources"]:
            for block in source.get("blocks", []):
                bid = block.get("note_block_id")
                if not bid:
                    continue
                blocks[bid] = source.get("contribution_id")
                for evidence in block.get("evidence", []):
                    spans[evidence.get("evidence_span_id")] = bid
                for asset in block.get("asset_ids", []):
                    assets[asset] = bid
        for block in draft.modules[0].payload.blocks:
            if (
                block.evidence_span_ids
                and set(block.evidence_span_ids) <= set(spans)
                and set(block.asset_ids) <= set(assets)
                and set(block.note_block_ids) <= set(blocks)
                and set(block.evidence_refs) <= set(blocks.values())
            ):
                parents = {spans[sid] for sid in block.evidence_span_ids} | {
                    assets[aid] for aid in block.asset_ids
                }
                block.note_block_ids = sorted(parents)
                block.evidence_refs = sorted({blocks[bid] for bid in parents})

    def _validate_or_repair_update_draft(self, draft, module_seed):
        self.owner._align_declared_provenance(draft, module_seed)
        try:
            self.owner._validate_update_draft(draft, module_seed)
        except ValueError as exc:
            value = self.owner._repair_module_schema(
                draft.modules[0].model_dump(), module_seed, str(exc)
            )
            draft.modules[0] = SemanticConceptModuleDraft.model_validate(value)
            self.owner._align_declared_provenance(draft, module_seed)
            self.owner._validate_update_draft(draft, module_seed)
