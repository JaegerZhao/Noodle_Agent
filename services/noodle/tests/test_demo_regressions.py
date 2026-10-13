from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from noodle_agent.db import Database
from noodle_agent.telemetry import TelemetryRecorder


def _log_worker(directory, ready, proceed):
    recorder = TelemetryRecorder(
        Database(f"sqlite:///{Path(directory) / 'test.db'}"), Path(directory)
    )
    recorder.logger.info("deleted", extra={"detail": {"job_id": "deleted"}})
    ready.set()
    if proceed.wait(20):
        recorder.logger.info("retained", extra={"detail": {"job_id": "retained"}})


def test_log_purge_while_other_process_logger_is_alive(tmp_path):
    recorder = TelemetryRecorder(Database(f"sqlite:///{tmp_path / 'test.db'}"), tmp_path)
    ctx = multiprocessing.get_context("spawn")
    ready, proceed = ctx.Event(), ctx.Event()
    worker = ctx.Process(target=_log_worker, args=(str(tmp_path), ready, proceed))
    worker.start()
    try:
        assert ready.wait(20)
        recorder.purge_jobs(["deleted"])
        proceed.set()
        worker.join(20)
        assert worker.exitcode == 0
        records = [
            json.loads(line)
            for line in (tmp_path / "logs/noodle.jsonl").read_text(encoding="utf8").splitlines()
        ]
        assert [record["job_id"] for record in records] == ["retained"]
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join()
