from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy import select

from .db import Database, LlmCallEvent, StageEvent, VisionCallEvent, new_id, utcnow


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
        }
        detail = getattr(record, "detail", None)
        if isinstance(detail, dict):
            payload.update(detail)
        return _json(payload)


@contextmanager
def _shared_log_lock(log_dir: Path):
    """Coordinate append/rotation/purge across API and Worker processes."""
    with (log_dir / ".noodle-log.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class _SharedJsonFileHandler(RotatingFileHandler):
    def emit(self, record):
        with _shared_log_lock(Path(self.baseFilename).parent):
            try:
                super().emit(record)
            finally:
                # Release Windows handles between events so another process can
                # rotate or purge the file under the same interprocess lock.
                if self.stream:
                    self.stream.close()
                    self.stream = None


def build_json_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"noodle.telemetry.{log_dir.resolve()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = _SharedJsonFileHandler(
            log_dir / "noodle.jsonl",
            maxBytes=5 * 1024 * 1024,
            backupCount=4,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
    return logger


@dataclass(frozen=True)
class TelemetryContext:
    trace_id: str
    job_id: str | None = None
    stage: str | None = None
    note_key: str | None = None


_context: contextvars.ContextVar[TelemetryContext | None] = contextvars.ContextVar(
    "noodle_telemetry_context", default=None
)


class TelemetryRecorder:
    """Persists sanitized execution telemetry and mirrors it to rotating JSONL."""

    def __init__(self, database: Database, data_dir: Path):
        self.database = database
        self.log_dir = data_dir / "logs"
        self.logger = build_json_logger(self.log_dir)
        self._lock = threading.RLock()

    def purge_jobs(self, job_ids: list[str]) -> None:
        """Remove JSONL mirrors for deleted jobs; database telemetry is purged separately."""

        removed = set(job_ids)
        if not removed:
            return
        with self._lock, _shared_log_lock(self.log_dir):
            handlers = list(self.logger.handlers)
            for handler in handlers:
                handler.acquire()
                try:
                    handler.flush()
                    handler.close()
                finally:
                    handler.release()
            for path in self.log_dir.glob("noodle.jsonl*"):
                if not path.is_file() or path.name.endswith(".tmp"):
                    continue
                retained: list[str] = []
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        retained.append(line)
                        continue
                    if payload.get("job_id") not in removed:
                        retained.append(line)
                temporary = path.with_suffix(path.suffix + ".tmp")
                content = "\n".join(retained)
                temporary.write_text(
                    f"{content}\n" if content else "",
                    encoding="utf-8",
                )
                temporary.replace(path)

    def current(self) -> TelemetryContext:
        return _context.get() or TelemetryContext(trace_id=new_id())

    @contextmanager
    def bind(
        self,
        *,
        job_id: str | None = None,
        stage: str | None = None,
        note_key: str | None = None,
        trace_id: str | None = None,
    ) -> Iterator[TelemetryContext]:
        parent = self.current()
        bound = replace(
            parent,
            trace_id=trace_id or parent.trace_id,
            job_id=job_id if job_id is not None else parent.job_id,
            stage=stage if stage is not None else parent.stage,
            note_key=note_key if note_key is not None else parent.note_key,
        )
        token = _context.set(bound)
        try:
            yield bound
        finally:
            _context.reset(token)

    def transition_stage(
        self,
        job_id: str,
        stage: str,
        progress: float,
        detail: dict[str, Any] | None = None,
    ) -> str:
        now = utcnow()
        with self._lock, self.database.session() as session:
            running = session.scalar(
                select(StageEvent)
                .where(StageEvent.job_id == job_id, StageEvent.status == "RUNNING")
                .order_by(StageEvent.started_at.desc())
                .limit(1)
            )
            if running is not None and running.stage == stage:
                running.progress = progress
                running.detail_json = _json(detail or {})
                event_id = running.id
                trace_id = running.trace_id
            else:
                if running is not None:
                    running.status = "COMPLETED"
                    running.completed_at = now
                    running.duration_ms = max(
                        0, int((now - running.started_at).total_seconds() * 1000)
                    )
                trace_id = running.trace_id if running is not None else new_id()
                event = StageEvent(
                    trace_id=trace_id,
                    job_id=job_id,
                    stage=stage,
                    status="RUNNING",
                    progress=progress,
                    detail_json=_json(detail or {}),
                    started_at=now,
                )
                session.add(event)
                session.flush()
                event_id = event.id
        self._log(
            "stage.transition",
            trace_id=trace_id,
            job_id=job_id,
            stage=stage,
            progress=progress,
            detail=detail or {},
        )
        return event_id

    def finish_stage(
        self,
        job_id: str,
        *,
        status: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = utcnow()
        with self._lock, self.database.session() as session:
            event = session.scalar(
                select(StageEvent)
                .where(StageEvent.job_id == job_id, StageEvent.status == "RUNNING")
                .order_by(StageEvent.started_at.desc())
                .limit(1)
            )
            if event is None:
                return
            event.status = status
            event.completed_at = now
            event.duration_ms = max(0, int((now - event.started_at).total_seconds() * 1000))
            if detail:
                event.detail_json = _json(detail)
            log_detail = {
                "trace_id": event.trace_id,
                "job_id": job_id,
                "stage": event.stage,
                "status": status,
                "duration_ms": event.duration_ms,
            }
        self._log("stage.finished", **log_detail)

    def record_llm(
        self,
        *,
        operation: str,
        provider: str,
        model: str,
        prompt_version: str | None,
        input_text: str,
        output_text: str | None,
        status: str,
        started_at: datetime,
        elapsed_ms: int,
        usage: dict[str, int | None] | None = None,
        response_id: str | None = None,
        finish_reason: str | None = None,
        error: Exception | None = None,
    ) -> None:
        context = self.current()
        error_detail = str(error)[:2_000] if error else None
        if error_detail and finish_reason:
            error_detail = f"{error_detail}; finish_reason={finish_reason}"[:2_000]
        event = LlmCallEvent(
            trace_id=context.trace_id,
            job_id=context.job_id,
            stage=context.stage,
            note_key=context.note_key,
            operation=operation,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            status=status,
            input_sha256=sha256_text(input_text),
            output_sha256=sha256_text(output_text) if output_text is not None else None,
            input_chars=len(input_text),
            output_chars=len(output_text) if output_text is not None else None,
            prompt_tokens=(usage or {}).get("prompt_tokens"),
            completion_tokens=(usage or {}).get("completion_tokens"),
            total_tokens=(usage or {}).get("total_tokens"),
            response_id=response_id,
            error_code=error.__class__.__name__ if error else None,
            error_detail=error_detail,
            started_at=started_at,
            completed_at=utcnow(),
            duration_ms=elapsed_ms,
        )
        with self._lock, self.database.session() as session:
            session.add(event)
        self._log(
            "llm.call",
            trace_id=context.trace_id,
            job_id=context.job_id,
            stage=context.stage,
            note_key=context.note_key,
            operation=operation,
            model=model,
            status=status,
            duration_ms=elapsed_ms,
            total_tokens=event.total_tokens,
            input_chars=event.input_chars,
            output_chars=event.output_chars,
            finish_reason=finish_reason,
            error_code=event.error_code,
        )

    def record_vision(self, **values: Any) -> None:
        context = self.current()
        event = VisionCallEvent(
            trace_id=context.trace_id,
            job_id=context.job_id,
            stage=context.stage,
            note_key=context.note_key,
            **values,
        )
        with self._lock, self.database.session() as session:
            session.add(event)
        self._log(
            "vision.call",
            trace_id=context.trace_id,
            job_id=context.job_id,
            stage=context.stage,
            note_key=context.note_key,
            model=event.model,
            status=event.status,
            duration_ms=event.duration_ms,
            image_sha256=event.image_sha256,
            error_code=event.error_code,
        )

    def _log(self, event: str, **detail: Any) -> None:
        self.logger.info(event, extra={"event": event, "detail": detail})


def elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))
