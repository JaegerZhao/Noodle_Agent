from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from typing import Any

from openai import OpenAI

from ...config import Settings
from ...telemetry import TelemetryRecorder

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


class TruncatedStructuredResponse(ValueError):
    """A completed provider request whose structured output hit its token limit."""


def _escape_invalid_json_slashes(text: str) -> str:
    """Normalize lone TeX slashes inside strings, consuming valid pairs once."""
    latex = {
        "bar",
        "breve",
        "beta",
        "big",
        "biggl",
        "biggr",
        "bigl",
        "bigr",
        "binom",
        "bmod",
        "boldsymbol",
        "bold",
        "bm",
        "bf",
        "boxed",
        "begin",
        "frac",
        "forall",
        "nabla",
        "neq",
        "ne",
        "nu",
        "notin",
        "neg",
        "nonumber",
        "rho",
        "right",
        "rangle",
        "rceil",
        "rfloor",
        "text",
        "textbf",
        "textit",
        "textstyle",
        "times",
        "theta",
        "tau",
        "tilde",
        "tag",
        "tanh",
        "tan",
        "to",
        "top",
        "tfrac",
    }
    result, inside, index, math_mode = [], False, 0, False
    while index < len(text):
        char = text[index]
        if char == '"':
            inside = not inside
            math_mode = False
        if inside and char == "$":
            width = 2 if text[index : index + 2] == "$$" else 1
            result.append("$" * width)
            math_mode = not math_mode
            index += width
            continue
        if inside and char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            if following in ('"', "\\", "/"):
                result.append(text[index : index + 2])
                if following == "\\" and text[index + 2 : index + 3] in ("[", "("):
                    math_mode = True
                elif following == "\\" and text[index + 2 : index + 3] in ("]", ")"):
                    math_mode = False
                index += 2
                continue
            if following == "u" and re.fullmatch(
                "[0-9a-fA-F]{4}", text[index + 2 : index + 6]
            ):
                result.append(text[index : index + 6])
                index += 6
                continue
            command = re.match("[a-zA-Z]+", text[index + 1 :])
            literal_latex = (
                command
                and command[0] in latex
                and (math_mode or following in ("b", "f"))
            )
            if following in ("b", "f", "n", "r", "t") and not literal_latex:
                result.append(text[index : index + 2])
                index += 2
                continue
            if following in ("[", "("):
                math_mode = True
            elif following in ("]", ")"):
                math_mode = False
            result.append("\\\\")
            result.append(following)
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _loads_structured_json(content: str, *, normalizations=None) -> dict[str, Any]:
    r"""Parse structured LLM output while tolerating unescaped LaTeX slashes.

    Some OpenAI-compatible providers occasionally return JSON strings containing
    LaTeX such as ``\alpha`` or ``\(`` with a single backslash. Those sequences
    are invalid JSON even though the surrounding object is otherwise usable.
    Correctly escaped pairs are consumed once. Lone TeX commands in math are
    preserved even when JSON would interpret them as tab/newline controls.
    Redundant closing object braces are accepted; other syntax errors fail.
    """

    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1]).strip()
    repaired = _escape_invalid_json_slashes(stripped)
    normalized = repaired
    try:
        result = json.loads(repaired)
    except json.JSONDecodeError as exc:
        # A complete dictionary followed only by redundant outer braces is
        # recoverable. Never discard values, a second object or trailing prose.
        if exc.msg != "Extra data" or not re.fullmatch(
            r"\}+", repaired[exc.pos :].strip()
        ):
            raise
        normalized = repaired[: exc.pos]
        result = json.loads(normalized)
    if not isinstance(result, dict):
        raise TypeError("LLM structured response must be a JSON object")
    if normalizations is not None and normalized != stripped:
        normalizations.append(
            {
                "policy": "json-lexical-v1",
                "normalized": normalized,
                "slashes": repaired != stripped,
                "redundant_closing_brace": normalized != repaired,
            }
        )
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
                in {
                    "contribution_id",
                    "evidence_refs",
                    "evidence_ref",
                    "allowed_contribution_ids",
                }
                else "B"
                if key in {"note_block_id", "note_block_ids"}
                else "E"
                if key
                in {
                    "evidence_span_id",
                    "evidence_span_ids",
                    "allowed_evidence_span_ids",
                }
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
            return re.sub(
                r"\bREF_[CBENA]\d+\b",
                lambda m: reverse.get(m.group(0), m.group(0)),
                obj,
            )
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
            from datetime import UTC, datetime
            from time import perf_counter

            start = perf_counter()
            started_at = datetime.now(UTC)
            try:
                response = self.client.embeddings.create(**kwargs)
            except Exception:
                if self.telemetry and hasattr(self.telemetry, "record_embedding"):
                    self.telemetry.record_embedding(
                        operation=getattr(self, "operation", "next.embedding"),
                        model=self.model_name,
                        started_at=started_at,
                        elapsed_ms=int((perf_counter() - start) * 1000),
                        status="FAILED",
                        input_text=json.dumps(batch),
                        usage=None,
                    )
                raise
            if self.telemetry and hasattr(self.telemetry, "record_embedding"):
                self.telemetry.record_embedding(
                    operation=getattr(self, "operation", "next.embedding"),
                    model=self.model_name,
                    started_at=started_at,
                    elapsed_ms=int((perf_counter() - start) * 1000),
                    status="COMPLETED",
                    input_text=json.dumps(batch),
                    usage=response.usage,
                )
            vectors.extend(item.embedding for item in response.data)
        return vectors
