"""Process-wide request slots; every worker still owns its provider and audit context."""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from threading import BoundedSemaphore
from time import perf_counter

_total = BoundedSemaphore(max(1, int(os.getenv("NOODLE_NEXT_LLM_CONCURRENCY", "4"))))
_atomic = BoundedSemaphore(max(1, int(os.getenv("NOODLE_NEXT_ATOMIC_CONCURRENCY", "3"))))
_maintenance = BoundedSemaphore(max(1, int(os.getenv("NOODLE_NEXT_CONCEPT_CONCURRENCY", "2"))))
trace = ContextVar("provider_trace", default=None)
call_guard = ContextVar('maintenance_call_guard', default=None)


def stage_for(operation):
    if operation.startswith("next.structure"):
        return "structure"
    if operation.startswith(("next.section_", "next.assembly_")):
        return "atomic"
    if operation.startswith("next.entity_plan"):
        return "routing"
    if operation.startswith("next.entity_"):
        return "concept"
    if operation.startswith("next.organization_"):
        return "organization"
    if operation.startswith("next.answer"):
        return "answer"
    return "other"


@contextmanager
def request_slot(operation):
    started = perf_counter()
    stage = stage_for(operation)
    lane = _atomic if stage in {"atomic", "structure"} else _maintenance
    with lane, _total:
        token = trace.set({"stage": stage, "queue_wait_ms": round((perf_counter() - started) * 1000, 3)})
        try:
            yield
        finally:
            trace.reset(token)


class LimitedAgent:
    def __init__(self, agent):
        self.agent = agent
        self.agent.next_json_closure = True

    def __getattr__(self, name):
        return getattr(self.agent, name)

    def structured_json(self, **kwargs):
        guard = call_guard.get()
        if guard:
            guard(kwargs)
        with request_slot(kwargs["operation"]):
            return self.agent.structured_json(**kwargs)
