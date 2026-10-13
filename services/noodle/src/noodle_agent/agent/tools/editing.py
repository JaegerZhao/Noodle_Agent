from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

from ..harness.models import RiskLevel, ToolResult
from ..harness.tool_registry import ToolDefinition, ToolExecutionContext, ToolRegistry

if TYPE_CHECKING:
    from ...services import NoodleService


class ReplaceSourceImagesInput(BaseModel):
    attachment_id: str = Field(min_length=1)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    source_note_id: str | None = None
    source_ref: str | None = None
    target_note_title: str | None = None
    caption: str | None = None
    purpose: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> ReplaceSourceImagesInput:
        if self.page_end < self.page_start:
            raise ValueError("page_end must be greater than or equal to page_start")
        if not self.source_note_id and not self.source_ref:
            raise ValueError("source_note_id or source_ref is required")
        return self


def register_editing_tools(registry: ToolRegistry, service: NoodleService) -> None:
    def replace_source_images(
        args: ReplaceSourceImagesInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        if args.attachment_id not in context.attachment_ids:
            return ToolResult(
                status="DENIED",
                summary="只能使用当前用户消息中明确附带的图片。",
                error_code="ATTACHMENT_SCOPE_DENIED",
            )
        result = service.replace_source_note_images(
            attachment_id=args.attachment_id,
            page_start=args.page_start,
            page_end=args.page_end,
            source_note_id=args.source_note_id,
            source_ref=args.source_ref,
            target_note_title=args.target_note_title,
            caption=args.caption,
            purpose=args.purpose,
            actor="user-via-agent",
        )
        if result["status"] == "ALREADY_APPLIED":
            summary = (
                f"这张图片已经应用到《{result['source_title']}》的"
                f" P{result['page_start']}–P{result['page_end']}，未重复创建版本。"
            )
        else:
            summary = (
                f"已修改《{result['source_title']}》：移除 P{result['page_start']}–"
                f"P{result['page_end']} 的 {result['removed_visual_count']} 个图片碎片，"
                f"并在“{result['target_note_title']}”中插入 1 张完整图片。"
                f"已发布 SourceNote v{result['source_note_version_no']}，旧版本仍可回滚。"
            )
        return ToolResult(
            summary=summary,
            data=result,
            references=[
                {
                    "source_note_id": result["source_note_id"],
                    "note_id": result.get("target_note_id"),
                    "title": result["source_title"],
                    "pages": [
                        str(page)
                        for page in range(result["page_start"], result["page_end"] + 1)
                    ],
                }
            ],
        )

    registry.register(
        ToolDefinition(
            name="note.replace_source_images",
            description=(
                "用当前消息附件中的一张完整图片，替换指定 SourceNote 页码范围内的"
                "MinerU 图片碎片；创建新的 AtomicNote 与 SourceNote 版本，旧版本不删除"
            ),
            input_model=ReplaceSourceImagesInput,
            handler=replace_source_images,
            risk_level=RiskLevel.INTERNAL_REVERSIBLE,
            side_effect=True,
            idempotent=True,
        )
    )
