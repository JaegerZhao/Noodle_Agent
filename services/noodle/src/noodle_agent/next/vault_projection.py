"""Journalled file projection: interrupted refreshes are resumable without overwriting edits."""

import json
import os
import shutil
from pathlib import Path

from .contracts import canonical, sha
from .repository import Conflict, InvalidKnowledge, identifier


def contained(root, key):
    path = (root / key).resolve()
    if Path(key).is_absolute() or not path.is_relative_to(root.resolve()):
        raise InvalidKnowledge("Projection path escapes Vault")
    return path


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + "." + identifier() + ".tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def recover_projection(root):
    journal = root / ".noodle-pending.json"
    if not journal.exists():
        return False
    plan = json.loads(journal.read_text(encoding="utf-8"))
    stage = contained(root, plan["stage"])
    if stage.parent != (root / ".noodle-stage").resolve():
        raise InvalidKnowledge("Invalid projection stage")
    for item in plan["writes"]:
        target = contained(root, item["path"])
        current = sha(target.read_bytes()) if target.exists() else None
        if current not in {item["old_sha256"], item["new_sha256"]}:
            raise Conflict("Local edit during interrupted refresh: " + item["path"])
        pending = contained(stage, item["path"])
        if not pending.is_file() or sha(pending.read_bytes()) != item["new_sha256"]:
            raise InvalidKnowledge("Projection stage hash differs")
    for item in plan["archives"]:
        target = contained(root, item["path"])
        archived = contained(root, item["archive"])
        if target.exists() and sha(target.read_bytes()) != item["sha256"]:
            raise Conflict("Local edit in stale note during refresh")
        if archived.exists() and sha(archived.read_bytes()) != item["sha256"]:
            raise Conflict("Stale note archive differs")
    # Only this known projection's files are replaced. The manifest is always last.
    writes = sorted(
        plan["writes"], key=lambda item: (item["path"] == ".noodle-manifest.json", item["path"])
    )
    for item in writes:
        if item["path"] == ".noodle-manifest.json":
            continue
        target = contained(root, item["path"])
        if not target.exists() or sha(target.read_bytes()) != item["new_sha256"]:
            atomic_write(target, contained(stage, item["path"]).read_bytes())
    for item in plan["archives"]:
        target = contained(root, item["path"])
        archived = contained(root, item["archive"])
        if target.exists():
            archived.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, archived)
    for item in writes:
        if item["path"] == ".noodle-manifest.json":
            atomic_write(contained(root, item["path"]), contained(stage, item["path"]).read_bytes())
    journal.unlink()
    # Resolved stage was checked to be one child of the private staging directory.
    if (
        stage.is_relative_to((root / ".noodle-stage").resolve())
        and stage != (root / ".noodle-stage").resolve()
    ):
        shutil.rmtree(stage)
    return True


def commit_projection(root, files, obsolete):
    changed = []
    stage_key = ".noodle-stage/" + identifier()
    stage = contained(root, stage_key)
    for path, data in files.items():
        target = contained(root, path)
        old = sha(target.read_bytes()) if target.exists() else None
        if old == sha(data):
            continue
        changed.append({"path": path, "old_sha256": old, "new_sha256": sha(data)})
        atomic_write(contained(stage, path), data)
    archives = [
        {
            "path": old["path"],
            "sha256": old["sha256"],
            "archive": ".noodle-archive/" + old["revision"] + "/" + old["path"],
        }
        for old in obsolete
        if contained(root, old["path"]).exists()
    ]
    if not changed and not archives:
        return False
    atomic_write(
        root / ".noodle-pending.json",
        canonical({"stage": stage_key, "writes": changed, "archives": archives}).encode(),
    )
    recover_projection(root)
    return True
