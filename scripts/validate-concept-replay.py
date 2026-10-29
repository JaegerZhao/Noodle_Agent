"""Frozen native Concept-only validation; no Atomic generation, vision or RAG."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path, help="Existing validation root containing library/")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--expect-concepts", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument('--ordered', action='store_true', help='Use directory-ordered Concept replay with exact input and index checks')
    parser.add_argument('--phase', choices=['prepare', 'run', 'resume', 'report'], default='prepare')
    parser.add_argument('--selection', type=Path, help='Optional JSON array of current Atomic IDs for a small ordered replay')
    parser.add_argument('--refresh-code', help='Record a reason and freeze a new generation for a stopped ordered run')
    parser.add_argument('--retry-review', action='append', default=[], help='Explicitly retry these failed Concept job IDs at the checkpoint')
    options = parser.parse_args()
    if options.ordered:
        from concept_replay_run import prepare, report, retry_review, run_replay, upgrade_execution
        base = options.base.resolve()
        if not (base / 'noodle.db').exists():
            base = base / 'library'
        if options.root is None:
            parser.error('Ordered replay requires an explicit isolated --root')
        target = options.root.resolve()
        if not target.is_relative_to((REPO / '.local-only').resolve()):
            parser.error('Ordered replay must remain inside .local-only')
        if options.prepare_only or options.phase=='prepare':
            selection = json.loads(options.selection.read_text(encoding='utf8')) if options.selection else None
            result = prepare(base, target, selection)
        elif options.phase=='report':
            result = report(target)
        else:
            if options.refresh_code:
                upgrade_execution(target,options.refresh_code)
            if options.retry_review:
                retry_review(target,options.retry_review)
            result = run_replay(target)
        print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        return
    if not 1 <= options.batch_size <= 40:
        parser.error("batch-size must be between 1 and 40")
    base = options.base.resolve() / "library"
    root = (options.root or REPO / ".local-only" / ("concept-replay-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))).resolve()
    if root.parent != (REPO / ".local-only").resolve() or root.exists():
        raise ValueError("Require a new direct child of .local-only")
    root.mkdir()

    def dump(name, value):
        (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # Import the frozen implementation so edits during a live test cannot affect it.
    files = {}
    for path in (REPO / "services/noodle/src").rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            relative = path.relative_to(REPO)
            files[relative.as_posix()] = digest(path.read_bytes())
            target = root / "execution-code" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for path in (Path(__file__), REPO / "scripts/concept_validation_replay.py"):
        shutil.copy2(path, root / path.name)
        files[path.relative_to(REPO).as_posix()] = digest(path.read_bytes())
    freeze = {"files": files, "env_sha256": digest((REPO / ".env").read_bytes()), "frozen_at": datetime.now(UTC).isoformat(),
              "batch_size": options.batch_size, "expected_concept_count": options.expect_concepts,
              "scope": "Concept routing, fusion, publication, organization only; no RAG/index/Atomic/vision"}
    dump("execution-freeze.json", freeze)
    sys.path.insert(0, str(root / "execution-code/services/noodle/src"))
    os.environ["NOODLE_DATA_DIR"] = str(root / "library")
    os.environ["NOODLE_EXPORT_DIR"] = str(root / "exports")
    os.environ["NOODLE_DATABASE_URL"] = "sqlite:///" + str(root / "library/noodle.db")
    from concept_validation_replay import atomic_snapshot, import_atomics
    from noodle_agent.config import Settings
    from noodle_agent.next.concepts.fragments import fragments
    from noodle_agent.next.contracts import canonical, sha
    from noodle_agent.next.knowledge_graph import concept_graph
    from noodle_agent.next.navigation import current_notes, frozen_candidate
    from noodle_agent.next.runtime import Application
    from noodle_agent.next.store import Store

    manifest = import_atomics(base, root / "library", Store, digest)
    dump("atomic-replay-manifest.json", manifest)
    store = Store(root / "library")
    for note in manifest["atomics"]:
        with store.db.engine.connect() as c:
            if not store.content.valid_revision(c, note["current_revision_id"]):
                raise ValueError("Imported Atomic is not eligible: " + note["title"])
        frozen_candidate(store, note["current_revision_id"])
    if options.prepare_only:
        dump("prepared.json", {"passed": True, "atomics": len(manifest["atomics"]), "network_calls": 0})
        store.close()
        print(json.dumps({"root": str(root), "prepared": True}), flush=True)
        return
    settings = replace(Settings.from_env(REPO), embedding_api_key=None)
    with store.repository.transaction() as c:
        atomics = [dict(r) for r in c.exec_driver_sql("SELECT id,current_revision_id,creation_key FROM notes WHERE kind='ATOMIC' ORDER BY creation_key").mappings()]
        sources = {}
        for note in atomics:
            source = note["creation_key"].split(":")[1]
            sources.setdefault(source, []).append({"note_id": note["id"], "revision_id": note["current_revision_id"]})
        for source, members in sources.items():
            for start in range(0, len(members), options.batch_size):
                batch = {"source_revision_id": source, "atomics": members[start:start + options.batch_size]}
                batch["input_hash"] = sha(canonical(batch))
                store.repository.event(c, "ATOMIC_BATCH_CLOSED", source, source, {"routing_batch": batch}, key="concept-replay:" + batch["input_hash"])
    start = time.perf_counter()
    outcomes, timings = [], {}
    phases = [("routing", ("ANALYZE_ATOMIC",)), ("fusion", ("BUILD_CONCEPT",)),
              ("organization", ("REVIEW_ORGANIZATION", "UPDATE_MAPS"))]
    try:
        for phase, kinds in phases:
            phase_start = time.perf_counter()
            app = Application(store, settings=settings, index_mode="KEYWORD_ONLY", job_kinds=kinds)
            for _ in range(500):
                result = app.run_once("concept-replay-" + phase)
                if result is None:
                    break
                item = {"phase": phase, "elapsed_s": round(time.perf_counter() - start, 3), **result}
                outcomes.append(item)
                with (root / "timeline.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
                print(json.dumps(item, ensure_ascii=True, default=str), flush=True)
                # Native local repair decisions remain inside each worker; the harness never edits output or retries failed jobs.
            timings[phase] = round(time.perf_counter() - phase_start, 3)
    except Exception as exc:
        dump("observer-exception.json", {"type": type(exc).__name__, "detail": str(exc)})
        raise
    finally:
        dump("stage-times.json", {**timings, "total_s": round(time.perf_counter() - start, 3)})
        dump("worker-outcomes.json", outcomes)

    with store.db.engine.connect() as c:
        jobs = [dict(r) for r in c.exec_driver_sql("SELECT id,kind,status,result_json,error_json FROM jobs ORDER BY rowid").mappings()]
        usage = [dict(r) for r in c.exec_driver_sql("SELECT kind,operation,model,status,count(*) calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(total_tokens) total_tokens,sum(elapsed_ms) elapsed_ms FROM provider_calls GROUP BY kind,operation,model,status").mappings()]
        routes = [dict(r) for r in c.exec_driver_sql("SELECT * FROM atomic_entity_routes").mappings()]
        decisions = [dict(r) for r in c.exec_driver_sql("SELECT * FROM contribution_decisions").mappings()]
        integrity = c.exec_driver_sql("PRAGMA integrity_check").scalar_one()
        foreign_keys = [list(r) for r in c.exec_driver_sql("PRAGMA foreign_key_check")]
    notes = current_notes(store, limit=10000)
    concepts = [n for n in notes["items"] if n["kind"] == "CONCEPT"]
    coverage = []
    for note in manifest["atomics"]:
        expected = {f["fragment_id"] for f in fragments(store, store.get_note(note["id"]))}
        accepted = {d["fragment_id"] for d in decisions if d["atomic_revision_id"] == note["current_revision_id"] and d["status"] == "ACCEPTED" and d["applied_revision_id"]}
        coverage.append({"title": note["title"], "expected_fragments": len(expected), "applied_fragments": len(accepted), "passed": expected == accepted})
    original = sqlite3.connect((base / "noodle.db").resolve().as_uri() + "?mode=ro", uri=True)
    original.row_factory = sqlite3.Row
    clone = sqlite3.connect(root / "library/noodle.db")
    clone.row_factory = sqlite3.Row
    unchanged = atomic_snapshot(original) == manifest["atomics"] == atomic_snapshot(clone)
    original.close()
    clone.close()
    unchanged = unchanged and all(digest((location / n["markdown_ref"]).read_bytes()) == n["markdown_sha256"] for n in manifest["atomics"] for location in (base, root / "library"))
    changed = [p for p, h in files.items() if digest((REPO / p).read_bytes()) != h]
    frozen_ok = not changed and digest((REPO / ".env").read_bytes()) == freeze["env_sha256"]
    selected_jobs = [j for j in jobs if j["kind"] in {k for _, kinds in phases for k in kinds}]
    excluded = [j for j in jobs if j["kind"] not in {k for _, kinds in phases for k in kinds} and j["status"] != "SUCCEEDED"]
    no_other_calls = all(u["kind"] == "LLM_CALL" and u["operation"].startswith(("next.entity_", "next.organization_")) for u in usage)
    passed = bool(concepts) and len(routes) == len(manifest["atomics"]) and all(r["status"] == "ACCEPTED" for r in routes) and all(x["passed"] for x in coverage)
    passed = passed and all(j["status"] == "SUCCEEDED" for j in selected_jobs) and unchanged and frozen_ok and no_other_calls and integrity == "ok" and not foreign_keys
    if options.expect_concepts is not None:
        passed = passed and len(concepts) == options.expect_concepts
    result = {"passed": passed, "timing": {**timings, "total_s": round(time.perf_counter() - start, 3)},
              "concepts": [{"id": n["id"], "title": n["title"], "revision": n["current_revision_id"]} for n in concepts],
              "coverage": coverage, "atomic_unchanged": unchanged, "freeze_passed": frozen_ok, "changed_files": changed,
              "only_concept_provider_calls": no_other_calls, "rag_not_run": True, "excluded_jobs": len(excluded),
              "integrity": integrity, "foreign_key_violations": foreign_keys}
    dump("run-result.json", result)
    dump("provider-usage.json", usage)
    dump("jobs-final.json", jobs)
    dump("routes.json", routes)
    dump("notes-final.json", notes)
    dump("concept-graph.json", concept_graph(store, limit=1000))
    dump("notes-for-render-check.json", [{"title": n["title"], "markdown": store.read(n["markdown_ref"], n["markdown_sha256"]).decode()} for n in concepts])
    for note in concepts:
        candidate = frozen_candidate(store, note["current_revision_id"])
        dump("concept-" + note["id"] + ".json", candidate.model_dump())
    store.close()
    print(json.dumps({"root": str(root), **result}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
