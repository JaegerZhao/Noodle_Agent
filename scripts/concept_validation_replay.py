"""Copy verified Atomic state into a fresh Concept-only validation library."""

import json
import shutil
import sqlite3


def atomic_snapshot(connection):
    from noodle_agent.next.eligibility import VALID
    return [dict(r) for r in connection.execute(VALID + """SELECT n.id,n.title,n.current_revision_id,r.markdown_ref,r.markdown_sha256
        FROM notes n JOIN note_revisions r ON r.id=n.current_revision_id JOIN valid v ON v.id=r.id
        WHERE n.kind='ATOMIC' ORDER BY n.creation_key,n.id""")]


def import_atomics(base, target, store_type, digest, *, note_ids=None):
    if target.exists():
        raise ValueError("Concept replay requires a new library; no implicit rerun")
    target.mkdir(parents=True)
    store_type(target).close()
    original = sqlite3.connect((base / "noodle.db").resolve().as_uri() + "?mode=ro", uri=True)
    original.row_factory = sqlite3.Row
    destination = sqlite3.connect(target / "noodle.db")
    snapshot = atomic_snapshot(original)
    if note_ids is not None:
        wanted = set(note_ids)
        snapshot = [n for n in snapshot if n['id'] in wanted]
        if {n['id'] for n in snapshot} != wanted:
            raise ValueError('Requested replay input is not currently eligible')
    selected_ids = json.dumps([n['id'] for n in snapshot])
    atomics = "SELECT value FROM json_each(:selected_ids)"
    revisions = f"SELECT id FROM note_revisions WHERE note_id IN ({atomics})"
    generation_jobs = """SELECT id FROM jobs WHERE kind='GENERATE_ATOMICS' AND status='SUCCEEDED'
        AND json_extract(input_json,'$.revision_id') IN
        (SELECT substr(creation_key,8,36) FROM notes WHERE id IN (SELECT value FROM json_each(:selected_ids)))"""
    queries = {
        "sources": "1", "source_revisions": "1", "parse_runs": "1", "evidence_anchors": "1",
        "notes": f"id IN ({atomics})", "note_revisions": f"note_id IN ({atomics})",
        "sections": f"revision_id IN ({revisions})", "section_evidence": f"revision_id IN ({revisions})",
        "publication_visibility": f"note_id IN ({atomics})", "author_origin_sources": f"revision_id IN ({revisions})",
        "links": f"from_revision_id IN ({revisions}) AND target_note_id IN ({atomics})",
        "build_artifacts": f"kind='PUBLICATION' AND storage_ref IN (SELECT 'artifacts/builds/'||id||'.json' FROM note_revisions WHERE note_id IN ({atomics}))",
        "events": f"id IN (SELECT event_id FROM jobs WHERE id IN ({generation_jobs}))",
        "jobs": f"id IN ({generation_jobs})", "job_checkpoints": f"job_id IN ({generation_jobs})",
    }
    counts = {}
    files = set()
    def collect(value):
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            ref = value.removeprefix('../')
            if ref.startswith(('artifacts/', 'assets/', 'blobs/', 'revisions/')) and len(ref) < 500:
                path = (base / ref).resolve()
                if path.is_relative_to(base.resolve()) and path.is_file():
                    files.add(ref)
    try:
        for table, predicate in queries.items():
            selected = original.execute(f"SELECT * FROM {table} WHERE {predicate}", {'selected_ids': selected_ids}).fetchall()
            counts[table] = len(selected)
            if selected:
                for row in selected:
                    collect(dict(row))
                columns = ",".join(selected[0].keys())
                destination.executemany(f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in selected[0])})", [tuple(r) for r in selected])
        destination.commit()
        if destination.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Atomic import has invalid references")
        copied = set()
        while files - copied:
            ref = sorted(files - copied)[0]
            path = base / ref
            data = path.read_bytes()
            output = target / ref
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
            copied.add(ref)
            if ref.endswith('.json'):
                try:
                    collect(json.loads(data))
                except (ValueError, UnicodeError):
                    pass
            if ref.endswith('.md'):
                import re
                for asset in re.findall(r'!\[[^\]]*\]\((?:\.\./)?(assets/[^)]+)\)', data.decode()):
                    collect(asset)
        for note in snapshot:
            ref = note["markdown_ref"]
            if digest((target / ref).read_bytes()) != note["markdown_sha256"] or (target / ref).read_bytes() != (base / ref).read_bytes():
                raise ValueError("Atomic body changed during replay")
        # Provider receipts are immutable files only, never new usage rows.
        if destination.execute("SELECT count(*) FROM provider_calls").fetchone()[0]:
            raise ValueError("Historical calls must not be charged as new calls")
        from noodle_agent.next.imported_metrics import capture
        source_store,target_store=store_type(base),store_type(target)
        try:
            history=capture(source_store,target_store)
        finally:
            source_store.close()
            target_store.close()
        return {"base": str(base), "imported_rows": counts, "atomics": snapshot,
                'original_generation_metrics_imported':len(history['jobs']),
                "old_concept_rows_imported": 0, "historical_provider_calls_imported": 0, 'copied_files': len(copied),
                "atomic_snapshot_sha256": digest(json.dumps(snapshot, sort_keys=True).encode())}
    finally:
        destination.close()
        original.close()
