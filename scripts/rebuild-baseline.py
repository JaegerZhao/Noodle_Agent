"""Create and verify a private, portable rebuild baseline without modifying live data."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def contained(root: Path, key: str) -> Path:
    relative = Path(key)
    target = (root / relative).resolve()
    if relative.is_absolute() or not target.is_relative_to(root.resolve()):
        raise ValueError("Manifest path escapes its root")
    return target


def inspect_database(path: Path) -> dict:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise ValueError("Database integrity check failed")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        names = {
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {
            name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in (
                "sources",
                "source_versions",
                "atomic_notes",
                "note_versions",
                "concepts",
                "concept_versions",
                "jobs",
            )
            if name in names
        }
    return {
        "integrity": "ok",
        "foreign_key_violations": len(violations),
        "counts": counts,
    }


def create(data: Path, output: Path, pdfs: list[Path]) -> dict:
    data, output = data.resolve(), output.resolve()
    if not (data / "noodle.db").is_file():
        raise ValueError("Source database does not exist")
    if output.is_relative_to(data) or data.is_relative_to(output):
        raise ValueError("Backup and live data must be disjoint")
    output.mkdir(parents=True, exist_ok=False)
    payload = output / "payload"
    payload.mkdir()
    with (
        sqlite3.connect((data / "noodle.db").as_uri() + "?mode=ro", uri=True) as src,
        sqlite3.connect(payload / "noodle.db") as dst,
    ):
        src.backup(dst)
    # Snapshotted DB is authoritative; immutable blob/artifact files are copied
    # afterwards. Mutable export/log copies are convenience files, not authority.
    for name in ("blobs", "artifacts", "exports", "uploads"):
        directory = data / name
        if not directory.exists():
            continue
        for source in directory.rglob("*"):
            if source.is_symlink():
                raise ValueError("Symlinks are not allowed in a recovery baseline")
            if source.is_file():
                destination = contained(payload, source.relative_to(data).as_posix())
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    files = [
        {
            "path": p.relative_to(payload).as_posix(),
            "bytes": p.stat().st_size,
            "sha256": digest(p),
        }
        for p in sorted(payload.rglob("*"))
        if p.is_file()
    ]
    references = []
    for pdf in pdfs:
        pdf = pdf.resolve()
        if not pdf.is_file():
            references.append({"path": str(pdf), "status": "MISSING"})
            continue
        import pymupdf

        with pymupdf.open(pdf) as document:
            pages = len(document)
        references.append(
            {
                "path": str(pdf),
                "status": "AVAILABLE",
                "pages": pages,
                "bytes": pdf.stat().st_size,
                "sha256": digest(pdf),
            }
        )
    report = {
        "contract": "rebuild-baseline-v1",
        "source_root": str(data),
        "database": inspect_database(payload / "noodle.db"),
        "files": files,
        "reference_pdfs": references,
    }
    (output / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return verify(output)


def verify(backup: Path) -> dict:
    backup = backup.resolve()
    report = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    if report.get("contract") != "rebuild-baseline-v1":
        raise ValueError("Unknown backup contract")
    seen = set()
    for record in report["files"]:
        if record["path"] in seen:
            raise ValueError("Duplicate manifest path")
        seen.add(record["path"])
        path = contained(backup / "payload", record["path"])
        if path.stat().st_size != record["bytes"] or digest(path) != record["sha256"]:
            raise ValueError(f"Recovery file mismatch: {record['path']}")
    if "noodle.db" not in seen:
        raise ValueError("Recovery database is missing")
    database = inspect_database(backup / "payload/noodle.db")
    if database != report["database"]:
        raise ValueError("Recovery database metadata differs")
    return {
        "verified": True,
        "files": len(seen),
        "bytes": sum(f["bytes"] for f in report["files"]),
        "database": database,
        "references": len(report["reference_pdfs"]),
    }


def restore(backup: Path, output: Path) -> dict:
    verify(backup)
    backup, output = backup.resolve(), output.resolve()
    if backup.is_relative_to(output) or output.is_relative_to(backup):
        raise ValueError("Restore destination must be disjoint from the backup")
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    for record in manifest["files"]:
        target = contained(output, record["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(contained(backup / "payload", record["path"]), target)
        if digest(target) != record["sha256"]:
            raise ValueError("Restore hash mismatch")
    database = inspect_database(output / "noodle.db")
    if database != manifest["database"]:
        raise ValueError("Restored database differs")
    return {"restored": True, "files": len(manifest["files"]), "database": database}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    create_parser = sub.add_parser("create")
    create_parser.add_argument("--data", type=Path, required=True)
    create_parser.add_argument("--output", type=Path, required=True)
    create_parser.add_argument("--pdf", type=Path, action="append", default=[])
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("backup", type=Path)
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.operation == "create":
        result = create(args.data, args.output, args.pdf)
    elif args.operation == "verify":
        result = verify(args.backup)
    else:
        result = restore(args.backup, args.output)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
