"""FX rate sources, ranked - so no INR figure ever depends on one provider.

The tax engine asks for "the rate for this currency on this date". This module
decides where that rate comes from and hands back full provenance with it, the
same way marketdata.py does for prices. Adding a provider changes nothing
downstream.

Resolution order:

    1. SBI_TTBR       - the statutory house rate, from the shipped table or a
                        user-uploaded SBI TTBR workbook
    2. GOOGLE_FINANCE - historical FX, used only where SBI TTBR is unavailable
    3. ECB            - euro foreign exchange reference rates, an independent
                        non-Indian source. EUR/INR direct; anything else is a
                        transparently derived cross-rate
    4. FBIL           - Financial Benchmarks India reference rates, supplied by
                        the preparer from FBIL's own publication. USD/INR is a
                        DIRECT rate; EUR, GBP and JPY are FBIL's own crosses
                        through USD, and say so
    5. MANUAL         - a rate the preparer entered and signed off
    6. unresolved     - the INR figure is left BLANK and raised for review.
                        Never zero. Never an unrelated period-end rate.

No provider is ever relabelled as another. In particular FBIL is licensed
benchmark data that this engine does not retrieve: an FBIL rate appears only in a
table the preparer supplies. A rate obtained anywhere else is not FBIL and is
never written as FBIL, however close the number.

None of sources 2-5 is an SBI TT buying rate and none is ever labelled as one.
Each carries its own provider name, methodology and fallback level.

A fallback is never presented as an SBI rate. Every quote carries its source
type, whether SBI was available, and any date adjustment actually applied.

Why a TABLE rather than live calls: GOOGLEFINANCE() is a Google Sheets
function. It is not an Excel function and no Python process can evaluate it. So
the engine consolidates every currency/date pair it actually needs into one
request table - deduplicated, one row per pair, never hundreds of redundant
calls - writes the GOOGLEFINANCE formula for each, and reads the populated
rates back. A live HTTP provider can be registered alongside without touching
anything else here.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# Source types, best first. The number is the FALLBACK LEVEL recorded on every
# quote, so a reviewer can see at a glance how far down the chain a figure came
# from without reading the basis text.
SBI_TTBR = "SBI_TTBR"
GOOGLE_FINANCE = "GOOGLE_FINANCE"
ECB = "ECB"                      # euro reference rates - direct EUR/INR, else cross
FBIL = "FBIL"                    # Financial Benchmarks India - direct USD/INR only
MANUAL = "MANUAL"
UNRESOLVED = "NONE"

RANK = {SBI_TTBR: 1, GOOGLE_FINANCE: 2, ECB: 3, FBIL: 4, MANUAL: 5, UNRESOLVED: 6}

# What each provider actually is. None of these IS an SBI TT buying rate, and
# the workbook never says otherwise.
PROVIDER_NAME = {
    SBI_TTBR: "SBI TT buying rate",
    GOOGLE_FINANCE: "Google Finance historical FX",
    FBIL: "FBIL reference rate (Financial Benchmarks India Pvt Ltd)",
    ECB: "ECB euro foreign exchange reference rate",
    MANUAL: "Preparer-entered rate",
}
# FBIL's methodology differs BY PAIR, and the difference is material: only
# USD/INR is measured from rupee transactions. Saying otherwise about EUR, GBP
# or JPY would put a false statement in the working paper, so the two are held
# apart and each quote carries the one that applies to it.
FBIL_DIRECT_METHOD = (
    "FBIL USD/INR reference rate - a DIRECT rate: the volume-weighted average "
    "of actual spot USD/INR transactions on electronic platforms in a randomly "
    "selected 15-minute window inside 11:30-12:30 IST, subject to a minimum of "
    "ten transactions aggregating USD 25 million, outliers removed on a +/-3SD "
    "rule. Published around 13:30 IST on Mumbai business days. FBIL took the "
    "reference rate over from the RBI in July 2018.")
FBIL_CROSS_METHOD = (
    "FBIL {pair} reference rate - NOT a direct rupee rate. FBIL computes it by "
    "taking the {leg} cross-currency rate from electronic platforms in the same "
    "15-minute window and crossing it with its USD/INR reference rate. Only "
    "FBIL's USD/INR is measured from actual rupee transactions.")

PROVIDER_METHOD = {
    SBI_TTBR: "State Bank of India TT buying rate for the date.",
    GOOGLE_FINANCE: "Google Finance historical close for the currency pair.",
    ECB: ("Euro foreign exchange reference rate, fixed around 14:15 CET and "
          "published about 16:00 CET on TARGET working days. The ECB states "
          "these are for information only and not intended for market "
          "transactions."),
    FBIL: FBIL_DIRECT_METHOD,
    MANUAL: "Entered by the preparer and signed off.",
}

# FBIL benchmark data is licensed. FBIL's own FAQ states that an End-Users
# Licence is required and that use is fee liable, and that any use, commercial
# use or distribution is only with FBIL's express authorisation. So the engine
# does NOT retrieve it: an FBIL rate reaches this application only as a table
# the preparer supplies, from FBIL's own publication, under whatever licence the
# firm holds. Nothing here fetches it, and no other provider is ever relabelled
# as FBIL.
FBIL_LICENCE_NOTE = (
    "FBIL benchmark data is licensed: FBIL requires an End-Users Licence and "
    "states that use is fee liable and that any use or distribution needs its "
    "express authorisation. This table is supplied by the preparer from FBIL's "
    "own publication; the engine does not retrieve it.")

TARGET_CURRENCY = "INR"

# SBI publishes on working days and the forex market closes at weekends, so a
# rate carried across a weekend or a public holiday is the correct rate. Beyond
# that the table simply does not hold the date.
MAX_CARRY_DAYS = 4

# SBI availability, recorded on every quote
SBI_AVAILABLE = "AVAILABLE"
SBI_NOT_AVAILABLE = "NOT_AVAILABLE"
SBI_STALE = "AVAILABLE_BUT_STALE"

# How far the nearest-available-date search may reach before the rate stops
# being a rate FOR this date and becomes an unrelated one. A long holiday cluster
# plus its weekends is the outer edge of "normal"; beyond MAX_CARRY_DAYS the
# substitution is materially distant and is flagged as such, and beyond
# MAX_NEAREST_DAYS it is not used at all.
MAX_NEAREST_DAYS = 30

# Universal nearest-available rule for the PRIMARY rate table. Where SBI has no
# rate for the date and no secondary source can supply one, the nearest rate
# within this many days - in either direction, prior winning a tie - is used and
# flagged. Beyond it the rate is not a rate for the date and is not used.
MAX_NEAREST_PRIMARY_DAYS = 7

# A rate table this engine reads. Same shape for every external source.
RATE_TABLE_COLUMNS = ["date", "base_currency", "quote_currency", "rate",
                      "source", "retrieved_on", "status", "notes"]

# Retrieval status
STATUS_EXACT = "EXACT_DATE"
STATUS_NEAREST = "NEAREST_AVAILABLE_DATE"
STATUS_ADJUSTED = "DATE_ADJUSTED_TO_PRIOR_PUBLISHED"
STATUS_CARRIED = "CARRIED_FORWARD"
STATUS_UNAVAILABLE = "UNAVAILABLE"

GOOGLE_COLUMNS = ["date", "base_currency", "quote_currency", "rate",
                  "source", "retrieved_on", "status", "notes"]

def fallback_banner(source_type: str) -> str:
    """The one-line "this is not a TTBR" statement for a given provider.

    Built from the provider name rather than written per provider, so a source
    added tomorrow announces itself in exactly the same words - and so no
    fallback can ever be printed without saying whose rate it is.
    """
    return (f"SBI TTBR unavailable - "
            f"{PROVIDER_NAME.get(source_type, source_type)} used")


#: The Google Finance banner, kept as a name because it is the most common one.
FALLBACK_BANNER = fallback_banner(GOOGLE_FINANCE)


def pair_symbol(base: str, target: str = TARGET_CURRENCY) -> str:
    """CURRENCY:USDINR, CURRENCY:EURINR, CURRENCY:CHFINR - built, never hardcoded."""
    return f"CURRENCY:{str(base).strip().upper()}{str(target).strip().upper()}"


def google_formula(base: str, date_ref: str, target: str = TARGET_CURRENCY) -> str:
    """The Google Sheets formula for one currency/date pair.

    `date_ref` is a cell reference (A2) or a DATE(...) literal, so the same
    consolidated table works whether the dates sit in a column or are written in.
    """
    return f'=INDEX(GOOGLEFINANCE("{pair_symbol(base, target)}","price",{date_ref}),2,2)'


@dataclass
class FXQuote:
    """One resolved rate, with everything needed to defend it in a working paper."""
    requested_date: dt.date
    used_date: dt.date
    base_currency: str
    target_currency: str
    rate: float
    source_type: str
    source_name: str
    verified: bool
    status: str
    sbi_availability: str = SBI_AVAILABLE
    purpose: str = ""
    convention: str = ""
    # Where a provider quotes against something other than INR, the INR rate is
    # DERIVED. These retain the component rates and the arithmetic, so the
    # figure can be checked without going back to the provider.
    components: tuple = ()          # ((label, pair, date, rate), ...)
    derivation: str = ""            # the calculation, in words
    # Some providers publish different things under one name - FBIL measures
    # USD/INR from rupee transactions but crosses EUR, GBP and JPY through it.
    # Where that is so, the quote carries the method that applies to IT.
    methodology_note: str = ""
    # True when the PROVIDER published this rate as a cross of its own - FBIL's
    # EUR/INR, for instance. The figure is taken as published, not recomputed
    # here, but it is not a directly quoted rupee rate either and the working
    # paper must not call it one.
    provider_cross: bool = False

    @property
    def provider(self) -> str:
        return PROVIDER_NAME.get(self.source_type, self.source_type)

    @property
    def methodology(self) -> str:
        return self.methodology_note or PROVIDER_METHOD.get(self.source_type, "")

    @property
    def fallback_level(self) -> int:
        return RANK.get(self.source_type, RANK[UNRESOLVED])

    @property
    def is_derived(self) -> bool:
        return bool(self.components)

    @property
    def rate_kind(self) -> str:
        """What KIND of rate this is - three states, not two.

        A provider's own cross is not a direct quote and is not something this
        engine derived. Collapsing it into either would misdescribe it.
        """
        if self.components:
            return "derived cross-rate"
        if self.provider_cross:
            return "provider's own cross-rate"
        return "directly quoted"

    @property
    def date_adjustment_days(self) -> int:
        """Signed: positive when a PRIOR date was used, negative when a later one."""
        return (self.requested_date - self.used_date).days

    @property
    def gap_days(self) -> int:
        """How far the rate's date is from the date required, in either direction."""
        return abs(self.date_adjustment_days)

    @property
    def direction(self) -> str:
        d = self.date_adjustment_days
        return "exact" if d == 0 else ("prior" if d > 0 else "later")

    @property
    def is_nearest_date(self) -> bool:
        return self.status == STATUS_NEAREST

    @property
    def is_materially_distant(self) -> bool:
        """Beyond a weekend or holiday cluster - not a routine substitution."""
        return self.gap_days > MAX_CARRY_DAYS

    @property
    def is_fallback(self) -> bool:
        return self.source_type != SBI_TTBR

    @property
    def fallback_banner(self) -> str:
        """Names the provider that actually supplied this rate. Never "TTBR"."""
        return fallback_banner(self.source_type)

    @property
    def pair(self) -> str:
        return f"{self.base_currency}/{self.target_currency}"

    def _date_phrase(self) -> str:
        if not self.is_nearest_date:
            return ""
        return (f"; exact date unavailable, nearest available {self.direction} "
                f"date {self.used_date:%d-%m-%Y} used ({self.gap_days} day"
                f"{'' if self.gap_days == 1 else 's'} from "
                f"{self.requested_date:%d-%m-%Y})"
                + (" - MATERIALLY DISTANT, review before filing"
                   if self.is_materially_distant else ""))

    @property
    def basis(self) -> str:
        if self.source_type == SBI_TTBR:
            if self.date_adjustment_days > MAX_CARRY_DAYS:
                return (f"SBI TTBR carried forward {self.date_adjustment_days} days "
                        f"from {self.used_date:%d-%m-%Y}")
            return "SBI TT buying rate"
        if self.source_type == GOOGLE_FINANCE and self.is_nearest_date:
            # Never let the substituted date read as the date required.
            return ("SBI TTBR unavailable; exact Google Finance date "
                    f"unavailable; nearest available {self.direction} date "
                    f"{self.used_date:%d-%m-%Y} used ({self.gap_days} day"
                    f"{'' if self.gap_days == 1 else 's'} from "
                    f"{self.requested_date:%d-%m-%Y})"
                    + (" - MATERIALLY DISTANT, review before filing"
                       if self.is_materially_distant else ""))
        # Every other fallback: name the provider, state any date substitution,
        # and where the INR rate was derived, state the arithmetic.
        txt = self.fallback_banner + self._date_phrase()
        if self.derivation:
            txt += f". {self.derivation}"
        return txt


class SBITTBRSource:
    """The statutory house rate. Exact date, else the most recent published one."""

    source_type = SBI_TTBR

    def __init__(self, frame_getter):
        self._frame = frame_getter        # callable, so a merge() is picked up

    def lookup(self, on: dt.date, currency: str, strict: bool = True,
               nearest_within: int | None = None):
        """The rate for this date, or the nearest one the caller will accept.

        `nearest_within` switches on the universal nearest-available rule: look
        BOTH ways up to that many days and take whichever published rate is
        closest, a tie going to the prior date. Without it only prior rates are
        considered, which is the ordinary carry-forward.
        """
        df = self._frame()
        pool = df[df["currency"] == currency]
        if pool.empty:
            return None
        if nearest_within is not None:
            exact = pool[pool["date"] == on]
            if not len(exact):
                prior = pool[pool["date"] < on]
                later = pool[pool["date"] > on]
                cands = []
                if len(prior):
                    r = prior.sort_values("date").iloc[-1]
                    cands.append(((on - r["date"]).days, 0, r))
                if len(later):
                    r = later.sort_values("date").iloc[0]
                    cands.append(((r["date"] - on).days, 1, r))
                if not cands:
                    return None
                gap, _, row = min(cands, key=lambda c: (c[0], c[1]))
                if gap > nearest_within:
                    return None
                return FXQuote(
                    requested_date=on, used_date=row["date"], base_currency=currency,
                    target_currency=TARGET_CURRENCY, rate=float(row["ttbr"]),
                    source_type=SBI_TTBR, source_name=str(row["source"]),
                    verified=bool(row["verified"]), status=STATUS_NEAREST,
                    sbi_availability=SBI_STALE)
        sub = pool[pool["date"] <= on]
        if sub.empty:
            return None
        row = sub.iloc[-1]
        gap = (on - row["date"]).days
        # In strict mode a rate more than a weekend old is NOT treated as
        # available for this date, so a secondary source gets its chance at the
        # exact date. The carried-forward rate is still offered afterwards, in
        # preference to reporting nothing at all.
        if strict and gap > MAX_CARRY_DAYS:
            return None
        return FXQuote(
            requested_date=on, used_date=row["date"], base_currency=currency,
            target_currency=TARGET_CURRENCY, rate=float(row["ttbr"]),
            source_type=SBI_TTBR, source_name=str(row["source"]),
            verified=bool(row["verified"]),
            status=STATUS_EXACT if gap == 0 else (
                STATUS_ADJUSTED if gap <= MAX_CARRY_DAYS else STATUS_CARRIED),
            sbi_availability=SBI_AVAILABLE if gap <= MAX_CARRY_DAYS else SBI_STALE)


class TableFXSource:
    """Any external historical source the engine reads from a local rate table.

    One search rule for all of them, so no provider quietly behaves differently:

        exact date first;
        otherwise the NEAREST available date within MAX_NEAREST_DAYS, looking
        both ways, a tie going to the PRIOR date;
        beyond that window the rate is not a rate for this date and is not used.

    Nothing is estimated and nothing is interpolated. A pair that is not in the
    table simply does not resolve. A blank, zero or non-numeric rate is dropped
    on load, so a provider that returned nothing for a date leaves it
    unresolved rather than converting it at zero.
    """

    source_type = "TABLE"

    def __init__(self, path: str | Path | None = None, frame: pd.DataFrame | None = None):
        self.path = Path(path) if path else None
        self.df = self._load(frame)

    def _load(self, frame) -> pd.DataFrame:
        if frame is not None:
            df = frame.copy()
        elif self.path and self.path.exists():
            try:
                df = pd.read_csv(self.path)
            except Exception:
                return pd.DataFrame(columns=RATE_TABLE_COLUMNS)
        else:
            return pd.DataFrame(columns=RATE_TABLE_COLUMNS)
        if df.empty:
            return pd.DataFrame(columns=RATE_TABLE_COLUMNS)
        for c in RATE_TABLE_COLUMNS:
            if c not in df.columns:
                df[c] = ""
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
        df["base_currency"] = df["base_currency"].astype(str).str.strip().str.upper()
        df["quote_currency"] = (df["quote_currency"].astype(str).str.strip()
                                .str.upper().replace({"": TARGET_CURRENCY,
                                                      "NAN": TARGET_CURRENCY}))
        df = df.dropna(subset=["date", "rate"])
        df = df[df["rate"] > 0]
        return df.sort_values("date")

    def merge(self, rows: pd.DataFrame) -> None:
        add = self._load(rows)
        if add.empty:
            return
        self.df = (pd.concat([add, self.df], ignore_index=True)
                   .drop_duplicates(subset=["date", "base_currency", "quote_currency"],
                                    keep="first")
                   .sort_values("date").reset_index(drop=True))

    # ------------------------------------------------------------------
    def _pick(self, pool: pd.DataFrame, on: dt.date):
        """Exact, else nearest within the window. Returns (row, status) or None."""
        if pool.empty:
            return None
        exact = pool[pool["date"] == on]
        if len(exact):
            return exact.iloc[-1], STATUS_EXACT
        prior, later = pool[pool["date"] < on], pool[pool["date"] > on]
        cands = []
        if len(prior):
            r = prior.sort_values("date").iloc[-1]
            cands.append(((on - r["date"]).days, 0, r))     # 0 sorts before 1
        if len(later):
            r = later.sort_values("date").iloc[0]
            cands.append(((r["date"] - on).days, 1, r))
        if not cands:
            return None
        gap, _, row = min(cands, key=lambda c: (c[0], c[1]))
        if gap > MAX_NEAREST_DAYS:
            return None
        return row, STATUS_NEAREST

    def _quote(self, row, status, on, currency, **extra) -> FXQuote:
        return FXQuote(
            requested_date=on, used_date=row["date"], base_currency=currency,
            target_currency=TARGET_CURRENCY, rate=float(row["rate"]),
            source_type=self.source_type,
            source_name=str(row["source"] or PROVIDER_NAME.get(self.source_type, "")),
            verified=False, status=status, sbi_availability=SBI_NOT_AVAILABLE,
            **extra)

    def lookup(self, on: dt.date, currency: str, strict: bool = True,
               nearest_within: int | None = None):
        """A DIRECT quote of `currency` against INR, if the table holds one."""
        cur = str(currency).strip().upper()
        if cur == TARGET_CURRENCY:
            return None
        pool = self.df[(self.df["base_currency"] == cur)
                       & (self.df["quote_currency"] == TARGET_CURRENCY)]
        got = self._pick(pool, on)
        return None if got is None else self._quote(got[0], got[1], on, cur)


class GoogleFinanceSource(TableFXSource):
    """Historical FX from the consolidated Google Finance fallback table.

    The table holds one row per currency/date pair the engine actually needed.
    It is produced by this engine (see FXTable.google_request_frame), populated
    by Google Sheets, and read back here.
    """

    source_type = GOOGLE_FINANCE


class FBILSource(TableFXSource):
    """FBIL reference rates - Financial Benchmarks India Pvt Ltd.

    FBIL publishes four rupee reference rates - USD/INR, EUR/INR, GBP/INR and
    JPY/INR - but they are NOT the same kind of thing, and the working paper must
    not pretend they are:

      * USD/INR is a DIRECT rate. It is the volume-weighted average of actual
        spot USD/INR transactions in a randomly selected 15-minute window inside
        11:30-12:30 IST, with a minimum of ten transactions aggregating USD 25
        million and outliers removed on a +/-3SD rule.
      * EUR/INR, GBP/INR and JPY/INR are FBIL's OWN CROSSES. FBIL takes the
        EUR/USD, GBP/USD or USD/JPY cross-currency rate from electronic
        platforms in the same window and crosses it with its USD/INR reference
        rate. No rupee transaction in those currencies is measured.

    So only USD/INR carries the direct methodology. The other three carry the
    cross methodology, name the leg FBIL crossed through, and - where the table
    supplies the legs rather than the published figure - show the arithmetic.

    Two further things this class will not do:

      * It is NOT the SBI TT buying rate and is never presented as one. FBIL's
        rate is a market average around midday; a TT buying rate is a bank's own
        quoted rate. Different things, different numbers.
      * No other provider is ever written into this table. FBIL benchmark data
        is licensed - FBIL requires an End-Users Licence, states that use is fee
        liable, and permits use or distribution only with its express
        authorisation - so the engine does not retrieve it. An FBIL rate reaches
        the engine only in a table the preparer supplies from FBIL's own
        publication. A rate obtained from a third party that derives it from the
        euro, or from anywhere else, is not FBIL and must not be labelled FBIL,
        however close the number.
    """

    source_type = FBIL

    #: the ONLY pair FBIL measures directly against the rupee
    DIRECT_PAIRS = ("USD",)

    #: what FBIL crosses through for each of the others, per its methodology
    CROSS_LEG = {"EUR": "EUR/USD", "GBP": "GBP/USD", "JPY": "USD/JPY"}

    def lookup(self, on: dt.date, currency: str, strict: bool = True,
               nearest_within: int | None = None):
        cur = str(currency).strip().upper()
        if cur == TARGET_CURRENCY:
            return None

        pool = self.df[(self.df["base_currency"] == cur)
                       & (self.df["quote_currency"] == TARGET_CURRENCY)]
        got = self._pick(pool, on)

        if cur == "USD":
            # The one direct rate. Nothing is derived and nothing is inferred.
            return None if got is None else self._quote(
                got[0], got[1], on, cur, methodology_note=FBIL_DIRECT_METHOD)

        leg = self.CROSS_LEG.get(cur)
        if leg is None:
            # FBIL publishes four rupee rates and no others. A currency outside
            # them is not an FBIL rate at all, whatever a table might contain.
            return None

        cross_method = FBIL_CROSS_METHOD.format(pair=f"{cur}/INR", leg=leg)
        if got is not None:
            # FBIL's own published figure for the pair. Taken as published - it
            # is not re-derived - but described as the cross that it is.
            row, status = got
            return self._quote(
                row, status, on, cur, methodology_note=cross_method,
                provider_cross=True,
                derivation=(
                    f"FBIL's published {cur}/INR reference rate for "
                    f"{row['date']:%d-%m-%Y}, taken as published. FBIL computes "
                    f"it by crossing the {leg} rate with its USD/INR reference "
                    f"rate; it is not a measured {cur}/INR rupee rate."))

        # The table carries the legs but not the published figure: reproduce
        # FBIL's own cross, from one published day, and show the arithmetic.
        return self._cross(on, cur, leg, cross_method)

    def _cross(self, on: dt.date, cur: str, leg: str, method: str):
        """FBIL's own construction: the cross-currency leg times USD/INR."""
        usd_inr_pool = self.df[(self.df["base_currency"] == "USD")
                               & (self.df["quote_currency"] == TARGET_CURRENCY)]
        got = self._pick(usd_inr_pool, on)
        if got is None:
            return None
        usd_inr, status = got
        day = usd_inr["date"]

        base, quote = leg.split("/")
        legs = self.df[(self.df["base_currency"] == base)
                       & (self.df["quote_currency"] == quote)
                       & (self.df["date"] == day)]
        if not len(legs):
            return None                      # no same-day leg, no cross
        leg_row = legs.iloc[-1]
        leg_rate = float(leg_row["rate"])
        if not leg_rate:
            return None

        # EUR/USD and GBP/USD are quoted per unit of the foreign currency, so
        # they MULTIPLY. USD/JPY is quoted per USD, so it divides.
        if quote == "USD":
            rate = leg_rate * float(usd_inr["rate"])
            how = (f"{leg} {leg_rate:.6f} x USD/INR "
                   f"{float(usd_inr['rate']):.4f} = {rate:.4f}")
        else:
            rate = float(usd_inr["rate"]) / leg_rate
            how = (f"USD/INR {float(usd_inr['rate']):.4f} / {leg} "
                   f"{leg_rate:.6f} = {rate:.4f}")

        return FXQuote(
            requested_date=on, used_date=day, base_currency=cur,
            target_currency=TARGET_CURRENCY, rate=rate,
            source_type=FBIL,
            source_name=str(usd_inr["source"] or PROVIDER_NAME[FBIL]),
            verified=False, status=status, sbi_availability=SBI_NOT_AVAILABLE,
            methodology_note=method,
            components=((leg, leg, day, leg_rate),
                        ("USD/INR", "USD/INR", day, float(usd_inr["rate"]))),
            derivation=(f"Reproduces FBIL's own construction for {day:%d-%m-%Y}: "
                        f"{cur}/INR = {how}. FBIL does not measure a {cur}/INR "
                        f"rupee rate - only its USD/INR is direct."))


class ECBSource(TableFXSource):
    """ECB euro foreign exchange reference rates - the independent third source.

    The ECB quotes everything against the EURO, so only EUR/INR is direct. For
    any other currency the INR rate is DERIVED:

        base/INR = (EUR/INR) / (EUR/base)

    Both component rates must come from the SAME published day, or the cross is
    not a rate for a date at all. The components and the arithmetic are carried
    on the quote and printed in the working paper, so the figure can be checked
    without going back to the ECB.

    The ECB states its reference rates are published for information only and
    are not intended for market transactions, so it never outranks the SBI TT
    buying rate. It sits above FBIL for a practical reason rather than a
    methodological one: it is openly published and freely retrievable, whereas
    FBIL is licensed data the engine cannot fetch and that reaches a run only
    when the preparer supplies it.
    """

    source_type = ECB

    def lookup(self, on: dt.date, currency: str, strict: bool = True,
               nearest_within: int | None = None):
        cur = str(currency).strip().upper()
        if cur == TARGET_CURRENCY:
            return None

        # EUR/INR is stated directly - always preferred over a derivation.
        eur_inr_pool = self.df[(self.df["base_currency"] == "EUR")
                               & (self.df["quote_currency"] == TARGET_CURRENCY)]
        if cur == "EUR":
            got = self._pick(eur_inr_pool, on)
            return None if got is None else self._quote(got[0], got[1], on, cur)

        # Anything else: cross through the euro, same published day for both.
        got = self._pick(eur_inr_pool, on)
        if got is None:
            return None
        eur_inr, status = got
        day = eur_inr["date"]
        eur_base = self.df[(self.df["base_currency"] == "EUR")
                           & (self.df["quote_currency"] == cur)
                           & (self.df["date"] == day)]
        if not len(eur_base):
            return None                      # no same-day pair, no cross
        eur_base = eur_base.iloc[-1]
        if not float(eur_base["rate"]):
            return None
        rate = float(eur_inr["rate"]) / float(eur_base["rate"])
        return FXQuote(
            requested_date=on, used_date=day, base_currency=cur,
            target_currency=TARGET_CURRENCY, rate=rate,
            source_type=ECB,
            source_name=str(eur_inr["source"] or PROVIDER_NAME[ECB]),
            verified=False, status=status, sbi_availability=SBI_NOT_AVAILABLE,
            components=(("EUR/INR", "EUR/INR", day, float(eur_inr["rate"])),
                        (f"EUR/{cur}", f"EUR/{cur}", day, float(eur_base["rate"]))),
            derivation=(f"Cross-rate derived from ECB euro reference rates on "
                        f"{day:%d-%m-%Y}: {cur}/INR = EUR/INR "
                        f"{float(eur_inr['rate']):.4f} / EUR/{cur} "
                        f"{float(eur_base['rate']):.6f} = {rate:.4f}. The ECB "
                        f"does not publish {cur}/INR directly."))


class ManualFXSource(TableFXSource):
    """Rates the preparer entered and signed off. Lowest rank, highest ceremony."""

    source_type = MANUAL
