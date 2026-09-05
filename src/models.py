"""Canonical data contract for the RSU / Schedule FA tool.

Every broker parser must emit these five tables. Nothing downstream knows or
cares which broker the data came from, which is what lets the same engine serve
E*TRADE, Shareworks, Fidelity, Schwab and a hand-filled CSV.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

# --------------------------------------------------------------------------
# Event vocabulary
# --------------------------------------------------------------------------
VEST = "VEST"                  # RSU vesting - creates a lot at FMV
BUY = "BUY"                    # ESPP / open-market purchase - creates a lot
SELL = "SELL"                  # disposal - consumes lots FIFO
DIV = "DIV"                    # dividend credited (GROSS, before US withholding)
DIV_TAX = "DIV_TAX"            # standalone foreign withholding tax line
TAX_WITHHOLD = "TAX_WITHHOLD"  # shares surrendered to the employer to fund tax.
                               # NOT a market sale: no proceeds reach the client,
                               # so it reduces the holding without creating an
                               # A3 gross-proceeds or a capital-gains entry.
CASH = "CASH"                  # cash movement with no share effect
OTHER = "OTHER"
TRANSFER_IN = "TRANSFER_IN"
TRANSFER_OUT = "TRANSFER_OUT"
SPLIT = "SPLIT"                # quantity multiplier, no value change
POSITION = "POSITION"          # a holding STATE the broker states at a date, not a
                               # transaction. Six monthly statements carry six
                               # snapshots of the same shares; only the latest one
                               # on or before the period end may become a holding,
                               # or the position would be counted once per file.

ACQUIRING_EVENTS = {VEST, BUY, TRANSFER_IN, POSITION}
DISPOSING_EVENTS = {SELL, TRANSFER_OUT, TAX_WITHHOLD}
# Only these reduce the holding AND produce sale proceeds. Share withholding and
# an internal transfer reduce the holding but are not disposals for A3 col 13.
PROCEEDS_EVENTS = {SELL}

# --------------------------------------------------------------------------
# FX conventions - these are NOT interchangeable
# --------------------------------------------------------------------------
# Schedule FA (Rule 115 proviso / FA instructions): TT buying rate as on the
# date of peak balance, date of investment, or the closing date of the period.
FX_SAME_DAY = "SAME_DAY"
# Rule 115 general: TT buying rate on the LAST DAY OF THE MONTH IMMEDIATELY
# PRECEDING the month in which the income accrues / the transfer takes place.
# Used for capital gains and for dividend income in Schedule OS / FSI.
FX_PREV_MONTH_END = "PREV_MONTH_END"

# --------------------------------------------------------------------------
# Table schemas
# --------------------------------------------------------------------------
EVENTS_COLUMNS = [
    "date",         # YYYY-MM-DD
    "broker",
    "account_no",
    "symbol",
    "event",        # one of the vocabulary above
    "quantity",     # shares; blank for DIV
    "price_fc",     # per-share price in foreign currency (FMV on vest, sale price)
    "amount_fc",    # gross cash amount in FC; for DIV this is the GROSS dividend
    "tax_fc",       # foreign tax withheld in FC (NRA/withholding) - Schedule FSI/TR
    "acquired_on",  # on a disposal: the acquisition date the broker itself states.
                    # A 1099-B style realised gain/loss report (Schwab year-end,
                    # Fidelity custom summary) is a COMPLETE closed-lot record -
                    # quantity, acquired, sold, proceeds and cost - so no FIFO
                    # inference is needed or wanted.
    "cost_fc",      # on a disposal: the cost the BROKER actually removed. Brokers
                    # using specific-lot identification state this, and it beats a
                    # FIFO guess, so the engine prefers it when present.
    "currency",
    "notes",
]

# Figures a broker's own realised gain/loss report states, carried alongside the
# disposal so the working paper can show what the broker said next to what the
# engine computed. These are SUPPORTING data, never the Indian tax input:
#
#   broker_gain_fc      - the report's ordinary "Gain/Loss". For an equity plan
#                         this is proceeds less the ACQUISITION cost only, so it
#                         still contains the ordinary income already taxed as a
#                         perquisite. It is reconciliation information, not a gain.
#   broker_adj_cost_fc  - "Adjusted Cost Basis": acquisition cost PLUS the
#                         ordinary income recognised. This is the cost actually
#                         borne, and the figure Indian capital gains works from.
#   broker_adj_gain_fc  - "Adjusted Gain/Loss": proceeds less adjusted cost.
#
# On the INTC sample the difference is not academic: the ordinary column says
# $29,357.68 and the adjusted column says $14,336.01, because $15,021.67 of
# ordinary income sits between them.
BROKER_FIGURE_COLUMNS = ["broker_gain_fc", "broker_adj_cost_fc", "broker_adj_gain_fc"]

# Year-high and period-end closing price per symbol. Only two numbers per symbol
# per period are needed, because peak and closing both convert at the period-end
# rate - so a full daily price feed is not required.
MARKET_COLUMNS = [
    "symbol", "period_end", "high_price_fc", "close_price_fc",
    "currency", "high_basis", "source", "verified",
]

PRICES_COLUMNS = ["date", "symbol", "close_fc", "currency"]

CASH_COLUMNS = ["date", "broker", "account_no", "balance_fc", "currency"]

ENTITIES_COLUMNS = [
    "symbol",
    "entity_name",
    "address",
    "zip",
    "country",
    "country_code",
    "nature_of_entity",   # e.g. "Listed company - equity shares"
]

ACCOUNTS_COLUMNS = [
    "broker",
    "account_no",
    "institution_name",
    "address",
    "zip",
    "country",
    "country_code",
    "status",             # Owner / Beneficial owner / Beneficiary
    "opening_date",
]

FX_COLUMNS = ["date", "currency", "ttbr", "source", "verified"]

# Form 1042-S. Used when it is the ONLY dividend source, or when transaction-level
# data does not span the whole period. Converted in aggregate at the period-end
# TTBR because the form carries no payment dates.
FORM1042S_COLUMNS = [
    "tax_year", "broker", "payer", "income_code", "gross_income_fc",
    "tax_withheld_fc", "currency", "source_file", "notes",
]

# Every parser records where each row came from, so any figure can be traced
# back to a document, page and row.
SOURCE_COLUMNS = ["source_file", "source_page", "source_row"]


@dataclass
class StatementData:
    """What a broker parser returns."""
    events: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=EVENTS_COLUMNS))
    prices: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=PRICES_COLUMNS))
    cash: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=CASH_COLUMNS))
    warnings: list[str] = field(default_factory=list)
    broker: str = ""
    source_file: str = ""

    def merge(self, other: "StatementData") -> "StatementData":
        return StatementData(
            events=pd.concat([self.events, other.events], ignore_index=True),
            prices=pd.concat([self.prices, other.prices], ignore_index=True),
            cash=pd.concat([self.cash, other.cash], ignore_index=True),
            warnings=self.warnings + other.warnings,
            broker=f"{self.broker}+{other.broker}".strip("+"),
            source_file=f"{self.source_file}; {other.source_file}".strip("; "),
        )


@dataclass
class Period:
    """The FA reporting period. User-supplied per client."""
    start: date
    end: date

    @property
    def label(self) -> str:
        return f"{self.start:%d-%m-%Y} to {self.end:%d-%m-%Y}"

    @property
    def calendar_year(self) -> int:
        return self.end.year


@dataclass
class Lot:
    """One acquisition tranche - the unit capital gains is computed on."""
    symbol: str
    acquired: date
    quantity: float
    price_fc: float           # FMV per share on acquisition
    currency: str
    broker: str
    account_no: str
    event: str
    remaining: float = 0.0
    # For a broker-STATED position: the date the broker struck it. `acquired`
    # may be earlier when the statement names the lot's own trade date, so the
    # two must not be confused - `stated_at` is what decides whether a disposal
    # is already reflected in the position.
    stated_at: date | None = None

    def __post_init__(self):
        if not self.remaining:
            self.remaining = self.quantity


@dataclass
class SaleMatch:
    """A disposal matched against the lot it came out of (FIFO)."""
    symbol: str
    sold_on: date
    acquired_on: date
    quantity: float
    sale_price_fc: float
    cost_price_fc: float
    currency: str
    proceeds_inr: float = 0.0
    cost_inr: float = 0.0
    gain_inr: float = 0.0
    holding_days: int = 0
    term: str = ""            # "Short Term" / "Long Term"
    stated_cost_fc: float | None = None   # broker-stated cost for this slice
    # What the broker's own report said, carried through for the working paper.
    broker_gain_fc: float | None = None        # ordinary Gain/Loss - supporting only
    broker_adj_cost_fc: float | None = None    # Adjusted Cost Basis - authoritative
    broker_adj_gain_fc: float | None = None    # Adjusted Gain/Loss - authoritative
    source_ref: str = ""                       # document | sheet | row


def empty(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)
