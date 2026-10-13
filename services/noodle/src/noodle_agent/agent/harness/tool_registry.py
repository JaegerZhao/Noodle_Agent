from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .models import PolicyDecision, RiskLevel, ToolResult
from .policy import PolicyEngine


@dataclass(frozen=True)
class ToolExecutionContext:
    run_id: str
    conversation_id: str
    allowed_risk_level: RiskLevel = RiskLevel.EXTERNAL_REVERSIBLE
    attachment_ids: tuple[str, ...] = ()


ToolHandler = Callable[[BaseModel, ToolExecutionContext], ToolResult]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    risk_level: RiskLevel = RiskLevel.READ_ONLY
    side_effect: bool = False
    idempotent: bool = True

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "risk_level": self.risk_level.value,
            "side_effect": self.side_effect,
            "idempotent": self.idempotent,
        }


class ToolRegistry:
    def __init__(self, policy: PolicyEngine | None = None):
        self._tools: dict[str, ToolDefinition] = {}
        self.policy = policy or PolicyEngine()

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool: {name}") from exc

    def manifests(self) -> list[dict[str, Any]]:
        return [self._tools[name].manifest() for name in sorted(self._tools)]

    def authorize(
        self, name: str, context: ToolExecutionContext
    ) -> tuple[ToolDefinition, PolicyDecision]:
        tool = self.get(name)
        return tool, self.policy.evaluate(tool.risk_level, context.allowed_risk_level)

    def execute(
        self, name: str, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        tool, decision = self.authorize(name, context)
        if not decision.allowed:
            code = "APPROVAL_REQUIRED" if decision.requires_approval else "POLICY_DENIED"
            return ToolResult(status="DENIED", summary=decision.reason, error_code=code)
        validated = tool.input_model.model_validate(arguments)
        return tool.handler(validated, context)
