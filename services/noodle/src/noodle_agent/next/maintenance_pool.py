"""Independent native consumers over the existing durable queue."""

import os
import threading

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
    def __init__(self, store, *, settings=None, stop=None, emit=lambda value: None, factory=None):
        self.store, self.settings = store, settings
        self.stop = stop if stop is not None else threading.Event()
        self.emit, self.factory = emit, factory
        self.errors, self.threads = [], []
        self.lock = threading.Lock()
        self.active = {}

    def start(self):
        for name, kinds in lanes().items():
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
                    result = app.run_once("next-" + name)
                    failures = 0
                    with self.lock:
                        self.active[name] = False
                    if result:
                        self.emit({"lane": name, **result})
                    else:
                        self.stop.wait(.2)
            except Exception as exc:  # noqa: BLE001 - isolate one failed lane, preserve other consumers
                from .repository import storage_busy
                if storage_busy(exc):
                    with self.lock:
                        self.active[name] = False
                    self.emit({'lane':name,'status':'LANE_WAITING_STORAGE','detail':str(exc)[:1000]})
                    self.stop.wait(1)
                    continue
                failures += 1
                error = {"lane": name, "type": type(exc).__name__, "detail": str(exc)[:2000], 'consecutive_failures': failures}
                with self.lock:
                    self.errors.append(error)
                    self.active[name] = False
                self.emit({**error, 'status': 'LANE_FAILED' if failures >= 3 else 'LANE_RESTARTING'})
                if failures >= 3:
                    return
                self.stop.wait(min(10, failures * 2))

    def close(self, timeout=2):
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=timeout)
