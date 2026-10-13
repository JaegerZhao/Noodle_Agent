from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..domain import (
    JobStatus,
)

TERMINAL_STATUSES = {
    JobStatus.COMPLETED.value,
    JobStatus.COMPLETED_WITH_WARNINGS.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
}

GENERATION_SCHEMA_VERSION = "source-note-generation-v7-occam"

KNOWLEDGE_MAP_CONTRACT_VERSION = "source-candidates-v2"

PLANNING_CONTRACT_VERSION = "source-plan-v2"

WRITING_CONTRACT_VERSION = "note-writer-v11"

JUDGE_CONTRACT_VERSION = "note-judge-v13"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _utc_iso(value: datetime | None) -> str | None:
    """Serialize the database's naive-UTC timestamps without timezone ambiguity."""
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _merged_event_duration_ms(events: list[Any]) -> int:
    """Return wall-clock model time, counting overlapping calls only once."""
    intervals: list[tuple[datetime, datetime]] = []
    for event in events:
        duration_ms = max(0, int(event.duration_ms or 0))
        if not event.started_at or duration_ms <= 0:
            continue
        intervals.append((event.started_at, event.started_at + timedelta(milliseconds=duration_ms)))
    if not intervals:
        return 0
    intervals.sort(key=lambda item: item[0])
    total_ms = 0
    current_start, current_end = intervals[0]
    for started_at, completed_at in intervals[1:]:
        if started_at <= current_end:
            current_end = max(current_end, completed_at)
            continue
        total_ms += int((current_end - current_start).total_seconds() * 1000)
        current_start, current_end = started_at, completed_at
    return total_ms + int((current_end - current_start).total_seconds() * 1000)


_WARNING_SEVERITIES = {"INFO", "WARNING", "ERROR"}

_INFO_WARNING_CODES = {"SKIPPED_SECTION"}


def _warning_severity(item: dict[str, Any]) -> str:
    """Return the public severity while remaining compatible with old rows."""
    explicit = str(item.get("severity") or "").upper()
    if explicit in _WARNING_SEVERITIES:
        return explicit
    return "INFO" if item.get("code") in _INFO_WARNING_CODES else "WARNING"


def _normalize_warnings(items: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in items if isinstance(items, list) else []:
        item = dict(raw) if isinstance(raw, dict) else {"detail": str(raw)}
        item["severity"] = _warning_severity(item)
        normalized.append(item)
    return normalized


def _has_material_warnings(items: list[dict[str, Any]]) -> bool:
    return any(_warning_severity(item) in {"WARNING", "ERROR"} for item in items)


def _public_job_status(status: str, warnings: list[dict[str, Any]]) -> str:
    """Downgrade historical info-only warning completions without mutating storage."""
    if (
        status == JobStatus.COMPLETED_WITH_WARNINGS.value
        and warnings
        and not _has_material_warnings(warnings)
    ):
        return JobStatus.COMPLETED.value
    return status


def _safe_filename(value: str, *, fallback: str = "note") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value).strip(" .-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:100].rstrip(" .-") or fallback


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


@lru_cache(maxsize=4096)
def _image_dimensions(path: str) -> dict[str, int]:
    import fitz

    try:
        image = fitz.Pixmap(path)
        return {"width": image.width, "height": image.height}
    except (OSError, RuntimeError, ValueError, fitz.mupdf.FzErrorBase):
        # Dimensions are optional layout metadata. A corrupt or unsupported
        # asset must not prevent reading an otherwise valid note.
        return {}
