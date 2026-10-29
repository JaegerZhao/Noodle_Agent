"""Replay the prior failed dependency snapshot through ordinary system events."""

import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/noodle/src"))
from noodle_agent.config import Settings
from noodle_agent.next.convergence import convergence_report
from noodle_agent.next.events import request_reconcile
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store

base = Path(".local-only/entity-concepts-2026-10-23/final/library")
root = Path(".local-only/ee6405-staged-2026-10-23/historical-dependency")
target = root / "library"
root.mkdir(parents=True, exist_ok=True)
if not target.exists():
    shutil.copytree(
        base, target, ignore=shutil.ignore_patterns("noodle.db-wal", "noodle.db-shm")
    )
    original = sqlite3.connect(
        (base / "noodle.db").resolve().as_uri() + "?mode=ro", uri=True
    )
    copy = sqlite3.connect(target / "noodle.db")
    original.backup(copy)
    copy.close()
    original.close()
s = Store(target)
try:
    before = convergence_report(s)
    prior = (
        json.loads((root / "result.json").read_text(encoding="utf-8"))
        if (root / "result.json").exists()
        else None
    )
    with s.db.engine.connect() as c:
        usage_before = c.exec_driver_sql(
            "SELECT coalesce(sum(total_tokens),0) FROM provider_calls"
        ).scalar_one()
    request_reconcile(s)
    app = Application(s, settings=Settings.from_env(), index_mode="KEYWORD_ONLY")
    for _ in range(1000):
        result = app.run_once("historical-dependency-replay")
        if result is None:
            break
        if result["status"] == "REVIEW":
            raise RuntimeError(str(result))
    after = convergence_report(s)
    with s.db.engine.connect() as c:
        usage_after = c.exec_driver_sql(
            "SELECT coalesce(sum(total_tokens),0) FROM provider_calls"
        ).scalar_one()
    report = {
        "passed": after["passed"],
        "scope": "Historical dependency regression fixture only, not fresh content quality or generation cost benchmark. Contains earlier operator-assisted revisions.",
        "before": before,
        "after": after,
        "additional_provider_tokens": usage_after - usage_before,
        "manual_result_edits": 0,
        "trigger": "request_reconcile -> ordinary maintenance events and jobs",
    }
    report["first_observed_before"] = (
        prior.get("first_observed_before", prior["before"]) if prior else before
    )
    report["total_additional_provider_tokens"] = (
        prior.get(
            "total_additional_provider_tokens",
            prior.get("additional_provider_tokens", 0),
        )
        if prior
        else 0
    ) + report["additional_provider_tokens"]
    with (root / "attempts.jsonl").open("a", encoding="utf-8") as f:
        if prior:
            f.write(json.dumps(prior, ensure_ascii=False) + "\n")
        f.write(json.dumps(report, ensure_ascii=False) + "\n")
    (root / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "before_invalid": [
                    n["title"] for n in before["ineligible_published_notes"]
                ],
                "after_counts": after["eligible_counts"],
                "additional_provider_tokens": report["additional_provider_tokens"],
            },
            ensure_ascii=False,
        )
    )
    if not report["passed"]:
        raise RuntimeError("Historical dependency convergence failed")
finally:
    s.close()
