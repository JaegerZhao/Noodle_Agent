from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..services import NoodleService
from .gateway import HarnessGateway


@dataclass(frozen=True)
class ApiContext:
    service: NoodleService
    settings: Settings
    harness: HarnessGateway
