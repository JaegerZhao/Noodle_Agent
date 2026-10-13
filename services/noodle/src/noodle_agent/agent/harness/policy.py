from __future__ import annotations

from .models import PolicyDecision, RiskLevel


class PolicyEngine:
    """Deterministic permission boundary; the model cannot override it."""

    def evaluate(self, risk_level: RiskLevel, allowed_level: RiskLevel) -> PolicyDecision:
        if risk_level.rank <= allowed_level.rank:
            return PolicyDecision(allowed=True, reason=f"{risk_level} is within runtime scope")
        if risk_level.rank >= RiskLevel.HIGH_IMPACT.rank:
            return PolicyDecision(
                allowed=False,
                requires_approval=True,
                reason=f"{risk_level} requires explicit approval",
            )
        return PolicyDecision(
            allowed=False,
            reason=f"{risk_level} exceeds runtime scope {allowed_level}",
        )
