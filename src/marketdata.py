"""Market data abstraction.

The tax engine must never depend on one website. It asks this module for a
price; the module decides where that price comes from and hands back full
provenance with it. Swapping or adding a provider changes nothing downstream.

Resolution order:
    1. BROKER_STATEMENT - the price is stated on the client's own statement
    2. PRIMARY          - historical market-data provider
    3. SECONDARY        - backup provider
    4. MANUAL           - entered by the preparer
Anything unresolved is a blocker, never a guess.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .review import (
    HIGH_FROM_MONTH_END, MISSING_ANNUAL_HIGH, Register, UNVERIFIED_PRICE,
)

# Source ranks, best first
BROKER_STATEMENT = "BROKER_STATEMENT"
PRIMARY = "PRIMARY"
SECONDARY = "SECONDARY"
MANUAL = "MANUAL"
RANK = {BROKER_STATEMENT: 0, PRIMARY: 1, SECONDARY: 2, MANUAL: 3}

# Price kinds
ANNUAL_HIGH = "ANNUAL_HIGH"
PERIOD_END_CLOSE = "PERIOD_END_CLOSE"

# How a high was derived - only INTRADAY and DAILY_CLOSE are a true annual high
BASIS_INTRADAY = "INTRADAY_HIGH"
BASIS_DAILY_CLOSE = "DAILY_CLOSE_HIGH"
BASIS_MONTH_END = "MONTH_END_CLOSE_HIGH"
APPROXIMATE_BASES = {BASIS_MONTH_END}

COLUMNS = [
    "ticker", "security_name", "exchange", "price_kind", "price", "price_date",
    "currency", "basis", "source_type", "source_name", "retrieved_on",
    "confidence", "verified", "notes",
]


@dataclass
class PricePoint:
    ticker: str
    price_kind: str
    price: float
    price_date: dt.date
    currency: str
    basis: str
    source_type: str
    source_name: str
    retrieved_on: str
    confidence: str
    verified: bool
    security_name: str = ""
    exchange: str = ""
    notes: str = ""

    @property
    def is_approximate(self) -> bool:
        return self.basis in APPROXIMATE_BASES


class MarketData:
    """Reads the price store and resolves the best available point per ticker."""

    def __init__(self, path: str | Path, register: Register):
        self.path = Path(path)
        self.register = register
        self.used: list[PricePoint] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(
                f"Market price store not found at {self.path}. "
                "Expected columns: " + ", ".join(COLUMNS))
        df = pd.read_csv(self.path)
        missing = set(COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(f"{self.path.name} missing columns: {sorted(missing)}")
        df["price_date"] = pd.to_datetime(df["price_date"]).dt.date
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["verified"] = (df["verified"].astype(str).str.strip().str.upper()
                          .isin(["Y", "YES", "TRUE", "1"]))
        df["_rank"] = df["source_type"].map(RANK).fillna(99)
        self.df = df.dropna(subset=["price"])

    # ------------------------------------------------------------------
    def get(self, ticker: str, kind: str, price_date: dt.date) -> PricePoint | None:
        sub = self.df[(self.df["ticker"] == ticker)
                      & (self.df["price_kind"] == kind)
                      & (self.df["price_date"] == price_date)]
        if sub.empty:
            return None
        r = sub.sort_values("_rank").iloc[0]
        p = PricePoint(
            ticker=ticker, price_kind=kind, price=float(r["price"]),
            price_date=r["price_date"], currency=str(r["currency"]),
            basis=str(r["basis"]), source_type=str(r["source_type"]),
            source_name=str(r["source_name"]), retrieved_on=str(r["retrieved_on"]),
            confidence=str(r["confidence"]), verified=bool(r["verified"]),
            security_name=str(r.get("security_name", "")),
            exchange=str(r.get("exchange", "")), notes=str(r.get("notes", "")),
        )
        if p not in self.used:
            self.used.append(p)
        return p

    def resolve(self, ticker: str, period_end: dt.date
                ) -> tuple[float, float, bool, bool, dict]:
        """Return (annual_high, period_end_close, high_ok, close_ok, provenance).

        `high`/`close` are 0.0 whenever the corresponding `_ok` flag is False,
        purely so arithmetic elsewhere does not explode on a plain float - the
        blocker this method raises already says the figure is missing, and it
        must never reach a working-paper cell as though it were a real,
        computed nil. Callers must gate any cell that multiplies through
        `high`/`close` on the matching `_ok` flag, the same way FX-unavailable
        cells are gated on their own quote.
        """
        high = self.get(ticker, ANNUAL_HIGH, period_end)
        close = self.get(ticker, PERIOD_END_CLOSE, period_end)
        prov = {"high": high, "close": close}

        if high is None:
            self.register.blocker(
                MISSING_ANNUAL_HIGH, ticker,
                f"No annual high on file for {ticker} for the period ending "
                f"{period_end:%d-%m-%Y}. Peak value cannot be computed.",
                self.path.name, "Add an ANNUAL_HIGH row to the market price store.")
        elif high.is_approximate:
            self.register.review(
                HIGH_FROM_MONTH_END, ticker,
                f"The high on file ({high.price:,.2f}) is the highest MONTH-END close, "
                "not the true annual high. The actual peak will be higher, so peak "
                "value is understated.",
                f"{high.source_name} ({high.retrieved_on})",
                "Replace with an intraday or daily-close annual high before filing.")

        if close is None:
            self.register.blocker(
                MISSING_ANNUAL_HIGH, ticker,
                f"No period-end closing price on file for {ticker} at "
                f"{period_end:%d-%m-%Y}. Closing value cannot be computed.",
                self.path.name, "Add a PERIOD_END_CLOSE row to the market price store.")

        for p in (high, close):
            if p is not None and not p.verified:
                self.register.review(
                    UNVERIFIED_PRICE, ticker,
                    f"{p.price_kind} {p.price:,.2f} from {p.source_name} is not yet "
                    "verified.", f"{p.source_name} ({p.retrieved_on})",
                    "Confirm against a second source and set verified=Y.")

        return (high.price if high else 0.0,
                close.price if close else 0.0,
                high is not None,
                close is not None,
                prov)

    # ------------------------------------------------------------------
    def audit_frame(self) -> pd.DataFrame:
        if not self.used:
            return pd.DataFrame(columns=[
                "Ticker", "Security", "Exchange", "Price type", "Price", "Price date",
                "Currency", "Basis", "Source type", "Source", "Retrieved on",
                "Confidence", "Verified"])
        return pd.DataFrame([{
            "Ticker": p.ticker, "Security": p.security_name, "Exchange": p.exchange,
            "Price type": p.price_kind, "Price": p.price, "Price date": p.price_date,
            "Currency": p.currency, "Basis": p.basis, "Source type": p.source_type,
            "Source": p.source_name, "Retrieved on": p.retrieved_on,
            "Confidence": p.confidence, "Verified": "Yes" if p.verified else "NO",
        } for p in self.used])
