"""Small synthetic backups exercise recovery without reading personal data."""

import json
import runpy
import sqlite3
from pathlib import Path

import pytest

TOOLS = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/rebuild-baseline.py"))


def fixture_data(tmp_path):
    root = tmp_path / "live"
    (root / "blobs").mkdir(parents=True)
    (root / "blobs/file").write_bytes(b"immutable original")
    with sqlite3.connect(root / "noodle.db") as connection:
        connection.execute("CREATE TABLE sources(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO sources VALUES ('fixture')")
    return root


def test_backup_and_restore_preserve_database_and_all_manifest_files(tmp_path):
    root = fixture_data(tmp_path)
    backup = tmp_path / "backup"
    assert TOOLS["create"](root, backup, [tmp_path / "missing.pdf"])["verified"]
    result = TOOLS["restore"](backup, tmp_path / "restored")
    assert result["restored"]
    assert result["database"]["counts"] == {"sources": 1}
    assert (tmp_path / "restored/blobs/file").read_bytes() == b"immutable original"
    with pytest.raises(FileExistsError):
        TOOLS["restore"](backup, tmp_path / "restored")


def test_corrupt_backup_fails_before_restoring(tmp_path):
    root = fixture_data(tmp_path)
    backup = tmp_path / "backup"
    TOOLS["create"](root, backup, [])
    (backup / "payload/blobs/file").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="mismatch"):
        TOOLS["restore"](backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_manifest_cannot_escape_its_root(tmp_path):
    root = fixture_data(tmp_path)
    backup = tmp_path / "backup"
    TOOLS["create"](root, backup, [])
    path = backup / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["files"][0]["path"] = "../outside"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        TOOLS["verify"](backup)


def test_backup_cannot_be_created_inside_live_store(tmp_path):
    root = fixture_data(tmp_path)
    with pytest.raises(ValueError, match="disjoint"):
        TOOLS["create"](root, root / "nested", [])
