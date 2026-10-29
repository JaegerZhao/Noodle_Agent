"""Stage-gated real acceptance using frozen parse/vision, never legacy edited notes.

Run S1..S6 sequentially. A failed stage blocks the next and retains all receipts.
"""

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/noodle/src"))
from noodle_agent.adapters.llm import OpenAIKnowledgeAgent
from noodle_agent.config import Settings
from noodle_agent.next.audit import ProviderAudit
from noodle_agent.next.checkpoints import Checkpoints
from noodle_agent.next.contracts import canonical, sha
from noodle_agent.next.events import dispatch
from noodle_agent.next.generation import Generation
from noodle_agent.next.planning import QualityFailure
from noodle_agent.next.repository import one
from noodle_agent.next.runtime import Application
from noodle_agent.next.store import Store
from noodle_agent.next.visual_replay import ReplayVision
from noodle_agent.next.worker import keep_lease

BASE = Path(".local-only/generation-optimization-2026-10-22/short-optimized")
VISUAL = Path(".local-only/entity-concepts-2026-10-23/final/library")


class FrozenParser:
    name = "frozen-mineru-replay"

    def submit(self, *args, **kwargs):
        raise QualityFailure("MINERU_NETWORK_DISABLED")


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def usage(store):
    with store.db.engine.connect() as c:
        return [
            dict(r)
            for r in c.exec_driver_sql(
                "SELECT kind,operation,count(*) calls,sum(total_tokens) total_tokens,sum(elapsed_ms) elapsed_ms FROM provider_calls GROUP BY kind,operation"
            ).mappings()
        ]


def frozen_row(root, prefix):
    c = sqlite3.connect(root / "noodle.db")
    c.row_factory = sqlite3.Row
    records = c.execute(
        "SELECT * FROM job_checkpoints WHERE state='DONE' AND stage LIKE ? ORDER BY created_at",
        (prefix + "%",),
    ).fetchall()
    c.close()
    for row in records:
        data = (root / row["artifact_ref"]).read_bytes()
        if sha(data) != row["artifact_sha256"]:
            raise QualityFailure("Frozen checkpoint hash mismatch")
        yield dict(row), json.loads(data)


def setup(root, store, settings):
    _, parsed = next(frozen_row(BASE, "parse.normalized"))
    c = sqlite3.connect(BASE / "noodle.db")
    c.row_factory = sqlite3.Row
    old = dict(c.execute("SELECT * FROM source_revisions LIMIT 1").fetchone())
    c.close()
    data = (BASE / old["blob_ref"]).read_bytes()
    assert sha(data) == old["sha256"]
    artifact = parsed["artifact"]
    assert artifact["page_count"] == 60
    image_count = 0
    for block in artifact["blocks"]:
        if block.get("asset_ref"):
            ref = block["asset_ref"]
            asset = (BASE / ref).read_bytes()
            store.put(ref, asset)
            image_count += 1
            if not block.get("visual") or block["visual"]["image_sha256"] != sha(asset):
                raise QualityFailure(
                    "Frozen parse lacks matching initial visual output"
                )
    records = []
    for row, value in frozen_row(VISUAL, "page-context.source-v3."):
        visual = json.loads(value["text"])
        ref = value["asset_ref"]
        asset = (VISUAL / ref).read_bytes()
        if sha(asset) != visual["image_sha256"]:
            raise QualityFailure("Frozen whole-page asset hash mismatch")
        store.put(ref, asset)
        records.append(
            {
                "kind": "SOURCE_PAGE",
                "page": value["page"],
                "image_sha256": visual["image_sha256"],
                "value": visual,
                "origin_ref": row["artifact_ref"],
                "origin_sha256": row["artifact_sha256"],
            }
        )
    models = {r["value"]["model"] for r in records}
    assert len(models) == 1 and records
    manifest = {
        "source_sha256": old["sha256"],
        "model": models.pop(),
        "records": records,
    }
    # Prove the cached page contexts refer to the very same original source.
    c = sqlite3.connect(VISUAL / "noodle.db")
    original_hashes = {r[0] for r in c.execute("SELECT sha256 FROM source_revisions")}
    c.close()
    assert old["sha256"] in original_hashes
    dump(root / "frozen-vision.json", manifest)
    source = store.add_source(
        data,
        "EE6405 · Term Weighting, Topic Modeling and Dimensionality Reduction",
        original_name=old["original_name"],
    )
    dispatch(store)
    lease = store.jobs.claim("staged-intake", kind="GENERATE_ATOMICS")
    if lease is None:
        raise QualityFailure("S1 must run against a fresh intake")
    profile = {
        "parser": FrozenParser.name,
        "model": settings.llm_fast_model,
        "generation_layout": "source-sections-v1",
        "concept_layout": "entity-v2",
        "writing": store.repository.policy_version,
        "vision_model": manifest["model"],
        "cache_condition": "parse-and-all-vision-frozen-no-network",
    }
    try:
        journal = Checkpoints(store.repository, lease)
        with keep_lease(store.jobs, lease):
            parse_id = store.sources.record_parse(
                source["revision_id"],
                "frozen-mineru",
                {"source_sha256": old["sha256"]},
                artifact,
                lease=lease,
            )
            journal.save(
                "parse.normalized",
                {
                    "revision": source["revision_id"],
                    "sha256": old["sha256"],
                    "profile": profile,
                },
                {"parse_id": parse_id, "artifact": artifact},
            )
        store.jobs.defer(lease, {"status": "S1_READY"})
    except Exception:
        store.jobs.fail(lease, {"code": "INTAKE_FAILED"}, retry=False)
        raise
    dump(
        root / "run-input.json",
        {"source": source, "job_id": lease.job_id, "profile": profile},
    )
    return {
        "source_sha256": old["sha256"],
        "pages": artifact["page_count"],
        "initial_visual_images": image_count,
        "whole_page_visual_records": len(records),
        "mineru_network_calls": 0,
        "vision_network_calls": 0,
    }


def generation(root, store, settings, stage):
    info = json.loads((root / "run-input.json").read_text(encoding="utf-8"))
    with store.db.engine.connect() as c:
        job = one(c, "SELECT * FROM jobs WHERE id=:id", id=info["job_id"])
    if job["status"] == "REVIEW":
        store.jobs.retry_review(job['id'])
    lease = store.jobs.claim("staged-" + stage, kind="GENERATE_ATOMICS")
    if lease is None:
        raise QualityFailure("No generation checkpoint available for this stage")
    replay = ReplayVision(
        json.loads((root / "frozen-vision.json").read_text(encoding="utf-8"))
    )
    audit = ProviderAudit(store.repository, lease)
    agent = OpenAIKnowledgeAgent(settings, telemetry=audit)
    try:
        with keep_lease(store.jobs, lease):
            g = Generation(
                store,
                FrozenParser(),
                agent,
                lease,
                vision=replay,
                profile=info["profile"],
                concurrency=4,
                progress=lambda name, value: print(
                    canonical({"progress": name, "detail": value}), flush=True
                ),
            )
            if stage == "S2":
                from noodle_agent.next.source_sections import (
                    load_source_plan,
                    units_from_blocks,
                )

                parsed = g.parsing.run(info["source"]["revision_id"])
                units = units_from_blocks(parsed["artifact"])
                g.journal.compute(
                    "units",
                    {"parse_id": parsed["parse_id"]},
                    lambda: {"units": [u.model_dump() for u in units]},
                )
                plan = load_source_plan(g, parsed, units)
                count = Counter(u for p in plan["plans"] for u in p["unit_ids"])
                count.update(e["unit_id"] for e in plan["layout_exclusions"])
                assert set(count) == {u.unit_id for u in units} and all(
                    n == 1 for n in count.values()
                )
                assert all(p["pages"] == sorted(p["pages"]) for p in plan["plans"])
                dump(root / "source-plan.json", plan)
                store.jobs.defer(lease, {"status": "S2_PLANNED"})
                return {
                    "source_units": len(units),
                    "plans": len(plan["plans"]),
                    "basis": plan["basis"],
                    "coverage": "EXACT_ONCE",
                    "document_profile": plan["document_profile"],
                    "titles": [p["title"] for p in plan["plans"]],
                    "planning_policy": plan.get("planning_policy"),
                }
            result = g.run()
        store.jobs.finish(lease, result)
        from noodle_agent.next.verification import verify_generation

        checked = verify_generation(store, lease.job_id)
        dump(
            root / "generation-result.json",
            {"source": info["source"], "job_id": lease.job_id, "result": result},
        )
        from noodle_agent.next.critical_quality import published_problems
        from noodle_agent.next.navigation import current_notes

        defects = []
        for n in current_notes(store, kind="ATOMIC", limit=1000)["items"]:
            issues = published_problems(store, store.get_note(n["id"]))
            if issues:
                defects.append({"title": n["title"], "issues": issues})
        if defects:
            raise QualityFailure("Known content regression: " + canonical(defects))
        return {
            **checked,
            "vision_cache_hits": replay.hits,
            "vision_network_calls": 0,
            "manual_candidate_edits": 0,
        }
    except Exception as exc:
        # A completed job must never be rewritten as failed due to a later readback.
        with store.db.engine.connect() as c:
            state = one(c, "SELECT status FROM jobs WHERE id=:id", id=lease.job_id)[
                "status"
            ]
        if state == "RUNNING":
            store.jobs.fail(
                lease,
                {"code": type(exc).__name__, "detail": str(exc)[:5000]},
                retry=False,
            )
        raise


def maintenance(root, store, settings, stage):
    kinds = {
        "S4": ["RECONCILE_DEPENDENCIES", "ANALYZE_ATOMIC"],
        "S5": [
            "REPAIR_ATOMIC",
            "RECONCILE_DEPENDENCIES",
            "ANALYZE_ATOMIC",
            "BUILD_CONCEPT",
            "REVIEW_ORGANIZATION",
        ],
        "S6": None,
    }[stage]
    if stage == "S5":
        from noodle_agent.next.organization import request_organization_review

        request_organization_review(store, cause="staged-acceptance")
    app = Application(store, settings=settings, job_kinds=kinds)
    with store.db.engine.connect() as c:
        reviews = [
            dict(r)
            for r in c.exec_driver_sql(
                "SELECT id,kind FROM jobs WHERE status='REVIEW'"
            ).mappings()
        ]
    for job in reviews:
        if kinds is None or job["kind"] in kinds:
            store.jobs.retry_review(job['id'])
    for _ in range(1000):
        result = app.run_once("staged-" + stage)
        if result is None:
            break
        print(canonical(result), flush=True)
        if result["status"] == "REVIEW":
            raise QualityFailure("Stage task requires review: " + canonical(result))
    else:
        raise QualityFailure("Maintenance stage exceeded bounded steps")
    with store.db.engine.connect() as c:
        unfinished = [
            dict(r)
            for r in c.exec_driver_sql(
                "SELECT kind,status FROM jobs WHERE status!='SUCCEEDED'"
            ).mappings()
            if (kinds is None or r["kind"] in kinds)
            and r["kind"] != "STAGED_RETRIEVAL_CHECK"
        ]
    if unfinished:
        raise QualityFailure(
            "Current stage tasks have not settled: " + canonical(unfinished)
        )
    from noodle_agent.next.convergence import convergence_report

    checked = convergence_report(store, require_idle=False)
    if not checked["passed"]:
        raise QualityFailure("Business convergence failed: " + canonical(checked))
    if stage == "S4":
        with store.db.engine.connect() as c:
            plans = [
                dict(r)
                for r in c.exec_driver_sql(
                    "SELECT artifact_ref,artifact_sha256 FROM concept_entity_plans WHERE state='READY'"
                ).mappings()
            ]
        actual = [
            json.loads(store.read(p["artifact_ref"], p["artifact_sha256"]))
            for p in plans
        ]
        dump(root / "entity-plans.json", actual)
        if not actual or not all(p.get("review", {}).get("passed") for p in actual):
            raise QualityFailure("Missing complete entity plan review")
        checked["entity_plan_count"] = len(actual)
    if stage in {"S5", "S6"}:
        from noodle_agent.next.verification import verify_current_generation

        info = json.loads((root / "run-input.json").read_text(encoding="utf-8"))
        lineage = verify_current_generation(store, info["job_id"])
        dump(root / "stage-S3-current.json", lineage)
        checked["current_atomic_lineage"] = lineage
    if stage == "S6":
        from ee6405_acceptance_outputs import final_outputs

        checked["final_outputs"] = final_outputs(root, store, settings, app)
        settled = convergence_report(store)
        checked = {**checked, **settled}
        checked["passed"] = settled["passed"] and checked["final_outputs"]["passed"]
        if not checked["passed"]:
            raise QualityFailure("Final convergence failed: " + canonical(checked))
    return checked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--retry-stage", action="store_true")
    parser.add_argument(
        "--stage", choices=["S1", "S2", "S3", "S4", "S5", "S6"], required=True
    )
    parser.add_argument("--root", default=".local-only/ee6405-staged-2026-10-23")
    args = parser.parse_args()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    number = int(args.stage[1:])
    if number > 1:
        previous = root / f"stage-S{number - 1}.json"
        if (
            not previous.exists()
            or not json.loads(previous.read_text(encoding="utf-8"))["passed"]
        ):
            raise QualityFailure("Previous stage has not passed")
    target = root / f"stage-{args.stage}.json"
    if (
        not args.retry_stage
        and target.exists()
        and json.loads(target.read_text(encoding="utf-8"))["passed"]
    ):
        print("Stage already accepted; no repeated provider calls.")
        return
    store = Store(root / "library")
    start = time.perf_counter()
    before = usage(store)
    settings = replace(Settings.from_env(), llm_max_retries=0, vision_max_retries=0)
    report = {
        "stage": args.stage,
        "passed": False,
        "started_at": __import__("datetime")
        .datetime.now(__import__("datetime").UTC)
        .isoformat(),
    }
    try:
        report["result"] = (
            setup(root, store, settings)
            if args.stage == "S1"
            else generation(root, store, settings, args.stage)
            if args.stage in {"S2", "S3"}
            else maintenance(root, store, settings, args.stage)
        )
        report["passed"] = True
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "detail": str(exc)}
        raise
    finally:
        report.update(
            {
                "wall_s": round(time.perf_counter() - start, 3),
                "usage_before": before,
                "usage_after": usage(store),
            }
        )
        with (root / "attempts.jsonl").open("a", encoding="utf-8") as f:
            f.write(canonical(report) + "\n")
        dump(target, report)
        store.close()
        print(canonical(report), flush=True)


if __name__ == "__main__":
    main()
