from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SCHEMA_VERSION = "concept-v3"

PROMPT_VERSION = "semantic-concept-writer-v5"

JUDGE_PROMPT_VERSION = "semantic-concept-module-judge-v2"

REPAIR_PROMPT_VERSION = "semantic-concept-module-repair-v1"


class ModuleReviewRequired(ValueError):
    """A source-backed candidate failed the semantic publication gate."""


class ConceptUpdateError(ValueError):
    """A persisted Concept update failed; callers can still inspect its Job."""

    def __init__(self, message: str, *, job_id: str, error_code: str) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.error_code = error_code


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    aliases = {
        "introduction": "overview",
        "discussion": "context-and-boundaries",
        "examples": "examples",
        "applications": "applications",
    }
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return aliases.get(normalized, normalized[:120] or "overview")


def _prefix_matches(number: str, prefix: str) -> bool:
    return number == prefix or number.startswith(prefix + ".")


def _prefix_depth(prefix: str) -> tuple[int, int]:
    return (prefix.count(".") + 1, len(prefix))


def _semantic_role(title: str, key: str) -> str:
    value = f"{title} {key}".casefold()
    if "example" in value or "application" in value:
        return "EXAMPLE" if "example" in value else "APPLICATION"
    if "limit" in value or "caveat" in value or "failure" in value:
        return "LIMITATION"
    if "compare" in value or " vs " in value or "versus" in value:
        return "COMPARISON"
    if key == "overview" or "introduction" in value:
        return "OVERVIEW"
    if "definition" in value or "what-is" in value:
        return "DEFINITION"
    return "MECHANISM"


def _collapsed_module(title: str) -> tuple[str, str, str]:
    """Collapse book-level micro-sections into reusable Concept module roles."""

    value = title.casefold()
    rules = [
        (("introduction", "definition", "basics"), "overview", "概述与定义", "OVERVIEW"),
        (("example", "worked"), "examples", "例子", "EXAMPLE"),
        (
            ("derivation", "algorithm", "fitting", "estimation", "learning", "training"),
            "mechanism-and-derivation",
            "机制与推导",
            "MECHANISM",
        ),
        (
            ("choosing", "tuning", "numerical", "computational", "speed", "scaling"),
            "implementation-and-selection",
            "实现与选择",
            "APPLICATION",
        ),
        (("comparison", "connection", " vs "), "comparisons", "比较与联系", "COMPARISON"),
        (("application",), "applications", "应用", "APPLICATION"),
        (
            ("limitation", "caution", "pros and cons", "curse", "failure", "unidentifiability"),
            "limitations",
            "边界与限制",
            "LIMITATION",
        ),
    ]
    for terms, key, label, role in rules:
        if any(term in value for term in terms):
            return key, label, role
    return "properties-and-variants", "关键性质与变体", "PROPERTIES"


MODULE_ROLE_ORDER = {
    "OVERVIEW": 0,
    "DEFINITION": 1,
    "INTUITION": 2,
    "MECHANISM": 3,
    "DERIVATION": 4,
    "PROPERTIES": 5,
    "COMPARISON": 6,
    "APPLICATION": 7,
    "EXAMPLE": 8,
    "LIMITATION": 9,
    "MISCONCEPTION": 10,
    "EXERCISE": 11,
}


def _module_title_hint(module_key: str) -> str:
    aliases = {
        "overview": "概述",
        "definition": "定义",
        "mechanism-and-derivation": "机制与推导",
        "properties-and-variants": "关键性质与变体",
        "implementation-and-selection": "实现与选择",
        "comparisons": "比较与联系",
        "applications": "应用",
        "examples": "例子",
        "limitations": "边界与限制",
        "exercises": "习题",
    }
    return aliases.get(module_key, module_key.replace("-", " "))


def _canonical_module_role(module_key: str) -> str:
    """Keep a shared module's role independent from textbook registration order."""

    roles = {
        "overview": "OVERVIEW",
        "definition": "DEFINITION",
        "mechanism-and-derivation": "MECHANISM",
        "properties-and-variants": "PROPERTIES",
        "implementation-and-selection": "APPLICATION",
        "comparisons": "COMPARISON",
        "applications": "APPLICATION",
        "examples": "EXAMPLE",
        "limitations": "LIMITATION",
        "exercises": "EXERCISE",
    }
    return roles.get(module_key, _semantic_role("", module_key))
