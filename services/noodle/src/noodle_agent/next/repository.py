"""Shared transactional connection and immutable file primitives."""

from __future__ import annotations

import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from ..storage.database import Database
from .contracts import canonical, sha


class Conflict(ValueError):
    """Expected revision or source changed before publication."""


class InvalidKnowledge(ValueError):
    """Content lacks current, valid support or violates a domain constraint."""


def storage_busy(exc):
    return isinstance(exc, OperationalError) and any(value in str(exc.orig).lower()
        for value in ('database is locked', 'database table is locked', 'database is busy'))


def identifier() -> str:
    return str(uuid.uuid4())


def now() -> str:
    return datetime.now(UTC).isoformat()


def storage_path_key(path: Path) -> Path:
    """Windows may resolve the same path with or without its extended prefix."""
    value = str(path)
    if os.name == 'nt':
        if value[:8].upper() == '\\\\?\\UNC\\':
            value = '\\\\' + value[8:]
        elif re.match(r'^\\\\\?\\[A-Za-z]:\\', value):
            value = value[4:]
    return Path(value)


def one(connection, sql: str, **params):
    return connection.execute(text(sql), params).mappings().one_or_none()


def rows(connection, sql: str, **params):
    return connection.execute(text(sql), params).mappings().all()


def insert(connection, table: str, **values):
    if not re.fullmatch(r"[a-z_]+", table):
        raise ValueError("Invalid internal table identifier")
    names = ",".join(values)
    placeholders = ",".join(f":{key}" for key in values)
    connection.execute(text(f"INSERT INTO {table} ({names}) VALUES ({placeholders})"), values)


class Repository:
    contract = "next-v1"

    def __init__(self, root: Path, *, policy_version: str = "writing-next-v1"):
        self.policy_version = policy_version
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = Database(f"sqlite:///{self.root / 'noodle.db'}", contract=self.contract)
        try:
            self.db.init()
            with self.db.engine.connect() as connection:
                active = one(connection, "SELECT active_policy FROM storage_contract WHERE id=1")
                if active["active_policy"] != policy_version:
                    raise Conflict("Publication policy mismatch; open with the active policy")
        except Exception:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self.db.engine.connect() as connection:
            for attempt in range(5):
                try:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    break
                except OperationalError as exc:
                    connection.rollback()
                    if not storage_busy(exc) or attempt == 4:
                        raise
                    time.sleep(min(1, .1 * 2**attempt))
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def path(self, ref: str) -> Path:
        result = (self.root / ref).resolve()
        if Path(ref).is_absolute() or not storage_path_key(result).is_relative_to(storage_path_key(self.root)):
            raise InvalidKnowledge("Storage reference escapes root")
        return result

    def put(self, ref: str, content: bytes) -> None:
        path = self.path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.is_symlink() or path.read_bytes() != content:
                raise InvalidKnowledge("Immutable file differs")
            return
        temporary = path.with_name(f".{path.name}.{identifier()}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Publish a fully written inode without replacing another worker's
            # immutable file. On Windows replacing a file concurrently opened
            # for reading can raise PermissionError, even for identical bytes.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != content:
                    raise InvalidKnowledge("Immutable file differs") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except PermissionError:
                # NTFS may temporarily hold the same inode through its published
                # name. A leftover private temp file is not a publication error.
                pass

    def read(self, ref: str, expected_hash: str) -> bytes:
        content = self.path(ref).read_bytes()
        if sha(content) != expected_hash:
            raise InvalidKnowledge("Stored content hash differs")
        return content

    def event(
        self,
        connection,
        event_type: str,
        entity_id: str,
        revision_id: str | None,
        payload: dict,
        *,
        key: str,
    ):
        connection.execute(
            text("""INSERT OR IGNORE INTO events
            (id,dedupe_key,event_type,entity_id,revision_id,payload_json,status,created_at)
            VALUES(:id,:key,:kind,:entity,:revision,:payload,'QUEUED',:created)"""),
            {
                "id": identifier(),
                "key": key,
                "kind": event_type,
                "entity": entity_id,
                "revision": revision_id,
                "payload": canonical(payload),
                "created": now(),
            },
        )

    def advance_epoch(self, connection):
        connection.exec_driver_sql(
            "UPDATE storage_contract SET validity_epoch=validity_epoch+1 WHERE id=1"
        )

    def activate_policy(self, policy_version: str, *, expected_policy: str):
        if not policy_version.strip():
            raise ValueError("Publication policy cannot be empty")
        with self.transaction() as connection:
            active = one(connection, "SELECT active_policy FROM storage_contract WHERE id=1")
            if active["active_policy"] != expected_policy:
                raise Conflict("Publication policy changed")
            if policy_version == expected_policy:
                return
            connection.execute(
                text(
                    "UPDATE storage_contract SET active_policy=:policy,validity_epoch=validity_epoch+1 WHERE id=1"
                ),
                {"policy": policy_version},
            )
            self.event(
                connection,
                "POLICY_CHANGED",
                "storage",
                None,
                {"previous_policy": expected_policy, "policy": policy_version},
                key=f"policy:{identifier()}",
            )
        self.policy_version = policy_version
