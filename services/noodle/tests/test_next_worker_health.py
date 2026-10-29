"""Consumer recovery and observable stale heartbeats, without model calls."""

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from noodle_agent.next.maintenance_pool import MaintenancePool
from noodle_agent.next.worker_health import publish, read


def test_consumer_recovers_after_three_failures_without_losing_other_lanes():
    recovered, other = threading.Event(), threading.Event()
    attempts = []

    class Consumer:
        def __init__(self, kinds):
            if kinds[0] == 'REPAIR_ATOMIC':
                attempts.append(1)
                if len(attempts) <= 3:
                    raise RuntimeError('injected lane failure')
                recovered.set()
            else:
                other.set()

        def run_once(self, _):
            stop.wait(.01)

    stop = threading.Event()
    pool = MaintenancePool(None, stop=stop, factory=Consumer, retry_delay=lambda _: 0).start()
    try:
        assert recovered.wait(3) and other.wait(3)
        assert len(pool.errors) == 3
        assert all(s['alive'] for s in pool.snapshot().values())
    finally:
        pool.close()
    assert not any(t.is_alive() for t in pool.threads)


def test_health_distinguishes_healthy_recovering_stopped_and_stale(tmp_path):
    store = SimpleNamespace(path=lambda ref: tmp_path / ref)
    lanes = {'index': {'alive': True, 'status': 'IDLE'}}
    pool = SimpleNamespace(snapshot=lambda: lanes)
    assert read(store)['status'] == 'UNKNOWN'
    assert publish(store, pool)['status'] == 'READY'
    assert read(store)['status'] == 'READY'
    lanes['index']['status'] = 'RECOVERING'
    assert publish(store, pool)['status'] == 'DEGRADED'
    assert read(store, at=datetime.now(UTC) + timedelta(seconds=30))['status'] == 'STALE'
    publish(store, pool, stopped=True)
    assert read(store)['status'] == 'STOPPED'
