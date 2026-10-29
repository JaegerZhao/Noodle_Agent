"""Offline accounting of all stage attempts, including failed calls and recovery."""

import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/noodle/src"))
from noodle_agent.next.convergence import convergence_report
from noodle_agent.next.critical_quality import published_problems
from noodle_agent.next.knowledge_graph import concept_graph, concept_outline
from noodle_agent.next.navigation import current_notes
from noodle_agent.next.store import Store

root = Path(".local-only/ee6405-staged-2026-10-23")
s = Store(root / "library")
try:
    attempts = [
        json.loads(line)
        for line in (root / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stages = {}
    for stage in ["S1", "S2", "S3", "S4", "S5", "S6"]:
        records = [r for r in attempts if r["stage"] == stage]
        final = json.loads((root / f"stage-{stage}.json").read_text(encoding="utf-8"))
        tokens = 0
        for row in records:
            before = sum(
                r["total_tokens"] or 0
                for r in row["usage_before"]
                if r["kind"] == "LLM_CALL"
            )
            after = sum(
                r["total_tokens"] or 0
                for r in row["usage_after"]
                if r["kind"] == "LLM_CALL"
            )
            tokens += after - before
        stages[stage] = {
            "passed": final["passed"],
            "attempts": len(records),
            "failed_attempts": sum(not r["passed"] for r in records),
            "execution_wall_s": round(sum(r["wall_s"] for r in records), 3),
            "llm_tokens": tokens,
        }
    with s.db.engine.connect() as c:
        providers = [
            dict(r)
            for r in c.exec_driver_sql(
                "SELECT kind,operation,count(*) calls,sum(total_tokens) total_tokens,sum(elapsed_ms) elapsed_ms,sum(CASE WHEN total_tokens IS NULL THEN 1 ELSE 0 END) unknown_usage FROM provider_calls GROUP BY kind,operation"
            ).mappings()
        ]
        integrity = c.exec_driver_sql("PRAGMA integrity_check").scalar_one()
        fk = c.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    with s.db.engine.connect() as c:
        llm_usage = dict(
            c.exec_driver_sql(
                "SELECT count(*) calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(total_tokens) total_tokens,sum(elapsed_ms) elapsed_ms,sum(CASE WHEN cached_input_tokens IS NULL THEN 1 ELSE 0 END) cache_usage_unknown_calls FROM provider_calls WHERE kind='LLM_CALL'"
            )
            .mappings()
            .one()
        )
        model_usage = [
            dict(r)
            for r in c.exec_driver_sql(
                "SELECT model,count(*) calls,sum(total_tokens) total_tokens FROM provider_calls WHERE kind='LLM_CALL' GROUP BY model"
            ).mappings()
        ]
    started = min(datetime.fromisoformat(a["started_at"]) for a in attempts)
    ended = max(
        datetime.fromisoformat(a["started_at"]) + timedelta(seconds=a["wall_s"])
        for a in attempts
    )
    llm = sum(r["total_tokens"] or 0 for r in providers if r["kind"] == "LLM_CALL")
    assert sum(v["llm_tokens"] for v in stages.values()) == llm
    ops = {
        r["operation"]: r["total_tokens"] or 0
        for r in providers
        if r["kind"] == "LLM_CALL"
    }
    evaluation = sum(
        value for key, value in ops.items() if key.startswith("acceptance.")
    )
    answers = sum(
        value
        for key, value in ops.items()
        if key in {"next.answer", "next.answer_review"}
    )
    current = current_notes(s, limit=1000)["items"]
    defects = []
    for n in current:
        if n["kind"] != "MOC":
            issues = published_problems(s, s.get_note(n["id"]))
            if issues:
                defects.append({"title": n["title"], "issues": issues})
    graph = concept_graph(s, limit=1000)
    children = {
        e["target"] for e in graph["edges"] if e.get("navigation_role") == "TOPIC_CHILD"
    }
    roots = [n for n in graph["nodes"] if n["id"] not in children]
    outlines = [concept_outline(s, n["id"]) for n in roots]
    covered = {e["concept_id"] for t in outlines for e in t["entries"]}
    export = json.loads((root / "final-output-check.json").read_text(encoding="utf-8"))
    receipts = {}
    for path in (root / "library/artifacts/rerank").glob("*.json"):
        receipt = json.loads(path.read_text(encoding="utf-8"))
        response = receipt["response"]
        receipts[response.get("request_id") or path.stem] = response
    rerank = sum(r.get("usage", {}).get("total_tokens", 0) for r in receipts.values())
    convergence = convergence_report(s)
    report = {
        "stage_results": stages,
        "execution_wall_s_excluding_code_and_tests": round(
            sum(r["wall_s"] for r in attempts), 3
        ),
        "llm_total_tokens": llm,
        "llm_usage": llm_usage,
        "llm_models": model_usage,
        "first_stage_started_at": started.isoformat(),
        "last_stage_finished_at": ended.isoformat(),
        "test_span_including_interstage_code_changes_s": round(
            (ended - started).total_seconds(), 3
        ),
        "generation_and_maintenance_llm_tokens": llm - evaluation - answers,
        "evaluation_llm_tokens": evaluation,
        "answer_llm_tokens": answers,
        "vision_network_calls": sum(
            r["calls"] for r in providers if r["kind"] == "VISION_CALL"
        ),
        "mineru_network_calls": 0,
        "embedding_total_tokens": sum(
            r["total_tokens"] or 0 for r in providers if r["kind"] == "EMBEDDING_CALL"
        ),
        "rerank_total_tokens_all_attempts": rerank,
        "rerank_provider_calls": len(receipts),
        "counts": {
            k: sum(n["kind"] == k for n in current)
            for k in ["ATOMIC", "CONCEPT", "MOC"]
        },
        "convergence": convergence,
        "known_regression_findings": defects,
        "integrity": integrity,
        "foreign_key_violations": [list(r) for r in fk],
        "graph_nodes": len(graph["nodes"]),
        "graph_edges": len(graph["edges"]),
        "graph_roots": [n["title"] for n in roots],
        "graph_root_coverage": covered == {n["id"] for n in graph["nodes"]},
        "final_outputs": export,
        "provider_operations": providers,
        "manual_candidate_edits": 0,
    }
    history = json.loads(
        (root / "historical-dependency/result.json").read_text(encoding="utf-8")
    )
    report["historical_dependency_replay_additional_tokens"] = history[
        "total_additional_provider_tokens"
    ]
    report["all_provider_tokens_fresh_run"] = (
        llm + report["embedding_total_tokens"] + rerank
    )
    report["all_provider_tokens_including_historical_replay"] = (
        report["all_provider_tokens_fresh_run"]
        + history["total_additional_provider_tokens"]
    )
    suites = list(ET.parse(root / "regression.xml").getroot().iter("testsuite"))
    report["regression"] = {
        key: sum(int(t.get(key, "0")) for t in suites)
        for key in ["tests", "failures", "errors", "skipped"]
    }
    report["regression"]["wall_s"] = sum(float(t.get("time", "0")) for t in suites)
    report["passed"] = (
        not report["regression"]["failures"]
        and not report["regression"]["errors"]
        and all(v["passed"] for v in stages.values())
        and convergence["passed"]
        and not defects
        and integrity == "ok"
        and not fk
        and report["graph_root_coverage"]
        and report["vision_network_calls"] == 0
    )
    (root / "acceptance-summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "concept-graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "concept-outlines.json").write_text(
        json.dumps(outlines, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"provider_operations", "convergence"}
            },
            ensure_ascii=False,
        )
    )
finally:
    s.close()
