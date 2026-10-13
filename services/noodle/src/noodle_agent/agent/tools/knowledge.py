from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ..harness.models import RiskLevel, ToolResult
from ..harness.tool_registry import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
)
from .editing import register_editing_tools

if TYPE_CHECKING:
    from ...services import NoodleService


class EmptyInput(BaseModel):
    pass


class QuestionInput(BaseModel):
    question: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=10)


class SearchInput(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=10)


class NoteInput(BaseModel):
    note_id: str = Field(min_length=1)


class JobInput(BaseModel):
    job_id: str = Field(min_length=1)


def _citations(hits) -> list[dict]:
    citations = []
    for hit in hits:
        pages = sorted(
            {str(item["page_label"]) for item in hit.evidence},
            key=lambda value: (0, int(value)) if value.isdigit() else (1, value),
        )
        citations.append(
            {
                "note_id": hit.note_id,
                "title": hit.title,
                "pages": pages,
                "score": round(hit.score, 4),
            }
        )
    return citations


def build_core_tool_registry(service: NoodleService) -> ToolRegistry:
    registry = ToolRegistry()

    def answer(args: QuestionInput, _context: ToolExecutionContext) -> ToolResult:
        result = service.ask(args.question, limit=args.limit)
        return ToolResult(
            summary=result.answer,
            data={"coverage_warning": result.coverage_warning},
            references=_citations(result.hits),
        )

    def search(args: SearchInput, _context: ToolExecutionContext) -> ToolResult:
        hits = service.search(
            args.query,
            limit=args.limit,
            min_score=service.settings.qa_min_relevance_score,
        )
        references = _citations(hits)
        summary = (
            "找到以下相关笔记：" + "、".join(item["title"] for item in references)
            if references
            else "当前知识库没有找到相关笔记。"
        )
        return ToolResult(summary=summary, data={"hits": references}, references=references)

    def list_notes(_args: EmptyInput, _context: ToolExecutionContext) -> ToolResult:
        notes = service.list_notes()
        compact = [
            {"note_id": note["note_id"], "title": note["title"], "thesis": note["thesis"]}
            for note in notes
        ]
        summary = (
            f"当前共有 {len(compact)} 篇活动笔记："
            + "、".join(item["title"] for item in compact)
            if compact
            else "当前没有活动笔记。"
        )
        return ToolResult(summary=summary, data={"notes": compact})

    def get_note(args: NoteInput, _context: ToolExecutionContext) -> ToolResult:
        note = service.get_note(args.note_id)
        pages = sorted({str(item["page_label"]) for item in note["evidence"]})
        summary = f"{note['title']}\n\n{note['thesis']}\n\n" + "\n\n".join(
            block["text"] for block in note["blocks"]
        )
        return ToolResult(
            summary=summary,
            data={"note_id": note["note_id"], "title": note["title"]},
            references=[{"note_id": note["note_id"], "title": note["title"], "pages": pages}],
        )

    def get_job(args: JobInput, _context: ToolExecutionContext) -> ToolResult:
        job = service.get_job(args.job_id)
        return ToolResult(
            summary=(
                f"任务 {job['id']} 当前状态为 {job['status']}，"
                f"阶段 {job['stage']}，进度 {job['progress']:.0%}。"
            ),
            data=job,
        )

    for definition in (
        ToolDefinition(
            name="knowledge.answer",
            description="根据活动原子笔记和页级证据回答知识问题",
            input_model=QuestionInput,
            handler=answer,
            risk_level=RiskLevel.READ_ONLY,
        ),
        ToolDefinition(
            name="knowledge.search",
            description="搜索活动原子笔记并返回标题、分数和来源页码",
            input_model=SearchInput,
            handler=search,
            risk_level=RiskLevel.READ_ONLY,
        ),
        ToolDefinition(
            name="note.list",
            description="列出当前全部活动原子笔记",
            input_model=EmptyInput,
            handler=list_notes,
            risk_level=RiskLevel.READ_ONLY,
        ),
        ToolDefinition(
            name="note.get",
            description="按 note_id 读取原子笔记正文和证据页码",
            input_model=NoteInput,
            handler=get_note,
            risk_level=RiskLevel.READ_ONLY,
        ),
        ToolDefinition(
            name="job.get_status",
            description="按 job_id 查询持久化文档处理任务状态",
            input_model=JobInput,
            handler=get_job,
            risk_level=RiskLevel.READ_ONLY,
        ),
    ):
        registry.register(definition)
    register_editing_tools(registry, service)
    return registry
