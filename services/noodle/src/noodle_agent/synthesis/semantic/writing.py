from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from ...composition import Component
from ..domain import (
    ConceptContentBlock,
    ConceptModulePayload,
    SemanticConceptDraft,
    SemanticConceptModuleDraft,
)
from ..machine_learning_blueprint import ConceptSpec
from .common import (
    PROMPT_VERSION,
)


class WritingModule(Component):
    @staticmethod
    def _fallback_module(seed: dict[str, Any]) -> SemanticConceptModuleDraft:
        source_chunks: list[str] = []
        for source in seed["sources"]:
            text = source["thesis"].strip() or source["title"]
            if text and text not in source_chunks:
                source_chunks.append(text)
            for block in source["blocks"][:2]:
                value = block["text"].strip()
                if value and value not in source_chunks:
                    source_chunks.append(value)
        markdown = "\n\n".join(source_chunks)[:10_000]
        if not markdown:
            markdown = f"{seed['title']}的教材来源已建立，等待进一步综合。"
        return SemanticConceptModuleDraft(
            block_key=seed["block_key"],
            title=seed["title"],
            render_type="PROSE",
            semantic_role=seed["semantic_role"],
            payload=ConceptModulePayload(
                blocks=[
                    {
                        "block_key": f"{seed['block_key']}-content-1",
                        "type": "PROSE",
                        "markdown": markdown,
                        "evidence_refs": sorted(
                            {
                                str(source.get("contribution_id") or "")
                                for source in seed["sources"]
                                if source.get("contribution_id")
                            }
                        ),
                    }
                ]
            ),
        )

    @staticmethod
    def _v3_module_candidate(raw: dict[str, Any], seed: dict[str, Any]) -> dict[str, Any]:
        """Normalize provider variants and bind every content block to valid evidence."""

        candidate = dict(raw)
        module_key = seed["block_key"]
        payload = dict(candidate.get("payload") or {})
        raw_blocks = payload.get("blocks") or candidate.pop("content_blocks", None)
        if not isinstance(raw_blocks, list) or not raw_blocks:
            legacy_type = str(candidate.get("render_type") or "PROSE").upper()
            raw_blocks = [
                {
                    "block_key": f"{module_key}-content-1",
                    "type": legacy_type,
                    **{
                        key: payload.get(key)
                        for key in (
                            "markdown",
                            "items",
                            "table_headers",
                            "table_rows",
                            "latex",
                            "symbols",
                            "steps",
                            "code",
                            "language",
                            "asset_ids",
                            "concept_key",
                        )
                        if payload.get(key)
                    },
                }
            ]
        allowed_evidence = {
            str(source.get("contribution_id") or "")
            for source in seed["sources"]
            if source.get("contribution_id")
        }
        aliases = {"PARAGRAPH": "PROSE", "FIGURE": "ASSET", "EXERCISE": "EXAMPLE"}
        blocks: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for index, value in enumerate(raw_blocks, 1):
            if not isinstance(value, dict):
                continue
            item = dict(value)
            raw_type = str(item.get("type") or item.pop("block_type", "PROSE")).upper()
            item["type"] = aliases.get(raw_type, raw_type)
            if item["type"] not in {
                "PROSE",
                "LIST",
                "TABLE",
                "FORMULA",
                "PROCEDURE",
                "EXAMPLE",
                "CODE",
                "ASSET",
                "CALLOUT",
                "CONCEPT_REF",
            }:
                item["type"] = "PROSE"
            key = str(item.get("block_key") or f"{module_key}-content-{index}")
            if key in seen_keys:
                key = f"{key}-{index}"
            seen_keys.add(key)
            item["block_key"] = key
            requested_evidence = item.get("evidence_refs")
            if isinstance(requested_evidence, list):
                evidence = [str(ref) for ref in requested_evidence if str(ref) in allowed_evidence]
            else:
                evidence = []
            item["evidence_refs"] = evidence
            for text_key in ("markdown", "latex", "code", "language", "concept_key"):
                if text_key in item and not isinstance(item[text_key], str):
                    item[text_key] = str(item[text_key] or "")
            for list_key in (
                "items",
                "steps",
                "asset_ids",
                "note_block_ids",
                "evidence_span_ids",
            ):
                value_list = item.get(list_key)
                if value_list is not None:
                    item[list_key] = (
                        [str(entry) for entry in value_list] if isinstance(value_list, list) else []
                    )
            headers = item.get("table_headers")
            if headers is not None:
                item["table_headers"] = (
                    [str(entry) for entry in headers] if isinstance(headers, list) else []
                )
            rows = item.get("table_rows")
            if rows is not None:
                item["table_rows"] = (
                    [[str(cell) for cell in row] for row in rows if isinstance(row, list)]
                    if isinstance(rows, list)
                    else []
                )
            normalized_symbols: list[dict[str, str]] = []
            raw_symbols = item.get("symbols")
            for symbol in raw_symbols if isinstance(raw_symbols, list) else []:
                if isinstance(symbol, dict):
                    name = str(symbol.get("symbol") or symbol.get("name") or "").strip()
                    meaning = str(symbol.get("meaning") or symbol.get("description") or "").strip()
                else:
                    raw_symbol = str(symbol).strip()
                    parts = re.split(r"\s*[:：]\s*", raw_symbol, maxsplit=1)
                    name = parts[0].strip()
                    meaning = parts[1].strip() if len(parts) > 1 else ""
                if name:
                    normalized_symbols.append({"symbol": name, "meaning": meaning})
            item["symbols"] = normalized_symbols
            if item["type"] == "ASSET" and not item.get("asset_ids"):
                if str(item.get("markdown") or "").strip():
                    item["type"] = "PROSE"
                else:
                    continue
            conflict = item.get("conflict")
            if isinstance(conflict, dict):
                conflict["status"] = str(conflict.get("status") or "UNRESOLVED").upper()
                if conflict["status"] not in {"UNRESOLVED", "RESOLVED"}:
                    conflict["status"] = "UNRESOLVED"
                positions = [
                    position
                    for position in conflict.get("positions", [])
                    if isinstance(position, dict)
                    and str(position.get("evidence_ref") or "") in allowed_evidence
                    and str(position.get("claim") or "").strip()
                ]
                if len(positions) < 2:
                    item.pop("conflict", None)
                else:
                    conflict["positions"] = positions
            try:
                blocks.append(ConceptContentBlock.model_validate(item).model_dump())
            except ValidationError:
                markdown = str(item.get("markdown") or "").strip()
                if markdown:
                    blocks.append(
                        ConceptContentBlock(
                            block_key=key,
                            type="PROSE",
                            markdown=markdown,
                            evidence_refs=item["evidence_refs"],
                        ).model_dump()
                    )
        payload["blocks"] = blocks
        candidate["payload"] = payload
        candidate["render_type"] = "PROSE"
        return candidate

    def _normalize_draft(
        self, payload: dict[str, Any], spec: ConceptSpec, modules: list[dict[str, Any]]
    ) -> tuple[SemanticConceptDraft, bool, str]:
        parsed: dict[str, SemanticConceptModuleDraft] = {}
        errors: list[str] = []
        expected_by_key = {item["block_key"]: item for item in modules}
        for raw in payload.get("modules", []) if isinstance(payload, dict) else []:
            if not isinstance(raw, dict):
                continue
            block_key = str(raw.get("block_key") or "")
            # Providers sometimes helpfully split a requested module into
            # smaller book-like subsections. Concept V3 intentionally owns the
            # module boundary, so extra keys are ignored instead of making the
            # otherwise valid response fall back to source excerpts.
            if block_key not in expected_by_key:
                continue
            candidate = self.owner._v3_module_candidate(raw, expected_by_key[block_key])
            if candidate.get("semantic_role") not in {
                "OVERVIEW",
                "DEFINITION",
                "INTUITION",
                "MECHANISM",
                "PROPERTIES",
                "DERIVATION",
                "COMPARISON",
                "APPLICATION",
                "EXAMPLE",
                "LIMITATION",
                "MISCONCEPTION",
                "EXERCISE",
            }:
                candidate["semantic_role"] = expected_by_key[block_key]["semantic_role"]
            try:
                item = SemanticConceptModuleDraft.model_validate(candidate)
            except ValidationError as exc:
                errors.append(str(exc))
                continue
            parsed[item.block_key] = item
        expected = set(expected_by_key)
        normalized = [
            parsed.get(item["block_key"]) or self.owner._fallback_module(item) for item in modules
        ]
        used_fallback = set(parsed) != expected or bool(errors)
        title = str(payload.get("title_zh") or spec["title"]).strip()
        summary = str(payload.get("summary") or spec["summary_hint"]).strip()
        raw_scope = payload.get("scope") if isinstance(payload, dict) else None
        raw_scope = raw_scope if isinstance(raw_scope, dict) else {}
        scope = {
            key: [str(item) for item in value] if isinstance(value, list) else []
            for key, value in {
                "includes": raw_scope.get("includes", [spec["summary_hint"]]),
                "excludes": raw_scope.get("excludes", ["特定来源的目录、前言、索引与重复说明"]),
                "prerequisites": raw_scope.get("prerequisites", []),
                "aliases": raw_scope.get("aliases", []),
            }.items()
        }
        draft = SemanticConceptDraft(
            title_zh=title,
            summary=summary,
            scope=scope,
            modules=normalized,
        )
        detail = "; ".join(errors[:2])
        if set(parsed) != expected:
            detail = (
                f"module keys expected={sorted(expected)}, actual={sorted(parsed)}; {detail}"
            ).strip("; ")
        return draft, used_fallback, detail

    def _write_draft(
        self,
        spec: ConceptSpec,
        modules: list[dict[str, Any]],
        update_context: dict[str, Any] | None = None,
    ) -> tuple[SemanticConceptDraft, bool, str]:
        prompt_modules = []
        per_module_budget = max(3_000, 32_000 // max(1, len(modules)))
        for module in modules:
            sources = []
            source_budget = max(
                500,
                per_module_budget // max(1, len(module["sources"])),
            )
            for source in module["sources"]:
                text = self.owner._source_text(source, max_chars=min(4_000, source_budget))
                if not text:
                    continue
                sources.append(
                    {
                        "contribution_id": source.get("contribution_id", ""),
                        "note_version_id": source["note_version_id"],
                        "section": source.get("source_ref")
                        or f"{source.get('section_number', '')} {source.get('section_title', '')}",
                        "pages": source.get("pages", []),
                        "text": text,
                        "blocks": source.get("blocks", []),
                    }
                )
            prompt_modules.append(
                {
                    "block_key": module["block_key"],
                    "title": module["title"],
                    "semantic_role": module["semantic_role"],
                    "sources": sources,
                }
            )
        payload = self.owner.agent.structured_json(
            system=(
                "你是长期知识库的知识编辑。请把多个来源笔记综合为一个跨教材、跨 Synthesis 复用的 Concept。"
                "Concept 不是原书章节复述；删除前言、目录口吻、学习目标和重复内容。"
                "输入仅包含当前有效来源。根据 update_context 的目标与保留要求更新；旧正文只用于对照，不得复用失效来源独有事实。"
                "必须进行去重、互补与冲突保留；重要旧知识在仍有有效支持时保留。数学公式保留条件，不能从任意有界函数推导无条件逼近。"
                "习题是 Concept 的有效 Block：保留题意、条件、问题与必要配图，不得省略；"
                "除非来源本身含解答，否则不要擅自补写答案。"
                "必须保留输入给出的全部 block_key，每个 key 恰好一次，不得新增或改名。"
                "同一 Block 内合并重复事实，保留必要定义、机制、公式、例子和适用边界；不得补写来源不支持的事实。"
                "输出严格 JSON：{title_zh,summary,scope:{includes:[],excludes:[],prerequisites:[],aliases:[]},"
                "modules:[{block_key,title,semantic_role,payload:{blocks:[contentBlock]}}]}。"
                "contentBlock={block_key,type,markdown,items,table_headers,table_rows,latex,symbols,steps,code,language,"
                "asset_ids,concept_key,evidence_refs,note_block_ids,evidence_span_ids,conflict}；type 只能是 PROSE/LIST/TABLE/FORMULA/PROCEDURE/"
                "EXAMPLE/CODE/CALLOUT/ASSET/CONCEPT_REF。每个 contentBlock 必须给出唯一 block_key，并在 evidence_refs"
                "中只使用输入提供的 contribution_id；note_block_ids 和 evidence_span_ids 必须精确引用输入 blocks 中"
                "真正支持本 Block 的 ID，不得笼统引用整篇笔记。asset_ids 只选择对理解本 Block 必要且由所选 NoteBlock"
                "提供的图片，装饰图不得选择。PROSE/CALLOUT/EXAMPLE 用 markdown；LIST 用 items；TABLE 用"
                "表头和行；FORMULA 的 latex 不含 $$ 定界符，并用 markdown 解释公式、symbols 解释主要符号；"
                "PROCEDURE 用 steps。一个 Module 可以有多个不同类型 contentBlock，使正文、公式、表格和例子自然穿插。"
                "每个 Module 最多输出 6 个 contentBlock；合并相邻内容，不重复结论。每块只选择最必要的支持 ID（通常各不超过8个），不要复制所有来源ID。"
                "若权威来源存在不能可靠消解的矛盾，在对应 Block 写 conflict={status:'UNRESOLVED',positions:["
                "{evidence_ref,claim},...],resolution:''}，不得静默选边。"
                "写作上使用简洁中文，专业名词首次出现保留英文括注；不用学习目标、本节目标和机械小结；"
                "不强求所有 Concept 具有同一栏目，也不生成空内容。Markdown 标题从三级以下开始，列表和表格之间留空行。"
            ),
            user={
                "concept": {
                    "canonical_key": self.owner.blueprint.stable_concept_key(spec),
                    "title": spec["title"],
                    "kind": spec["kind"],
                    "summary_hint": spec["summary_hint"],
                },
                "required_modules": prompt_modules,
                "update_context": update_context or {},
            },
            operation="semantic_synthesis.write_concept_v3",
            prompt_version=PROMPT_VERSION,
            use_pro_model=spec["kind"] == "COMPOSITE",
        )
        return self.owner._normalize_draft(payload, spec, modules)
