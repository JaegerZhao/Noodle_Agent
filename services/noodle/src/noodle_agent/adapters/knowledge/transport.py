from __future__ import annotations

import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from ...composition import Component
from ...config import Settings
from ...domain import (
    Answer,
    SearchHit,
)
from ...telemetry import elapsed_ms
from .common import (
    _loads_structured_json,
    compact_reference_ids,
)


class TransportModule(Component):
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
        if self.owner.note_language.lower() in {"zh", "zh-cn", "chinese"}:
            return (
                "无论来源使用何种语言，标题、核心结论、正文和标签都必须使用简体中文。"
                "关键专业术语首次出现时写成“中文名称（Original Term）”，后续可只用中文或通用缩写；"
                "公式、代码、模型名、人名和论文名保留原文。"
            )
        return f"输出语言必须为 {self.owner.note_language}。"

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
        selected_model = model or self.owner.fast_model
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
            response = self.owner.client.chat.completions.create(**request)
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
            if self.owner.telemetry is not None:
                self.owner.telemetry.record_llm(
                    operation=operation
                    or self.owner.telemetry.current().stage
                    or "structured_json",
                    provider=self.owner.name,
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

        if self.owner.telemetry is not None:
            self.owner.telemetry.record_llm(
                operation=operation or self.owner.telemetry.current().stage or "structured_json",
                provider=self.owner.name,
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
        return self.owner._json_call(
            system=system,
            user=user,
            model=self.owner.pro_model if use_pro_model else self.owner.fast_model,
            operation=operation,
            prompt_version=prompt_version,
            thinking=thinking,
        )

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
        response = self.owner._json_call(
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
            {str(note_id) for note_id in raw_used if str(note_id).strip()}
            if isinstance(raw_used, list)
            else set()
        )
        selected = [hit for hit in hits if hit.note_id in used]
        return Answer(
            answer=str(response.get("answer") or "当前知识库没有足够证据回答这个问题。"),
            hits=selected,
            coverage_warning=response.get("coverage_warning"),
        )
