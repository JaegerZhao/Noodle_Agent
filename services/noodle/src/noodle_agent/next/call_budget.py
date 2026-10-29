"""A logical work unit cannot multiply independent retry loops indefinitely."""

from contextlib import contextmanager
from contextvars import ContextVar

_budget = ContextVar("model_call_budget", default=None)


@contextmanager
def model_budget(limit=8):
    token = _budget.set({"remaining": limit})
    try:
        yield
    finally:
        _budget.reset(token)


def charge():
    value = _budget.get()
    if value is not None:
        if value["remaining"] <= 0:
            value.setdefault('threshold_exceeded', True)
        value["remaining"] -= 1
