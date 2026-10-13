from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from sqlite3 import OperationalError as SqliteOperationalError

from sqlalchemy import (
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import Session, sessionmaker


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30.0}
            if url.startswith("sqlite")
            else {},
        )
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite)
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        # Configure lock waiting before WAL negotiation. On a cold concurrent
        # start, switching the brand-new database into WAL itself takes a write
        # lock and must wait for the sibling process instead of failing fast.
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except SqliteOperationalError as exc:
            # WAL mode is persistent for the database file. During the first
            # simultaneous connections one sibling may already be switching
            # it; that operation reports SQLITE_BUSY without honoring the busy
            # handler. The schema-level BEGIN IMMEDIATE below remains the real
            # serialization barrier, so only this exact transient is safe to
            # defer to the winning connection.
            if "locked" not in str(exc).casefold():
                raise
        cursor.close()

    def init(self) -> None:
        from .runner import upgrade

        self.last_migration = upgrade(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def healthcheck(self) -> bool:
        with self.engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar_one() == 1

    def close(self) -> None:
        self.engine.dispose()
