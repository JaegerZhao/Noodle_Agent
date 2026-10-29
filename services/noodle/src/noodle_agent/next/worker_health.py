"""Local worker heartbeat; liveness is separate from knowledge readiness."""

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

from .contracts import canonical

REF = 'artifacts/runtime/worker-health.json'


def publish(store, pool, *, stopped=False):
    lanes = pool.snapshot()
    status = 'STOPPED' if stopped else 'DEGRADED' if any(
        not lane['alive'] or lane['status'] == 'RECOVERING' for lane in lanes.values()) else 'READY'
    value = {'status': status, 'pid': os.getpid(), 'sampled_at': datetime.now(UTC).isoformat(), 'lanes': lanes}
    target = store.path(REF)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.' + str(uuid4()) + '.tmp')
    try:
        temporary.write_text(canonical(value), encoding='utf8')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def read(store, *, at=None, stale_seconds=20):
    try:
        value = json.loads(store.path(REF).read_text(encoding='utf8'))
        age = ((at or datetime.now(UTC)) - datetime.fromisoformat(value['sampled_at'])).total_seconds()
        if age > stale_seconds or age < -5:
            return {**value, 'status': 'STALE', 'age_seconds': max(0, round(age))}
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {'status': 'UNKNOWN', 'lanes': {}}
