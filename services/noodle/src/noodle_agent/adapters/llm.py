from __future__ import annotations

from typing import Any

import httpx
from openai import OpenAI

from ..config import Settings
from ..telemetry import TelemetryRecorder
from .knowledge.common import (
    HashEmbedding,
    OpenAIEmbedding,
    _loads_structured_json,
    _recover_structured_mojibake,
    compact_reference_ids,
)
from .knowledge.transport import TransportModule

__all__ = [
    "HashEmbedding",
    "OpenAIEmbedding",
    "OpenAIKnowledgeAgent",
    "_loads_structured_json",
    "_recover_structured_mojibake",
    "compact_reference_ids",
]


class OpenAIKnowledgeAgent:
    """Public service boundary; implementation is composed by responsibility."""

    name = "openai-compatible"

    def __init__(self, settings: Settings, telemetry: TelemetryRecorder | None = None):
        if not settings.llm_api_key or not settings.llm_fast_model:
            raise ValueError(
                "An LLM API key and fast model are required when using a real LLM"
            )
        timeout_seconds = float(
            getattr(settings, "llm_timeout_seconds", 180.0) or 180.0
        )
        kwargs: dict[str, Any] = {
            "api_key": settings.llm_api_key,
            "timeout": httpx.Timeout(
                timeout_seconds, connect=min(10.0, timeout_seconds)
            ),
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
            0, self._nonnegative_int_setting(settings, "generation_task_retries", 1)
        )
        self._transport_module = TransportModule(self)

    @staticmethod
    def _positive_int_setting(settings: Settings, name: str, default: int) -> int:
        return TransportModule._positive_int_setting(settings, name, default)

    @staticmethod
    def _nonnegative_int_setting(settings: Settings, name: str, default: int) -> int:
        return TransportModule._nonnegative_int_setting(settings, name, default)

    @property
    def _language_instruction(self) -> str:
        return self._transport_module._language_instruction

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
        return self._transport_module._json_call(
            system=system,
            user=user,
            model=model,
            operation=operation,
            prompt_version=prompt_version,
            thinking=thinking,
        )

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
        return self._transport_module.structured_json(
            system=system,
            user=user,
            operation=operation,
            prompt_version=prompt_version,
            use_pro_model=use_pro_model,
            thinking=thinking,
        )
