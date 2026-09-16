from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Route:
    event: str
    payload: dict[str, Any] = field(default_factory=dict)


class Router(Protocol):
    def route(self, event: str, payload: dict[str, Any]) -> Route | None:
        """None keeps the default (no extra event)."""


class NoopRouter:
    def route(self, event: str, payload: dict[str, Any]) -> Route | None:
        return None


def dispatch(router: Router | None, event: str, payload: dict[str, Any] | None = None) -> Route | None:
    if router is None:
        return None
    return router.route(event, payload or {})
