"""SBI TT buying rate handling.

Design principle: for tax work the rate table is EVIDENCE, not a black box.
Every rate the engine uses is written into the working sheet with its source and
whether it has been verified, so the whole schedule can be defended.

Rates come from config/fx_rates.csv. A fetcher can PROPOSE new rows, but nothing
is ever applied silently - unverified rates raise a visible warning on every run.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .models import FX_PREV_MONTH_END, FX_SAME_DAY, FX_COLUMNS
from .fxsources import (
    ECB, FBIL, GOOGLE_COLUMNS, GOOGLE_FINANCE, MANUAL, MAX_CARRY_DAYS,
    MAX_NEAREST_PRIMARY_DAYS, PROVIDER_METHOD, PROVIDER_NAME, RANK,
    SBI_AVAILABLE, SBI_NOT_AVAILABLE, SBI_STALE,
    SBI_TTBR, STATUS_CARRIED,
    STATUS_EXACT, STATUS_NEAREST, STATUS_UNAVAILABLE, TARGET_CURRENCY,
    FBIL_LICENCE_NOTE, UNRESOLVED, ECBSource, FBILSource, FXQuote,
    GoogleFinanceSource, ManualFXSource, SBITTBRSource, fallback_banner,
    google_formula, pair_symbol,
)
from .review import FX_FALLBACK, FX_UNAVAILABLE, STALE_FX


#: The ranked chain, in one place. Everything that needs the order - the
#: blocker text, the source-hierarchy frame, the workbook - reads it from here,
#: so a change of rank cannot leave one rendering saying something else.
RANKED = tuple(sorted((SBI_TTBR, GOOGLE_FINANCE, ECB, FBIL, MANUAL),
                      key=lambda s: RANK[s]))


def _review_flag(u) -> str:
    """The one-line status a reviewer scans the FX_WORKING column for."""
    if not u.rate:
        return "FX_UNAVAILABLE - INR left blank"
    if u.is_fallback:
        banner = fallback_banner(u.source_type)
        tail = (" - DERIVED CROSS-RATE" if u.components
                else (" - PROVIDER'S OWN CROSS-RATE" if u.provider_cross else ""))
        if u.status == STATUS_NEAREST:
            if u.gap_days > MAX_CARRY_DAYS:
                tail = " - MATERIALLY DISTANT" + tail
            return (f"{banner}; nearest available date used "
                    f"({u.gap_days}d){tail}")
        return banner + tail
    if u.date_adjustment_days > MAX_CARRY_DAYS:
        return "Carried forward - review"
    return ""


def _standing(source_type: str) -> str:
    """What this source is, and is not, in one line for the working paper."""
    if source_type == SBI_TTBR:
        return "The statutory house rate"
    txt = ("NOT an SBI TT buying rate - labelled as "
           + PROVIDER_NAME[source_type] + " wherever used")
    if source_type == FBIL:
        txt += (". Only FBIL's USD/INR is a direct rupee rate; EUR/INR, GBP/INR "
                "and JPY/INR are FBIL's own crosses through USD. " +
                FBIL_LICENCE_NOTE)
    return txt


def _rate_kind(u) -> str:
    """Three states, because a provider's own cross is neither of the other two."""
    if u.components:
        return "derived cross-rate"
    if u.provider_cross:
        return "provider's own cross-rate"
    return "directly quoted"


def _components_text(u) -> str:
    """The component rates behind a derived cross-rate, one per line."""
    return " | ".join(f"{label} on {d:%d-%m-%Y} = {rate:.6f}"
                      for label, _pair, d, rate in u.components)


@dataclass
class RateUse:
    """Audit record: which rate was used, for what, and from when."""
    requested_date: dt.date
    used_date: dt.date
    currency: str
    rate: float
    convention: str
    purpose: str
    source: str
    verified: bool
    source_type: str = SBI_TTBR
    status: str = STATUS_EXACT
    sbi_availability: str = SBI_AVAILABLE
    basis: str = "SBI TT buying rate"
    target_currency: str = TARGET_CURRENCY
    gap_days: int = 0
    components: tuple = ()
    derivation: str = ""
    methodology_note: str = ""
    provider_cross: bool = False

    @property
    def is_carried_forward(self) -> bool:
        return self.used_date != self.requested_date

    @property
    def is_fallback(self) -> bool:
        return self.source_type != SBI_TTBR

    @property
    def provider(self) -> str:
        return PROVIDER_NAME.get(self.source_type, self.source_type)

    @property
    def methodology(self) -> str:
        # A provider whose method differs by pair states the one that applies
        # to this rate - FBIL measures USD/INR but crosses EUR, GBP and JPY.
        return self.methodology_note or PROVIDER_METHOD.get(self.source_type, "")

    @property
    def fallback_level(self) -> int:
        return RANK.get(self.source_type, RANK[UNRESOLVED])

    @property
    def date_adjustment_days(self) -> int:
        return (self.requested_date - self.used_date).days


class FXTable:
    """Resolves a rate through ranked sources and records how it got there.

    SBI TTBR first. Where SBI has nothing for the date, Google Finance, then
    the ECB, then FBIL, then a preparer-entered rate. Where none of them has
    it, the rate is UNRESOLVED - the caller leaves the INR figure blank and a
    blocker is raised. Never zero, never an unrelated period-end rate, never an
    estimate.

    Each external source is searched under the SAME rule: exact date, else the
    nearest available date within 30 days either way with a tie going to the
    prior date. The search never crosses into a different year to find a rate.
    """

    #: where the fetched external tables live, if they have been populated
    FBIL_TABLE = "config/fx_fbil.csv"
    ECB_TABLE = "config/fx_ecb.csv"

    def __init__(self, path: str | Path, register=None, google_path: str | Path | None = None,
                 google_frame: pd.DataFrame | None = None,
                 fbil_path: str | Path | None = None,
                 fbil_frame: pd.DataFrame | None = None,
                 ecb_path: str | Path | None = None,
                 ecb_frame: pd.DataFrame | None = None):
        self.path = Path(path)
        self.uses: list[RateUse] = []
        self.warnings: list[str] = []
        self.missing: set = set()
        self.required: set = set()      # every currency/date pair asked for
        self.unresolved: set = set()    # pairs no source could supply
        self.register = register
        self._load()
        root = self.path.parent
        self.google = GoogleFinanceSource(path=google_path, frame=google_frame)
        self.fbil = FBILSource(
            path=(fbil_path if fbil_path is not None else root / "fx_fbil.csv"),
            frame=fbil_frame)
        self.ecb = ECBSource(
            path=(ecb_path if ecb_path is not None else root / "fx_ecb.csv"),
            frame=ecb_frame)
        self.manual = ManualFXSource()
        self.sbi = SBITTBRSource(lambda: self.df)
        # Rank order. A future provider is one entry in this list.
        self.sources = [self.sbi, self.google, self.ecb, self.fbil, self.manual]

    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(
                f"FX rate table not found at {self.path}. "
                "Create it with columns: " + ", ".join(FX_COLUMNS)
            )
        df = pd.read_csv(self.path, dtype={"currency": str, "source": str})
        missing = set(FX_COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(f"fx_rates.csv is missing columns: {sorted(missing)}")
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["ttbr"] = pd.to_numeric(df["ttbr"], errors="coerce")
        df["verified"] = (
            df["verified"].astype(str).str.strip().str.upper().isin(["Y", "YES", "TRUE", "1"])
        )
        # A blank, zero or negative TTBR is not a rate - dropped on load, same
        # as every other source (see TableFXSource._load), so a bad or empty
        # cell in the SBI table leaves the date unresolved rather than
        # resolving it at zero or below.
        df = df.dropna(subset=["ttbr"])
        df = df[df["ttbr"] > 0].sort_values("date")
        self.df = df

    def merge(self, rows: pd.DataFrame, priority: bool = True) -> None:
        """Fold in a user-supplied TTBR table. User rates win on a clash."""
        if rows is None or rows.empty:
            return
        add = rows.copy()
        add["date"] = pd.to_datetime(add["date"]).dt.date
        add["ttbr"] = pd.to_numeric(add["ttbr"], errors="coerce")
        add["verified"] = (add["verified"].astype(str).str.strip().str.upper()
                           .isin(["Y", "YES", "TRUE", "1"]))
        combined = (pd.concat([add, self.df], ignore_index=True) if priority
                    else pd.concat([self.df, add], ignore_index=True))
        combined = combined.dropna(subset=["ttbr"])
        combined = combined[combined["ttbr"] > 0]
        self.df = (combined
                   .drop_duplicates(subset=["date", "currency"], keep="first")
                   .sort_values("date").reset_index(drop=True))

    def merge_google(self, rows: pd.DataFrame) -> None:
        """Fold in a populated Google Finance FX table supplied with the documents."""
        self.google.merge(rows)

    def merge_fbil(self, rows: pd.DataFrame) -> None:
        """Fold in an FBIL reference-rate table (see src/fxfetch.py)."""
        self.fbil.merge(rows)

    def merge_ecb(self, rows: pd.DataFrame) -> None:
        """Fold in an ECB euro reference-rate table (see src/fxfetch.py)."""
        self.ecb.merge(rows)

    # ------------------------------------------------------------------
    def resolve(self, on: dt.date, currency: str) -> FXQuote | None:
        """Walk the ranked sources. Returns None when no source has the date.

        SBI is asked twice on purpose. First strictly - "do you have this date,
        or one a weekend away?" If not, each external source in turn gets its
        chance at the EXACT date, which beats an SBI rate weeks stale. Only if
        none of them has anything does the stale SBI rate come back, flagged,
        because a disclosed approximation still beats reporting nothing.

        The first source that answers wins outright: a lower-ranked source is
        never preferred merely because its date is closer. Within a source,
        exact beats nearest.
        """
        self.required.add((on, currency))
        for src in self.sources:
            q = src.lookup(on, currency, strict=True)
            if q is not None:
                if q.is_fallback:
                    self._flag_fallback(q)
                return q

        # Nothing had the date. Before falling back on an old rate carried
        # forward, take the NEAREST published rate within a week either way -
        # a rate three days after the date beats one thirty days before it.
        # Universal: it applies to every currency and every broker.
        q = self.sbi.lookup(on, currency, strict=False,
                            nearest_within=MAX_NEAREST_PRIMARY_DAYS)
        if q is not None and q.status == STATUS_NEAREST:
            self._flag_nearest_primary(q)
            return q

        # Otherwise SBI's most recent published rate rather than nothing -
        # visibly, as a carried-forward approximation.
        q = self.sbi.lookup(on, currency, strict=False)
        if q is not None:
            self._flag_stale(q)
            return q

        self.missing.add((on, currency))
        self.unresolved.add((on, currency))
        if self.register is not None:
            tried = ", ".join(PROVIDER_NAME[s] for s in RANKED)
            self.register.blocker(
                FX_UNAVAILABLE, f"{currency} {on:%d-%m-%Y}",
                f"No rate for {currency}/{TARGET_CURRENCY} on or near "
                f"{on:%d-%m-%Y} from any ranked source. Sources searched, in "
                f"order: {tried}. Every INR figure needing this rate is left "
                "BLANK - it is not converted at zero and not converted at an "
                "unrelated period-end rate.",
                self.path.name,
                f"Add the SBI TTBR for {on:%d-%m-%Y}, or populate the Google "
                f"Finance fallback table with {pair_symbol(currency)} for that "
                f"date, or supply an FBIL or ECB table covering it, and re-run.")
        return None

    def _flag_fallback(self, q: FXQuote) -> None:
        """One review entry per fallback, naming the provider that supplied it.

        The wording is built from the quote, not written per source, so FBIL and
        the ECB are reported to the same standard as Google Finance and no
        provider can slip through with a thinner disclosure than another.
        """
        if self.register is None:
            return
        adj, action = "", (
            "Obtain the SBI TTBR for this date if the assessing officer requires "
            f"the statutory rate, or accept the {q.provider} rate in writing.")
        if q.is_nearest_date:
            adj = (f" {q.provider} publishes no quote for "
                   f"{q.requested_date:%d-%m-%Y} (weekend, holiday or other "
                   f"non-trading date), so the nearest available {q.direction} "
                   f"date {q.used_date:%d-%m-%Y} was used - a gap of "
                   f"{q.gap_days} day{'' if q.gap_days == 1 else 's'}.")
        if q.is_derived:
            adj += (f" The INR rate is DERIVED, not quoted. {q.derivation} "
                    "Component rates: "
                    + "; ".join(f"{label} on {d:%d-%m-%Y} = {rate:.6f}"
                                for label, _p, d, rate in q.components) + ".")
        if q.is_materially_distant:
            # A weekend or holiday is routine. This is not, and must not pass on
            # the same footing as one.
            adj += (f" This gap of {q.gap_days} days is MATERIALLY DISTANT - it is "
                    "wider than a weekend or holiday cluster, so the rate is not a "
                    "rate for the date required.")
            action = (f"Supply the rate for {q.requested_date:%d-%m-%Y} itself - "
                      f"SBI TTBR, or a {q.provider} quote for that date - or "
                      "accept this substitution in writing before filing.")
        self.register.review(
            FX_FALLBACK, f"{q.pair} {q.requested_date:%d-%m-%Y}",
            f"{q.fallback_banner} (fallback level {q.fallback_level} of "
            f"{RANK[UNRESOLVED]}). Rate {q.rate:.4f} for {q.pair} from "
            f"{pair_symbol(q.base_currency)}, dated {q.used_date:%d-%m-%Y} "
            f"({q.status}).{adj} Provider methodology: {q.methodology} This is "
            "NOT an SBI TT buying rate and is labelled as such throughout the "
            "workbook.",
            q.source_name, action)

    def _flag_nearest_primary(self, q: FXQuote) -> None:
        """An SBI rate taken from the nearest date rather than the date itself."""
        if self.register is None:
            return
        self.register.review(
            STALE_FX, f"{q.base_currency} {q.requested_date:%d-%m-%Y}",
            f"No SBI TTBR is published for {q.requested_date:%d-%m-%Y} and no "
            f"secondary source could supply it. The nearest available "
            f"{q.direction} rate, {q.rate:.4f} dated {q.used_date:%d-%m-%Y}, was "
            f"used - a gap of {q.gap_days} day"
            f"{'' if q.gap_days == 1 else 's'}, within the "
            f"{MAX_NEAREST_PRIMARY_DAYS}-day nearest-available window. The "
            "substituted date is recorded on FX_WORKING; it is not presented as "
            "the date required.",
            self.path.name,
            f"Add the SBI TTBR for {q.requested_date:%d-%m-%Y} itself, or accept "
            "the nearest-date rate in writing.")

    def _flag_stale(self, q: FXQuote) -> None:
        if self.register is None:
            return
        self.register.review(
            STALE_FX, f"{q.base_currency} {q.requested_date:%d-%m-%Y}",
            f"No SBI TTBR is on file for {q.requested_date:%d-%m-%Y} and no "
            f"secondary source - Google Finance, the ECB or FBIL - has a rate "
            f"for it either. The rate "
            f"of {q.rate:.4f} dated {q.used_date:%d-%m-%Y} was carried forward "
            f"{q.date_adjustment_days} days. SBI publishes daily on working "
            "days, so a gap this wide is a missing rate, not a non-working day.",
            self.path.name,
            f"Add the SBI TTBR for {q.requested_date:%d-%m-%Y}, populate the "
            "Google Finance fallback table, or accept the carried-forward rate "
            "in writing.")

    def rate_quote(self, on: dt.date, currency: str = "USD",
                   convention: str = FX_SAME_DAY, purpose: str = ""):
        """The resolved quote, or None. Callers blank the INR figure on None."""
        target = on if convention == FX_SAME_DAY else _prev_month_end(on)
        q = self.resolve(target, currency)
        if q is not None:
            q.purpose, q.convention = purpose, convention
        self.uses.append(RateUse(
            requested_date=target,
            used_date=q.used_date if q else target,
            currency=currency,
            rate=q.rate if q else 0.0,
            convention=convention,
            purpose=purpose,
            source=q.source_name if q else "UNRESOLVED - no rate from any source",
            verified=bool(q.verified) if q else False,
            source_type=q.source_type if q else "NONE",
            status=q.status if q else STATUS_UNAVAILABLE,
            sbi_availability=q.sbi_availability if q else SBI_NOT_AVAILABLE,
            basis=q.basis if q else "No rate available - INR figure left blank",
            gap_days=q.gap_days if q else 0,
            components=q.components if q else (),
            derivation=q.derivation if q else "",
            methodology_note=q.methodology_note if q else "",
            provider_cross=bool(q.provider_cross) if q else False,
        ))
        if q is not None and not q.verified:
            msg = (f"UNVERIFIED rate used: {currency} {q.rate} dated "
                   f"{q.used_date:%d-%m-%Y} ({q.source_name})")
            if msg not in self.warnings:
                self.warnings.append(msg)
        return q

    def rate_with_date(self, on: dt.date, currency: str = "USD",
                       convention: str = FX_SAME_DAY, purpose: str = ""):
        """rate() plus the date the rate actually carries - for the working paper.

        A conversion is only defensible if the sheet shows WHICH published rate
        was used, not merely which date was wanted.
        """
        q = self.rate_quote(on, currency, convention, purpose)
        target = on if convention == FX_SAME_DAY else _prev_month_end(on)
        return (q.rate if q else 0.0), (q.used_date if q else target)

    def rate(
        self,
        on: dt.date,
        currency: str = "USD",
        convention: str = FX_SAME_DAY,
        purpose: str = "",
    ) -> float:
        """The rate for a date, or 0.0 when no source has it.

        A caller that can leave a cell blank should use rate_quote() and check
        for None. 0.0 is returned only so arithmetic does not explode; it must
        never reach the working paper as a converted figure.
        """
        q = self.rate_quote(on, currency, convention, purpose)
        return q.rate if q else 0.0

    # ------------------------------------------------------------------
    AUDIT_COLUMNS = ["Purpose", "Convention", "Date required", "Rate date used",
                     "Gap (days)", "Rate date is", "Currency", "Converted to",
                     "Currency pair", "Rate", "FX source", "Provider",
                     "Fallback level", "SBI TTBR availability",
                     "Retrieval status", "Rate is", "Cross-rate components",
                     "Derivation", "Provider methodology",
                     "Basis stated in the working paper",
                     "Source", "Verified", "Review flag"]

    def audit_frame(self) -> pd.DataFrame:
        """Every rate lookup made during a run - goes into the working sheet.

        Carries full provenance per the working-paper requirement: FX date,
        source and target currency, rate, source type, whether SBI TTBR was
        available, retrieval status, any date adjustment actually applied, and
        the review flag. A Google Finance rate is never presented as an SBI rate.
        """
        if not self.uses:
            return pd.DataFrame(columns=self.AUDIT_COLUMNS)
        return pd.DataFrame(
            [
                {
                    "Purpose": u.purpose,
                    "Convention": u.convention,
                    "Date required": u.requested_date,
                    "Rate date used": u.used_date if u.rate else "",
                    "Gap (days)": (u.gap_days if u.rate and u.gap_days else ""),
                    "Rate date is": ("" if not u.rate or not u.gap_days else
                                     ("prior to the date required"
                                      if u.date_adjustment_days > 0
                                      else "later than the date required")),
                    "Currency": u.currency,
                    "Converted to": u.target_currency,
                    "Currency pair": pair_symbol(u.currency, u.target_currency),
                    "Rate": u.rate if u.rate else "",
                    "FX source": u.source_type,
                    "Provider": u.provider,
                    "Fallback level": u.fallback_level,
                    "SBI TTBR availability": u.sbi_availability,
                    "Retrieval status": u.status,
                    "Rate is": (_rate_kind(u) if u.rate else ""),
                    "Cross-rate components": _components_text(u),
                    "Derivation": u.derivation,
                    "Provider methodology": u.methodology,
                    "Basis stated in the working paper": u.basis,
                    "Source": u.source,
                    "Verified": "Yes" if u.verified else "NO",
                    "Review flag": _review_flag(u),
                }
                for u in self.uses
            ]
        ).drop_duplicates()

    SOURCE_COLUMNS = ["Rank", "FX source", "Provider", "Table loaded",
                      "Currency/date pairs supplied", "Of which derived",
                      "Methodology", "Standing"]

    def source_frame(self) -> pd.DataFrame:
        """The ranked chain and what each source actually supplied in this run.

        Reporting only - it counts what `resolve()` already decided. It is here
        rather than in a renderer so the screen, the API and the workbook all
        state the hierarchy from one place and cannot drift apart.
        """
        loaded = {SBI_TTBR: len(self.df), GOOGLE_FINANCE: len(self.google.df),
                  FBIL: len(self.fbil.df), ECB: len(self.ecb.df),
                  MANUAL: len(self.manual.df)}
        rows = []
        for st in RANKED:
            used = {(u.requested_date, u.currency) for u in self.uses
                    if u.rate and u.source_type == st}
            derived = {(u.requested_date, u.currency) for u in self.uses
                       if u.rate and u.source_type == st and u.components}
            rows.append({
                "Rank": RANK[st],
                "FX source": st,
                "Provider": PROVIDER_NAME[st],
                "Table loaded": (f"{loaded[st]} row(s)" if loaded[st]
                                 else "not supplied"),
                "Currency/date pairs supplied": len(used) or "",
                "Of which derived": len(derived) or "",
                "Methodology": PROVIDER_METHOD[st],
                "Standing": _standing(st),
            })
        rows.append({
            "Rank": RANK[UNRESOLVED],
            "FX source": UNRESOLVED,
            "Provider": "No rate from any source",
            "Table loaded": "",
            "Currency/date pairs supplied": len(self.unresolved) or "",
            "Of which derived": "",
            "Methodology": "",
            "Standing": ("The INR figure is left BLANK and raised as "
                         "FX_UNAVAILABLE. Never zero, never an unrelated "
                         "period-end rate, never an estimate."),
        })
        return pd.DataFrame(rows, columns=self.SOURCE_COLUMNS)

    def google_request_frame(self, only_missing: bool = True) -> pd.DataFrame:
        """The consolidated Google Finance FX fallback table.

        ONE row per currency/date pair the engine actually needed - deduplicated,
        never one call per transaction. Each row carries the GOOGLEFINANCE
        formula for its own pair, built from the source currency rather than
        hardcoded, so opening this block in Google Sheets fills the rate column
        in place. Saving it as CSV and passing it back as the Google fallback
        table is all that is needed to resolve the rows.

        `only_missing` keeps the table to the pairs SBI could not supply, which
        is the whole point of a fallback - ask Google for eight dates, not eight
        hundred.
        """
        pairs = sorted(self.unresolved if only_missing else self.required)
        rows = []
        for i, (d, cur) in enumerate(pairs, start=2):   # row 2 = first data row
            rows.append({
                "date": d,
                "base_currency": str(cur).upper(),
                "quote_currency": TARGET_CURRENCY,
                "google_pair": pair_symbol(cur),
                "rate": "",
                "google_finance_formula": google_formula(cur, f"A{i}"),
                "source": "Google Finance historical FX",
                "retrieved_on": "",
                "status": "REQUESTED - paste the rate or open this block in "
                          "Google Sheets",
                "notes": (f"No rate for {d:%d-%m-%Y} from any ranked source - "
                          "SBI TTBR, Google Finance, ECB, FBIL or "
                          "preparer-entered"),
            })
        return pd.DataFrame(rows, columns=[
            "date", "base_currency", "quote_currency", "google_pair", "rate",
            "google_finance_formula", "source", "retrieved_on", "status", "notes"])

    @property
    def fallback_used(self) -> bool:
        return any(u.is_fallback and u.rate for u in self.uses)

    @property
    def fx_status(self) -> str:
        """One line for SUMMARY - what actually converted this workbook."""
        parts = []
        if self.fallback_used:
            # One clause per provider that actually converted something, in rank
            # order - a reviewer should not have to open FX_WORKING to find out
            # that two different providers were involved.
            for st in sorted({u.source_type for u in self.uses
                              if u.is_fallback and u.rate},
                             key=lambda s: RANK.get(s, RANK[UNRESOLVED])):
                n = len({(u.requested_date, u.currency) for u in self.uses
                         if u.rate and u.source_type == st})
                parts.append(f"{fallback_banner(st)} for {n} date(s)")
            derived = len({(u.requested_date, u.currency) for u in self.uses
                           if u.rate and u.components})
            if derived:
                parts.append(f"of which {derived} is a derived cross-rate, not a "
                             "directly quoted INR rate")
            near = {(u.requested_date, u.currency) for u in self.uses
                    if u.rate and u.status == STATUS_NEAREST}
            if near:
                far = len({(u.requested_date, u.currency) for u in self.uses
                           if u.rate and u.status == STATUS_NEAREST
                           and u.gap_days > MAX_CARRY_DAYS})
                parts.append(
                    f"of which {len(near)} used the nearest available published "
                    f"date rather than the date required"
                    + (f" ({far} materially distant)" if far else ""))
        if self.unresolved:
            parts.append(
                f"INCOMPLETE - {len(self.unresolved)} currency/date pair(s) have no "
                "rate from any source; those INR figures are blank")
        return " | ".join(parts) if parts else "SBI TT buying rate throughout"

    def coverage_gaps(self, start: dt.date, end: dt.date, currency: str = "USD") -> list[dt.date]:
        """Month-ends in the period with no rate on file - the ones that matter most."""
        gaps = []
        for d in _month_ends(start, end):
            sub = self.df[(self.df["currency"] == currency) & (self.df["date"] == d)]
            if sub.empty:
                gaps.append(d)
        return gaps


# ----------------------------------------------------------------------
def _prev_month_end(on: dt.date) -> dt.date:
    """Rule 115: last day of the month immediately preceding."""
    first_of_month = on.replace(day=1)
    return first_of_month - dt.timedelta(days=1)


def _month_ends(start: dt.date, end: dt.date) -> list[dt.date]:
    out = []
    cur = start.replace(day=1)
    while cur <= end:
        nxt = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        me = nxt - dt.timedelta(days=1)
        if start <= me <= end:
            out.append(me)
        cur = nxt
    return out
