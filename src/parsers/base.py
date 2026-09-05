"""Broker parser plugin interface.

Every parser's only job is to turn one broker's statement into the canonical
tables in models.py. All valuation, FX and schedule logic lives downstream, so
adding a broker never touches the engine.

To add a broker:
    @register("etrade")
    class ETradeParser(BrokerParser):
        display_name = "E*TRADE Stock Plan"
        def sniff(cls, path, text): ...   # does this file belong to me?
        def parse(self, path) -> StatementData: ...
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Type

from ..models import StatementData

REGISTRY: dict[str, Type["BrokerParser"]] = {}


def register(key: str) -> Callable[[Type["BrokerParser"]], Type["BrokerParser"]]:
    def wrap(cls):
        cls.key = key
        REGISTRY[key] = cls
        return cls
    return wrap


class BrokerParser:
    key: str = ""
    display_name: str = ""
    # File extensions this parser can handle
    extensions: tuple[str, ...] = (".csv", ".xlsx", ".pdf")

    @classmethod
    def sniff(cls, path: Path, text: str) -> float:
        """Confidence 0-1 that this parser owns the file. Override with real markers."""
        return 0.0

    def parse(self, path: Path) -> StatementData:
        raise NotImplementedError


def detect(path: Path, text: str) -> tuple[str, float]:
    """Pick the parser most confident about a statement."""
    scores = [(k, c.sniff(path, text)) for k, c in REGISTRY.items()]
    if not scores:
        return "", 0.0
    return max(scores, key=lambda kv: kv[1])
