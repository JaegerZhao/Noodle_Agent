"""Frozen planning-only corpus: verified MinerU replays plus a locally extracted paper."""

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def baseline_state(library):
    import hashlib

    with sqlite3.connect((library.resolve() / "noodle.db").as_uri() + "?mode=ro", uri=True) as c:
        state = {"jobs": c.execute("SELECT id,status,attempts FROM jobs ORDER BY id").fetchall(),
            "notes": c.execute("SELECT id,current_revision_id FROM notes ORDER BY id").fetchall(),
            "controls": c.execute("SELECT * FROM job_controls ORDER BY job_id").fetchall(),
            "provider_count": c.execute("SELECT count(*) FROM provider_calls").fetchone()[0]}
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def native_paper(path):
    import fitz

    blocks = []
    with fitz.open(path) as pdf:
        for page_number, page in enumerate(pdf, 1):
            for block in page.get_text("dict")["blocks"]:
                if block["type"] != 0:
                    continue
                paragraph = []

                def flush(paragraph=paragraph, page_number=page_number):
                    if paragraph:
                        blocks.append({"id": f"p{page_number}-b{len(blocks)}", "page": page_number, "text": "\n".join(paragraph), "kind": "TEXT"})
                        paragraph.clear()

                for line in block["lines"]:
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    if not text:
                        continue
                    heading = len(text) <= 150 and (re.match(r"^(?:\d{1,2}(?:\.\d+)*\s+[A-Z]|Abstract$|References$|Acknowledg|Appendix)", text)
                        or max(s["size"] for s in line["spans"]) >= 15)
                    if heading or re.fullmatch(r"\d{1,2}", text):
                        flush()
                        blocks.append({"id": f"p{page_number}-b{len(blocks)}", "page": page_number, "text": text,
                            "kind": "PAGE_NUMBER" if text.isdigit() else "HEADING"})
                    else:
                        paragraph.append(text)
                flush()
        return {"title": "Attention Is All You Need", "page_count": len(pdf), "blocks": blocks,
            "parser": "pymupdf-native-text-v1", "parser_version": "native-planning-only-v1",
            "extraction_scope": "Text and headings only; no OCR, figure interpretation or formula transcription validation"}


def cached_input(library, title_contains=None):
    from noodle_agent.next.contracts import sha

    library = library.resolve()
    with sqlite3.connect((library / "noodle.db").as_uri() + "?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        source = c.execute("SELECT r.*,s.title FROM sources s JOIN source_revisions r ON r.id=s.current_revision_id WHERE s.title LIKE ? LIMIT 1", ("%" + (title_contains or "") + "%",)).fetchone()
        row = c.execute("""SELECT k.* FROM job_checkpoints k JOIN jobs j ON j.id=k.job_id
            WHERE k.stage='parse.normalized' AND k.state='DONE' AND json_extract(j.input_json,'$.revision_id')=? ORDER BY k.created_at DESC LIMIT 1""", (source["id"],)).fetchone()
    data, raw = (library / source["blob_ref"]).read_bytes(), (library / row["artifact_ref"]).read_bytes()
    if sha(data) != source["sha256"] or sha(raw) != row["artifact_sha256"]:
        raise ValueError("Original source or parse hash mismatch")
    return data, json.loads(raw)["artifact"], {"library": str(library), "baseline_state_sha256": baseline_state(library), "source_sha256": sha(data),
        "parse_ref": row["artifact_ref"], "parse_sha256": row["artifact_sha256"], "original_name": source["original_name"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book-library", type=Path, default=REPO / "data/knowledge")
    parser.add_argument("--slides-library", type=Path, default=REPO / ".local-only/pdf-pipeline-20261023T203848Z/library")
    parser.add_argument("--paper", type=Path, default=REPO / ".local-only/adaptive-planning-inputs/attention-is-all-you-need-nips2017.pdf")
    parser.add_argument("--offline", action="store_true", help="Plan trusted book TOC only; classify all inputs without model calls")
    parser.add_argument("--reuse-planning-root", type=Path, help="Reuse only exact-request model receipts; never reuse the aggregate source plan")
    options = parser.parse_args()
    root = REPO / ".local-only" / ("adaptive-planning-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    root.mkdir(exist_ok=False)
    files = {}
    for p in (REPO / "services/noodle/src").rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            relative = p.relative_to(REPO)
            target = root / "frozen-code" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
    shutil.copy2(__file__, root / "runner.py")
    sys.path.insert(0, str(root / "frozen-code/services/noodle/src"))
    from noodle_agent.config import Settings
    from noodle_agent.next.audit import ProviderAudit
    from noodle_agent.next.checkpoints import Checkpoints
    from noodle_agent.next.contracts import sha
    from noodle_agent.next.document_outline import build_outline
    from noodle_agent.next.events import dispatch
    from noodle_agent.next.generation import Generation
    from noodle_agent.next.material_profiles import assess_source
    from noodle_agent.next.runtime import real_agent
    from noodle_agent.next.source_sections import load_source_plan, units_from_blocks
    from noodle_agent.next.source_structure import source_structure
    from noodle_agent.next.store import Store
    from noodle_agent.next.worker import keep_lease

    def dump(folder, name, value):
        (folder / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    for p in (root / "frozen-code").rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            files[p.relative_to(root / "frozen-code").as_posix()] = sha(p.read_bytes())
    freeze = {"files": files, "runner_sha256": sha(Path(__file__).read_bytes()), "env_sha256": sha((REPO / ".env").read_bytes()), "offline": options.offline}
    dump(root, "execution-freeze.json", freeze)
    book = cached_input(options.book_library, "Probabilistic Machine Learning")
    slides = cached_input(options.slides_library)
    paper_data = options.paper.read_bytes()
    paper = (paper_data, native_paper(options.paper), {"path": str(options.paper.resolve()), "source_sha256": sha(paper_data),
        "download_url": "https://papers.nips.cc/paper_files/paper/2017/file/3f5ee243547dee91fbd053c1c4a845aa-Paper.pdf", "parse_method": "local native text; not MinerU"})
    settings, results = Settings.from_env(REPO), []
    for label, expected, (data, artifact, origin) in [("book", "BOOK", book), ("slides", "SLIDES", slides), ("paper", "PAPER", paper)]:
        folder = root / label
        folder.mkdir()
        store = Store(folder / "library")
        source = store.add_source(data, artifact["title"], original_name=origin.get("original_name", options.paper.name))
        dispatch(store)
        lease = store.jobs.claim("adaptive-planning-only", kind="GENERATE_ATOMICS")
        profile = {"parser": "verified-planning-input", "model": settings.llm_fast_model, "planning_model": settings.llm_pro_model,
            "planning_mode": "adaptive-source-v1", "generation_layout": "source-sections-v1", "checking_policy": "single-check-v1",
            "review_policy": "essential-v3", "writing": store.repository.policy_version}
        started = time.perf_counter()
        try:
            with keep_lease(store.jobs, lease):
                journal = Checkpoints(store.repository, lease)
                journal.save("profile", profile, profile)
                parse_id = store.sources.record_parse(source["revision_id"], artifact.get("parser", "verified-cache"), origin, artifact, lease=lease)
                parsed = {"parse_id": parse_id, "artifact": artifact}
                journal.save("parse.normalized", {"source": sha(data)}, parsed)
                units = units_from_blocks(artifact)
                journal.save("units", {"parse_id": parse_id}, {"units": [u.model_dump() for u in units]})
                replay_count = 0
                if options.reuse_planning_root and label != "book":
                    origin_library = options.reuse_planning_root.resolve() / label / "library"
                    with sqlite3.connect((origin_library / "noodle.db").as_uri() + "?mode=ro", uri=True) as connection:
                        connection.row_factory = sqlite3.Row
                        receipts = connection.execute("SELECT * FROM job_checkpoints WHERE stage LIKE 'source.learning.objects.%' AND state='DONE'").fetchall()
                    for receipt in receipts:
                        raw = (origin_library / receipt["artifact_ref"]).read_bytes()
                        if sha(raw) != receipt["artifact_sha256"]:
                            raise ValueError("Replay model receipt hash mismatch")
                        store.put(receipt["artifact_ref"], raw)
                        fields = dict(receipt)
                        fields["job_id"] = lease.job_id
                        from noodle_agent.next.repository import insert

                        with store.repository.transaction() as connection:
                            insert(connection, "job_checkpoints", **fields)
                        replay_count += 1
                    dump(folder, "model-replay.json", {"origin": str(origin_library), "receipt_count": replay_count,
                        "policy": "Native Checkpoints.load requires exact request hash; no aggregate plan or historical usage is imported"})
                material = assess_source(data, artifact, build_outline(data, artifact["blocks"]))
                dump(folder, "input-manifest.json", {"origin": origin, "source": source, "profile": profile, "units": len(units)})
                if options.offline and label != "book" and not options.reuse_planning_root:
                    result = {"status": "CLASSIFIED_ONLY", "material_profile": material, "passed": material["kind"] == expected}
                else:
                    class NoServices:
                        name = "external-parsing-disabled"
                        fast_model = settings.llm_fast_model
                        pro_model = settings.llm_pro_model

                        def structured_json(self, **_kwargs):
                            raise RuntimeError("OFFLINE_MODEL_DISABLED")

                        def submit(self, *_args, **_kwargs):
                            raise RuntimeError("PARSER_DISABLED")

                    agent = NoServices() if options.offline else real_agent(settings, ProviderAudit(store.repository, lease))
                    generation = Generation(store, NoServices(), agent, lease, profile=profile)
                    plan = load_source_plan(generation, parsed, units)
                    coverage = Counter(u for p in plan["plans"] for u in p["unit_ids"])
                    coverage.update(e["unit_id"] for e in plan["layout_exclusions"])
                    valid_coverage = set(coverage) == {u.unit_id for u in units} and all(n == 1 for n in coverage.values())
                    dump(folder, "source-plan.json", plan)
                    reading = source_structure(store, source["revision_id"])
                    dump(folder, "source-structure.json", reading)
                    result = {"status": "PLANNED_NOT_GENERATED", "pages": artifact["page_count"], "material_profile": plan["document_profile"],
                        "replayed_model_receipts": replay_count,
                        "atomic_count": len(plan["plans"]), "coverage_passed": valid_coverage, "basis": plan["basis"],
                        "chapter_tasks": plan.get("chapter_tasks", []), "granularity": plan["granularity"],
                        "topics": [{k: p[k] for k in ("title", "pages", "chapter_key", "outline_node_id", "split_origin")} for p in plan["plans"]],
                        "passed": plan["document_profile"]["kind"] == expected and valid_coverage}
            store.jobs.finish(lease, {"status": result["status"], "atomic_body_generation": False})
        except Exception as exc:  # noqa: BLE001 - preserve failures and continue independent corpus inputs
            result = {"status": "FAILED", "passed": False, "error": type(exc).__name__, "detail": str(exc)}
            store.jobs.fail(lease, {"code": type(exc).__name__, "detail": str(exc)}, retry=False)
        with store.db.engine.connect() as c:
            usage = [dict(row) for row in c.exec_driver_sql("SELECT operation,model,status,count(*) calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(total_tokens) total_tokens,sum(elapsed_ms) elapsed_ms FROM provider_calls GROUP BY operation,model,status").mappings()]
            result.update({"elapsed_s": round(time.perf_counter() - started, 3), "usage": usage,
                "published_notes": c.exec_driver_sql("SELECT count(*) FROM notes").scalar_one(), "mineru_calls": 0, "vision_calls": 0})
        dump(folder, "result.json", result)
        results.append({"label": label, **result})
        print(json.dumps({k: result.get(k) for k in ("status", "passed", "atomic_count", "elapsed_s", "usage", "detail")}, ensure_ascii=True), flush=True)
        store.close()
    changed = [p for p, h in files.items() if sha((REPO / p).read_bytes()) != h or sha((root / "frozen-code" / p).read_bytes()) != h]
    report = {"root": str(root), "results": results, "passed": all(r["passed"] for r in results),
        "freeze_passed": not changed and sha((REPO / ".env").read_bytes()) == freeze["env_sha256"], "changed_files": changed,
        "runner_unchanged": sha(Path(__file__).read_bytes()) == freeze["runner_sha256"], "atomic_body_generation": False}
    report["baseline_states_unchanged"] = all(baseline_state(Path(source[2]["library"])) == source[2]["baseline_state_sha256"] for source in (book, slides))
    dump(root, "corpus-result.json", report)
    print(json.dumps({"root": str(root), "passed": report["passed"], "freeze_passed": report["freeze_passed"]}), flush=True)


if __name__ == "__main__":
    main()
