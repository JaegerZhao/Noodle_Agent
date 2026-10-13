from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass
