from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import UTC, datetime
from importlib.resources import files
from time import perf_counter
from typing import Any

import httpx
from openai import OpenAI

from ..config import Settings
from ..domain import (
    Answer,
    DocumentProfile,
    GenerationBundle,
    KnowledgeConcept,
    KnowledgeConceptCandidate,
    KnowledgeMap,
    KnowledgePlan,
    KnowledgeWindow,
    NoteBlockDraft,
    NoteDraft,
    NoteEvaluation,
    NoteSetPlan,
    NoteSetReview,
    NoteSpec,
    ParsedBlock,
    ParsedDocument,
    SearchHit,
    SourceNoteOutlineNodeSpec,
    SourceNoteOutlinePlan,
    SourceNotePlanningBundle,
    SourceReleaseReview,
    TopicSegment,
)
from ..quality import (
    deduplicate_asset_placements,
    ensure_required_visual_blocks,
    normalize_note_blocks,
    note_content_sha256,
    public_visual_context,
)
from ..telemetry import TelemetryRecorder, elapsed_ms


def _slug(value: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "-", value.lower(), flags=re.UNICODE).strip("-")
    return normalized[:120] or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _sections(document: ParsedDocument) -> list[tuple[str, list[ParsedBlock]]]:
    content_blocks = [
        block
        for block in document.blocks
        if block.block_type not in {"HEADER", "FOOTER", "PAGE_NUMBER"}
    ]
    headings = [
        block for block in content_blocks if block.block_type in {"TITLE", "HEADING"}
    ]
    # Slide decks often expose one level-2 heading per page. Those headings
    # belong in the persisted outline, but treating every slide as a chapter
    # creates dozens of LLM calls and mechanically couples notes to pages.
    if document.page_count >= 20 and len(headings) / document.page_count >= 0.6:
        return [(document.title, content_blocks)]

    result: list[tuple[str, list[ParsedBlock]]] = []
    current_title = document.title
    current: list[ParsedBlock] = []
    for block in content_blocks:
        if block.block_type in {"TITLE", "HEADING"} and block.text:
            if current:
                result.append((current_title, current))
            current_title = block.text
            current = [block]
        else:
            current.append(block)
    if current:
        result.append((current_title, current))
    return result or [(document.title, document.blocks)]


def _prompt(name: str) -> str:
    return files("noodle_agent").joinpath("prompts", name).read_text(encoding="utf-8")


def _canonical_tags(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = " ".join(str(value).strip().split())
        key = re.sub(r"[^\w\u4e00-\u9fff]+", "", tag.casefold(), flags=re.UNICODE)
        if not tag or not key or key in seen:
            continue
        seen.add(key)
        result.append(tag)
    return result


def _loads_structured_json(content: str) -> dict[str, Any]:
    r"""Parse structured LLM output while tolerating unescaped LaTeX slashes.

    Some OpenAI-compatible providers occasionally return JSON strings containing
    LaTeX such as ``\alpha`` or ``\(`` with a single backslash. Those sequences
    are invalid JSON even though the surrounding object is otherwise usable.
    Only backslashes that are not already valid JSON escapes are doubled here;
    syntax errors with any other cause still fail loudly.
    """

    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1]).strip()
    try:
        result = json.loads(stripped)
    except json.JSONDecodeError:
        # Keep actual JSON escapes, but do not mistake LaTeX commands such as
        # ``\beta``, ``\frac`` or ``\underbrace`` for JSON's b/f/u escapes.
        # A unicode escape is valid only when ``u`` is followed by four hex
        # digits; every other lone slash is source notation and must be
        # escaped before the JSON parser sees it.
        repaired = re.sub(
            r'\\(?!["\\/nrt]|u[0-9a-fA-F]{4})',
            r"\\\\",
            stripped,
        )
        if repaired == stripped:
            raise
        result = json.loads(repaired)
    if not isinstance(result, dict):
        raise TypeError("LLM structured response must be a JSON object")
    return _recover_structured_mojibake(result)


def _recover_structured_mojibake(value: Any) -> Any:
    """Recover the provider's occasional GBK-bytes-as-Latin-1 response text."""

    if isinstance(value, dict):
        return {key: _recover_structured_mojibake(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_recover_structured_mojibake(item) for item in value]
    if not isinstance(value, str):
        return value
    try:
        decoded = value.encode("latin-1").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    original_cjk = sum("\u4e00" <= char <= "\u9fff" for char in value)
    decoded_cjk = sum("\u4e00" <= char <= "\u9fff" for char in decoded)
    suspicious = sum(0x80 <= ord(char) <= 0xFF for char in value)
    if suspicious >= 2 and decoded_cjk >= max(2, original_cjk + 2):
        return decoded
    return value


def _block_catalog(
    blocks: list[ParsedBlock], *, max_text_chars: int | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "block_id": block.block_id,
            "type": block.block_type,
            "page_index": block.page_index,
            "page": block.page_label,
            "text": (
                block.text[:max_text_chars]
                if max_text_chars is not None
                else block.text
            ),
            "asset_available": bool(block.asset_path),
        }
        for block in blocks
        if block.text or block.asset_path
    ]


def _compact_profile(profile: DocumentProfile, *, include_segments: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "document_type": profile.document_type,
        "document_summary": profile.document_summary,
        "learning_goal": profile.learning_goal,
    }
    if include_segments:
        payload["segments"] = [
            {
                "stable_key": item.stable_key,
                "title": item.title,
                "start_page_index": item.start_page_index,
                "end_page_index": item.end_page_index,
            }
            for item in profile.segments
        ]
    return payload


def _compact_knowledge_map(knowledge_map: KnowledgeMap) -> dict[str, Any]:
    return {
        "document_summary": knowledge_map.document_summary,
        "concepts": [
            concept.model_dump(
                include={
                    "stable_key",
                    "title",
                    "question_answered",
                    "core_claim",
                    "concept_type",
                    "importance",
                    "evidence_block_ids",
                    "asset_block_ids",
                    "source_sections",
                }
            )
            for concept in knowledge_map.concepts
        ],
        "skipped_sections": knowledge_map.skipped_sections,
    }


def _compact_planning_knowledge_map(knowledge_map: KnowledgeMap) -> dict[str, Any]:
    """Drop deterministic block bindings from semantic planning payloads."""

    return {
        "document_summary": knowledge_map.document_summary,
        "concepts": [
            concept.model_dump(
                include={
                    "stable_key",
                    "title",
                    "question_answered",
                    "core_claim",
                    "concept_type",
                    "importance",
                    "source_sections",
                }
            )
            for concept in knowledge_map.concepts
        ],
    }


class FixtureKnowledgeAgent:
    """Deterministic offline agent used by tests and first-run demonstrations."""

    name = "fixture"

    def structured_json(
        self,
        *,
        system: str,
        user: dict[str, Any],
        operation: str,
        prompt_version: str,
        use_pro_model: bool = False,
        thinking: bool = False,
    ) -> dict[str, Any]:
        """Small deterministic contract used by offline backbone tests."""
        if operation == "backbone.translate_titles":
            return {
                "titles": [
                    {"stable_key": item["stable_key"], "title_zh": item["title"]}
                    for item in user.get("sections", [])
                ]
            }
        if operation == "backbone.translate_figure_captions":
            return {
                "translations": [
                    {
                        "figure_id": item["figure_id"],
                        "caption_zh": f"中文图注：{item['caption']}",
                    }
                    for item in user.get("figures", [])
                ]
            }
        section = user.get("section", {})
        evidence = user.get("evidence", {})
        excerpt = str(evidence.get("text") or "").strip()
        return {
            "title_zh": section.get("title", "Concept"),
            "scope": f"本概念对应教材 {section.get('number', '')} 节。".strip(),
            "summary": excerpt[:240] or "等待真实模型补全。",
            "blocks": [
                {
                    "block_key": "explanation",
                    "block_type": "MARKDOWN",
                    "content": excerpt[:1200] or "等待真实模型补全。",
                }
            ],
        }

    def build_plan(self, document: ParsedDocument) -> KnowledgePlan:
        merged: dict[str, dict[str, Any]] = {}
        skipped: list[dict[str, str]] = []
        for section_title, blocks in _sections(document):
            meaningful = [
                block
                for block in blocks
                if block.text and block.block_type not in {"TITLE", "HEADING"}
            ]
            if not meaningful:
                skipped.append({"section": section_title, "reason": "LOW_INFORMATION"})
                continue
            key = _slug(section_title)
            bucket = merged.setdefault(
                key,
                {"title": section_title, "blocks": [], "sections": [], "pages": set()},
            )
            bucket["blocks"].extend(meaningful)
            bucket["sections"].append(section_title)
            bucket["pages"].update(block.page_label for block in meaningful)

        notes: list[NoteDraft] = []
        for stable_key, bucket in merged.items():
            source_blocks: list[ParsedBlock] = bucket["blocks"]
            body_text = " ".join(
                block.text.strip() for block in source_blocks if block.text.strip()
            )
            thesis = body_text.split("。", 1)[0].split(".", 1)[0].strip()
            if len(thesis) > 180:
                thesis = thesis[:177] + "..."
            note_blocks = [
                NoteBlockDraft(
                    block_type="PARAGRAPH",
                    text=body_text,
                    evidence_block_ids=[block.block_id for block in source_blocks],
                    asset_block_ids=[block.block_id for block in source_blocks if block.asset_path],
                )
            ]
            notes.append(
                NoteDraft(
                    stable_key=stable_key,
                    title=bucket["title"],
                    thesis=thesis or bucket["title"],
                    blocks=note_blocks,
                    tags=[_slug(bucket["title"])],
                    source_sections=bucket["sections"],
                )
            )
        return KnowledgePlan(
            document_summary=f"{document.title}：共识别 {len(notes)} 个知识主题。",
            notes=notes,
            skipped_sections=skipped,
        )

    def answer(self, question: str, hits: list[SearchHit]) -> Answer:
        if not hits:
            return Answer(answer="当前知识库没有足够证据回答这个问题。", hits=[])
        best = hits[0]
        pages = sorted(
            {
                str(item.get("page_label"))
                for item in best.evidence
                if item.get("page_label") is not None
            }
        )
        citation = f"（来源页：{', '.join(pages)}）" if pages else ""
        return Answer(
            answer=f"{best.thesis}\n\n{best.body}{citation}",
            hits=[best],
        )


_REFERENCE_KEYS = {"contribution_id", "note_version_id", "note_block_id", "evidence_span_id", "asset_id", "evidence_ref",
                   "evidence_refs", "note_block_ids", "evidence_span_ids", "asset_ids", "allowed_contribution_ids", "allowed_evidence_span_ids"}


def compact_reference_ids(value):
    """Ephemeral aliases reduce token use; persisted outputs are decoded to real IDs."""
    forward = {}

    def encode(obj, key=""):
        if isinstance(obj, dict):
            return {k: encode(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [encode(v, key) for v in obj]
        if isinstance(obj, str) and key in _REFERENCE_KEYS and len(obj) == 36:
            kind = "C" if key in {"contribution_id", "evidence_refs", "evidence_ref", "allowed_contribution_ids"} else "B" if key in {"note_block_id", "note_block_ids"} else "E" if key in {"evidence_span_id", "evidence_span_ids", "allowed_evidence_span_ids"} else "A" if key in {"asset_id", "asset_ids"} else "N"
            forward.setdefault(obj, f"REF_{kind}{len(forward)+1}")
            return forward[obj]
        return obj

    encoded = encode(value)
    reverse = {v:k for k,v in forward.items()}

    def decode(obj, key=""):
        if isinstance(obj, dict):
            return {k: decode(v, k) for k,v in obj.items()}
        if isinstance(obj, list):
            return [decode(v, key) for v in obj]
        if isinstance(obj, str):
            # Messages about references must keep their meaning across model calls.
            return re.sub(r"\bREF_[CBENA]\d+\b", lambda m: reverse.get(m.group(0), m.group(0)), obj)
        return obj

    return encoded, decode


class OpenAIKnowledgeAgent:
    name = "openai-compatible"

    def __init__(self, settings: Settings, telemetry: TelemetryRecorder | None = None):
        if not settings.llm_api_key or not settings.llm_fast_model:
            raise ValueError(
                "An LLM API key and fast model are required when using a real LLM"
            )
        timeout_seconds = float(getattr(settings, "llm_timeout_seconds", 180.0) or 180.0)
        kwargs: dict[str, Any] = {
            "api_key": settings.llm_api_key,
            "timeout": httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
            "max_retries": int(getattr(settings, "llm_max_retries", 0) or 0),
        }
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        self.client = OpenAI(**kwargs)
        self.fast_model = settings.llm_fast_model
        self.pro_model = settings.llm_pro_model or settings.llm_fast_model
        self.note_language = str(getattr(settings, "note_language", "zh-CN") or "zh-CN")
        self.telemetry = telemetry
        self.planning_single_call_concept_limit = self._positive_int_setting(
            settings, "planning_single_call_concept_limit", 18
        )
        self.planning_shard_size = self._positive_int_setting(
            settings, "planning_shard_size", 8
        )
        self.planning_concurrency = self._positive_int_setting(
            settings, "planning_concurrency", 3
        )
        self.planning_task_retries = max(
            0,
            self._nonnegative_int_setting(settings, "generation_task_retries", 1),
        )

    @staticmethod
    def _positive_int_setting(settings: Settings, name: str, default: int) -> int:
        raw = getattr(settings, name, default)
        if isinstance(raw, bool):
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(1, value)

    @staticmethod
    def _nonnegative_int_setting(settings: Settings, name: str, default: int) -> int:
        raw = getattr(settings, name, default)
        if isinstance(raw, bool):
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(0, value)

    @property
    def _language_instruction(self) -> str:
        if self.note_language.lower() in {"zh", "zh-cn", "chinese"}:
            return (
                "无论来源使用何种语言，标题、核心结论、正文和标签都必须使用简体中文。"
                "关键专业术语首次出现时写成“中文名称（Original Term）”，后续可只用中文或通用缩写；"
                "公式、代码、模型名、人名和论文名保留原文。"
            )
        return f"输出语言必须为 {self.note_language}。"

    def _json_call(
        self,
        *,
        system: str,
        user: dict[str, Any],
        model: str | None = None,
        operation: str | None = None,
        prompt_version: str | None = None,
        thinking: bool = False,
    ) -> dict:
        selected_model = model or self.fast_model
        decode_output = lambda value: value
        if operation and operation.startswith("semantic_synthesis."):
            user, decode_output = compact_reference_ids(user)
            system += " 输入 R 编号是合法引用，输出沿用输入编号，禁止编造编号。"
        user_json = json.dumps(user, ensure_ascii=False)
        telemetry_input = f"{system}\n{user_json}"
        started_clock = perf_counter()
        started_at = datetime.now(UTC).replace(tzinfo=None)
        response = None
        content: str | None = None
        finish_reason: str | None = None
        usage: dict[str, int | None] | None = None
        try:
            request: dict[str, Any] = {
                "model": selected_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_json},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            }
            if selected_model.startswith("deepseek-v4"):
                request["extra_body"] = {
                    "thinking": {"type": "enabled" if thinking else "disabled"}
                }
            if operation and operation.startswith("semantic_synthesis."):
                request["max_tokens"] = 16384
            response = self.client.chat.completions.create(**request)
            choice = response.choices[0]
            content = choice.message.content
            raw_finish_reason = getattr(choice, "finish_reason", None)
            finish_reason = raw_finish_reason if isinstance(raw_finish_reason, str) else None
            usage_obj = getattr(response, "usage", None)
            usage = {
                "prompt_tokens": getattr(usage_obj, "prompt_tokens", None),
                "completion_tokens": getattr(usage_obj, "completion_tokens", None),
                "total_tokens": getattr(usage_obj, "total_tokens", None),
            }
            if not content:
                raise RuntimeError("LLM returned an empty structured response")
            if finish_reason in {"length", "max_tokens"}:
                raise RuntimeError(
                    "LLM structured response was truncated before the JSON object completed"
                )
            result = decode_output(_loads_structured_json(content))
        except Exception as exc:
            if self.telemetry is not None:
                self.telemetry.record_llm(
                    operation=operation or self.telemetry.current().stage or "structured_json",
                    provider=self.name,
                    model=selected_model,
                    prompt_version=prompt_version,
                    input_text=telemetry_input,
                    output_text=content,
                    status="FAILED",
                    started_at=started_at,
                    elapsed_ms=elapsed_ms(started_clock),
                    usage=usage,
                    response_id=getattr(response, "id", None),
                    finish_reason=finish_reason,
                    error=exc,
                )
            raise

        if self.telemetry is not None:
            self.telemetry.record_llm(
                operation=operation or self.telemetry.current().stage or "structured_json",
                provider=self.name,
                model=selected_model,
                prompt_version=prompt_version,
                input_text=telemetry_input,
                output_text=content,
                status="COMPLETED",
                started_at=started_at,
                elapsed_ms=elapsed_ms(started_clock),
                usage=usage,
                response_id=getattr(response, "id", None),
                finish_reason=finish_reason,
            )
        return result

    def structured_json(
        self,
        *,
        system: str,
        user: dict[str, Any],
        operation: str,
        prompt_version: str,
        use_pro_model: bool = False,
        thinking: bool = False,
    ) -> dict[str, Any]:
        """Public structured-call boundary for domain modules and Harness tools."""
        return self._json_call(
            system=system,
            user=user,
            model=self.pro_model if use_pro_model else self.fast_model,
            operation=operation,
            prompt_version=prompt_version,
            thinking=thinking,
        )

    def build_plan(self, document: ParsedDocument) -> KnowledgePlan:
        local_candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for section_title, blocks in _sections(document):
            evidence = [
                {
                    "block_id": block.block_id,
                    "type": block.block_type,
                    "page": block.page_label,
                    "text": block.text,
                    "asset_available": bool(block.asset_path),
                }
                for block in blocks
                if block.text or block.asset_path
            ]
            if not evidence:
                skipped.append({"section": section_title, "reason": "LOW_INFORMATION"})
                continue
            result = self._json_call(
                system=(
                    "你是严谨的课程助教。分析局部章节，提取可独立查询的知识候选，不机械复述章节。"
                    f"{self._language_instruction}"
                    "只能引用输入中的 block_id。输出 JSON："
                    '{"candidates":[{"title":"","thesis":"","body":"","evidence_block_ids":[],'
                    '"asset_block_ids":[],"tags":[]}],'
                    '"skip_reason":null}。知识不足时 candidates 为空并写 skip_reason。'
                ),
                user={"document": document.title, "section": section_title, "blocks": evidence},
            )
            candidates = result.get("candidates") or []
            if not candidates:
                skipped.append(
                    {
                        "section": section_title,
                        "reason": result.get("skip_reason") or "LOW_INFORMATION",
                    }
                )
            for candidate in candidates:
                candidate["source_section"] = section_title
                local_candidates.append(candidate)

        result = self._json_call(
            system=(
                "你是整门课程的优秀助教。对局部知识候选进行全局合并、拆分、去重，"
                "生成紧凑但可独立理解的原子笔记。章节只是结构先验，不是笔记边界。"
                f"{self._language_instruction}"
                "不得引入候选外的事实，不得虚构 evidence_block_ids 或 asset_block_ids。输出 JSON："
                '{"document_summary":"","notes":[{"stable_key":"","title":"","thesis":"","'
                '"blocks":[{"block_type":"PARAGRAPH","text":"","evidence_block_ids":[],'
                '"asset_block_ids":[]}],'
                '"tags":[],"source_sections":[]}]}。'
            ),
            user={"document": document.title, "candidates": local_candidates},
            model=self.pro_model,
        )
        notes = []
        for index, raw in enumerate(result.get("notes") or []):
            raw.setdefault("stable_key", _slug(str(raw.get("title") or f"note-{index}")))
            notes.append(NoteDraft.model_validate(raw))
        return KnowledgePlan(
            document_summary=str(result.get("document_summary") or document.title),
            notes=notes,
            skipped_sections=skipped,
        )

    def profile_document(self, document: ParsedDocument) -> DocumentProfile:
        pages: dict[int, dict[str, Any]] = {
            index: {"page_index": index, "headings": [], "excerpt": [], "asset_count": 0}
            for index in range(document.page_count)
        }
        for block in document.blocks:
            page = pages.setdefault(
                block.page_index,
                {
                    "page_index": block.page_index,
                    "headings": [],
                    "excerpt": [],
                    "asset_count": 0,
                },
            )
            if block.block_type in {"TITLE", "HEADING"} and block.text:
                page["headings"].append(block.text[:200])
            elif block.text and sum(len(item) for item in page["excerpt"]) < 240:
                page["excerpt"].append(block.text[:160])
            if block.asset_path:
                page["asset_count"] += 1

        result = self._json_call(
            system=f"{_prompt('document_profiler.v2.md')}\n{self._language_instruction}",
            user={"document": document.title, "page_count": document.page_count, "pages": list(pages.values())},
            model=self.fast_model,
            operation="profile_document_v2",
            prompt_version="document-profiler-v2",
        )
        raw_segments = result.get("segments") or []
        segments: list[TopicSegment] = []
        cursor = 0
        for index, raw in enumerate(sorted(raw_segments, key=lambda item: int(item.get("start_page_index", 0)))):
            if cursor >= document.page_count:
                break
            end = min(
                document.page_count - 1,
                max(cursor, int(raw.get("end_page_index", cursor))),
            )
            title = str(raw.get("title") or f"主题区段 {index + 1}").strip()
            segments.append(
                TopicSegment(
                    stable_key=str(raw.get("stable_key") or _slug(title)),
                    title=title,
                    start_page_index=cursor,
                    end_page_index=end,
                    rationale=str(raw.get("rationale") or ""),
                )
            )
            cursor = end + 1
        if not segments:
            segments = [
                TopicSegment(
                    stable_key=_slug(document.title),
                    title=document.title,
                    start_page_index=0,
                    end_page_index=max(0, document.page_count - 1),
                    rationale="文档画像未返回有效区段，使用完整文档回退区段。",
                )
            ]
        elif cursor < document.page_count:
            segments[-1].end_page_index = document.page_count - 1
        return DocumentProfile(
            document_type=str(result.get("document_type") or "COURSE_MATERIAL"),
            document_summary=document.title,
            learning_goal="",
            segments=segments,
        )

    @staticmethod
    def _blocks_for_segment(
        document: ParsedDocument, segment: TopicSegment
    ) -> list[ParsedBlock]:
        return [
            block
            for block in document.blocks
            if segment.start_page_index <= block.page_index <= segment.end_page_index
            and block.block_type not in {"HEADER", "FOOTER", "PAGE_NUMBER"}
        ]

    @staticmethod
    def _extraction_windows(
        document: ParsedDocument,
        segment: TopicSegment,
        *,
        character_budget: int = 24_000,
    ) -> list[tuple[str, list[ParsedBlock]]]:
        """Bound model inputs without turning transport windows into note boundaries."""
        blocks = OpenAIKnowledgeAgent._blocks_for_segment(document, segment)
        pages: dict[int, list[ParsedBlock]] = defaultdict(list)
        for block in blocks:
            pages[block.page_index].append(block)
        windows: list[tuple[str, list[ParsedBlock]]] = []
        current: list[ParsedBlock] = []
        current_chars = 0
        for page_index in sorted(pages):
            page_blocks = pages[page_index]
            page_chars = sum(min(len(block.text), 3_000) + 120 for block in page_blocks)
            if current and current_chars + page_chars > character_budget:
                windows.append((segment.title, current))
                current = []
                current_chars = 0
            current.extend(page_blocks)
            current_chars += page_chars
        if current:
            windows.append((segment.title, current))
        return windows or [(segment.title, blocks)]

    def knowledge_windows(
        self, document: ParsedDocument, profile: DocumentProfile
    ) -> list[KnowledgeWindow]:
        result: list[KnowledgeWindow] = []
        for segment in profile.segments:
            windows = self._extraction_windows(document, segment)
            for window_index, (_title, blocks) in enumerate(windows, start=1):
                result.append(
                    KnowledgeWindow(
                        stable_key=f"{segment.stable_key}--{window_index}",
                        segment_key=segment.stable_key,
                        segment_title=segment.title,
                        window_index=window_index,
                        window_count=len(windows),
                        block_ids=[block.block_id for block in blocks],
                    )
                )
        return result

    def extract_knowledge_window(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        window: KnowledgeWindow,
    ) -> tuple[list[KnowledgeConceptCandidate], str | None]:
        wanted = set(window.block_ids)
        evidence = _block_catalog(
            [block for block in document.blocks if block.block_id in wanted],
            max_text_chars=3_000,
        )
        if not evidence:
            return [], "LOW_INFORMATION"
        result = self._json_call(
            system=f"{_prompt('candidate_extractor.v2.md')}\n{self._language_instruction}",
            user={
                "document": document.title,
                "segment": window.model_dump(),
                "blocks": evidence,
            },
            model=self.fast_model,
            operation="extract_source_candidates_v2",
            prompt_version="candidate-extractor-v2",
        )
        concepts: list[KnowledgeConceptCandidate] = []
        allowed_ids = set(window.block_ids)
        for index, raw in enumerate(result.get("candidates") or []):
            title = str(raw.get("title") or f"{window.segment_title} 概念 {index + 1}")
            evidence_ids = list(
                dict.fromkeys(str(item) for item in raw.get("evidence_block_ids") or [])
            )
            asset_ids = list(
                dict.fromkeys(str(item) for item in raw.get("asset_ids") or [])
            )
            invalid_ids = (set(evidence_ids) | set(asset_ids)) - allowed_ids
            if invalid_ids:
                raise RuntimeError(
                    f"Candidate extractor returned ids outside {window.stable_key}: "
                    + ",".join(sorted(invalid_ids))
                )
            if not evidence_ids:
                continue
            concepts.append(
                KnowledgeConceptCandidate(
                    stable_key=f"{window.stable_key}--c{index + 1}",
                    title=title,
                    question_answered=title,
                    core_claim=str(raw.get("claim") or title),
                    concept_type="CONCEPT",
                    importance="SUPPORTING",
                    evidence_block_ids=evidence_ids,
                    asset_block_ids=asset_ids,
                    source_sections=[window.segment_title],
                )
            )
        return concepts, result.get("skip_reason")

    def merge_knowledge_map(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        local_concepts: list[KnowledgeConceptCandidate],
        skipped: list[dict[str, str]],
    ) -> KnowledgeMap:
        concepts = [
            KnowledgeConcept(
                stable_key=item.stable_key,
                title=item.title,
                question_answered=item.title,
                core_claim=item.core_claim,
                concept_type="CONCEPT",
                importance="SUPPORTING",
                evidence_block_ids=list(dict.fromkeys(item.evidence_block_ids)),
                asset_block_ids=list(dict.fromkeys(item.asset_block_ids)),
                source_sections=list(dict.fromkeys(item.source_sections)),
            )
            for item in local_concepts
        ]
        return KnowledgeMap(
            document_summary=document.title,
            concepts=concepts,
            skipped_sections=skipped,
        )

    def build_knowledge_map(
        self, document: ParsedDocument, profile: DocumentProfile
    ) -> KnowledgeMap:
        local_concepts: list[KnowledgeConceptCandidate] = []
        skipped: list[dict[str, str]] = []
        for window in self.knowledge_windows(document, profile):
            concepts, skip_reason = self.extract_knowledge_window(document, profile, window)
            local_concepts.extend(concepts)
            if not concepts:
                skipped.append(
                    {
                        "section": window.segment_title,
                        "reason": str(skip_reason or "LOW_INFORMATION"),
                    }
                )
        return self.merge_knowledge_map(
            document, profile, local_concepts, skipped
        )

    def plan_source_note_bundle(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
    ) -> SourceNotePlanningBundle:
        """Create one global outline and NoteSpec list from compact Candidates."""

        max_note_count = max(
            1,
            len(profile.segments),
            (len(knowledge_map.concepts) + 1) // 2,
        )
        result = self._json_call(
            system=f"{_prompt('source_plan.v2.md')}\n{self._language_instruction}",
            user={
                "document": {
                    "title": document.title,
                    "document_type": profile.document_type,
                },
                "sections": [
                    {
                        "key": item.stable_key,
                        "title": item.title,
                        "start_page": item.start_page_index,
                        "end_page": item.end_page_index,
                    }
                    for item in profile.segments
                ],
                "candidates": [
                    {
                        "id": item.stable_key,
                        "title": item.title,
                        "claim": item.core_claim,
                        "section_key": (
                            item.source_sections[0] if item.source_sections else ""
                        ),
                        "asset_ids": item.asset_block_ids,
                    }
                    for item in knowledge_map.concepts
                ],
                "max_note_count": max_note_count,
            },
            model=self.pro_model,
            operation="plan_source_v2",
            prompt_version="source-plan-v2",
        )
        candidate_ids = {item.stable_key for item in knowledge_map.concepts}

        def normalize_raw_notes(payload: dict[str, Any]) -> list[dict[str, Any]]:
            normalized_notes: list[dict[str, Any]] = []
            for raw in payload.get("notes") or []:
                bound = list(
                    dict.fromkeys(
                        str(item)
                        for item in raw.get("candidate_ids") or []
                        if str(item) in candidate_ids
                    )
                )
                # A model may mirror an empty profile segment into the outline
                # and invent a note for it. It has no grounded unit and must not
                # reach drafting.
                if not bound:
                    continue
                normalized = dict(raw)
                normalized["candidate_ids"] = bound
                normalized_notes.append(normalized)
            return normalized_notes

        raw_notes = normalize_raw_notes(result)
        if len(raw_notes) > max_note_count:
            initial_result = result
            initial_notes = list(raw_notes)
            candidate_outline = {
                candidate_id: str(raw.get("outline_key") or "")
                for raw in initial_notes
                for candidate_id in raw.get("candidate_ids") or []
            }
            grouped = self._json_call(
                system=(
                    f"{_prompt('source_plan_grouper.v1.md')}\n"
                    f"{self._language_instruction}"
                ),
                user={
                    "document": document.title,
                    "max_group_count": max_note_count,
                    "candidates": [
                        {
                            "id": item.stable_key,
                            "title": item.title,
                            "claim": item.core_claim,
                            "section": (
                                item.source_sections[0]
                                if item.source_sections
                                else ""
                            ),
                            "outline_key": candidate_outline.get(
                                item.stable_key, ""
                            ),
                        }
                        for item in knowledge_map.concepts
                    ],
                },
                model=self.pro_model,
                operation="group_source_candidates_v1",
                prompt_version="source-plan-grouper-v1",
            )
            concept_assets = {
                item.stable_key: item.asset_block_ids
                for item in knowledge_map.concepts
            }
            grouped_notes: list[dict[str, Any]] = []
            for index, raw in enumerate(grouped.get("groups") or []):
                bound = list(
                    dict.fromkeys(
                        str(item)
                        for item in raw.get("candidate_ids") or []
                        if str(item) in candidate_ids
                    )
                )
                if not bound:
                    continue
                outline_key = str(raw.get("outline_key") or "")
                if outline_key not in {
                    candidate_outline.get(item, "") for item in bound
                }:
                    outline_key = candidate_outline.get(bound[0], "")
                grouped_notes.append(
                    {
                        "key": raw.get("key") or f"knowledge-unit-{index + 1}",
                        "title": raw.get("title") or "知识单元",
                        "question": raw.get("question") or raw.get("title"),
                        "outline_key": outline_key,
                        "candidate_ids": bound,
                        "asset_ids": list(
                            dict.fromkeys(
                                asset_id
                                for candidate_id in bound
                                for asset_id in concept_assets.get(candidate_id, [])
                            )
                        ),
                    }
                )
            flattened = [
                candidate_id
                for raw in grouped_notes
                for candidate_id in raw["candidate_ids"]
            ]
            grouping_is_valid = (
                len(grouped_notes) <= max_note_count
                and len(flattened) == len(candidate_ids)
                and set(flattened) == candidate_ids
            )
            if not grouping_is_valid:
                # Reliable final guard: pair adjacent notes from the original
                # reading-order plan. This path is deterministic and only runs
                # when the focused grouping call violates its tiny contract.
                grouped_notes = []
                for index in range(0, len(initial_notes), 2):
                    pair = initial_notes[index : index + 2]
                    bound = list(
                        dict.fromkeys(
                            candidate_id
                            for raw in pair
                            for candidate_id in raw.get("candidate_ids") or []
                        )
                    )
                    first = pair[0]
                    title = str(first.get("title") or "知识单元")
                    if len(pair) > 1:
                        title = f"{title}与相关机制"
                    grouped_notes.append(
                        {
                            "key": first.get("key") or f"knowledge-unit-{index // 2 + 1}",
                            "title": title,
                            "question": f"{title}包含哪些关键概念与机制？",
                            "outline_key": first.get("outline_key"),
                            "candidate_ids": bound,
                            "asset_ids": list(
                                dict.fromkeys(
                                    asset_id
                                    for candidate_id in bound
                                    for asset_id in concept_assets.get(candidate_id, [])
                                )
                            ),
                        }
                    )
            result = {
                "outline": initial_result.get("outline") or [],
                "notes": grouped_notes,
            }
            raw_notes = normalize_raw_notes(result)
        if len(raw_notes) > max_note_count:
            raise RuntimeError(
                "SourcePlan remains over-fragmented after early repair: "
                f"{len(raw_notes)} notes for {len(knowledge_map.concepts)} candidates "
                f"(max {max_note_count})"
            )
        note_set = self._note_set_from_result(
            {
                "notes": [
                    {
                        "stable_key": raw.get("key"),
                        "title": raw.get("title"),
                        "question_answered": raw.get("question"),
                        "target_thesis": raw.get("question") or raw.get("title"),
                        "outline_node_key": raw.get("outline_key"),
                        "concept_keys": raw.get("candidate_ids") or [],
                        "asset_block_ids": raw.get("asset_ids") or [],
                    }
                    for raw in raw_notes
                ],
                "skipped_concepts": [],
            },
            document.title,
        )
        note_set = self._hydrate_note_set_from_concepts(note_set, knowledge_map)

        raw_nodes = list(result.get("outline") or [])
        parent_candidates = {
            str(raw.get("key") or ""): (
                str(raw.get("parent_key")) if raw.get("parent_key") else None
            )
            for raw in raw_nodes
        }
        used_outline_keys = {
            str(raw.get("outline_key"))
            for raw in raw_notes
            if raw.get("outline_key")
        }
        retained_outline_keys = set(used_outline_keys)
        for key in list(used_outline_keys):
            parent = parent_candidates.get(key)
            seen: set[str] = set()
            while parent and parent not in seen:
                seen.add(parent)
                retained_outline_keys.add(parent)
                parent = parent_candidates.get(parent)
        raw_nodes = [
            raw
            for raw in raw_nodes
            if str(raw.get("key") or "") in retained_outline_keys
        ]
        parent_by_key = {
            str(raw.get("key") or ""): raw.get("parent_key") for raw in raw_nodes
        }

        def node_level(key: str) -> int:
            level = 1
            seen: set[str] = set()
            parent = parent_by_key.get(key)
            while parent and parent not in seen and level < 3:
                seen.add(str(parent))
                level += 1
                parent = parent_by_key.get(str(parent))
            return level

        nodes = [
            SourceNoteOutlineNodeSpec(
                stable_key=str(
                    raw.get("key") or _slug(str(raw.get("title") or "主题"))
                ),
                title=str(raw.get("title") or "知识主题"),
                level=node_level(str(raw.get("key") or "")),
                parent_key=(
                    str(raw.get("parent_key")) if raw.get("parent_key") else None
                ),
                learning_goal="",
                source_sections=[],
            )
            for raw in raw_nodes
        ]
        summary_titles = list(dict.fromkeys(node.title for node in nodes if node.parent_key is None)) or [spec.title for spec in note_set.notes[:6]]
        outline = SourceNoteOutlinePlan(
            title=document.title,
            summary=(
                "主要内容包括：" + "、".join(summary_titles)
                if summary_titles
                else document.title
            ),
            learning_goal="",
            nodes=nodes,
        )
        note_set = self._bind_notes_to_outline_leaves(note_set, outline)
        return SourceNotePlanningBundle(
            schema_version="source-plan-v2",
            source_note_outline=outline,
            note_set_plan=note_set,
        )

    def _plan_source_note_bundle_single(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
    ) -> SourceNotePlanningBundle:
        result = self._json_call(
            system=(
                f"{_prompt('source_note_planner.v3.md')}\n"
                f"{_prompt('writing_policy.v2.md')}\n{self._language_instruction}"
            ),
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_knowledge_map(knowledge_map),
            },
            model=self.pro_model,
            operation="plan_source_note_bundle_v3",
            prompt_version="source-note-planner-v4-nonthinking",
        )
        outline_raw = result.get("outline") or {}
        note_set_raw = result.get("note_set") or {}

        outline = self._outline_from_result(
            outline_raw, document.title, knowledge_map.document_summary
        )
        note_set = self._note_set_from_result(
            note_set_raw, knowledge_map.document_summary
        )
        note_set = self._hydrate_note_set_from_concepts(note_set, knowledge_map)
        note_set = self._bind_notes_to_outline_leaves(note_set, outline)
        return SourceNotePlanningBundle(
            source_note_outline=outline,
            note_set_plan=note_set,
        )

    def _plan_source_note_bundle_sharded(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
    ) -> SourceNotePlanningBundle:
        """Use one global outline and bounded note shards for large knowledge maps."""

        outline_result = self._json_call(
            system=(
                f"{_prompt('source_note_outline_router.v1.md')}\n"
                f"{self._language_instruction}"
            ),
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_planning_knowledge_map(knowledge_map),
            },
            model=self.pro_model,
            operation="plan_source_note_outline_v3",
            prompt_version="source-note-outline-router-v1-nonthinking",
        )
        outline = self._outline_from_result(
            outline_result, document.title, knowledge_map.document_summary
        )
        shards = self._planning_concept_shards(knowledge_map.concepts)

        def plan_shard(
            shard_index: int, concepts: list[KnowledgeConcept]
        ) -> tuple[int, NoteSetPlan]:
            last_error: Exception | None = None
            for _attempt in range(self.planning_task_retries + 1):
                try:
                    user = {
                        "document": document.title,
                        "batch": {
                            "index": shard_index + 1,
                            "count": len(shards),
                            "concept_count": len(concepts),
                            "max_notes": max(1, math.ceil(len(concepts) / 2)),
                        },
                        "source_note_outline": outline.model_dump(),
                        "concepts": [
                            concept.model_dump(
                                include={
                                    "stable_key",
                                    "title",
                                    "question_answered",
                                    "core_claim",
                                    "concept_type",
                                    "importance",
                                    "source_sections",
                                }
                            )
                            for concept in concepts
                        ],
                    }
                    if self.telemetry is None:
                        result = self._json_call(
                            system=(
                                f"{_prompt('note_set_planner_shard.v1.md')}\n"
                                f"{_prompt('writing_policy.v2.md')}\n"
                                f"{self._language_instruction}"
                            ),
                            user=user,
                            model=self.pro_model,
                            operation="plan_note_set_shard_v1",
                            prompt_version="note-set-planner-shard-v1-nonthinking",
                        )
                    else:
                        with self.telemetry.bind(
                            note_key=f"source-note-planning-shard-{shard_index + 1}"
                        ):
                            result = self._json_call(
                                system=(
                                    f"{_prompt('note_set_planner_shard.v1.md')}\n"
                                    f"{_prompt('writing_policy.v2.md')}\n"
                                    f"{self._language_instruction}"
                                ),
                                user=user,
                                model=self.pro_model,
                                operation="plan_note_set_shard_v1",
                                prompt_version="note-set-planner-shard-v1-nonthinking",
                            )
                    partial_map = KnowledgeMap(
                        document_summary=knowledge_map.document_summary,
                        concepts=concepts,
                    )
                    planned = self._note_set_from_result(
                        result, knowledge_map.document_summary
                    )
                    planned = self._hydrate_note_set_from_concepts(
                        planned, partial_map
                    )
                    return shard_index, planned
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
            raise RuntimeError(
                f"Planning shard {shard_index + 1}/{len(shards)} failed after retries: "
                f"{last_error}"
            ) from last_error

        planned_by_index: dict[int, NoteSetPlan] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.planning_concurrency, len(shards)),
            thread_name_prefix="noodle-planning",
        ) as executor:
            futures = {
                executor.submit(copy_context().run, plan_shard, index, shard): index
                for index, shard in enumerate(shards)
            }
            for future in as_completed(futures):
                shard_index, planned = future.result()
                planned_by_index[shard_index] = planned

        notes: list[NoteSpec] = []
        skipped_concepts: list[dict[str, str]] = []
        used_note_keys: set[str] = set()
        for shard_index in range(len(shards)):
            planned = planned_by_index[shard_index]
            for spec in planned.notes:
                base_key = spec.stable_key
                stable_key = base_key
                suffix = 2
                while stable_key in used_note_keys:
                    stable_key = f"{base_key}-{suffix}"
                    suffix += 1
                if stable_key != spec.stable_key:
                    spec = spec.model_copy(update={"stable_key": stable_key})
                used_note_keys.add(stable_key)
                notes.append(spec)
            skipped_concepts.extend(planned.skipped_concepts)

        note_set = NoteSetPlan(
            document_summary=knowledge_map.document_summary,
            notes=notes,
            skipped_concepts=skipped_concepts,
        )
        note_set = self._bind_notes_to_outline_leaves(note_set, outline)
        return SourceNotePlanningBundle(
            schema_version="source-note-planning-v5-sharded",
            source_note_outline=outline,
            note_set_plan=note_set,
        )

    def _planning_concept_shards(
        self, concepts: list[KnowledgeConcept]
    ) -> list[list[KnowledgeConcept]]:
        """Keep source-adjacent concepts together while bounding response size."""

        if not concepts:
            return []
        target = max(2, self.planning_shard_size)
        minimum = max(2, target // 2)
        hard_limit = target + 2
        shards: list[list[KnowledgeConcept]] = []
        current: list[KnowledgeConcept] = []
        current_sections: set[str] = set()
        for concept in concepts:
            sections = {item.casefold() for item in concept.source_sections if item}
            has_overlap = bool(current_sections & sections)
            crosses_topic = bool(current) and not has_overlap
            if current and (
                len(current) >= hard_limit
                or (len(current) >= minimum and crosses_topic)
            ):
                shards.append(current)
                current = []
                current_sections = set()
            current.append(concept)
            current_sections.update(sections)
        if current:
            shards.append(current)
        return shards

    @staticmethod
    def _outline_from_result(
        outline_raw: dict[str, Any], document_title: str, default_summary: str
    ) -> SourceNoteOutlinePlan:

        nodes: list[SourceNoteOutlineNodeSpec] = []
        seen: set[str] = set()
        for index, raw in enumerate(outline_raw.get("nodes") or []):
            title = str(raw.get("title") or f"知识主题 {index + 1}")
            raw.setdefault("stable_key", _slug(title))
            raw["learning_goal"] = ""
            node = SourceNoteOutlineNodeSpec.model_validate(raw)
            if node.stable_key in seen:
                continue
            nodes.append(node)
            seen.add(node.stable_key)

        outline = SourceNoteOutlinePlan(
            title=str(outline_raw.get("title") or document_title),
            summary=str(
                outline_raw.get("summary") or default_summary
            ),
            learning_goal="",
            nodes=nodes,
        )
        return outline

    @staticmethod
    def _hydrate_note_set_from_concepts(
        note_set: NoteSetPlan, knowledge_map: KnowledgeMap
    ) -> NoteSetPlan:
        concepts = {concept.stable_key: concept for concept in knowledge_map.concepts}
        hydrated: list[NoteSpec] = []
        for spec in note_set.notes:
            concept_keys = list(
                dict.fromkeys(key for key in spec.concept_keys if key in concepts)
            )
            evidence: list[str] = []
            allowed_assets: list[str] = []
            sections: list[str] = []
            for key in concept_keys:
                concept = concepts[key]
                evidence.extend(concept.evidence_block_ids)
                allowed_assets.extend(concept.asset_block_ids)
                sections.extend(concept.source_sections)
            allowed_asset_set = set(allowed_assets)
            assets = [
                asset_id
                for asset_id in dict.fromkeys(spec.asset_block_ids)
                if asset_id in allowed_asset_set
            ]
            hydrated.append(
                spec.model_copy(
                    update={
                        "concept_keys": concept_keys,
                        "evidence_block_ids": list(dict.fromkeys(evidence)),
                        "asset_block_ids": list(dict.fromkeys(assets)),
                        "source_sections": list(dict.fromkeys(sections)),
                        "tags": _canonical_tags(spec.tags),
                    }
                )
            )
        return note_set.model_copy(update={"notes": hydrated})

    @staticmethod
    def _bind_notes_to_outline_leaves(
        note_set: NoteSetPlan, outline: SourceNoteOutlinePlan
    ) -> NoteSetPlan:
        parent_keys = {node.parent_key for node in outline.nodes if node.parent_key}
        leaves = [node for node in outline.nodes if node.stable_key not in parent_keys]
        if not leaves:
            return note_set
        leaf_keys = {node.stable_key for node in leaves}
        rebound: list[NoteSpec] = []
        for spec in note_set.notes:
            if spec.outline_node_key in leaf_keys:
                rebound.append(spec)
                continue
            wanted_sections = {item.casefold() for item in spec.source_sections}
            best = max(
                leaves,
                key=lambda node: len(
                    wanted_sections
                    & {item.casefold() for item in node.source_sections}
                ),
            )
            rebound.append(
                spec.model_copy(update={"outline_node_key": best.stable_key})
            )
        return note_set.model_copy(update={"notes": rebound})

    def plan_note_set(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        source_note_outline: SourceNoteOutlinePlan,
    ) -> NoteSetPlan:
        result = self._json_call(
            system=(
                f"{_prompt('note_set_planner.v2.md')}\n"
                f"{_prompt('writing_policy.v2.md')}\n{self._language_instruction}"
            ),
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_knowledge_map(knowledge_map),
                "source_note_outline": source_note_outline.model_dump(),
            },
            model=self.fast_model,
        )
        return self._note_set_from_result(result, knowledge_map.document_summary)

    def plan_source_note_outline(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
    ) -> SourceNoteOutlinePlan:
        result = self._json_call(
            system=(
                f"{_prompt('source_note_outline_planner.v2.md')}\n"
                f"{self._language_instruction}"
            ),
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_knowledge_map(knowledge_map),
            },
            model=self.pro_model,
        )
        nodes: list[SourceNoteOutlineNodeSpec] = []
        seen: set[str] = set()
        for index, raw in enumerate(result.get("nodes") or []):
            title = str(raw.get("title") or f"知识主题 {index + 1}")
            raw.setdefault("stable_key", _slug(title))
            node = SourceNoteOutlineNodeSpec.model_validate(raw)
            if node.stable_key in seen:
                continue
            nodes.append(node)
            seen.add(node.stable_key)
        return SourceNoteOutlinePlan(
            title=str(result.get("title") or document.title),
            summary=str(result.get("summary") or knowledge_map.document_summary),
            learning_goal="",
            nodes=nodes,
        )

    @staticmethod
    def _note_set_from_result(
        result: dict[str, Any], default_summary: str
    ) -> NoteSetPlan:
        notes: list[NoteSpec] = []
        seen: set[str] = set()
        for index, raw in enumerate(result.get("notes") or []):
            title = str(raw.get("title") or f"原子笔记 {index + 1}")
            raw.setdefault("stable_key", _slug(title))
            raw.setdefault("question_answered", title)
            raw.setdefault("target_thesis", title)
            raw.setdefault("concept_keys", [])
            spec = NoteSpec.model_validate(raw)
            if spec.stable_key in seen:
                continue
            notes.append(spec)
            seen.add(spec.stable_key)
        return NoteSetPlan(
            document_summary=str(result.get("document_summary") or default_summary),
            notes=notes,
            skipped_concepts=result.get("skipped_concepts") or [],
        )

    def review_note_set(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        source_note_outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
    ) -> NoteSetReview:
        result = self._json_call(
            system=f"{_prompt('note_set_judge.v2.md')}\n{self._language_instruction}",
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_knowledge_map(knowledge_map),
                "source_note_outline": source_note_outline.model_dump(),
                "note_set_plan": note_set.model_dump(),
            },
            model=self.pro_model,
        )
        return NoteSetReview.model_validate(result)

    def repair_note_set(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        source_note_outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
        review: NoteSetReview,
    ) -> NoteSetPlan:
        result = self._json_call(
            system=(
                f"{_prompt('note_set_repair.v2.md')}\n"
                f"{_prompt('writing_policy.v2.md')}\n{self._language_instruction}"
            ),
            user={
                "document": document.title,
                "profile": _compact_profile(profile),
                "knowledge_map": _compact_knowledge_map(knowledge_map),
                "source_note_outline": source_note_outline.model_dump(),
                "original_note_set": note_set.model_dump(),
                "review": review.model_dump(),
            },
            model=self.pro_model,
        )
        return self._note_set_from_result(result, knowledge_map.document_summary)

    @staticmethod
    def _evidence_for_spec(
        document: ParsedDocument, spec: NoteSpec
    ) -> list[dict[str, Any]]:
        wanted = set(spec.evidence_block_ids) | set(spec.asset_block_ids)
        return _block_catalog(
            [block for block in document.blocks if block.block_id in wanted],
            max_text_chars=2_400,
        )

    @staticmethod
    def _nearby_note_boundaries(note_set: NoteSetPlan, spec: NoteSpec) -> list[dict[str, str]]:
        """Return only boundaries that can realistically overlap this note."""

        ordered = list(note_set.notes)
        current_index = next(
            (index for index, item in enumerate(ordered) if item.stable_key == spec.stable_key),
            0,
        )
        related_concepts = set(spec.concept_keys)
        candidates: list[NoteSpec] = []
        for index, item in enumerate(ordered):
            if item.stable_key == spec.stable_key:
                continue
            is_neighbor = abs(index - current_index) <= 2
            same_leaf = item.outline_node_key == spec.outline_node_key
            concept_overlap = bool(related_concepts & set(item.concept_keys))
            if is_neighbor or same_leaf or concept_overlap:
                candidates.append(item)
        candidates.sort(
            key=lambda item: (
                item.outline_node_key != spec.outline_node_key,
                not bool(related_concepts & set(item.concept_keys)),
                abs(ordered.index(item) - current_index),
            )
        )
        return [
            {
                "stable_key": item.stable_key,
                "title": item.title,
                "question": item.question_answered,
            }
            for item in candidates[:8]
        ]

    def draft_notes(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
    ) -> KnowledgePlan:
        notes: list[NoteDraft] = []
        for spec in note_set.notes:
            notes.append(
                self.draft_note(document, profile, knowledge_map, note_set, spec)
            )
        return KnowledgePlan(
            document_summary=note_set.document_summary,
            notes=notes,
            skipped_sections=knowledge_map.skipped_sections,
        )

    def draft_note(
        self,
        document: ParsedDocument,
        profile: DocumentProfile,
        knowledge_map: KnowledgeMap,
        note_set: NoteSetPlan,
        spec: NoteSpec,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> NoteDraft:
        concepts = {concept.stable_key: concept for concept in knowledge_map.concepts}
        related_concepts = [
            concepts[key].model_dump(
                include={
                    "stable_key",
                    "title",
                    "core_claim",
                }
            )
            for key in spec.concept_keys
            if key in concepts
        ]
        # Flash handles the many compact, evidence-bounded notes.  Reserve the
        # slower model for structures where cross-checking variables or
        # comparison axes materially improves correctness; the pro Judge still
        # reviews every batch.
        writer_model = (
            self.pro_model
            if len(related_concepts) >= 3
            or spec.note_kind in {"FORMULA", "COMPARISON", "CASE"}
            or any(
                concept.concept_type in {"FORMULA", "COMPARISON"}
                for concept in (concepts[key] for key in spec.concept_keys if key in concepts)
            )
            else self.fast_model
        )
        writer_system = (
            f"{_prompt('note_writer.v5.md')}\n"
            f"{_prompt('writing_policy.v2.md')}\n{self._language_instruction}"
        )
        writer_input = {
                "document": document.title,
                "document_type": profile.document_type,
                "note_spec": {
                    "key": spec.stable_key,
                    "title": spec.title,
                    "question": spec.question_answered,
                    "candidate_ids": spec.concept_keys,
                    "asset_ids": spec.asset_block_ids,
                },
                "candidates": related_concepts,
                "evidence_catalog": self._evidence_for_spec(document, spec),
                "vision_catalog": [
                    public_visual_context(vision_context[block_id])
                    for block_id in spec.asset_block_ids
                    if vision_context and block_id in vision_context
                ],
                "other_note_boundaries": self._nearby_note_boundaries(note_set, spec),
            }
        try:
            result = self._json_call(
                system=writer_system,
                user=writer_input,
                model=writer_model,
                operation="draft_note_v6",
                prompt_version="note-writer-v12",
            )
        except RuntimeError as exc:
            # Some OpenAI-compatible flash routes occasionally return a 200
            # response with an empty message for particular evidence payloads.
            # Escalate only that note to the pro route; completed notes and
            # normal flash calls remain cached in the generation artifact.
            if writer_model == self.pro_model or "empty structured response" not in str(exc):
                raise
            result = self._json_call(
                system=writer_system,
                user=writer_input,
                model=self.pro_model,
                operation="draft_note_v6_fallback",
                prompt_version="note-writer-v12",
            )
        result["stable_key"] = spec.stable_key
        result.setdefault("title", spec.title)
        result.setdefault("thesis", spec.target_thesis)
        result.setdefault("tags", spec.tags)
        result.setdefault("source_sections", spec.source_sections)
        draft = NoteDraft.model_validate(result)
        draft.tags = _canonical_tags(draft.tags)
        for block in draft.blocks:
            block.evidence_block_ids = list(dict.fromkeys(block.evidence_block_ids))
            block.asset_block_ids = list(dict.fromkeys(block.asset_block_ids))
        return deduplicate_asset_placements(
            ensure_required_visual_blocks(
                normalize_note_blocks(draft), spec, vision_context
            )
        )

    def evaluate_plan(
        self,
        document: ParsedDocument,
        note_set: NoteSetPlan,
        plan: KnowledgePlan,
        knowledge_map: KnowledgeMap | None = None,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> list[NoteEvaluation]:
        specs = {spec.stable_key: spec for spec in note_set.notes}
        relevant_concept_keys = {
            concept_key
            for note in plan.notes
            if note.stable_key in specs
            for concept_key in specs[note.stable_key].concept_keys
        }
        payload = []
        for note in plan.notes:
            spec = specs.get(note.stable_key)
            evidence_ids = {
                block_id
                for block in note.blocks
                for block_id in (*block.evidence_block_ids, *block.asset_block_ids)
            }
            if spec is not None:
                evidence_ids.update(spec.evidence_block_ids)
                evidence_ids.update(spec.asset_block_ids)
            catalog = _block_catalog(
                [block for block in document.blocks if block.block_id in evidence_ids]
            )
            for item in catalog:
                item["text"] = str(item.get("text") or "")[:900]
            payload.append(
                {
                    "note_spec": spec.model_dump() if spec else None,
                    "note": note.model_dump(),
                    "evidence_catalog": catalog,
                    "vision_catalog": [
                        public_visual_context(vision_context[block_id])
                        for block_id in (spec.asset_block_ids if spec else [])
                        if vision_context and block_id in vision_context
                    ],
                }
            )
        judge_system = (
            f"{_prompt('note_judge.v3.md')}\n"
            f"{_prompt('writing_policy.v2.md')}\n{self._language_instruction}"
        )
        judge_user = {
            "document": document.title,
            "document_summary": (
                knowledge_map.document_summary if knowledge_map else note_set.document_summary
            ),
            "concepts": [
                concept.model_dump(
                    include={"stable_key", "title", "question_answered", "core_claim"}
                )
                for concept in (knowledge_map.concepts if knowledge_map else [])
                if concept.stable_key in relevant_concept_keys
            ],
            "notes": payload,
        }
        complex_kinds = {"FORMULA", "COMPARISON", "CASE", "PROCEDURE", "EXAMPLE"}
        use_pro = any(
            (spec := specs.get(note.stable_key)) is not None
            and (
                spec.note_kind.upper() in complex_kinds
                or bool(spec.asset_block_ids)
                or len(spec.concept_keys) >= 3
                or len(spec.evidence_block_ids) >= 10
            )
            for note in plan.notes
        )
        evaluator_model = self.pro_model if use_pro else self.fast_model
        result = self._json_call(
            system=judge_system,
            user=judge_user,
            model=evaluator_model,
            operation="judge_note_batch_v11",
            prompt_version="note-judge-v12",
        )
        by_key = {note.stable_key for note in plan.notes}
        note_by_key = {note.stable_key: note for note in plan.notes}
        def parse_evaluations(raw_items: list[dict[str, Any]]) -> list[NoteEvaluation]:
            return [
                NoteEvaluation(
                    **NoteEvaluation.model_validate(raw).model_dump(
                        exclude={"content_sha256", "evaluator_model", "prompt_version"}
                    ),
                    content_sha256=note_content_sha256(
                        note_by_key[str(raw.get("stable_key"))]
                    ),
                    evaluator_model=evaluator_model,
                    prompt_version="note-judge-v12",
                )
                for raw in raw_items
                if raw.get("stable_key") in by_key
            ]

        evaluations = parse_evaluations(result.get("evaluations") or [])
        returned = {item.stable_key for item in evaluations}
        missing = by_key - returned
        if missing:
            retry_result = self._json_call(
                system=judge_system,
                user={
                    **judge_user,
                    "notes": [
                        item
                        for item in payload
                        if (item.get("note") or {}).get("stable_key") in missing
                    ],
                    "retry_instruction": (
                        "上一次审校漏掉了候选。必须对本次 notes 中的每个 stable_key "
                        "恰好返回一条 evaluation，不得省略。"
                    ),
                },
                model=evaluator_model,
                operation="judge_note_missing_retry_v2",
                prompt_version="note-judge-v12",
            )
            retry_evaluations = parse_evaluations(retry_result.get("evaluations") or [])
            retry_by_key = {item.stable_key: item for item in retry_evaluations}
            evaluations.extend(retry_by_key[key] for key in sorted(missing & retry_by_key.keys()))
            returned.update(retry_by_key)
        evaluations.extend(
            NoteEvaluation(
                stable_key=key,
                passed=False,
                issues=["JUDGE_MISSING_RESULT: 两次审校都未返回该候选的评价"],
                content_sha256=note_content_sha256(note_by_key[key]),
                evaluator_model=evaluator_model,
                prompt_version="note-judge-v12",
            )
            for key in by_key - returned
        )
        return evaluations

    def build_generation(self, document: ParsedDocument) -> GenerationBundle:
        profile = self.profile_document(document)
        knowledge_map = self.build_knowledge_map(document, profile)
        planning_bundle = self.plan_source_note_bundle(document, profile, knowledge_map)
        source_note_outline = planning_bundle.source_note_outline
        note_set = planning_bundle.note_set_plan
        notes_by_concept = {
            concept.stable_key: [
                spec.stable_key
                for spec in note_set.notes
                if concept.stable_key in spec.concept_keys
            ]
            for concept in knowledge_map.concepts
        }
        note_set_review = NoteSetReview(
            passed=all(notes_by_concept.values()),
            coverage=[
                {
                    "concept_key": key,
                    "status": "COVERED" if note_keys else "MISSING",
                    "note_keys": note_keys,
                    "reason": "由联合规划结果确定性映射。",
                }
                for key, note_keys in notes_by_concept.items()
            ],
        )
        final_plan = self.draft_notes(document, profile, knowledge_map, note_set)
        evaluations = self.evaluate_plan(
            document, note_set, final_plan, knowledge_map
        )
        return GenerationBundle(
            profile=profile,
            knowledge_map=knowledge_map,
            source_note_outline=source_note_outline,
            note_set_plan=note_set,
            note_set_review=note_set_review,
            final_plan=final_plan,
            evaluations=evaluations,
        )

    def repair_plan(
        self,
        document: ParsedDocument,
        original: KnowledgePlan,
        failures: list[dict[str, Any]],
        note_set: NoteSetPlan | None = None,
        vision_context: dict[str, dict[str, Any]] | None = None,
    ) -> KnowledgePlan:
        failed_keys = {str(item.get("candidate")) for item in failures}
        specs = {
            spec.stable_key: spec
            for spec in (note_set.notes if note_set is not None else [])
        }
        notes_by_key = {note.stable_key: note for note in original.notes}
        failures_by_key = {
            str(item.get("candidate")): item for item in failures if item.get("candidate")
        }
        repair_system = (
            f"{_prompt('note_repair.v2.md')}\n"
                f"{_prompt('writing_policy.v2.md')}\n{self._language_instruction}"
        )

        def repair_one(key: str) -> NoteDraft | None:
            note = notes_by_key.get(key)
            spec = specs.get(key)
            if note is None:
                return None
            referenced_ids = {
                block_id
                for block in note.blocks
                for block_id in (*block.evidence_block_ids, *block.asset_block_ids)
            }
            if spec is not None:
                referenced_ids.update(spec.evidence_block_ids)
                referenced_ids.update(spec.asset_block_ids)
            evidence_catalog = [
                {
                    "block_id": block.block_id,
                    "type": block.block_type,
                    "page": block.page_label,
                    "text": block.text,
                    "asset_available": bool(block.asset_path),
                }
                for block in document.blocks
                if block.block_id in referenced_ids
            ]
            user = {
                "document": document.title,
                "failed_notes": [note.model_dump()],
                "failed_note_specs": [spec.model_dump()] if spec is not None else [],
                "failures": [failures_by_key[key]] if key in failures_by_key else [],
                "evidence_catalog": evidence_catalog,
                "vision_catalog": [
                    public_visual_context(vision_context[block_id])
                    for block_id in (spec.asset_block_ids if spec is not None else [])
                    if vision_context and block_id in vision_context
                ],
            }
            # A failed repair must not cause every successful sibling to be
            # regenerated. Keep retries local and let the caller retain a
            # precise remaining-failure record for an omitted key.
            last_error: Exception | None = None
            for _attempt in range(2):
                try:
                    result = self._json_call(
                        system=repair_system,
                        user=user,
                        model=self.pro_model,
                        operation="repair_note_v5",
                        prompt_version="note-repair-v7",
                    )
                    candidates = result.get("notes") or []
                    raw = next(
                        (
                            item
                            for item in candidates
                            if str(item.get("stable_key") or "") == key
                        ),
                        candidates[0] if len(candidates) == 1 else None,
                    )
                    if raw is None:
                        continue
                    raw["stable_key"] = key
                    return normalize_note_blocks(NoteDraft.model_validate(raw))
                except Exception as exc:  # noqa: BLE001 - per-note retry boundary
                    last_error = exc
            if last_error is not None:
                return None
            return None

        repaired_by_key: dict[str, NoteDraft] = {}
        workers = min(2, len(failed_keys))
        if workers:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="noodle-repair"
            ) as executor:
                future_map = {
                    executor.submit(copy_context().run, repair_one, key): key
                    for key in sorted(failed_keys)
                }
                for future in as_completed(future_map):
                    repaired_note = future.result()
                    if repaired_note is not None:
                        repaired_by_key[repaired_note.stable_key] = repaired_note
        repaired = [
            repaired_by_key[key] for key in sorted(repaired_by_key)
        ]
        for note in repaired:
            spec = specs.get(note.stable_key)
            if spec is None:
                continue
            for block in note.blocks:
                if not block.evidence_block_ids and not block.asset_block_ids:
                    # A repaired statement still goes through the pro Judge;
                    # bind the allowed source catalog so it can assess the
                    # claim instead of failing before semantic review.
                    block.evidence_block_ids = list(spec.evidence_block_ids)
            ensure_required_visual_blocks(note, spec, vision_context)
        return KnowledgePlan(
            document_summary=original.document_summary,
            notes=repaired,
            skipped_sections=[],
        )

    def review_source_release(
        self,
        document: ParsedDocument,
        outline: SourceNoteOutlinePlan,
        note_set: NoteSetPlan,
        notes: list[NoteDraft],
    ) -> SourceReleaseReview:
        """Run one compact, document-level editorial consistency check."""

        specs = {item.stable_key: item for item in note_set.notes}
        outline_order = {
            node.stable_key: index for index, node in enumerate(outline.nodes)
        }
        note_order = {
            item.stable_key: index for index, item in enumerate(note_set.notes)
        }
        ordered_notes = sorted(
            notes,
            key=lambda note: (
                outline_order.get(
                    specs[note.stable_key].outline_node_key
                    if note.stable_key in specs
                    else "",
                    len(outline_order),
                ),
                note_order.get(note.stable_key, len(note_order)),
            ),
        )
        result = self._json_call(
            system=(
                f"{_prompt('source_release_judge.v2.md')}\n"
                f"{self._language_instruction}"
            ),
            user={
                "document": document.title,
                "source_note": {
                    "title": outline.title,
                    "summary": outline.summary,
                    "outline": [
                        {
                            "stable_key": node.stable_key,
                            "title": node.title,
                            "level": node.level,
                            "parent_key": node.parent_key,
                        }
                        for node in outline.nodes
                    ],
                },
                "notes": [
                    {
                        "stable_key": note.stable_key,
                        "outline_node_key": (
                            specs[note.stable_key].outline_node_key
                            if note.stable_key in specs
                            else ""
                        ),
                        "planned_title": (
                            specs[note.stable_key].title if note.stable_key in specs else ""
                        ),
                        "question": (
                            specs[note.stable_key].question_answered
                            if note.stable_key in specs
                            else ""
                        ),
                        "target_thesis": (
                            specs[note.stable_key].target_thesis
                            if note.stable_key in specs
                            else ""
                        ),
                        "title": note.title,
                        "thesis": note.thesis,
                        "body": "\n".join(
                            block.plain_text() for block in note.blocks if block.plain_text()
                        )[:1_200],
                    }
                    for note in ordered_notes
                ],
            },
            model=self.pro_model,
            operation="judge_source_release_v2",
            prompt_version="source-release-judge-v2",
        )
        review = SourceReleaseReview.model_validate(result)
        # Providers occasionally emit passed=true while also returning blocking
        # note/global issues. The issue lists are authoritative for release safety.
        review.passed = not review.note_issues and not review.global_issues
        return review

    def answer(self, question: str, hits: list[SearchHit]) -> Answer:
        if not hits:
            return Answer(answer="当前知识库没有足够证据回答这个问题。", hits=[])
        context = [
            {
                "note_id": hit.note_id,
                "title": hit.title,
                "thesis": hit.thesis,
                "body": hit.body,
                "evidence": hit.evidence,
                "assets": hit.assets,
            }
            for hit in hits
        ]
        response = self._json_call(
            system=(
                "只根据给定知识库证据回答。证据不足时明确说明。"
                "回答必须使用简体中文；关键专业术语首次出现时附原文括注。"
                '输出 JSON：{"answer":"...","used_note_ids":["..."],"coverage_warning":null}。'
            ),
            user={"question": question, "context": context},
        )
        # Empty ``used_note_ids`` is an explicit no-evidence/refusal signal,
        # not permission to cite every retrieved candidate. Missing or invalid
        # citation IDs likewise fail closed to protect provenance.
        raw_used = response.get("used_note_ids")
        used = (
            {
                str(note_id)
                for note_id in raw_used
                if str(note_id).strip()
            }
            if isinstance(raw_used, list)
            else set()
        )
        selected = [hit for hit in hits if hit.note_id in used]
        return Answer(
            answer=str(response.get("answer") or "当前知识库没有足够证据回答这个问题。"),
            hits=selected,
            coverage_warning=response.get("coverage_warning"),
        )

class HashEmbedding:
    """Small deterministic embedding for offline hybrid-search tests.

    It is intentionally an adapter, not a claim of semantic-model quality.
    """

    model_name = "local-hash-v1"

    def __init__(self, dimension: int = 256):
        self.dimension = dimension

    @staticmethod
    def _tokens(text: str) -> list[str]:
        latin = re.findall(r"[a-zA-Z0-9_]+", text.lower())
        chinese = re.findall(r"[\u4e00-\u9fff]", text)
        bigrams = ["".join(chinese[i : i + 2]) for i in range(max(0, len(chinese) - 1))]
        return latin + chinese + bigrams

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            counts: dict[str, int] = defaultdict(int)
            for token in self._tokens(text):
                counts[token] += 1
            for token, count in counts.items():
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest, "big") % self.dimension
                sign = 1.0 if digest[0] & 1 else -1.0
                vector[index] += sign * (1.0 + math.log(count))
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class OpenAIEmbedding:
    def __init__(self, settings: Settings, telemetry: TelemetryRecorder | None = None):
        if not settings.embedding_api_key or not settings.embedding_model:
            raise ValueError("An embedding API key and EMBEDDING_MODEL are required")
        kwargs: dict[str, Any] = {"api_key": settings.embedding_api_key}
        if settings.embedding_base_url:
            kwargs["base_url"] = settings.embedding_base_url
        self.client = OpenAI(**kwargs)
        self.model_name = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self.telemetry = telemetry
        self.batch_size = 10 if self.model_name == "text-embedding-v4" else 25

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            kwargs: dict[str, Any] = {"model": self.model_name, "input": batch}
            if self.model_name in {"text-embedding-v3", "text-embedding-v4"}:
                kwargs["dimensions"] = self.dimensions
            response = self.client.embeddings.create(**kwargs)
            vectors.extend(item.embedding for item in response.data)
        return vectors


def build_knowledge_agent(settings: Settings, telemetry: TelemetryRecorder | None = None):
    if settings.llm == "fixture":
        return FixtureKnowledgeAgent()
    if settings.llm in {"openai", "openai-compatible", "deepseek"}:
        return OpenAIKnowledgeAgent(settings, telemetry)
    raise ValueError(f"Unsupported NOODLE_LLM: {settings.llm}")


def build_embedding(settings: Settings, telemetry: TelemetryRecorder | None = None):
    if settings.embedding_model and settings.embedding_api_key:
        return OpenAIEmbedding(settings, telemetry)
    return HashEmbedding()
