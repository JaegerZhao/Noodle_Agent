"""Independent native consumers over the existing durable queue."""

import os
import threading
from collections import deque

from .runtime import Application, backend_from_env


def lanes():
    result = {
        "analysis": ("REPAIR_ATOMIC", "RECONCILE_DEPENDENCIES", "ANALYZE_ATOMIC"),
        "index": ("INDEX_NOTE", "BUILD_INDEX"),
        "projection": ("UPDATE_MAPS", "REVIEW_ORGANIZATION"),
    }
    for index in range(max(1, int(os.getenv("NOODLE_NEXT_CONCEPT_WORKERS", "2")))):
        result[f"concept-{index}"] = ("BUILD_CONCEPT",)
    return result


class MaintenancePool:
    def __init__(self, store, *, settings=None, stop=None, emit=lambda value: None, factory=None,
                 retry_delay=lambda failures: min(30, 2 ** min(failures, 5))):
        self.store, self.settings = store, settings
        self.stop = stop if stop is not None else threading.Event()
        self.emit, self.factory = emit, factory
        self.errors, self.threads = deque(maxlen=50), []
        self.lock = threading.Lock()
        self.active = {}
        self.states = {}
        self.retry_delay = retry_delay

    def snapshot(self):
        with self.lock:
            live = {t.name.removeprefix('next-'): t.is_alive() for t in self.threads}
            return {name: {**state, 'alive': live.get(name, False)} for name, state in self.states.items()}

    def start(self):
        for name, kinds in lanes().items():
            with self.lock:
                self.states[name] = {'status': 'STARTING', 'consecutive_failures': 0}
            thread = threading.Thread(target=self._consume, args=(name, kinds), name="next-" + name, daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def _consume(self, name, kinds):
        failures = 0
        while not self.stop.is_set():
            try:
                app = self.factory(kinds) if self.factory else Application(self.store, settings=self.settings, backend=backend_from_env(), job_kinds=kinds)
                while not self.stop.is_set():
                    with self.lock:
                        self.active[name] = True
                        self.states[name] = {'status': 'RUNNING', 'consecutive_failures': failures}
                    result = app.run_once("next-" + name)
                    failures = 0
                    with self.lock:
                        self.active[name] = False
                        self.states[name] = {'status': 'IDLE', 'consecutive_failures': 0}
                    if result:
                        self.emit({"lane": name, **result})
                    else:
                        self.stop.wait(.2)
            except Exception as exc:  # noqa: BLE001 - isolate one failed lane, preserve other consumers
                from .repository import storage_busy
                if storage_busy(exc):
                    with self.lock:
                        self.active[name] = False
                        self.states[name] = {'status': 'WAITING_STORAGE', 'consecutive_failures': failures}
                    self.emit({'lane':name,'status':'LANE_WAITING_STORAGE','detail':str(exc)[:1000]})
                    self.stop.wait(1)
                    continue
                failures += 1
                error = {"lane": name, "type": type(exc).__name__, "detail": str(exc)[:2000], 'consecutive_failures': failures}
                with self.lock:
                    self.errors.append(error)
                    self.active[name] = False
                    self.states[name] = {'status': 'RECOVERING', 'consecutive_failures': failures,
                                         'error_type': type(exc).__name__}
                self.emit({**error, 'status': 'LANE_RECOVERING'})
                self.stop.wait(self.retry_delay(failures))

    def close(self, timeout=2):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=timeout)
