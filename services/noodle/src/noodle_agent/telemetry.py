"""Provider telemetry protocol; persistence belongs to next.audit.ProviderAudit."""

import hashlib
from time import perf_counter
from typing import Any, Protocol


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def elapsed_ms(started: float) -> int:
    return int((perf_counter() - started) * 1000)


class TelemetryRecorder(Protocol):
    def current(self) -> Any: ...
    def record_llm(self, **details) -> Any: ...
    def record_vision(self, **details) -> Any: ...
    def record_embedding(self, **details) -> Any: ...
