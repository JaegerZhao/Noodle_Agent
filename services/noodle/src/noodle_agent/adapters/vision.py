from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import time
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx
from openai import OpenAI

from ..config import Settings
from ..domain import VisualUnderstanding
from ..telemetry import TelemetryRecorder, elapsed_ms, sha256_text
from .llm import _loads_structured_json


class DisabledVisionProvider:
    name = "disabled"
    model_name = "disabled"
    enabled = False


class DoubaoVisionProvider:
    name = "doubao-ark"
    enabled = True
    prompt_version = "visual-understanding-v5"

    def __init__(self, settings: Settings, telemetry: TelemetryRecorder):
        if not settings.vision_api_key:
            raise ValueError("DOUBAO_API_KEY or VISION_API_KEY is required")
        self.model_name = settings.vision_model
        self.reasoning_effort = settings.vision_reasoning_effort
        self.max_output_tokens = settings.vision_max_output_tokens
        self.telemetry = telemetry
        self.client = OpenAI(
            api_key=settings.vision_api_key,
            base_url=settings.vision_base_url,
            timeout=httpx.Timeout(
                settings.vision_timeout_seconds,
                connect=min(10.0, settings.vision_timeout_seconds),
            ),
            max_retries=settings.vision_max_retries,
        )
        self.system_prompt = files("noodle_agent").joinpath(
            "prompts", "visual_understanding.v5.md"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _image_data_url(path: Path) -> tuple[str, str]:
        payload = path.read_bytes()
        media_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(payload).decode("ascii")
        return f"data:{media_type};base64,{encoded}", hashlib.sha256(payload).hexdigest()

    def analyze(
        self,
        image_path: Path,
        *,
        external_block_id: str,
        visual_asset_id: str = "",
        context: dict[str, Any] | None = None,
    ) -> VisualUnderstanding:
        return self.analyze_batch(
            [
                {
                    "image_path": image_path,
                    "external_block_id": external_block_id,
                    "visual_asset_id": visual_asset_id,
                    "context": context or {},
                }
            ]
        )[external_block_id]

    def analyze_batch(self, items: list[dict[str, Any]]) -> dict[str, VisualUnderstanding]:
        """Analyze a small image batch with one compact reasoning request.

        Same-page MinerU fragments stay adjacent in the request so the model can
        recognize their shared teaching role without paying for one request per
        fragment.
        """

        if not items:
            return {}
        encoded: list[dict[str, Any]] = []
        for item in items:
            image_url, image_sha256 = self._image_data_url(Path(item["image_path"]))
            encoded.append(
                {
                    **item,
                    "image_url": image_url,
                    "image_sha256": image_sha256,
                }
            )
        compact_context = [
            {
                "asset_id": item["external_block_id"],
                "context": item.get("context") or {},
            }
            for item in encoded
        ]
        user_text = (
            "逐张分析下面图片。必须为每个 asset_id 返回一个 images 数组元素。\n"
            f"图片上下文：{json.dumps(compact_context, ensure_ascii=False)}"
        )
        content_parts: list[dict[str, Any]] = [
            {"type": "text", "text": user_text}
        ]
        page_contexts = {str(item["page_context_path"]) for item in encoded if item.get("page_context_path")}
        for path in sorted(page_contexts):
            page_url, _ = self._image_data_url(Path(path))
            content_parts.extend([
                {"type": "text", "text": "以下为原始整页上下文，不是额外 asset。请核对裁切图的面板标签、方向和相互关系，仍只返回输入 asset_id。"},
                {"type": "image_url", "image_url": {"url": page_url}},
            ])
        for item in encoded:
            content_parts.extend(
                [
                    {
                        "type": "text",
                        "text": f"asset_id={item['external_block_id']}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": item["image_url"]},
                    },
                ]
            )
        batch_sha256 = hashlib.sha256(
            "|".join(item["image_sha256"] for item in encoded).encode("ascii")
        ).hexdigest()
        started_clock = perf_counter()
        started_at = datetime.now(UTC).replace(tzinfo=None)
        response = None
        content: str | None = None
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {
                        "role": "user",
                        "content": content_parts,
                    },
                ],
                response_format={"type": "json_object"},
                reasoning_effort=self.reasoning_effort,
                max_tokens=self.max_output_tokens,
            )
            content = response.choices[0].message.content
            if not content:
                raise RuntimeError("Vision model returned an empty response")
            result = _loads_structured_json(content)
            raw_images = result.get("images")
            if not isinstance(raw_images, list) and len(encoded) == 1:
                raw_images = [{**result, "asset_id": encoded[0]["external_block_id"]}]
            if not isinstance(raw_images, list):
                raise TypeError("Vision batch response has no images array")
            if any(not isinstance(raw, dict) for raw in raw_images):
                raise TypeError("Vision images must be objects")
            ids = [str(raw.get("asset_id") or "") for raw in raw_images]
            expected = {str(item["external_block_id"]) for item in encoded}
            if len(ids) != len(set(ids)) or set(ids) != expected:
                raise ValueError("Vision asset IDs must match the batch exactly")
            by_id = dict(zip(ids, raw_images, strict=True))
            understandings: dict[str, VisualUnderstanding] = {}
            for item in encoded:
                external_id = str(item["external_block_id"])
                raw = by_id.get(external_id)
                if raw is None:
                    raise RuntimeError(f"Vision batch omitted {external_id}")
                is_fragment = bool(raw.get("is_fragment", False))
                fragment_group = str(raw.get("fragment_group") or "")
                understandings[external_id] = VisualUnderstanding(
                    visual_asset_id=str(item.get("visual_asset_id") or ""),
                    external_block_id=external_id,
                    image_sha256=item["image_sha256"],
                    model=self.model_name,
                    prompt_version=self.prompt_version,
                    asset_type=str(raw.get("type") or raw.get("asset_type") or "OTHER"),
                    is_decorative=bool(
                        raw.get("decorative", raw.get("is_decorative", False))
                    ),
                    essential=bool(raw.get("essential", False)),
                    caption=str(raw.get("caption") or ""),
                    teaching_role=str(raw.get("role") or raw.get("teaching_role") or ""),
                    ocr_text=raw.get("ocr_text") or [],
                    factual_observations=raw.get("factual_observations") or [],
                    spatial_relations=raw.get("spatial_relations") or [],
                    suggested_placement=str(raw.get("suggested_placement") or ""),
                    confidence=float(raw.get("confidence") or 0.0),
                    analysis_tier="API_FAST",
                    route_reason="planner-selected batch analysis",
                    is_fragment=is_fragment,
                    fragment_group=fragment_group,
                    group_id=str(raw.get("group_id") or fragment_group),
                    group_type=str(
                        raw.get("group_type")
                        or ("FRAGMENT" if is_fragment else "SINGLE")
                    ),
                    related_asset_ids=[str(value) for value in raw.get("related_asset_ids") or []],
                )
        except Exception as exc:
            self._record(
                visual_asset_id=None,
                image_sha256=batch_sha256,
                input_text=user_text,
                output_text=content,
                response=response,
                status="FAILED",
                started_at=started_at,
                elapsed=elapsed_ms(started_clock),
                error=exc,
            )
            raise
        self._record(
            visual_asset_id=None,
            image_sha256=batch_sha256,
            input_text=user_text,
            output_text=content,
            response=response,
            status="COMPLETED",
            started_at=started_at,
            elapsed=elapsed_ms(started_clock),
        )
        return understandings

    def _record(
        self,
        *,
        visual_asset_id: str | None,
        image_sha256: str,
        input_text: str,
        output_text: str | None,
        response,
        status: str,
        started_at: datetime,
        elapsed: int,
        error: Exception | None = None,
    ) -> None:
        usage_obj = getattr(response, "usage", None)
        self.telemetry.record_vision(
            visual_asset_id=visual_asset_id or None,
            operation="understand_course_visual",
            provider=self.name,
            model=self.model_name,
            prompt_version=self.prompt_version,
            status=status,
            image_sha256=image_sha256,
            input_sha256=sha256_text(input_text),
            output_sha256=sha256_text(output_text) if output_text is not None else None,
            prompt_tokens=getattr(usage_obj, "prompt_tokens", None),
            completion_tokens=getattr(usage_obj, "completion_tokens", None),
            total_tokens=getattr(usage_obj, "total_tokens", None),
            response_id=getattr(response, "id", None),
            error_code=error.__class__.__name__ if error else None,
            error_detail=str(error)[:2_000] if error else None,
            started_at=started_at,
            completed_at=datetime.now(UTC).replace(tzinfo=None),
            duration_ms=elapsed,
        )


def build_vision_provider(settings: Settings, telemetry: TelemetryRecorder):
    if settings.vision_api_key:
        return DoubaoVisionProvider(settings, telemetry)
    return DisabledVisionProvider()


def resilient_vision_batch(
    items: list[dict[str, Any]],
    call: Callable[[list[dict[str, Any]]], dict[str, VisualUnderstanding]],
    *, max_retries: int, sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, VisualUnderstanding], list[dict[str, str]]]:
    """Split malformed multi-image responses; retain successful child results."""
    if not items:
        return {}, []
    error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return call(items), []
        except Exception as exc:  # noqa: BLE001
            error = exc
            if len(items) > 1 and isinstance(exc, (ValueError, TypeError)):
                break
            if attempt < max_retries:
                sleep(min(2.0 ** attempt, 8.0))
    if len(items) > 1:
        middle = len(items) // 2
        left, lf = resilient_vision_batch(items[:middle], call, max_retries=max_retries, sleep=sleep)
        right, rf = resilient_vision_batch(items[middle:], call, max_retries=max_retries, sleep=sleep)
        return {**left, **right}, [*lf, *rf]
    return {}, [{"block_id": str(items[0]["external_block_id"]), "error": str(error)[:500]}]
