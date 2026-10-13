from __future__ import annotations

import json
import sqlite3
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from sqlalchemy import select

from ..config import Settings
from ..db import (
    AgentRun,
    AgentToolCall,
    AuditEvent,
    Conversation,
    ConversationMessage,
    new_id,
    utcnow,
)
from ..services import NoodleService
from .graphs.conversation import build_conversation_graph
from .harness.context import ContextBuilder
from .harness.models import AgentChatResult, RiskLevel, ToolIntent, ToolResult
from .harness.planner import ToolPlanner
from .harness.skills import SkillLoader
from .harness.tool_registry import ToolExecutionContext
from .tools.knowledge import build_core_tool_registry


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _parse_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class AgentHarness:
    """LangGraph runtime that governs model-selected calls into NoodleService."""

    def __init__(self, service: NoodleService, settings: Settings):
        self.service = service
        self.settings = settings
        self.registry = build_core_tool_registry(service)
        self.skills = SkillLoader(settings.skills_dir)
        self.context_builder = ContextBuilder(service.db, self.skills, self.registry)
        self.planner = ToolPlanner(settings)
        checkpoint_path = settings.data_dir / "agent_checkpoints.db"
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_connection = sqlite3.connect(
            checkpoint_path,
            check_same_thread=False,
        )
        self.checkpointer = SqliteSaver(self._checkpoint_connection)
        self.graph = build_conversation_graph(
            self.planner,
            self._execute_tool,
            self.checkpointer,
        )

    def close(self) -> None:
        self._checkpoint_connection.close()

    def _execute_tool(
        self, run_id: str, conversation_id: str, intent: ToolIntent
    ) -> ToolResult:
        with self.service.db.session() as session:
            run = session.get(AgentRun, run_id)
            run_context = _parse_json(run.context_json, {}) if run else {}
        context = ToolExecutionContext(
            run_id=run_id,
            conversation_id=conversation_id,
            allowed_risk_level=RiskLevel.EXTERNAL_REVERSIBLE,
            attachment_ids=tuple(run_context.get("attachment_ids") or []),
        )
        call_id = new_id()
        call_index = 0
        try:
            tool = self.registry.get(intent.tool_name)
        except ValueError:
            return ToolResult(
                status="FAILED",
                summary=f"Agent 选择了不存在的工具：{intent.tool_name}",
                error_code="UNKNOWN_TOOL",
            )
        with self.service.db.session() as session:
            last_index = session.scalar(
                select(AgentToolCall.call_index)
                .where(AgentToolCall.agent_run_id == run_id)
                .order_by(AgentToolCall.call_index.desc())
                .limit(1)
            )
            call_index = (last_index if last_index is not None else -1) + 1
            session.add(
                AgentToolCall(
                    id=call_id,
                    agent_run_id=run_id,
                    call_index=call_index,
                    tool_name=tool.name,
                    risk_level=tool.risk_level.value,
                    status="RUNNING",
                    arguments_json=_json(intent.arguments),
                )
            )
        try:
            result = self.registry.execute(intent.tool_name, intent.arguments, context)
        except Exception as exc:  # noqa: BLE001
            result = ToolResult(
                status="FAILED",
                summary=f"工具 {intent.tool_name} 执行失败：{exc}",
                error_code=exc.__class__.__name__,
                retryable=False,
            )
        compact_result = {
            "status": result.status,
            "summary": result.summary[:4_000],
            "data": result.data,
            "references": result.references,
            "error_code": result.error_code,
        }
        with self.service.db.session() as session:
            call = session.get(AgentToolCall, call_id)
            call.status = result.status
            call.result_json = _json(compact_result)
            call.error_code = result.error_code
            call.completed_at = utcnow()
            session.add(
                AuditEvent(
                    actor="noodle-agent",
                    action="AGENT_TOOL_CALL",
                    risk_level=tool.risk_level.value,
                    target_type="AgentToolCall",
                    target_id=call.id,
                    detail_json=_json(
                        {
                            "run_id": run_id,
                            "tool": tool.name,
                            "status": result.status,
                            "rationale": intent.rationale,
                        }
                    ),
                )
            )
        return result

    def chat(
        self,
        message: str,
        *,
        conversation_id: str | None = None,
        channel: str = "WEB",
        attachment_ids: list[str] | None = None,
        active_source_note_id: str | None = None,
    ) -> AgentChatResult:
        message = message.strip()
        if not message:
            raise ValueError("Message cannot be empty")
        attachment_ids = list(dict.fromkeys(attachment_ids or []))
        attachments = self.service.get_agent_attachments(attachment_ids)
        conversation_id = conversation_id or new_id()
        run_id = new_id()
        message_id = new_id()
        with self.service.db.session() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                conversation = Conversation(
                    id=conversation_id,
                    channel=channel,
                    title=message[:80],
                )
                session.add(conversation)
                session.flush()
            session.add(
                ConversationMessage(
                    id=message_id,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    role="user",
                    content=message,
                )
            )
            session.add(
                AgentRun(
                    id=run_id,
                    conversation_id=conversation_id,
                    status="RUNNING",
                    input_text=message,
                )
            )

        self.service.claim_agent_attachments(
            attachment_ids,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        ui_context = (
            {"active_source_note_id": active_source_note_id}
            if active_source_note_id
            else {}
        )
        context, skill, context_hash = self.context_builder.build(
            conversation_id,
            message,
            attachments=attachments,
            ui_context=ui_context,
        )
        with self.service.db.session() as session:
            run = session.get(AgentRun, run_id)
            run.selected_skill = skill.name if skill else None
            run.context_json = _json(
                {
                    "manifest_hash": context_hash,
                    "skill": skill.name if skill else None,
                    "tools": [item["name"] for item in context["tools"]],
                    "recent_message_count": len(context["recent_messages"]),
                    "attachment_ids": attachment_ids,
                    "attachment_count": len(attachments),
                    "active_source_note_id": active_source_note_id,
                }
            )

        try:
            state = self.graph.invoke(
                {
                    "run_id": run_id,
                    "conversation_id": conversation_id,
                    "context": context,
                    "skill": skill.name if skill else None,
                    "tool_intent": {},
                    "tool_result": {},
                    "answer": "",
                    "citations": [],
                    "step_count": 0,
                },
                config={"configurable": {"thread_id": conversation_id}},
            )
        except Exception as exc:
            with self.service.db.session() as session:
                run = session.get(AgentRun, run_id)
                run.status = "FAILED"
                run.error_code = exc.__class__.__name__
                run.error_detail = str(exc)
                run.completed_at = utcnow()
            raise

        answer = str(state.get("answer") or "Agent 没有生成回答。")
        with self.service.db.session() as session:
            run = session.get(AgentRun, run_id)
            run.status = "COMPLETED"
            run.final_answer = answer
            run.step_count = int(state.get("step_count") or 0)
            run.completed_at = utcnow()
            conversation = session.get(Conversation, conversation_id)
            conversation.updated_at = utcnow()
            session.add(
                ConversationMessage(
                    conversation_id=conversation_id,
                    run_id=run_id,
                    role="assistant",
                    content=answer,
                )
            )
        return AgentChatResult(
            conversation_id=conversation_id,
            run_id=run_id,
            answer=answer,
            skill=skill.name if skill else None,
            tool_calls=self._tool_calls(run_id),
            citations=list(state.get("citations") or []),
        )

    def _tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        with self.service.db.session() as session:
            calls = session.scalars(
                select(AgentToolCall)
                .where(AgentToolCall.agent_run_id == run_id)
                .order_by(AgentToolCall.call_index)
            ).all()
            return [
                {
                    "call_id": call.id,
                    "tool_name": call.tool_name,
                    "risk_level": call.risk_level,
                    "status": call.status,
                    "arguments": _parse_json(call.arguments_json, {}),
                    "result": _parse_json(call.result_json, {}),
                }
                for call in calls
            ]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.service.db.session() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                raise ValueError(f"Unknown agent run: {run_id}")
            return {
                "run_id": run.id,
                "conversation_id": run.conversation_id,
                "status": run.status,
                "input": run.input_text,
                "answer": run.final_answer,
                "skill": run.selected_skill,
                "context": _parse_json(run.context_json, {}),
                "step_count": run.step_count,
                "error_code": run.error_code,
                "error_detail": run.error_detail,
                "tool_calls": self._tool_calls(run_id),
            }

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        with self.service.db.session() as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                raise ValueError(f"Unknown conversation: {conversation_id}")
            messages = session.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conversation_id)
                .order_by(ConversationMessage.created_at)
            ).all()
            return {
                "conversation_id": conversation.id,
                "channel": conversation.channel,
                "title": conversation.title,
                "summary": conversation.summary,
                "messages": [
                    {
                        "message_id": item.id,
                        "run_id": item.run_id,
                        "role": item.role,
                        "content": item.content,
                    }
                    for item in messages
                ],
            }
