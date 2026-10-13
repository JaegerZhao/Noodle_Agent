"""Shared resource ownership for composed application components."""

from __future__ import annotations

from typing import Any


class Component:
    """Receive an owner explicitly; components do not create shared resources."""

    def __init__(self, owner: Any):
        self.owner = owner
