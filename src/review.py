"""Exception register.

Every uncertainty the engine meets is raised as a Flag rather than resolved
silently. The REVIEW_REQUIRED tab is the register printed, and it is the tab a
reviewer reads before signing the working paper.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


class Severity(str, Enum):
    BLOCKER = "Blocker"      # a figure cannot be relied on until resolved
    REVIEW = "Review"        # a documented approximation, needs sign-off
    NOTE = "Note"            # disclosure only


# Canonical reasons, so the same condition always reports identically.
MISSING_VEST_DATE = "Missing vest date"
MISSING_VEST_TTBR = "Missing vest-date TTBR"
SALE_TTBR_FALLBACK = "Sale-date TTBR used for cost"
LIMITED_STATEMENT = "Limited statement coverage"
PERIOD_END_FX_BASIS = "Period-end TTBR used due to limited information"
MISSING_ANNUAL_HIGH = "Annual high not available"
HIGH_FROM_MONTH_END = "Annual high derived from month-end data"
MISSING_DIV_DATES = "Dividend transaction dates unavailable"
AGG_1042S = "1042-S aggregate conversion"
DIV_SOURCE_CONFLICT = "Dividend source precedence applied"
PEAK_QTY_FELL = "Quantity fell during the period"
RECON_BREAK = "Holding reconciliation break"
MISSING_TICKER = "Security/ticker mapping missing"
UNVERIFIED_FX = "Unverified SBI TTBR"
STALE_FX = "SBI TTBR carried forward"
FX_FALLBACK = "Secondary FX source used - SBI TTBR unavailable"
FX_UNAVAILABLE = "FX_UNAVAILABLE"
CG_SOURCE_PRECEDENCE = "Capital gain source precedence applied"
CG_CONTROL_TOTAL = "Realised gain control total"
UNVERIFIED_PRICE = "Unverified market price"
DUPLICATE_CANDIDATE = "Possible duplicate transaction"


@dataclass
class Flag:
    reason: str
    severity: Severity
    subject: str = ""          # security, account or client the flag concerns
    detail: str = ""
    source: str = ""           # file / tab / row the issue came from
    action: str = ""           # what the reviewer must do


@dataclass
class Register:
    flags: list[Flag] = field(default_factory=list)

    def add(self, reason: str, severity: Severity, subject: str = "",
            detail: str = "", source: str = "", action: str = "") -> None:
        f = Flag(reason, severity, subject, detail, source, action)
        if f not in self.flags:
            self.flags.append(f)

    def blocker(self, reason, subject="", detail="", source="", action=""):
        self.add(reason, Severity.BLOCKER, subject, detail, source, action)

    def review(self, reason, subject="", detail="", source="", action=""):
        self.add(reason, Severity.REVIEW, subject, detail, source, action)

    def note(self, reason, subject="", detail="", source="", action=""):
        self.add(reason, Severity.NOTE, subject, detail, source, action)

    @property
    def blockers(self) -> list[Flag]:
        return [f for f in self.flags if f.severity == Severity.BLOCKER]

    def counts(self) -> dict[str, int]:
        return {s.value: sum(1 for f in self.flags if f.severity == s) for s in Severity}

    def frame(self) -> pd.DataFrame:
        order = {Severity.BLOCKER: 0, Severity.REVIEW: 1, Severity.NOTE: 2}
        rows = sorted(self.flags, key=lambda f: (order[f.severity], f.reason, f.subject))
        return pd.DataFrame([{
            "Severity": f.severity.value,
            "Issue": f.reason,
            "Security / Account": f.subject,
            "Detail": f.detail,
            "Source": f.source,
            "Action required": f.action,
        } for f in rows] or [{
            "Severity": "", "Issue": "No exceptions raised",
            "Security / Account": "", "Detail":
                "The engine met no uncertainty. Figures still require professional "
                "review before filing.",
            "Source": "", "Action required": "",
        }])
