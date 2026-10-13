from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RiskLevel(StrEnum):
    READ_ONLY = "L0"
    INTERNAL_REVERSIBLE = "L1"
    EXTERNAL_REVERSIBLE = "L2"
    HIGH_IMPACT = "L3"
    IRREVERSIBLE = "L4"

    @property
    def rank(self) -> int:
        return int(self.value[1:])


class ToolIntent(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class ToolResult(BaseModel):
    status: str = "SUCCEEDED"
    summary: str
    data: dict[str, Any] | list[dict[str, Any]] = Field(default_factory=dict)
    references: list[dict[str, Any]] = Field(default_factory=list)
    error_code: str | None = None
    retryable: bool = False


class PolicyDecision(BaseModel):
    allowed: bool
    requires_approval: bool = False
    reason: str


class AgentChatResult(BaseModel):
    conversation_id: str
    run_id: str
    answer: str
    skill: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
