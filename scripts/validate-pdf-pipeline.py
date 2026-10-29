"""One frozen, unattended PDF run through native parsing/planning and existing workers.

No source-specific production rules, manual repairs, or implicit second run.
Fixed acceptance questions are frozen before any paid request and never sent to generation.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--cases", type=Path, required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--reuse-parse", type=Path, help="Reuse a verified parse from an earlier isolated run; never resubmit MinerU")
    ap.add_argument("--reuse-plan", type=Path, help="Reuse the earlier accepted plan when source and planner implementation match")
    ap.add_argument("--vision-cache-root", type=Path, action="append", default=[], help="Verified visual cache roots; incompatible or missing pages use real vision")
    args = ap.parse_args()
    original = args.pdf.resolve()
    data = original.read_bytes()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    assert data.startswith(b"%PDF-") and cases["cases"]
    ids = [c["id"] for c in cases["cases"]]
    assert len(set(ids)) == len(ids)
    root = REPO / (".local-only/pdf-pipeline-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    root.mkdir(exist_ok=False)
    print(json.dumps({"root": str(root), "status": "PREPARING"}), flush=True)
    manifests = {}
    for folder in ("services/noodle/src", "services/noodle/migrations"):
        for path in (REPO / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                relative = path.relative_to(REPO)
                content = path.read_bytes()
                manifests[relative.as_posix()] = digest(content)
                target = root / "frozen-code" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
    runners = [Path(__file__), *(REPO / "scripts" / name for name in (
        "pdf_validation_replay.py",
        "validate-week4-throughput.py", "probe-week4-throughput.py",
        "validate-katex-notes.mjs", "report-pdf-pipeline.py"))]
    runner_hashes = {}
    (root / "frozen-runners").mkdir()
    for path in runners:
        runner_hashes[path.relative_to(REPO).as_posix()] = digest(path.read_bytes())
        shutil.copy2(path, root / "frozen-runners" / path.name)
    dump(root / "code-manifest.json", manifests)
    dump(root / "runner-manifest.json", runner_hashes)
    dump(root / "acceptance-cases.json", cases)
    dump(root / "pdf-local-reference.json", {"path": str(original), "sha256": digest(data)})
    os.environ["NOODLE_DATA_DIR"] = str(root / "library")
    os.environ["NOODLE_EXPORT_DIR"] = str(root / "library/exports")
    os.environ["NOODLE_DATABASE_URL"] = "sqlite:///" + str(root / "library/noodle.db")
    os.environ["NOODLE_NEXT_VECTOR_BACKEND"] = "exact"
    os.environ["NOODLE_NEXT_VISION_CONCURRENCY"] = "3"
    sys.path.insert(0, str(root / "frozen-code/services/noodle/src"))

    import fitz
    from noodle_agent.adapters.mineru import build_parser
    from noodle_agent.config import Settings
    from noodle_agent.next.audit import ProviderAudit
    from noodle_agent.next.checkpoints import Checkpoints
    from noodle_agent.next.events import dispatch
    from noodle_agent.next.generation import Generation
    from noodle_agent.next.parsing import ParsePending, PdfParsing
    from noodle_agent.next.runtime import real_agent
    from noodle_agent.next.source_sections import load_source_plan, units_from_blocks
    from noodle_agent.next.store import Store
    from noodle_agent.next.worker import keep_lease

    settings = replace(Settings.from_env(REPO), mineru_mode="precision", llm_max_retries=0)
    with fitz.open(stream=data, filetype="pdf") as document:
        pages = len(document)
    dump(root / "planning-input-manifest.json", {"env_sha256": digest((REPO / ".env").read_bytes()),
        "llm_pro_model": settings.llm_pro_model, "llm_endpoint": settings.llm_base_url,
        "runner_sha256": digest(Path(__file__).read_bytes()), "acceptance_sha256": digest((root / "acceptance-cases.json").read_bytes())})
    for case in cases["cases"]:
        assert all(1 <= p <= pages for p in case.get("pages", []))
    store = Store(root / "library")
    lease = None
    stage = "INTAKE"
    started = time.perf_counter()
    start_at = datetime.now(UTC).isoformat()
    dump(root / "pipeline-start.json", {"started_at": start_at, "pages": pages, "label": args.label,
        "parse_mode": "VERIFIED_PARSE_REPLAY" if args.reuse_parse else "FRESH_MINERU", "visual_mode": "VERIFIED_CACHE_WITH_NECESSARY_MISSES" if args.reuse_parse or args.vision_cache_root else "FRESH_AS_NEEDED", "manual_intervention": False})
    try:
        source = store.add_source(data, args.label, original_name=original.name)
        dispatch(store)
        lease = store.jobs.claim("pdf-validation-intake", kind="GENERATE_ATOMICS")
        assert lease is not None
        parser = build_parser(settings)
        profile = {"parser": parser.name, "parse_mode": settings.mineru_mode,
            "parse_model": settings.mineru_model_version, "language": settings.mineru_language,
            "model": settings.llm_fast_model, "llm_endpoint": settings.llm_base_url,
            "generation_layout": "source-sections-v1", "concept_layout": "entity-v2",
            "execution_policy": "pipeline-throughput-v1", "checking_policy": "single-check-v1",
            "planning_mode": "adaptive-source-v1", "review_policy": "essential-v3",
            "quality_policy": "structured-quality-v2", "planning_model": settings.llm_pro_model,
            "writing": store.repository.policy_version, "vision_model": settings.vision_model,
            "vision_endpoint": settings.vision_base_url, "vision_prompt": "visual-understanding-v5",
            "cache_condition": "verified-replay" if args.reuse_parse else "cold-isolated-no-vision-replay"}
        info = {"source": source, "job_id": lease.job_id, "profile": profile, "baseline": str(root),
            "source_sha256": digest(data), "benchmark_label": args.label,
            "cases_file": "acceptance-cases.json", "parse_mode": "VERIFIED_PARSE_REPLAY" if args.reuse_parse else "FRESH_MINERU"}
        dump(root / "run-input.json", info)
        journal = Checkpoints(store.repository, lease)
        with keep_lease(store.jobs, lease):
            journal.save("profile", profile, profile)
            stage = "PARSE"
            parse_start = time.perf_counter()
            provenance = None
            if args.reuse_parse:
                from pdf_validation_replay import verified_parse
                artifact, assets, provenance = verified_parse(args.reuse_parse, digest(data))
                assert artifact["page_count"] == pages
                for ref, content in assets.items():
                    store.put(ref, content)
                parse_id = store.sources.record_parse(source["revision_id"], "verified-replay", provenance, artifact, lease=lease)
                parsed = {"parse_id":parse_id,"artifact":artifact}
                journal.save("parse.normalized", {"revision":source["revision_id"],"sha256":digest(data),"profile":profile}, parsed)
            else:
                parsing = PdfParsing(store, parser, journal, profile=profile)
                while True:
                    try:
                        parsed = parsing.run(source["revision_id"])
                        break
                    except ParsePending as exc:
                        print(json.dumps({"stage": "PARSE_PENDING", "state": str(exc),
                            "elapsed_s": round(time.perf_counter()-parse_start, 3)}), flush=True)
                        time.sleep(2)
            parse_seconds = time.perf_counter()-parse_start
            origin_parse = json.loads((args.reuse_parse / "parse-result.json").read_text(encoding="utf-8")) if args.reuse_parse else {}
            dump(root / "parse-result.json", {"mode": info["parse_mode"], "elapsed_seconds": parse_seconds,
                "provenance":provenance, "historical_elapsed_seconds":origin_parse.get("historical_elapsed_seconds",origin_parse.get("elapsed_seconds")) if args.reuse_parse else None,
                "pages": pages, "parse_id": parsed["parse_id"], "tokens": None,
                "token_note": "MinerU does not provide token usage in these receipts; unknown is not zero"})
            print(json.dumps({"stage": "PARSED", "pages": pages, "elapsed_s": parse_seconds}), flush=True)
            stage = "PLANNING"
            planning_start = time.perf_counter()
            generation = Generation(store, parser, real_agent(settings, ProviderAudit(store.repository, lease)),
                lease, profile=profile, concurrency=3)
            units = units_from_blocks(parsed["artifact"])
            if args.reuse_plan:
                from pdf_validation_replay import verified_plan
                plan, plan_provenance = verified_plan(args.reuse_plan, REPO, units, digest(data))
                plan = {**plan,"parse_id":parsed["parse_id"],"source_revision_id":source["revision_id"]}
                journal.save("source.sections.plan.learning-objects-v1", {"parse_id":parsed["parse_id"],"policy":"learning-objects-v1"}, plan)
                dump(root / "planning-replay.json", plan_provenance)
            else:
                plan = load_source_plan(generation, parsed, units)
            coverage = Counter(u for p in plan["plans"] for u in p["unit_ids"])
            coverage.update(e["unit_id"] for e in plan["layout_exclusions"])
            assert set(coverage) == {u.unit_id for u in units} and all(n == 1 for n in coverage.values())
            dump(root / "source-plan.json", plan)
            planning = {"status": "PLANNED", "root": str(root), "pages": pages,
                "atomic_count": len(plan["plans"]), "elapsed_seconds": time.perf_counter()-planning_start,
                "topics": [{k:p[k] for k in ("title", "pages", "split_reason", "split_origin")} for p in plan["plans"]],
                "granularity": plan["granularity"], "excluded_units": plan["layout_exclusions"]}
            if args.reuse_plan:
                prior = json.loads((args.reuse_plan / "planning-result.json").read_text(encoding="utf-8"))
                planning.update(mode="VERIFIED_PLAN_REPLAY", historical_elapsed_seconds=prior.get("historical_elapsed_seconds",prior["elapsed_seconds"]))
            dump(root / "planning-result.json", planning)
            print(json.dumps({"stage": "PLANNED", **{k:planning[k] for k in ("atomic_count","elapsed_seconds","topics")},
                "mode":planning.get("mode","FRESH_PLANNING")}, ensure_ascii=True), flush=True)
        store.jobs.defer(lease, {"status": "SOURCE_PLAN_READY", "next_stage": "ATOMIC_GENERATION"})
        lease = None
    except Exception as exc:  # noqa: BLE001 - retain the failed stage without repairing or rerunning
        dump(root / "preparation-failure.json", {"stage":stage, "error":type(exc).__name__,
            "detail":str(exc), "elapsed_s":time.perf_counter()-started, "traceback":traceback.format_exc()})
        if lease:
            store.jobs.fail(lease, {"code": type(exc).__name__, "detail": str(exc)}, retry=False)
    finally:
        store.close()

    def run(script, *arguments):
        relative = "scripts/" + script
        assert digest((REPO / relative).read_bytes()) == runner_hashes[relative]
        command = [sys.executable, str(REPO / relative), str(root), *arguments]
        if script.endswith(".mjs"):
            command[0] = str(Path("D:/Software/nodejs/node.exe"))
        with (root / (script + ".log")).open("w", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONIOENCODING":"utf-8"}, check=False)
        print(json.dumps({"script": script, "exit_code": result.returncode}), flush=True)
        return result.returncode

    if not (root / "preparation-failure.json").exists():
        cache_roots = list(dict.fromkeys([*args.vision_cache_root, *([args.reuse_parse] if args.reuse_parse else [])]))
        run("validate-week4-throughput.py", *[part for folder in cache_roots for part in ("--vision-cache-root",str(folder.resolve()))])
    dump(root / "pipeline-wall.json", {"started_at": start_at, "finished_at": datetime.now(UTC).isoformat(),
        "wall_seconds": time.perf_counter()-started, "excludes": "Preflight, snapshots, QA and exports"})
    execution_path = root / "run-result.json"
    if execution_path.exists():
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        if (execution["generation"].get("terminal") or {}).get("status") == "SUCCEEDED":
            run("probe-week4-throughput.py")
            if (root / "notes-for-render-check.json").exists():
                run("validate-katex-notes.mjs")
        else:
            dump(root / "qa-skipped.json", {"reason":"Generation stopped without a usable completed result; preserve severe failure without manual retries"})
    changed = [str(p) for p,h in manifests.items() if digest((REPO / p).read_bytes()) != h]
    changed += [p for p,h in runner_hashes.items() if digest((REPO / p).read_bytes()) != h]
    from pdf_validation_replay import frozen_cases_unchanged
    dump(root / "final-freeze-check.json", {"passed":not changed, "changed":changed,
        "cases_unchanged":frozen_cases_unchanged(root),
        "env_unchanged":digest((REPO / ".env").read_bytes()) == json.loads((root / "planning-input-manifest.json").read_text())["env_sha256"]})
    run("report-pdf-pipeline.py")
    print(json.dumps({"root":str(root), "status":"FINISHED_NO_MANUAL_INTERVENTION"}), flush=True)


if __name__ == "__main__":
    main()
