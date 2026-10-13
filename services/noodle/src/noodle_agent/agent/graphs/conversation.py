from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..harness.models import ToolIntent, ToolResult
from ..harness.planner import ToolPlanner


class ConversationState(TypedDict, total=False):
    run_id: str
    conversation_id: str
    context: dict[str, Any]
    skill: str | None
    tool_intent: dict[str, Any]
    tool_result: dict[str, Any]
    answer: str
    citations: list[dict[str, Any]]
    step_count: int


ToolExecutor = Callable[[str, str, ToolIntent], ToolResult]


def build_conversation_graph(planner: ToolPlanner, executor: ToolExecutor, checkpointer):
    def plan_node(state: ConversationState) -> dict[str, Any]:
        intent = planner.plan(state["context"])
        return {"tool_intent": intent.model_dump(), "step_count": 1}

    def execute_node(state: ConversationState) -> dict[str, Any]:
        intent = ToolIntent.model_validate(state["tool_intent"])
        result = executor(state["run_id"], state["conversation_id"], intent)
        return {"tool_result": result.model_dump()}

    def final_node(state: ConversationState) -> dict[str, Any]:
        result = ToolResult.model_validate(state["tool_result"])
        return {"answer": result.summary, "citations": result.references}

    graph = StateGraph(ConversationState)
    graph.add_node("plan", plan_node)
    graph.add_node("execute_tool", execute_node)
    graph.add_node("finalize", final_node)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "execute_tool")
    graph.add_edge("execute_tool", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer, name="noodle-conversation-agent")
