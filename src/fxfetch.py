"""Retrieval of the external FX reference tables the engine falls back on.

The engine NEVER fetches during a run. A tax computation must be reproducible:
the same documents and the same rate tables have to produce the same workbook
next month, next year and in front of an assessing officer. So retrieval is a
separate, deliberate step that writes a dated CSV, and the engine only ever
reads that CSV.

Two sources are supported here.

FBIL - Financial Benchmarks India Pvt Ltd  (NOT FETCHED - see below)
--------------------------------------------------------------------
What it is
    The Indian rupee reference rate. FBIL took it over from the RBI with effect
    from July 2018 (RBI letter dated 26 December 2017).
Method, and why the pairs are not alike
    USD/INR is a DIRECT rate: a volume-weighted average of ACTUAL spot USD/INR
    transactions in a randomly selected 15-minute window inside 11:30-12:30 IST,
    subject to a minimum of ten transactions aggregating USD 25 million, with
    outliers removed on a +/-3SD rule. Published around 13:30 IST on Mumbai
    business days.
    EUR/INR, GBP/INR and JPY/INR are FBIL's OWN CROSSES: FBIL takes the EUR/USD,
    GBP/USD or USD/JPY rate from electronic platforms in the same window and
    crosses it with its USD/INR reference rate. No rupee transaction in those
    currencies is measured. The engine says so on every such row.
Usage terms - why there is no fetcher here
    FBIL's own FAQ states that an End-Users Licence is required and that use is
    FEE LIABLE, that all entities in India intending to use FBIL benchmarks are
    fee liable, and that "any use of its Benchmarks including commercial use and
    distribution/ display will be only with the express authorization of FBIL".
    Unauthorised use is "dealt with legally". FBIL's methodology document is
    silent on usage - and silence is not permission; an earlier version of this
    file wrongly inferred that it was.
    So this module does NOT retrieve FBIL data. There is no endpoint here and no
    scraping of fbil.org.in.
How an FBIL rate reaches the engine
    Only as a table the PREPARER supplies, transcribed from FBIL's own
    publication under whatever licence the firm holds, with the source document
    and date recorded on every row. `load_preparer_fbil()` below validates such a
    table and stamps that provenance onto it.
A warning about substitutes
    A third-party service that derives USD/INR from a euro table is not FBIL,
    however close the number. Such data must never be written into the FBIL
    table. This was a real defect in an earlier version of this file, which
    fetched euro-derived rates and labelled them "FBIL reference rate, direct
    quote".

ECB - euro foreign exchange reference rates
-------------------------------------------
What it is
    The European Central Bank's daily euro reference rates. INR is one of the
    published currencies.
Method
    Fixed at around 14:15 CET and published about 16:00 CET on TARGET working
    days. Rates are expressed as units of foreign currency PER ONE EURO.
Coverage
    eurofxref-hist.xml carries the full history from 1999 for the currencies in
    the series; INR has been included throughout the period this engine deals
    with. Working days only.
Access
    https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml - a single
    public XML file, no key. A 90-day file and daily/CSV/SDMX-ML variants exist
    at the same location.
Usage terms
    The ECB publishes these "for information purposes only" and states they are
    "not intended to be used in any market transactions". The page carries no
    explicit licence or attribution requirement. That caveat is exactly why the
    ECB sits BELOW the Indian sources in the ranking and why every ECB-sourced
    figure carries the caveat in its methodology text.
Standing
    NOT an SBI TT buying rate and, for anything other than EUR/INR, not even a
    quoted INR rate - it is a cross-rate the engine derives and shows its
    working for.

Offline by design
-----------------
Each source has a PARSER that takes bytes already on disk and a FETCHER that is
a thin wrapper around urllib. The parsers are what the tests exercise and what
the engine depends on; a machine with no outbound network can download the ECB
XML by hand, drop it in, and parse it with no loss of function.
"""
from __future__ import annotations

import csv
import datetime as dt
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

from .fxsources import (
    ECB, FBIL, FBIL_LICENCE_NOTE, PROVIDER_NAME, RATE_TABLE_COLUMNS,
    TARGET_CURRENCY,
)

ECB_HIST_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"

#: The four rupee pairs FBIL publishes. Only the first is a direct rupee rate.
FBIL_PAIRS = ("USD", "EUR", "GBP", "JPY")
#: Currencies worth pulling from the ECB for an INR working paper.
ECB_CURRENCIES = ("USD", "EUR", "GBP", "JPY")

_GESMES = "{http://www.gesmes.org/xml/2002-08-01}"
_ECB_NS = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"


def _rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=RATE_TABLE_COLUMNS)


# ----------------------------------------------------------------------
# ECB
# ----------------------------------------------------------------------
def parse_ecb_hist(xml_bytes: bytes, currencies: tuple[str, ...] | None = None,
                   start: dt.date | None = None,
                   end: dt.date | None = None) -> pd.DataFrame:
    """Turn eurofxref-hist.xml into the engine's rate-table shape.

    The file is a series of daily cubes:

        <Cube time="2023-04-03">
          <Cube currency="INR" rate="89.4340"/>
          <Cube currency="USD" rate="1.0906"/>
          ...

    Every rate is EUR -> that currency, so each row is written with
    base_currency EUR and quote_currency the listed one. EUR/INR is therefore a
    direct quote; every other INR rate is derived downstream by ECBSource, which
    requires both legs to come from the same published day.

    `currencies` limits the output - there is no point carrying forty currencies
    when a client holds shares in one. INR is always kept, because without it no
    cross-rate can be formed at all.
    """
    keep = None
    if currencies:
        keep = {c.strip().upper() for c in currencies} | {TARGET_CURRENCY}
    root = ET.fromstring(xml_bytes)
    today = dt.date.today().isoformat()
    rows: list[dict] = []
    for day_cube in root.iter(f"{_ECB_NS}Cube"):
        when = day_cube.get("time")
        if not when:
            continue
        try:
            day = dt.date.fromisoformat(when)
        except ValueError:
            continue
        if (start and day < start) or (end and day > end):
            continue
        for c in day_cube:
            cur, rate = c.get("currency"), c.get("rate")
            if not cur or rate in (None, "", "N/A"):
                continue      # a currency not quoted that day is simply absent
            cur = cur.strip().upper()
            if keep is not None and cur not in keep:
                continue
            try:
                value = float(rate)
            except ValueError:
                continue
            if value <= 0:
                continue      # never let a zero or negative reach the engine
            rows.append({
                "date": day.isoformat(),
                "base_currency": "EUR",
                "quote_currency": cur,
                "rate": value,
                "source": PROVIDER_NAME[ECB],
                "retrieved_on": today,
                "status": "FETCHED",
                "notes": ("ECB euro foreign exchange reference rate, units of "
                          f"{cur} per 1 EUR. Published for information purposes "
                          "only; not a TT buying rate."),
            })
    return _rows_to_frame(rows)


def fetch_ecb_hist(timeout: int = 60) -> bytes:
    """Download eurofxref-hist.xml. Kept separate so the parser stays testable."""
    import urllib.request
    with urllib.request.urlopen(ECB_HIST_URL, timeout=timeout) as r:
        return r.read()


# ----------------------------------------------------------------------
# FBIL - loaded, never fetched
# ----------------------------------------------------------------------
FBIL_TEMPLATE_COLUMNS = ["date", "base_currency", "quote_currency", "rate",
                         "fbil_publication", "transcribed_by", "transcribed_on"]


def fbil_template() -> pd.DataFrame:
    """The empty sheet a preparer fills in from FBIL's own publication.

    Every row must name the FBIL publication it came from, who transcribed it
    and when. That is the provenance the working paper stands on: there is no
    fetch to point at, so the preparer IS the chain of custody.
    """
    return pd.DataFrame(columns=FBIL_TEMPLATE_COLUMNS)


def load_preparer_fbil(source: str | Path | pd.DataFrame) -> pd.DataFrame:
    """Validate a preparer-supplied FBIL table and stamp its provenance.

    Deliberately strict, because nothing downstream can tell a transcribed FBIL
    rate from an invented one:

      * only the four pairs FBIL publishes are accepted - anything else is not
        an FBIL rate at all and is dropped;
      * every row must name the FBIL publication it was taken from;
      * a zero, negative or non-numeric rate is dropped, never read as nil;
      * USD/INR is marked as FBIL's DIRECT rate, and EUR, GBP and JPY are marked
        as FBIL's own crosses through USD, because that is what they are.

    Returns a frame in the engine's rate-table shape, ready for
    `FXTable.merge_fbil()` or to be written to config/fx_fbil.csv.
    """
    df = source if isinstance(source, pd.DataFrame) else pd.read_csv(Path(source))
    if df is None or df.empty:
        return _rows_to_frame([])
    df = df.copy()
    for c in FBIL_TEMPLATE_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    today = dt.date.today().isoformat()
    rows: list[dict] = []
    for _, r in df.iterrows():
        cur = str(r["base_currency"]).strip().upper()
        quote = str(r["quote_currency"]).strip().upper() or TARGET_CURRENCY
        if cur not in FBIL_PAIRS or quote != TARGET_CURRENCY:
            continue                     # FBIL publishes these four and no more
        pub = str(r["fbil_publication"]).strip()
        if not pub:
            continue                     # unattributed: not evidence, dropped
        try:
            day = pd.to_datetime(r["date"]).date()
            value = float(r["rate"])
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        who = str(r["transcribed_by"]).strip()
        when = str(r["transcribed_on"]).strip() or today
        direct = cur == "USD"
        rows.append({
            "date": day.isoformat(),
            "base_currency": cur,
            "quote_currency": TARGET_CURRENCY,
            "rate": value,
            "source": PROVIDER_NAME[FBIL],
            "retrieved_on": when,
            "status": "PREPARER_SUPPLIED",
            "notes": (
                f"Transcribed from {pub}"
                + (f" by {who}" if who else "")
                + ". "
                + ("FBIL USD/INR reference rate - a DIRECT rate measured from "
                   "actual spot rupee transactions."
                   if direct else
                   f"FBIL {cur}/INR reference rate - FBIL's OWN CROSS through "
                   "USD, not a measured rupee rate.")
                + " " + FBIL_LICENCE_NOTE),
        })
    return _rows_to_frame(rows)


# ----------------------------------------------------------------------
def write_table(frame: pd.DataFrame, path: str | Path) -> Path:
    """Write a rate table where the engine will find it, merging what is there.

    Existing rows win on a clash: a table already reviewed is not silently
    overwritten by a later download.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = frame
    if path.exists():
        try:
            old = pd.read_csv(path)
            out = pd.concat([old, frame], ignore_index=True)
        except Exception:
            out = frame
    for c in RATE_TABLE_COLUMNS:
        if c not in out.columns:
            out[c] = ""
    out = (out[RATE_TABLE_COLUMNS]
           .drop_duplicates(subset=["date", "base_currency", "quote_currency"],
                            keep="first")
           .sort_values(["date", "base_currency", "quote_currency"]))
    out.to_csv(path, index=False, quoting=csv.QUOTE_MINIMAL)
    return path


def refresh(config_dir: str | Path = "config", start: dt.date | None = None,
            end: dt.date | None = None,
            currencies: tuple[str, ...] = ECB_CURRENCIES) -> dict[str, str]:
    """Fetch what may be fetched, and say plainly what may not.

    Run deliberately, not as part of a computation. Returns what happened per
    source, including the failures - a source that could not be reached is
    reported, never quietly skipped.

    The ECB is fetched. FBIL is NOT, and never will be from here: it is licensed
    benchmark data requiring FBIL's express authorisation, so it arrives only as
    a preparer-supplied table. Saying so on every refresh is deliberate - a
    silently absent source looks like a bug, and someone eventually "fixes" it
    by pointing it at whatever endpoint returns a plausible number.
    """
    config_dir = Path(config_dir)
    start = start or dt.date(2018, 7, 1)
    end = end or dt.date.today()
    out: dict[str, str] = {}
    try:
        x = parse_ecb_hist(fetch_ecb_hist(),
                           currencies=tuple(currencies) + (TARGET_CURRENCY,),
                           start=start, end=end)
        write_table(x, config_dir / "fx_ecb.csv")
        out[ECB] = f"{len(x)} rows written to config/fx_ecb.csv"
    except Exception as e:
        out[ECB] = f"NOT FETCHED - {type(e).__name__}: {e}"
    fbil_csv = config_dir / "fx_fbil.csv"
    out[FBIL] = (
        f"NOT FETCHED BY DESIGN - {FBIL_LICENCE_NOTE} "
        + (f"A preparer-supplied table is present at {fbil_csv}."
           if fbil_csv.exists() else
           f"No preparer-supplied table at {fbil_csv}; FBIL will supply nothing. "
           "Use fxfetch.fbil_template() and fxfetch.load_preparer_fbil()."))
    return out


if __name__ == "__main__":       # pragma: no cover - operator convenience
    for src, msg in refresh().items():
        print(f"{src:>16}: {msg}")
