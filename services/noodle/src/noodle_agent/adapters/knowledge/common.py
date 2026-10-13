from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from importlib.resources import files
from typing import Any

from openai import OpenAI

from ...config import Settings
from ...domain import (
    Answer,
    DocumentProfile,
    KnowledgeMap,
    KnowledgePlan,
    NoteBlockDraft,
    NoteDraft,
    ParsedBlock,
    ParsedDocument,
    SearchHit,
)
from ...telemetry import TelemetryRecorder


def _slug(value: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "-", value.lower(), flags=re.UNICODE).strip("-")
    return normalized[:120] or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _sections(document: ParsedDocument) -> list[tuple[str, list[ParsedBlock]]]:
    content_blocks = [
        block
        for block in document.blocks
        if block.block_type not in {"HEADER", "FOOTER", "PAGE_NUMBER"}
    ]
    headings = [block for block in content_blocks if block.block_type in {"TITLE", "HEADING"}]
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
            "text": (block.text[:max_text_chars] if max_text_chars is not None else block.text),
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


_REFERENCE_KEYS = {
    "contribution_id",
    "note_version_id",
    "note_block_id",
    "evidence_span_id",
    "asset_id",
    "evidence_ref",
    "evidence_refs",
    "note_block_ids",
    "evidence_span_ids",
    "asset_ids",
    "allowed_contribution_ids",
    "allowed_evidence_span_ids",
}


def compact_reference_ids(value):
    """Ephemeral aliases reduce token use; persisted outputs are decoded to real IDs."""
    forward = {}

    def encode(obj, key=""):
        if isinstance(obj, dict):
            return {k: encode(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [encode(v, key) for v in obj]
        if isinstance(obj, str) and key in _REFERENCE_KEYS and len(obj) == 36:
            kind = (
                "C"
                if key
                in {"contribution_id", "evidence_refs", "evidence_ref", "allowed_contribution_ids"}
                else "B"
                if key in {"note_block_id", "note_block_ids"}
                else "E"
                if key in {"evidence_span_id", "evidence_span_ids", "allowed_evidence_span_ids"}
                else "A"
                if key in {"asset_id", "asset_ids"}
                else "N"
            )
            forward.setdefault(obj, f"REF_{kind}{len(forward) + 1}")
            return forward[obj]
        return obj

    encoded = encode(value)
    reverse = {v: k for k, v in forward.items()}

    def decode(obj, key=""):
        if isinstance(obj, dict):
            return {k: decode(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [decode(v, key) for v in obj]
        if isinstance(obj, str):
            # Messages about references must keep their meaning across model calls.
            return re.sub(r"\bREF_[CBENA]\d+\b", lambda m: reverse.get(m.group(0), m.group(0)), obj)
        return obj

    return encoded, decode


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
