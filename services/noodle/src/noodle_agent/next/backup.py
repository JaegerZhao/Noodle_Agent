"""Consistent recovery packages, with file hashes and validated restore paths."""

import io
import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

from .contracts import canonical, sha
from .repository import InvalidKnowledge


def recovery_zip(store):
    output = io.BytesIO()
    manifest = []
    with tempfile.TemporaryDirectory() as temporary:
        snapshot = Path(temporary) / "noodle.db"
        with (
            closing(sqlite3.connect(store.root / "noodle.db")) as src,
            closing(sqlite3.connect(snapshot)) as dst,
        ):
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise InvalidKnowledge("Database snapshot integrity failure")
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            entries = [("noodle.db", snapshot)]
            entries.extend(
                (p.relative_to(store.root).as_posix(), p)
                for p in store.root.rglob("*")
                if p.is_file()
                and p.relative_to(store.root).parts[0]
                in {"blobs", "assets", "artifacts", "vectors", "revisions", "indexes"}
                and not p.is_symlink()
                and not p.name.endswith(".tmp")
            )
            for name, p in entries:
                data = p.read_bytes()
                archive.writestr(name, data)
                manifest.append({"path": name, "sha256": sha(data), "bytes": len(data)})
            archive.writestr(
                "recovery-manifest.json",
                canonical({"contract": "next-recovery-v1", "files": manifest}),
            )
    return output.getvalue()


def restore_package(content, destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise InvalidKnowledge("Restore requires a new directory")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise InvalidKnowledge("Duplicate archive paths")
        manifest = json.loads(archive.read("recovery-manifest.json"))
        if manifest.get("contract") != "next-recovery-v1":
            raise InvalidKnowledge("Unknown recovery contract")
        expected = {f["path"]: f for f in manifest["files"]}
        if len(expected) != len(manifest["files"]) or set(names) != set(expected) | {
            "recovery-manifest.json"
        }:
            raise InvalidKnowledge("Recovery manifest differs from archive")
        if "noodle.db" not in expected:
            raise InvalidKnowledge("Missing database")
        if sum(f["bytes"] for f in expected.values()) > 10 * 1024**3:
            raise InvalidKnowledge("Recovery exceeds size budget")
        for name, entry in expected.items():
            target = (destination / name).resolve()
            if Path(name).is_absolute() or not target.is_relative_to(destination):
                raise InvalidKnowledge("Archive path escapes destination")
            info = archive.getinfo(name)
            if info.file_size != entry["bytes"] or info.file_size > 1024**3:
                raise InvalidKnowledge("Unexpected archive member size")
            if sha(archive.read(name)) != entry["sha256"]:
                raise InvalidKnowledge("Recovery hash differs")
        with closing(sqlite3.connect(":memory:")) as database:
            database.deserialize(archive.read("noodle.db"))
            if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise InvalidKnowledge("Recovery database integrity failure")
            for query in (
                "SELECT blob_ref,sha256 FROM source_revisions",
                "SELECT artifact_ref,artifact_sha256 FROM parse_runs",
                "SELECT markdown_ref,markdown_sha256 FROM note_revisions",
                "SELECT storage_ref,sha256 FROM build_artifacts",
                "SELECT artifact_ref,artifact_sha256 FROM job_checkpoints WHERE artifact_ref IS NOT NULL",
                "SELECT manifest_ref,manifest_sha256 FROM index_generations",
                "SELECT matrix_ref,matrix_sha256 FROM index_generations WHERE matrix_ref IS NOT NULL",
            ):
                for ref, digest in database.execute(query):
                    if ref not in expected or expected[ref]["sha256"] != digest:
                        raise InvalidKnowledge(
                            "Recovery is missing or differs from a database file reference"
                        )
        destination.mkdir(parents=True)
        for name in expected:
            p = destination / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(archive.read(name))
    with closing(sqlite3.connect(destination / "noodle.db")) as db:
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise InvalidKnowledge("Restored database is inconsistent")
    return {"path": str(destination), "files": len(expected), "integrity": "ok"}
