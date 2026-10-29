"""Verify replay inputs without calling a model, parser or modifying old runs."""

import hashlib
import json
import sqlite3
from pathlib import Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def frozen_cases_unchanged(folder):
    root = Path(folder)
    manifest = json.loads((root / "planning-input-manifest.json").read_text(encoding="utf-8"))
    return digest((root / "acceptance-cases.json").read_bytes()) == manifest["acceptance_sha256"]


def verified_parse(folder, source_hash):
    root = Path(folder).resolve()
    info = json.loads((root / "run-input.json").read_text(encoding="utf-8"))
    if info["source_sha256"] != source_hash:
        raise ValueError("Replay PDF hash differs from the requested PDF")
    with sqlite3.connect((root / "library/noodle.db").as_uri() + "?mode=ro", uri=True) as c:
        source = c.execute("SELECT sha256,blob_ref FROM source_revisions WHERE id=?", (info["source"]["revision_id"],)).fetchone()
        row = c.execute("SELECT artifact_ref,artifact_sha256 FROM job_checkpoints WHERE job_id=? AND stage='parse.normalized' AND state='DONE'", (info["job_id"],)).fetchone()
    if not source or source[0] != source_hash or digest((root / "library" / source[1]).read_bytes()) != source_hash or not row:
        raise ValueError("Replay source bytes or successful parse receipt missing")
    data = (root / "library" / row[0]).read_bytes()
    if digest(data) != row[1]:
        raise ValueError("Replay parse checkpoint hash mismatch")
    parsed = json.loads(data)
    assets = {}
    for block in parsed["artifact"]["blocks"]:
        ref = block.get("asset_ref")
        if ref:
            path = (root / "library" / ref).resolve()
            if not path.is_relative_to((root / "library/assets").resolve()):
                raise ValueError("Replay asset escapes the asset directory")
            content = path.read_bytes()
            if digest(content) != path.stem:
                raise ValueError("Replay asset bytes hash mismatch")
            assets[ref] = content
    return parsed["artifact"], assets, {"origin_root":str(root), "origin_ref":row[0], "origin_sha256":row[1], "source_sha256":source_hash}


def verified_plan(folder, repository, units, source_hash):
    root = Path(folder).resolve()
    info = json.loads((root / "run-input.json").read_text(encoding="utf-8"))
    if info["source_sha256"] != source_hash:
        raise ValueError("Replay planning source mismatch")
    manifest = json.loads((root / "code-manifest.json").read_text(encoding="utf-8"))
    for name in ("source_sections.py", "learning_objects.py", "atomic_planning.py", "document_outline.py", "content_selection.py"):
        relative = "services/noodle/src/noodle_agent/next/" + name
        if digest((repository / relative).read_bytes()) != manifest[relative]:
            raise ValueError("Planning implementation changed; cannot replay the old plan: " + name)
    path = root / "source-plan.json"
    data = path.read_bytes()
    plan = json.loads(data)
    if plan.get("planning_policy") == "adaptive-source-v1":
        for name in ("material_profiles.py", "adaptive_planning.py"):
            relative = "services/noodle/src/noodle_agent/next/" + name
            if digest((repository / relative).read_bytes()) != manifest.get(relative):
                raise ValueError("Adaptive routing implementation changed; cannot replay the old plan: " + name)
    # The exported plan must be the actual accepted immutable checkpoint, not
    # an edited file that merely has valid source IDs.
    with sqlite3.connect((root / "library/noodle.db").as_uri() + "?mode=ro", uri=True) as c:
        rows = c.execute("SELECT artifact_ref,artifact_sha256 FROM job_checkpoints WHERE job_id=? AND stage LIKE 'source.sections.plan.%' AND state='DONE'", (info["job_id"],)).fetchall()
    if not any(digest((root / "library" / ref).read_bytes()) == expected and json.loads((root / "library" / ref).read_bytes()) == plan for ref, expected in rows):
        raise ValueError("Replay source plan differs from its accepted checkpoint")
    coverage = [u for p in plan["plans"] for u in p["unit_ids"]] + [e["unit_id"] for e in plan["layout_exclusions"]]
    expected = {u.unit_id for u in units}
    if set(coverage) != expected or len(coverage) != len(expected):
        raise ValueError("Replay plan does not match the actual parsed source units")
    return plan, {"origin_root":str(root), "origin_sha256":digest(data), "source_sha256":source_hash,
        "policy":"Original accepted plan; only new store source/parse identifiers are rebound by code"}
