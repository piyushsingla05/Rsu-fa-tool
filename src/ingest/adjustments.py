"""Broker-specific adjustment extension point.

Configuration first. A broker's mapping, row shapes, terminology, split style,
cost-basis interpretation and section layout all belong in its YAML profile.

This module exists for the residue: behaviour a real statement exhibits that
cannot reasonably be expressed as configuration. A profile names a hook with
`rules.adjustment: <name>`; the hook receives the normalised rows and the source
record and returns them modified. Nothing is registered speculatively - a hook is
added only when an actual source document demands it, and the reason is recorded
beside it.

    @register("schwab_dividend_pairing")
    def _pair(rows, rec, profile):
        ...
        return rows

Deliberately NOT a place to encode one broker's convention as everyone's rule.
"""
from __future__ import annotations

from typing import Callable

REGISTRY: dict[str, Callable] = {}
REASONS: dict[str, str] = {}


def register(name: str, reason: str = ""):
    def wrap(fn):
        REGISTRY[name] = fn
        REASONS[name] = reason or (fn.__doc__ or "").strip().splitlines()[0]
        return fn
    return wrap


def apply(name: str, rows, rec, profile):
    """Run a named adjustment. An unknown name is a no-op, never a crash."""
    fn = REGISTRY.get(name)
    return fn(rows, rec, profile) if fn else rows


def describe() -> list[tuple[str, str]]:
    return sorted((k, REASONS.get(k, "")) for k in REGISTRY)
