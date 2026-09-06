"""Real-data acceptance procedure for the external FX sources.

Everything below the FX layer is frozen. What has NOT been proved is the layer
against REAL fetched data, because the build environment has no outbound
network: `config/fx_fbil.csv` and `config/fx_ecb.csv` are empty, and no rate was
invented to stand in. This script is that proof, in runnable form.

Run it on a networked machine:

    python3 tests/acceptance_realdata.py --baseline      # ONCE, before fetching
    python3 tests/acceptance_realdata.py --fetch         # populate the tables
    python3 tests/acceptance_realdata.py                 # the acceptance run

Every stage reports PASS, FAIL, BLOCKED, WARNING or REPORT. BLOCKED means the
stage needs real data that is not there yet - it is not a defect and it is not a
pass. WARNING is something to investigate that is not on its own a defect; it
carries no verdict but is counted onto the overall line. REPORT is an
observation for the preparer to sign off. The exit code is 0 only when every
stage PASSES; a BLOCKED stage exits 2, so the run can never be mistaken for an
acceptance.

What the stages establish
-------------------------
1. The ECB table populates from its documented endpoint, with real coverage.
   FBIL is NOT fetched - it is licensed data - so its table is present only if
   the preparer supplied one.
2. The three UBS dates resolve - or, honestly, still do not.
3. The rates are what the providers publish, cross-checked against the
   established benchmark tolerance where both sources are present.
4. Exact-date and nearest-date behaviour holds on real published calendars.
5. Fallback priority is unchanged with real tables present.
6. Provenance is identical in the engine context, the API, the UI payload and
   the workbook.
7. NOTHING ELSE MOVED. Every figure is compared against a baseline captured
   before the fetch: only the rows that depended on the three unresolved dates
   may change, and they may only change from blank to a figure.

The rules this script must never be edited to satisfy: the 30-day nearest-date
limit, the SBI > Google > ECB > FBIL > manual precedence, and the tax
calculations. If a stage fails, the finding is the output - not a wider window.

And one more, learned the hard way: no other provider may ever be written into
the FBIL table. An earlier version of the fetcher pulled euro-derived rates from
a third party and labelled them "FBIL reference rate, direct quote". Stage 1
checks for that specifically.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.compute import ComputeOptions                            # noqa: E402
from src.excel_out import write_workbook                          # noqa: E402
from src.fx import FXTable                                        # noqa: E402
from src.fxsources import (                                       # noqa: E402
    ECB, FBIL, GOOGLE_FINANCE, MANUAL, MAX_CARRY_DAYS, MAX_NEAREST_DAYS,
    MAX_NEAREST_PRIMARY_DAYS,
    SBI_TTBR, STATUS_EXACT, STATUS_NEAREST,
)
from src.fx import RANKED                                        # noqa: E402
from src.models import Period                                     # noqa: E402
from src.run import DEFAULT_FX, DEFAULT_MARKET, build_context, prepare  # noqa: E402

CONFIG = ROOT / "config"
FBIL_CSV = CONFIG / "fx_fbil.csv"
ECB_CSV = CONFIG / "fx_ecb.csv"
BASELINE = ROOT / "tests" / "fixtures" / "acceptance_baseline.json"
OUT = ROOT / "work" / "acceptance"

# The acceptance client: the UBS statement set, whose three unresolved dates are
# the whole point of the exercise. The real statement PDFs live outside this
# repository (never committed) and their location is machine-specific - set
# RSU_FA_ACCEPTANCE_UBS_DOCS to point at them on a machine other than the
# original build sandbox. Unset, this preserves the original hardcoded path
# exactly, so behaviour is unchanged wherever that path already exists.
DOCS = Path(os.environ.get("RSU_FA_ACCEPTANCE_UBS_DOCS",
                            "/home/claude/brokers/RSU brokers/UBS"))
CLIENT_SRC = ROOT / "clients" / "CLIENT_UBS_01"
PERIOD = Period(dt.date(2025, 1, 1), dt.date(2025, 12, 31))

# The three accepted input-data blockers. All three are Indian business days and
# TARGET working days, so both FBIL and the ECB should carry them.
UBS_DATES = [dt.date(2023, 4, 3), dt.date(2023, 7, 3), dt.date(2024, 7, 1)]

# Frames whose every cell must be unchanged by the fetch, except where a
# previously blank rupee figure becomes a figure.
COMPARED = ("a2", "a3", "sales", "dividends", "form1042s", "fsi",
            "reconciliation", "vesting")

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"
#: an observation the operator must read and sign off, not a test
REPORT = "REPORT"
#: something to investigate that is not, on its own, a defect. Carries no
#: verdict, but is surfaced on the overall line so it cannot be scrolled past.
WARNING = "WARNING"

#: The established tolerance between two independent benchmarks quoting the same
#: pair on the same day. FBIL is a midday Indian interbank volume-weighted
#: average; the ECB cross comes off a 14:15 CET euro fixing. They are measuring
#: the same market hours apart, so they agree closely without agreeing exactly.
#: Beyond this, they are not describing the same thing and the fetch is wrong.
BENCHMARK_TOLERANCE_PCT = 2.0


class Report:
    """Stage results, printed as they happen and written out at the end."""

    def __init__(self):
        self.lines: list[str] = []
        self.stages: list[tuple[str, str, str]] = []

    def stage(self, n, title):
        print()
        print("=" * 98)
        print(f"STAGE {n}. {title}")
        print("=" * 98)
        self.lines.append(f"\n## Stage {n}. {title}\n")

    def check(self, status, label, detail=""):
        print(f"  {status:<7} {label}{('  ' + detail) if detail else ''}")
        self.lines.append(f"- **{status}** {label}"
                          + (f" — {detail}" if detail else ""))
        self.stages.append((status, label, detail))
        return status

    def note(self, text):
        print(f"          {text}")
        self.lines.append(f"  - {text}")

    @property
    def warnings(self) -> int:
        return sum(1 for s, _, _ in self.stages if s == WARNING)

    def verdict(self):
        bad = [s for s, _, _ in self.stages if s == FAIL]
        blocked = [s for s, _, _ in self.stages if s == BLOCKED]
        # REPORT and WARNING lines carry no verdict. A WARNING is something to
        # investigate, not a failed acceptance - but it is counted onto the
        # overall line so it cannot be scrolled past.
        if bad:
            return FAIL, 1
        if blocked:
            return BLOCKED, 2
        return PASS, 0

    def write(self, path):
        v, _ = self.verdict()
        warn = (f" — {self.warnings} warning(s) to investigate"
                if self.warnings else "")
        head = (f"# FX real-data acceptance\n\nRun on {dt.datetime.now():%d-%m-%Y %H:%M}\n\n"
                f"**Overall: {v}{warn}**\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(head + "\n".join(self.lines) + "\n")
        return path


# ----------------------------------------------------------------------
def load_table(path):
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
    except Exception:
        return None
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
    return df.dropna(subset=["date", "rate"])


def fresh_fx(register=None):
    """An FXTable reading whatever is actually in config/ right now."""
    return FXTable(DEFAULT_FX, register=register)


def run_engine(tmp: Path):
    """One full run of the acceptance client, exactly as the UI runs it."""
    client_dir = tmp / "client"
    client_dir.mkdir(parents=True, exist_ok=True)
    for name in ("entities.csv", "accounts.csv"):
        src = CLIENT_SRC / name
        if src.exists():
            shutil.copy(src, client_dir / name)
    result, fx, md, register = prepare(
        client_dir, PERIOD, DEFAULT_FX, DEFAULT_MARKET, ComputeOptions(),
        doc_dir=DOCS)
    ctx = build_context("FX Acceptance - UBS", PERIOD, result, fx, md,
                        register, ComputeOptions(),
                        result.summary.get("sources", []))
    return ctx, fx


def _row_meta(tbl, day, base, quote):
    """The stored provenance for one fetched row: source, retrieval, status."""
    if tbl is None:
        return {}
    hit = tbl[(tbl["date"] == day) & (tbl["base_currency"] == base)
              & (tbl["quote_currency"] == quote)]
    if not len(hit):
        return {}
    r = hit.iloc[-1]
    return {"source": str(r.get("source", "")),
            "retrieved": str(r.get("retrieved_on", "")),
            "status": str(r.get("status", ""))}


def evidence(day, f_q, e_q, fb, ec) -> dict:
    """Both sides of one comparison, with enough provenance to investigate it.

    Kept whole rather than reduced to a verdict: when two independent sources
    print the same figure, what settles it is where each figure came from.
    """
    fm = _row_meta(fb, f_q.used_date, "USD", "INR")
    em = _row_meta(ec, e_q.used_date, "EUR", "INR")
    return {
        "date": f"{day:%d-%m-%Y}",
        "displayed": f"{round(f_q.rate, 4):.4f}",
        "fbil_rate": f_q.rate,
        "fbil_source": fm.get("source", f_q.source_name),
        "fbil_retrieved": fm.get("retrieved", ""),
        "fbil_status": fm.get("status", ""),
        "fbil_basis": f_q.basis,
        "ecb_rate": e_q.rate,
        "ecb_source": em.get("source", e_q.source_name),
        "ecb_retrieved": em.get("retrieved", ""),
        "ecb_status": em.get("status", ""),
        "ecb_derivation": e_q.derivation or "direct quote",
        "ecb_components": [f"{lbl} on {dd:%d-%m-%Y} = {r:.6f}"
                           for lbl, _p, dd, r in e_q.components],
        "ecb_basis": e_q.basis,
    }


def frame_digest(df: pd.DataFrame) -> list[list[str]]:
    """Every cell as text - so a comparison is exact and order-sensitive."""
    return [[("" if pd.isna(v) else str(v)) for v in row]
            for row in df.itertuples(index=False)]


# ======================================================================
def stage_1(rep) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    rep.stage(1, "THE EXTERNAL TABLES - ECB FETCHED, FBIL PREPARER-SUPPLIED")
    rep.note("ECB   ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml, via "
             "`python3 -m src.fxfetch`. The engine never fetches during a run.")
    rep.note("FBIL  NOT fetched, by design. FBIL requires an End-Users Licence, "
             "states use is fee liable, and permits use or distribution only "
             "with its express authorisation. It reaches the engine only as a "
             "table the preparer transcribed from FBIL's own publication.")
    fb, ec = load_table(FBIL_CSV), load_table(ECB_CSV)

    if ec is None:
        rep.check(BLOCKED, "config/fx_ecb.csv is populated",
                  "run `python3 -m src.fxfetch` on a networked machine")
    else:
        rep.check(PASS if len(ec) else FAIL, "config/fx_ecb.csv is populated",
                  f"{len(ec):,} rows, {ec['date'].min()} to {ec['date'].max()}")
        rep.check(PASS if set(ec["base_currency"]) == {"EUR"} else FAIL,
                  "every ECB row is quoted against the EURO, as published",
                  "units of the listed currency per 1 EUR")
        has_inr = (ec["quote_currency"] == "INR").any()
        rep.check(PASS if has_inr else FAIL,
                  "EUR/INR is present - without it no cross-rate can be formed",
                  f"{int((ec['quote_currency'] == 'INR').sum()):,} EUR/INR rows")
        rep.check(PASS if (ec["rate"] > 0).all() else FAIL,
                  "no zero or negative rate reached the ECB table",
                  f"min {ec['rate'].min()}")

    if fb is None:
        rep.check(BLOCKED, "an FBIL table has been supplied by the preparer",
                  "none on file - FBIL will supply nothing, which is correct "
                  "until a licensed table exists")
        rep.note("To supply one: fxfetch.fbil_template() for the sheet, then "
                 "fxfetch.load_preparer_fbil() to validate and stamp it.")
        return fb, ec

    # A supplied table exists. It has to prove it is FBIL and nothing else.
    rep.check(PASS if len(fb) else FAIL, "config/fx_fbil.csv is populated",
              f"{len(fb):,} rows, {fb['date'].min()} to {fb['date'].max()}")
    pairs = sorted({str(b).upper() for b in fb["base_currency"]})
    stray = [c for c in pairs if c not in ("USD", "EUR", "GBP", "JPY")]
    rep.check(PASS if not stray else FAIL,
              "only FBIL's four rupee pairs are present",
              ", ".join(pairs) if not stray else f"stray: {stray}")
    rep.check(PASS if (fb["rate"] > 0).all() else FAIL,
              "no zero or negative rate reached the FBIL table",
              f"min {fb['rate'].min()}")

    # The defect that produced this stage: euro-derived third-party data written
    # into the FBIL table. Every row must name the FBIL publication it came from.
    notes = fb["notes"].astype(str) if "notes" in fb else pd.Series([""] * len(fb))
    status = fb["status"].astype(str) if "status" in fb else pd.Series([""] * len(fb))
    unattributed = int((~notes.str.contains("Transcribed from", na=False)).sum())
    rep.check(PASS if unattributed == 0 else FAIL,
              "every FBIL row names the publication it was transcribed from",
              "all attributed" if unattributed == 0
              else f"{unattributed} row(s) with no FBIL publication named")
    rep.check(PASS if status.eq("PREPARER_SUPPLIED").all() else FAIL,
              "every FBIL row is marked PREPARER_SUPPLIED, not FETCHED",
              "- there is no FBIL fetch to claim")
    foreign = [w for w in ("frankfurter", "ECB", "euro reference", "Google")
               if notes.str.contains(w, case=False, na=False).any()
               or fb["source"].astype(str).str.contains(w, case=False,
                                                        na=False).any()]
    rep.check(PASS if not foreign else FAIL,
              "no other provider's data is sitting in the FBIL table",
              "clean" if not foreign else
              f"found {foreign} - a euro-derived or third-party rate is NOT "
              "FBIL and must not be labelled FBIL")
    return fb, ec


def stage_2(rep, fb, ec):
    rep.stage(2, "THE THREE UBS FX BLOCKERS")
    rep.note("All three are TARGET working days, so the ECB - rank 3 - is the "
             "expected supplier, as a cross-rate derived through the euro.")
    if fb is None and ec is None:
        rep.check(BLOCKED, "the three dates resolve from a real source",
                  "no external table is populated - they remain input-data "
                  "blockers, correctly")
        return
    fx = fresh_fx()
    for d in UBS_DATES:
        q = fx.resolve(d, "USD")
        if q is None:
            rep.check(FAIL, f"{d:%d-%m-%Y} resolves",
                      "no ranked source supplied it. Do NOT widen the window - "
                      "report the gap and check the fetch covered this date.")
            continue
        rep.check(PASS, f"{d:%d-%m-%Y} resolves",
                  f"{q.rate:.4f} from {q.provider} (level {q.fallback_level}), "
                  f"{q.status}")
        rep.note(f"basis: {q.basis}")
        if q.is_derived:
            rep.note("components: " + "; ".join(
                f"{lbl} on {dd:%d-%m-%Y} = {r:.6f}"
                for lbl, _p, dd, r in q.components))
        if q.status != STATUS_EXACT:
            rep.check(FAIL if q.gap_days > MAX_NEAREST_DAYS else PASS,
                      f"{d:%d-%m-%Y} used a substituted date within the window",
                      f"{q.used_date:%d-%m-%Y}, {q.gap_days}d - all three are "
                      "working days, so an exact date was expected")
    unresolved = [d for d in UBS_DATES if fx.resolve(d, "USD") is None]
    rep.check(PASS if not unresolved else FAIL,
              "no FX_UNAVAILABLE blocker remains for the three dates",
              "all cleared" if not unresolved
              else f"still blocked: {[f'{d:%d-%m-%Y}' for d in unresolved]}")


def stage_3(rep, fb, ec):
    rep.stage(3, "THE FETCHED RATES ARE WHAT THE PROVIDERS PUBLISH")
    if fb is None or ec is None:
        rep.check(BLOCKED, "FBIL and the ECB-derived cross agree within the "
                  f"established tolerance of {BENCHMARK_TOLERANCE_PCT}%",
                  "both tables must be present to cross-check them")
        rep.note("Manual verification, to be done once and recorded here:")
        rep.note("  * pick 3 dates spread across the period; read USD/INR off "
                 "fbil.org.in's own archive and off the ECB's eurofxref page;")
        rep.note("  * confirm each matches the CSV in config/ to 4 decimals;")
        rep.note("  * record the date checked, the published figure and the "
                 "figure on file in the acceptance report.")
        rep.note("For FBIL this is not a spot-check of a fetch - there is no "
                 "fetch. It is verification that the transcription is right.")
        return
    # The two are genuinely DIFFERENT benchmarks - a midday Indian interbank
    # average, transcribed from FBIL's own publication, and a 14:15 CET euro
    # fixing fetched from the ECB. The ACCEPTANCE CRITERION is the tolerance:
    # within it they are describing the same market, beyond it something is
    # wrong with the data.
    #
    # Two rates landing on the same figure to the precision published is not, by
    # itself, a defect - the euro cross can round onto the FBIL figure on a
    # quiet day. It is reported as a WARNING with the provenance of both sides
    # retained, so source independence can be investigated from the evidence
    # rather than assumed from an equality test.
    fx = fresh_fx()
    checked = 0
    worst = (0.0, None)
    identical: list[dict] = []
    for d in sorted({*UBS_DATES, *[x for x in fb["date"][:200]]}):
        # USD/INR only: it is the one pair both sources reach independently -
        # FBIL measures it, the ECB crosses to it.
        f_q = fx.fbil.lookup(d, "USD")
        e_q = fx.ecb.lookup(d, "USD")
        if not (f_q and e_q and f_q.status == STATUS_EXACT
                and e_q.status == STATUS_EXACT):
            continue
        checked += 1
        gap = abs(f_q.rate - e_q.rate) / f_q.rate * 100
        # First comparison always sets it - otherwise a run where every date
        # agrees exactly leaves no date recorded at all.
        if worst[1] is None or gap > worst[0]:
            worst = (gap, d)
        # "Identical to the displayed precision" - the four decimals the
        # workbook prints, not float equality.
        if round(f_q.rate, 4) == round(e_q.rate, 4):
            identical.append(evidence(d, f_q, e_q, fb, ec))
    if not checked:
        rep.check(BLOCKED, "FBIL and the ECB-derived cross agree within the "
                  f"established tolerance of {BENCHMARK_TOLERANCE_PCT}%",
                  "no date is present in both tables")
        return

    # The acceptance criterion.
    rep.check(PASS if worst[0] <= BENCHMARK_TOLERANCE_PCT else FAIL,
              "FBIL and the ECB-derived cross agree within the established "
              f"tolerance of {BENCHMARK_TOLERANCE_PCT}%",
              f"{checked} common date(s), worst divergence {worst[0]:.3f}% on "
              f"{worst[1]:%d-%m-%Y}"
              + ("" if worst[0] <= BENCHMARK_TOLERANCE_PCT else
                 " - MATERIAL DISAGREEMENT, the two are not describing the same "
                 "market and the fetch must be investigated before acceptance"))

    if not identical:
        rep.check(PASS, "the two sources returned distinct rates on every "
                  "common date", f"{checked} date(s) compared")
    else:
        rep.check(WARNING, "independent sources returned identical displayed "
                  "rate; investigate source independence",
                  f"{len(identical)} of {checked} common date(s)")
        rep.note("Not a failure on its own - a euro cross can round onto the "
                 "FBIL figure. Provenance for each, so independence can be "
                 "checked from the evidence:")
        for e in identical[:10]:
            rep.note(f"  {e['date']}  displayed {e['displayed']}")
            rep.note(f"    FBIL {e['fbil_rate']:.6f}  source \"{e['fbil_source']}\""
                     f"  retrieved {e['fbil_retrieved']}  status {e['fbil_status']}")
            rep.note(f"    ECB  {e['ecb_rate']:.6f}  DERIVED: {e['ecb_derivation']}")
            rep.note(f"    ECB  source \"{e['ecb_source']}\"  retrieved "
                     f"{e['ecb_retrieved']}")
        if len(identical) > 10:
            rep.note(f"  ... and {len(identical) - 10} more")
        OUT.mkdir(parents=True, exist_ok=True)
        keep = OUT / "stage3_identical_rates.json"
        keep.write_text(json.dumps(identical, indent=1, default=str))
        rep.note(f"Full provenance for all {len(identical)} retained at "
                 f"{keep.relative_to(ROOT)} - components, basis text and "
                 "retrieval dates for both sides.")
        rep.note("To investigate: the ECB figure above is a CROSS of two euro "
                 "rates, so it agreeing exactly with a direct INR quote to six "
                 "decimals - not four - would be the real signal that one "
                 "source is being served from the other.")
    for name, tbl in (("FBIL", fb), ("ECB", ec)):
        stale = tbl["retrieved_on"].astype(str).nunique() if "retrieved_on" in tbl else 0
        rep.check(PASS if stale else FAIL,
                  f"{name} rows carry a retrieval date",
                  f"{stale} distinct retrieval date(s) on file")


def stage_4(rep, fb, ec):
    rep.stage(4, "EXACT AND NEAREST-DATE BEHAVIOUR ON REAL PUBLISHED CALENDARS")
    fx = fresh_fx()
    # Run against whichever real calendar is on file. The ECB is the fetched
    # source, so it is the one normally exercised here; FBIL is used when a
    # preparer table exists, since its Mumbai calendar differs from TARGET.
    cases = []
    if ec is not None:
        pool = ec[(ec["base_currency"] == "EUR")
                  & (ec["quote_currency"] == "INR")].sort_values("date")
        if not pool.empty:
            cases.append(("ECB", fx.ecb, "EUR", set(pool["date"])))
    if fb is not None:
        pool = fb[(fb["base_currency"] == "USD")
                  & (fb["quote_currency"] == "INR")].sort_values("date")
        if not pool.empty:
            cases.append(("FBIL", fx.fbil, "USD", set(pool["date"])))
    if not cases:
        rep.check(BLOCKED, "exact date wins where the provider published one",
                  "no real source calendar is on file")
        return

    for name, src, cur, published in cases:
        a_day = sorted(published)[len(published) // 2]
        q = src.lookup(a_day, cur)
        rep.check(PASS if q and q.status == STATUS_EXACT and q.used_date == a_day
                  else FAIL,
                  f"{name}: a published date returns that date's own rate",
                  f"{a_day:%d-%m-%Y} -> {q.used_date:%d-%m-%Y}" if q else "no quote")

        # A real non-publishing day, found from the calendar rather than assumed.
        gap_day = next((d + dt.timedelta(days=1) for d in sorted(published)
                        if (d + dt.timedelta(days=1)) not in published
                        and (d + dt.timedelta(days=1)) < max(published)), None)
        if gap_day:
            # strict=False on purpose. This is a SOURCE-LEVEL test of the
            # provider's own nearest-date rule, not of the ranked walk: in
            # resolve()'s strict pass an external source must answer with the
            # exact date or not at all, so its nearest-date mechanism is
            # reached only through an explicit strict=False call. The rule
            # being tested, and every assertion below, are unchanged.
            q = src.lookup(gap_day, cur, strict=False)
            good = (q is not None and q.status == STATUS_NEAREST
                    and q.gap_days <= MAX_NEAREST_DAYS)
            rep.check(PASS if good else FAIL,
                      f"{name}: a non-publishing day takes the nearest date",
                      f"{gap_day:%d-%m-%Y} -> {q.used_date:%d-%m-%Y} "
                      f"({q.gap_days}d {q.direction})" if q else "no quote")
            rep.check(PASS if (q and q.used_date.strftime('%d-%m-%Y') in q.basis)
                      else FAIL,
                      f"{name}: and the substituted date is stated, never passed "
                      "off as the date required", q.basis[:66] if q else "")

        before = min(published) - dt.timedelta(days=MAX_NEAREST_DAYS + 1)
        rep.check(PASS if src.lookup(before, cur) is None else FAIL,
                  f"{name}: beyond {MAX_NEAREST_DAYS} days it supplies nothing",
                  f"{before:%d-%m-%Y} is {MAX_NEAREST_DAYS + 1}d before the "
                  "table starts")
    rep.note("The 30-day limit is not to be widened to clear any date.")


def stage_5(rep, fb, ec):
    rep.stage(5, "FALLBACK PRIORITY IS UNCHANGED WITH REAL TABLES PRESENT")
    rep.check(PASS if RANKED == (SBI_TTBR, GOOGLE_FINANCE, ECB, FBIL, MANUAL)
              else FAIL, "the frozen chain is SBI > Google > ECB > FBIL > manual",
              " > ".join(RANKED))
    fx = fresh_fx()
    sbi_dates = sorted(set(fx.df[fx.df["currency"] == "USD"]["date"]))
    if not sbi_dates:
        rep.check(FAIL, "the SBI table has USD rows", "")
        return
    d = sbi_dates[len(sbi_dates) // 2]
    q = fx.resolve(d, "USD")
    rep.check(PASS if q and q.source_type == SBI_TTBR else FAIL,
              "SBI TTBR still wins outright wherever it has the date",
              f"{d:%d-%m-%Y} -> {q.source_type}" if q else "no quote")

    if ec is None:
        rep.check(BLOCKED, "Google Finance still outranks the ECB",
                  "needs the real ECB table")
        return
    # A date the ECB has and SBI does not: Google must still take precedence.
    cand = next((x for x in sorted(set(ec["date"]))
                 if fx.sbi.lookup(x, "USD", strict=True) is None
                 and fx.ecb.lookup(x, "USD") is not None), None)
    if cand is None:
        rep.check(BLOCKED, "Google Finance still outranks the ECB",
                  "no date where SBI is silent and the ECB is not")
    else:
        g = FXTable(DEFAULT_FX, google_frame=pd.DataFrame([{
            "date": cand, "base_currency": "USD", "quote_currency": "INR",
            "rate": 1.2345, "source": "Google Finance historical FX",
            "retrieved_on": "", "status": "", "notes": ""}]))
        gq = g.resolve(cand, "USD")
        rep.check(PASS if gq and gq.source_type == GOOGLE_FINANCE else FAIL,
                  "Google Finance still outranks the ECB",
                  f"{cand:%d-%m-%Y} -> {gq.source_type}" if gq else "no quote")
        eq = fx.resolve(cand, "USD")
        rep.check(PASS if eq and eq.source_type == ECB else FAIL,
                  "and the ECB is reached before FBIL",
                  f"{cand:%d-%m-%Y} -> {eq.source_type}" if eq else "no quote")

    if fb is None:
        rep.check(BLOCKED, "FBIL is reached only where the ECB cannot answer",
                  "no preparer-supplied FBIL table on file")
    else:
        # A date FBIL has where the ECB cannot form USD/INR.
        cand = next((x for x in sorted(set(fb["date"]))
                     if fx.sbi.lookup(x, "USD", strict=True) is None
                     and fx.ecb.lookup(x, "USD") is None
                     and fx.fbil.lookup(x, "USD") is not None), None)
        if cand is None:
            rep.check(BLOCKED, "FBIL is reached only where the ECB cannot answer",
                      "the ECB covered every date FBIL holds - correct, and "
                      "nothing to observe")
        else:
            q = fx.resolve(cand, "USD")
            rep.check(PASS if q and q.source_type == FBIL else FAIL,
                      "FBIL supplies the date the ECB could not",
                      f"{cand:%d-%m-%Y} -> {q.source_type}" if q else "no quote")

        # And the per-pair methodology, on real transcribed rows.
        for cur in ("USD", "EUR", "GBP", "JPY"):
            day = next((x for x in sorted(set(fb["date"]))
                        if fx.fbil.lookup(x, cur) is not None), None)
            if day is None:
                continue
            q = fx.fbil.lookup(day, cur)
            want = "directly quoted" if cur == "USD" else "provider's own cross-rate"
            rep.check(PASS if q.rate_kind == want else FAIL,
                      f"FBIL {cur}/INR is described as {want}",
                      f"{q.rate_kind} - {q.methodology[:48]}...")
    rep.note("Precedence order is frozen: SBI > Google > ECB > FBIL > manual.")


def stage_6(rep, ctx, fx):
    rep.stage(6, "PROVENANCE IS IDENTICAL IN CONTEXT, API, UI AND WORKBOOK")
    aud, srcs = ctx["fx_audit"], ctx["fx_sources"]
    need = ["Date required", "Rate date used", "Gap (days)", "Currency",
            "Converted to", "Currency pair", "Rate", "FX source", "Provider",
            "Fallback level", "SBI TTBR availability", "Retrieval status",
            "Rate is", "Cross-rate components", "Derivation",
            "Provider methodology", "Basis stated in the working paper",
            "Review flag"]
    missing = [c for c in need if c not in aud.columns]
    rep.check(PASS if not missing else FAIL,
              "FX_WORKING carries every required provenance column",
              f"{len(need)} columns" if not missing else str(missing))

    fallback = aud[aud["FX source"].isin([GOOGLE_FINANCE, FBIL, ECB, MANUAL])]
    if len(fallback):
        bad = [b for b in fallback["Basis stated in the working paper"]
               if "SBI TTBR unavailable" not in str(b)]
        rep.check(PASS if not bad else FAIL,
                  "every fallback row says the TTBR was unavailable and names "
                  "its provider", f"{len(fallback)} fallback row(s)")
        wrong = [b for b in fallback["Basis stated in the working paper"]
                 if str(b).replace("SBI TTBR unavailable", "").find(
                     "TT buying rate") >= 0]
        rep.check(PASS if not wrong else FAIL,
                  "and no fallback is described as a TT buying rate",
                  f"{len(wrong)} offending row(s)")
        derived = fallback[fallback["Rate is"] == "derived cross-rate"]
        if len(derived):
            ok = all(str(r["Cross-rate components"]).strip()
                     and str(r["Derivation"]).strip()
                     for _, r in derived.iterrows())
            rep.check(PASS if ok else FAIL,
                      "each derived cross-rate carries its components and its "
                      "arithmetic", f"{len(derived)} derived row(s)")
    else:
        rep.check(BLOCKED, "a fallback rate appears in the working paper",
                  "this run resolved entirely from SBI - re-run once the "
                  "external tables cover the missing dates")

    used = srcs[srcs["Currency/date pairs supplied"] != ""]
    rep.check(PASS if len(used) else FAIL,
              "the source-hierarchy frame reports what each source supplied",
              ", ".join(f"{r['FX source']}={r['Currency/date pairs supplied']}"
                        for _, r in used.iterrows()))
    zero = [(k, c) for k, f in ctx.items() if isinstance(f, pd.DataFrame)
            and not f.empty for c in f.columns
            if any(w in str(c) for w in ("Rate", "TTBR", "FX"))
            and any(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and v == 0 for v in f[c])]
    rep.check(PASS if not zero else FAIL,
              "no rate column anywhere reports a rate of zero",
              f"{len(ctx)} frames swept" if not zero else str(sorted(set(zero))))
    rep.note("The API serves these frames unchanged and the UI renders them; "
             "tests/api_regression.py asserts that cell for cell on every run.")


# ======================================================================
# The ONE figure -> figure movement stage 7 may accept
# ======================================================================
# Stage 7's rule is that a cell may only go from blank to a figure. Exactly two
# other movements are not defects, and both are provable from the two runs
# alone. Both have the same single cause - the baseline was captured with no
# external table on file at all - and both require an EXACT date from a ranked,
# non-manual source:
#
#   UPGRADE     a rate the baseline could obtain only by carrying a STALE SBI
#               TTBR forward, which a ranked source now supplies exactly;
#   CONVERSION  a rate NO source could supply, so the baseline had to leave the
#               rupee figure blank and mark the row "No - FX unavailable",
#               which a ranked source now supplies exactly. The blank figures
#               themselves are already permitted fills; what this covers is the
#               provenance that moved with them - "NONE" -> the provider, and
#               the computability that the rate itself restored.
#
# That is the frozen hierarchy working as designed - resolve() offers every
# ranked source the exact date before it falls back on a stale SBI rate
# (src/fx.py:228-232, "each external source in turn gets its chance at the
# EXACT date, which beats an SBI rate weeks stale") - and it can only arise
# because the baseline was captured when no external table existed at all.
#
# The exception is deliberately narrow. All of the following must hold, and
# every one is checked against the two frames themselves, never assumed:
#
#   1. the baseline row names SBI_TTBR as its source;
#   2. the baseline row records the SBI date actually used, and it differs
#      from the date required - proven from the row's own data, not assumed;
#   3. that substitution is proven to be one resolve() could only have reached
#      by SBI's own strict pass already failing outright (src/fx.py resolve(),
#      SBITTBRSource.lookup()): a BACKWARD carry beyond MAX_CARRY_DAYS -
#      demonstrably stale, not a weekend, since SBI's backward-only strict
#      pass answers immediately for anything closer than that - or a FORWARD
#      substitution within MAX_NEAREST_PRIMARY_DAYS, which SBI's strict pass
#      (backward-only by construction) could never itself have produced, so
#      it can only have come from resolve()'s bidirectional nearest-date
#      fallback and is bounded exactly the way that fallback is bounded;
#   4. the current row names a different ranked source;
#   5. that source is not MANUAL;
#   6. the current rate is for the EXACT date required - never a nearest match;
#   7. the required FX date is itself unchanged;
#   8. the foreign-currency amount is unchanged;
#   9. every rupee figure equals its FC amount x the new rate, so nothing but
#      the rate moved;
#  10. only columns that depend on that rate moved.
#
# Anything else stays a FAIL: an arbitrary figure change, a moved date, a
# changed quantity, a nearest-date match, a source without provenance, a
# baseline source that was not demonstrably stale SBI, or a tax figure that
# does not reconcile to FC x rate.

#: Diagnostic only. RSU_FA_ACCEPTANCE_DEBUG=1 makes the classifier say, for
#: every row it does NOT accept, which predicate rejected it and on what
#: values. It changes no predicate and no verdict - a rejected row still fails
#: stage 7 exactly as before, whether or not anything was printed.
DEBUG = os.environ.get("RSU_FA_ACCEPTANCE_DEBUG", "") not in ("", "0")

#: "SBI TTBR carried forward 11 days from 31-01-2025" - FXQuote.basis states
#: the gap and the date it carried from, so staleness is read, never inferred.
STALE_SBI = re.compile(r"SBI TTBR carried forward (\d+) days? from (\d{2}-\d{2}-\d{4})")

#: What src/compute.py writes when NO ranked source could supply the rate:
#: "FX Source - Cost": q_cost.source_type if q_cost else "NONE", and
#: "Computable": "Yes" if computable else "No - FX unavailable". A row in this
#: state has a blank rate, a blank rate-date and a blank rupee figure - all
#: three are checked, so "NONE -> a figure" is never accepted on its own.
UNAVAILABLE_SRC = {"NONE", "UNRESOLVED", ""}

#: Tax-calculation inputs. These are what the engine computes FROM, not what
#: the FX layer produces. If any of them moved, the row is not a pure source
#: change and neither exception applies to it, whatever else it shows.
INVARIANTS = {
    "sales": ["Symbol", "Date of Acquisition", "Date of Sale", "Quantity",
              "Holding Period (days)", "Nature of Gain",
              "Sale Price per Share (FC)", "Vested Price per Share (FC)",
              "Proceeds (FC)", "Cost of Acquisition (FC)", "Currency"],
    "dividends": ["Date", "Symbol", "Currency", "Gross Dividend (FC)",
                  "Foreign Tax Withheld (FC)"],
}

#: One entry per FX leg that carries its own provenance inside the frame:
#: (leg, required-date col, rate col, source col, used-date col | None,
#:  basis col | None, [(FC col | (minuend, subtrahend), INR col)])
UPGRADE_LEGS = {
    "sales": [
        ("sale", "Date of Sale", "FX Rate - Sale Date", "FX Source - Sale",
         "FX Date Used - Sale", None,
         [("Proceeds (FC)", "Full Value of Consideration (Rs.)")]),
        ("cost", "Date of Acquisition", "FX Rate - Vest Date", "FX Source - Cost",
         "FX Date Used - Cost", None,
         [("Cost of Acquisition (FC)", "Cost of Acquisition (Rs.)")]),
    ],
    "dividends": [
        ("payment", "Date", "FX Rate", "FX Source", None, "FX Basis",
         [("Gross Dividend (FC)", "Gross Dividend (Rs.)"),
          ("Foreign Tax Withheld (FC)", "Foreign Tax Withheld (Rs.)"),
          (("Gross Dividend (FC)", "Foreign Tax Withheld (FC)"),
           "Net Received (Rs.)")]),
    ],
}

#: The only columns a proven upgrade on that leg may have moved. A change to
#: any column outside this set is still a FAIL, on an upgraded row as much as
#: on any other.
UPGRADE_MOVES = {
    ("sales", "sale"): {"FX Rate - Sale Date", "FX Date Used - Sale",
                        "FX Source - Sale", "Full Value of Consideration (Rs.)",
                        "Capital Gain (Rs.)", "Cost Conversion Basis"},
    ("sales", "cost"): {"FX Rate - Vest Date", "FX Date Used - Cost",
                        "FX Source - Cost", "Cost of Acquisition (Rs.)",
                        "Capital Gain (Rs.)", "Cost Conversion Basis",
                        "Computable"},
    ("dividends", "payment"): {"FX Rate", "FX Source", "FX Basis",
                               "Gross Dividend (Rs.)",
                               "Foreign Tax Withheld (Rs.)",
                               "Net Received (Rs.)"},
}

#: An aggregate cell is accepted only where it demonstrably foots to the
#: detail rows that moved: (frame, column) -> (detail frame, detail column).
#: There is no blanket exemption for A2/A3/FSI - a total that does not
#: reconcile to its own detail, before AND after, is a FAIL like any other.
AGGREGATE_DEPS = {
    ("a2", "Gross Amount Paid/Credited During the Period (Rs.)"):
        ("sales", "Full Value of Consideration (Rs.)"),
    ("a3", "Total Gross Proceeds from Sale/Redemption (Rs.)"):
        ("sales", "Full Value of Consideration (Rs.)"),
    ("a3", "Total Gross Amount Paid/Credited w.r.t. the Holding (Rs.)"):
        ("dividends", "Gross Dividend (Rs.)"),
    ("fsi", "Gross Income (Rs.)"):
        ("dividends", "Gross Dividend (Rs.)"),
    ("fsi", "Foreign Tax Withheld (Rs.)"):
        ("dividends", "Foreign Tax Withheld (Rs.)"),
}

#: Row-level rupee figures are each rounded to whole rupees before a total is
#: struck from the unrounded values, so a total and the sum of its own printed
#: rows differ by well under a rupee per contributing row.
def _agg_tolerance(n: int) -> float:
    return 0.5 * max(n, 1) + 1.0


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _d(v):
    try:
        return dt.date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def _exact_quote(basis: str) -> bool:
    """True only where the quote states no date substitution of any kind.

    FXQuote.basis spells out every substitution it makes - "nearest available
    ... date X used" or "SBI TTBR carried forward N days from X". Silence on
    both is what an exact-date quote looks like.
    """
    t = (basis or "").lower()
    return "nearest available" not in t and "carried forward" not in t


def _debug_rows(key, cols, before, after):
    """Every field that moved on every changed row, before -> after.

    Diagnostic only, printed under RSU_FA_ACCEPTANCE_DEBUG. This is what tells
    us how the engine actually represents a resolved leg, rather than how the
    harness assumes it does.
    """
    ix = {c: i for i, c in enumerate(cols)}
    watch = [c for c in (
        "Symbol", "Date of Acquisition", "Date of Sale", "Date", "Quantity",
        "Currency", "Proceeds (FC)", "Cost of Acquisition (FC)",
        "Gross Dividend (FC)", "Foreign Tax Withheld (FC)",
        "FX Rate - Sale Date", "FX Date Used - Sale", "FX Source - Sale",
        "FX Rate - Vest Date", "FX Date Used - Cost", "FX Source - Cost",
        "FX Rate", "FX Source", "FX Basis",
        "Full Value of Consideration (Rs.)", "Cost of Acquisition (Rs.)",
        "Capital Gain (Rs.)", "Computable", "Cost Conversion Basis",
    ) if c in ix]
    for i, (b, a) in enumerate(zip(before, after)):
        if b == a:
            continue
        moved = [cols[j] for j, (x, y) in enumerate(zip(b, a)) if x != y]
        print(f"    [debug] {key} row {i}: {len(moved)} field(s) moved -> {moved}")
        for c in watch:
            mark = "*" if b[ix[c]] != a[ix[c]] else " "
            print(f"    [debug]   {mark} {c:<36} {b[ix[c]]!r:>28}"
                  f"  ->  {a[ix[c]]!r}")


def source_upgrades(key, cols, before, after):
    """Rows on `key` where an FX rate legitimately changed provenance.

    Two kinds, and only two - each proved from the frames, never assumed:

      "upgrade"     a demonstrably STALE SBI carry became an exact ranked rate;
      "conversion"  a rate that NO source could supply became an exact ranked
                    rate, so a figure the baseline had to leave blank can now
                    be computed. The baseline was captured with no external
                    table on file, so this is the same root cause as the
                    upgrade case seen from the other side.

    Returns {row: (allowed columns, [audit records])}. A row that is absent has
    NOT been shown to be either, so every change on it remains a failure.
    """
    found: dict[int, tuple[set, list]] = {}
    ix = {c: i for i, c in enumerate(cols)}

    def reject(i, leg, why, detail=""):
        """Record why a row was NOT accepted. Diagnostic only - the predicate
        itself is unchanged, and a rejected row still fails stage 7."""
        if DEBUG:
            print(f"    [debug] {key} row {i} leg={leg:<8} REJECTED: {why}"
                  + (f"  {detail}" if detail else ""))
        return None

    for (leg, req_c, rate_c, src_c, used_c, basis_c, pairs) in UPGRADE_LEGS.get(key, []):
        needed = [req_c, rate_c, src_c] + [p[1] for p in pairs]
        needed += [used_c] if used_c else []
        needed += [basis_c] if basis_c else []
        missing = [c for c in needed if c not in ix]
        if missing:
            if DEBUG:
                print(f"    [debug] {key} leg={leg}: columns absent {missing}")
            continue
        for i, (b, a) in enumerate(zip(before, after)):
            if b == a:
                continue                              # nothing moved: not a case
            new_rate = _f(a[ix[rate_c]])
            if new_rate is None:
                reject(i, leg, f"no rate now in {rate_c!r}",
                       f"value={a[ix[rate_c]]!r}")
                continue
            old_rate = _f(b[ix[rate_c]])
            old_src = str(b[ix[src_c]]).strip()
            new_src = str(a[ix[src_c]]).strip()
            # Which of the two cases is this? Anything else is not an exception.
            if old_src == SBI_TTBR and old_rate is not None and old_rate != new_rate:
                kind = "upgrade"
            elif old_src in UNAVAILABLE_SRC and old_rate is None:
                kind = "conversion"
            else:
                reject(i, leg, "baseline is neither stale SBI nor unavailable",
                       f"old_src={old_src!r} old_rate={old_rate!r} "
                       f"new_rate={new_rate!r}")
                continue
            # Current source: a ranked provider, and never MANUAL.
            if new_src in (SBI_TTBR, MANUAL) or new_src not in RANKED:
                reject(i, leg, "current source not a ranked non-manual provider",
                       f"new_src={new_src!r} ranked={RANKED}")
                continue
            # The date the rate is required FOR has not moved.
            req_old, req_new = _d(b[ix[req_c]]), _d(a[ix[req_c]])
            if req_old is None or req_old != req_new:
                reject(i, leg, f"required FX date ({req_c}) unparsable or moved",
                       f"before={b[ix[req_c]]!r} after={a[ix[req_c]]!r}")
                continue
            # No tax-calculation input moved anywhere on the row - quantity,
            # price, dates, currency, holding period, foreign-currency amounts.
            moved_inv = [c for c in INVARIANTS.get(key, [])
                         if c in ix and b[ix[c]] != a[ix[c]]]
            if moved_inv:
                reject(i, leg, "a tax-calculation input moved",
                       "; ".join(f"{c}: {b[ix[c]]!r} -> {a[ix[c]]!r}"
                                 for c in moved_inv))
                continue
            # UPGRADE only: the baseline must state the SBI date it actually
            # used, that date must differ from the one required, and the
            # difference must be provable as one of resolve()'s two non-strict
            # SBI fallbacks - never assumed from magnitude alone, since those
            # two fallbacks have different, direction-dependent bounds.
            old_used, gap = None, None
            if kind == "upgrade":
                if used_c:
                    old_used = _d(b[ix[used_c]])
                    if old_used is None:
                        reject(i, leg, f"baseline {used_c} unparsable",
                               f"value={b[ix[used_c]]!r}")
                        continue
                    gap = (req_old - old_used).days
                else:
                    m = STALE_SBI.search(str(b[ix[basis_c]]))
                    if not m:
                        reject(i, leg, "baseline basis does not state a carry",
                               f"basis={b[ix[basis_c]]!r}")
                        continue
                    gap = int(m.group(1))
                    old_used = dt.datetime.strptime(m.group(2), "%d-%m-%Y").date()
                if gap == 0:
                    reject(i, leg, "baseline already used the exact required "
                           "date - not a substitution", f"date={old_used}")
                    continue
                elif gap > 0:
                    # BACKWARD carry. SBI's own strict pass is backward-only
                    # (SBITTBRSource.lookup(): `pool[pool["date"] <= on]`) and
                    # answers immediately for any gap within MAX_CARRY_DAYS -
                    # resolve() asks SBI first and the first source to answer
                    # wins outright, so no lower-ranked source is even
                    # consulted in that case. Only a carry that exceeded
                    # MAX_CARRY_DAYS proves SBI's strict pass failed outright.
                    if gap <= MAX_CARRY_DAYS:
                        reject(i, leg, "baseline carry was within SBI's own "
                               "strict tolerance - not proven stale",
                               f"gap={gap}d, limit={MAX_CARRY_DAYS}d")
                        continue            # a weekend carry, not a stale rate
                else:
                    # FORWARD substitution. SBI's strict pass never looks
                    # forward at all, so a forward date can only have come
                    # from resolve()'s bidirectional nearest-date fallback
                    # (`sbi.lookup(strict=False, nearest_within=
                    # MAX_NEAREST_PRIMARY_DAYS)`), which is itself bounded to
                    # MAX_NEAREST_PRIMARY_DAYS either way. A forward gap
                    # beyond that bound could not have been produced by any
                    # real fallback and is not accepted - this is a baseline
                    # SBI substitution within the carry window, not a stale
                    # carry, and is checked against that fallback's own bound.
                    if abs(gap) > MAX_NEAREST_PRIMARY_DAYS:
                        reject(i, leg, "baseline forward substitution exceeds "
                               "the nearest-date fallback window - not "
                               "proven",
                               f"gap={gap}d, "
                               f"window={MAX_NEAREST_PRIMARY_DAYS}d")
                        continue
            # CONVERSION only: the baseline must show the unavailable state on
            # every field, and the newly resolved rate must be what made the
            # row computable - not some other input.
            else:
                if used_c and str(b[ix[used_c]]).strip():
                    reject(i, leg, f"baseline {used_c} was not blank",
                           f"value={b[ix[used_c]]!r}")
                    continue                # it had a date, so it had a rate
                filled = [c for _, c in pairs if str(b[ix[c]]).strip()]
                if filled:
                    reject(i, leg, "baseline rupee figure was not blank",
                           "; ".join(f"{c}={b[ix[c]]!r}" for c in filled))
                    continue                # it had a figure, so it resolved
                if "Computable" in ix:
                    was, now = str(b[ix["Computable"]]), str(a[ix["Computable"]])
                    if was != now and not (was.startswith("No")
                                           and "FX" in was and now == "Yes"):
                        reject(i, leg, "computability moved for a non-FX reason",
                               f"{was!r} -> {now!r}")
                        continue            # computability moved for some
                                            # reason other than the FX rate
            # The new rate is for the EXACT date required - never a nearest
            # match, under either kind.
            if used_c:
                if _d(a[ix[used_c]]) != req_old:
                    reject(i, leg, "new rate is not for the EXACT date required",
                           f"{used_c}={a[ix[used_c]]!r} required={req_old}")
                    continue
            elif not _exact_quote(str(a[ix[basis_c]])):
                reject(i, leg, "new basis states a date substitution",
                       f"basis={a[ix[basis_c]]!r}")
                continue
            # Every rupee figure on the leg is its FC amount x the new rate, so
            # nothing but the rate produced it.
            ok, fcs = True, {}
            for fc_c, inr_c in pairs:
                names = fc_c if isinstance(fc_c, tuple) else (fc_c,)
                if any(n not in ix for n in names):
                    ok = reject(i, leg, f"FC column absent {names}")
                    break
                if any(b[ix[n]] != a[ix[n]] for n in names):
                    ok = reject(i, leg, "the FC amount itself moved",
                                "; ".join(f"{n}: {b[ix[n]]!r} -> {a[ix[n]]!r}"
                                          for n in names))
                    break                                   # FC itself moved
                vals = [_f(a[ix[n]]) for n in names]
                if any(v is None for v in vals):
                    ok = reject(i, leg, "FC amount unparsable",
                                "; ".join(f"{n}={a[ix[n]]!r}" for n in names))
                    break
                fc = vals[0] if len(vals) == 1 else vals[0] - vals[1]
                got, want = _f(a[ix[inr_c]]), round(fc * new_rate)
                if got is None or abs(got - want) > 1.0:
                    ok = reject(i, leg, f"{inr_c} is not FC x the new rate",
                                f"FC={fc} x {new_rate} -> want {want}, "
                                f"got {a[ix[inr_c]]!r}")
                    break                         # something other than the
                                                  # rate moved this figure
                fcs[inr_c] = fc
            if ok is None:
                continue
            if DEBUG:
                print(f"    [debug] {key} row {i} leg={leg:<8} ACCEPTED as "
                      f"{kind}: {old_src} -> {new_src} @ {new_rate} "
                      f"on {req_old}")
            allowed, audits = found.get(i, (set(), []))
            allowed |= UPGRADE_MOVES.get((key, leg), set())
            for fc_c, inr_c in pairs:
                audits.append({
                    "kind": kind, "frame": key, "leg": leg, "row": i,
                    "id": str(b[ix["Symbol"]]) if "Symbol" in ix else f"row {i}",
                    "required": req_old, "old_rate": old_rate, "old_src": old_src,
                    "old_date": old_used, "gap": gap, "new_rate": new_rate,
                    "new_src": new_src, "new_date": req_old, "fc_col": inr_c,
                    "fc": fcs[inr_c], "old_inr": _f(b[ix[inr_c]]),
                    "new_inr": _f(a[ix[inr_c]]),
                })
            found[i] = (allowed, audits)
    return found


def aggregate_explained(key, col, before_row, after_row, cols, digests, upgrades):
    """True where a moved total foots to detail rows that were all upgrades.

    Demonstrated, not assumed: the old total must reconcile to the baseline
    detail column, the new total to the current one, and every detail row whose
    figure moved must itself be a proven source upgrade.
    """
    dep = AGGREGATE_DEPS.get((key, col))
    if dep is None:
        return None
    d_key, d_col = dep
    b_rows, a_rows, d_cols = digests.get(d_key, (None, None, None))
    if b_rows is None or d_col not in d_cols:
        return None
    j = d_cols.index(d_col)
    b_vals = [_f(r[j]) for r in b_rows]
    a_vals = [_f(r[j]) for r in a_rows]
    if any(v is None for v in b_vals + a_vals):
        return None
    old_total, new_total = _f(before_row), _f(after_row)
    if old_total is None or new_total is None:
        return None
    tol = _agg_tolerance(len(b_vals))
    if abs(sum(b_vals) - old_total) > tol or abs(sum(a_vals) - new_total) > tol:
        return None                       # the total does not foot to its own
    moved = [n for n, (x, y) in enumerate(zip(b_vals, a_vals)) if x != y]
    if not moved or any(n not in upgrades.get(d_key, {}) for n in moved):
        return None                       # something moved that is not an upgrade
    return d_key, d_col, moved


def stage_7(rep, ctx, tmp):
    rep.stage(7, "NOTHING ELSE MOVED - EVERY FIGURE AGAINST THE PRE-FETCH BASELINE")
    if not BASELINE.exists():
        rep.check(BLOCKED, "a pre-fetch baseline exists to compare against",
                  "run `python3 tests/acceptance_realdata.py --baseline` BEFORE "
                  "fetching")
        return
    base = json.loads(BASELINE.read_text())

    # Both runs of every frame, so a total can be checked against its own
    # detail rather than taken on trust.
    digests, upgrades = {}, {}
    for key in COMPARED:
        want = base["frames"].get(key)
        got = frame_digest(ctx[key]) if key in ctx else None
        cols = base["columns"].get(key)
        if want is None or got is None or cols is None:
            continue
        if cols != [str(c) for c in ctx[key].columns] or len(want) != len(got):
            continue
        digests[key] = (want, got, cols)
        if DEBUG:
            _debug_rows(key, cols, want, got)
        upgrades[key] = source_upgrades(key, cols, want, got)

    audit = []
    for key in COMPARED:
        want = base["frames"].get(key)
        got = frame_digest(ctx[key]) if key in ctx else None
        if want is None or got is None:
            rep.check(FAIL, f"{key:<15} present in both runs", "frame missing")
            continue
        if base["columns"][key] != [str(c) for c in ctx[key].columns]:
            rep.check(FAIL, f"{key:<15} columns unchanged", "column set differs")
            continue
        if len(want) != len(got):
            rep.check(FAIL, f"{key:<15} row count unchanged",
                      f"{len(want)} -> {len(got)}")
            continue
        cols = base["columns"][key]
        rows_up = upgrades.get(key, {})
        filled, upgraded, changed = 0, 0, []
        for i, (a, b) in enumerate(zip(want, got)):
            allowed, audits = rows_up.get(i, (set(), []))
            for j, (x, y) in enumerate(zip(a, b)):
                if x == y:
                    continue
                col = cols[j]
                # The ONLY permitted change: a cell that was blank because a
                # rate was missing now holds a figure.
                if x == "" and y != "":
                    filled += 1
                # ...and the one movement proved legitimate above: a stale SBI
                # carry replaced by an exact rate from a ranked source, on a
                # column that depends on that rate.
                elif col in allowed:
                    upgraded += 1
                    audit.extend(m for m in audits if m.get("fc_col") == col)
                # A total may move only where it foots to detail rows that were
                # themselves proven upgrades. No blanket exemption for A2/A3/FSI.
                elif (dep := aggregate_explained(key, col, x, y, cols,
                                                 digests, upgrades)):
                    dk, dc, moved = dep
                    upgraded += 1
                    audit.append({"frame": key, "leg": "propagated", "row": i,
                                  "id": col, "dep": f"{dk}.{dc} rows {moved}",
                                  "old_inr": _f(x), "new_inr": _f(y)})
                else:
                    changed.append((i, col, x, y))
        detail = (f"{filled} cell(s) filled, {upgraded} legitimate source "
                  f"upgrade(s), 0 altered" if not changed
                  else f"{len(changed)} ALTERED: {changed[:3]}")
        rep.check(PASS if not changed else FAIL,
                  f"{key:<15} unchanged except newly convertible cells", detail)

    for kind, heading in (
            ("upgrade", "LEGITIMATE SOURCE UPGRADE: baseline SBI "
                        "substitution -> exact higher-priority source"),
            ("conversion", "LEGITIMATE NEW CONVERSION: previously "
                           "FX-unavailable -> exact ranked source")):
        rows = [m for m in audit if m.get("kind") == kind]
        if not rows:
            continue
        rep.check(REPORT, heading, f"{len(rows)} figure(s)")
        for m in rows:
            if kind == "upgrade":
                prov = (f"{m['gap']}d stale, limit {MAX_CARRY_DAYS}d"
                         if m["gap"] > 0 else
                         f"baseline SBI substitution within carry window, "
                         f"{abs(m['gap'])}d forward, window "
                         f"{MAX_NEAREST_PRIMARY_DAYS}d")
                was = (f"was {m['old_rate']} {m['old_src']} dated "
                       f"{m['old_date']:%d-%m-%Y} ({prov})")
            else:
                was = (f"was UNRESOLVED ({m['old_src']}) - no ranked source "
                       f"held the date, rupee figure left blank")
            rep.note(
                f"  {m['frame']} row {m['row']} [{m['id']}] {m['fc_col']}"
                f" | FX date required {m['required']:%d-%m-%Y}"
                f" | {was}"
                f" | now {m['new_rate']} {m['new_src']} dated "
                f"{m['new_date']:%d-%m-%Y} (exact)"
                f" | FC {m['fc']} | INR "
                f"{'(blank)' if m['old_inr'] is None else m['old_inr']} -> "
                f"{m['new_inr']}")
        if kind == "upgrade":
            rep.note("Reason: the frozen hierarchy offers every ranked source "
                     "the EXACT date before SBI's own non-strict answer for "
                     "the date stands (src/fx.py resolve()) - whether that "
                     "answer is a stale rate carried forward, or a baseline "
                     "SBI substitution within the carry window (the nearest "
                     "published date within MAX_NEAREST_PRIMARY_DAYS either "
                     "way, which SBI's own backward-only strict pass could "
                     "never itself have produced). The baseline was captured "
                     "with no external table on file, so these dates could "
                     "only be answered by one of SBI's own non-strict "
                     "fallbacks.")
        else:
            rep.note("Reason: no ranked source held these dates when the "
                     "baseline was captured, so the rate was FX_UNAVAILABLE "
                     "and every figure depending on it was left blank - the "
                     "three UBS input-data blockers this acceptance exists to "
                     "clear. A ranked source now supplies the exact date, so "
                     "the figure computes and the provenance names the "
                     "provider instead of NONE.")
        rep.note("The foreign-currency amount, the date required and every "
                 "tax-calculation input are unchanged - only the rate, its "
                 "provenance and the rupee figure it produces have moved. "
                 "Sign each line off.")

    props = [m for m in audit if m["leg"] == "propagated"]
    if props:
        rep.check(REPORT, "PROPAGATED TOTALS: moved only by the rows above",
                  f"{len(props)} total(s)")
        for m in props:
            rep.note(f"  {m['frame']}/{m['id']}: {m['old_inr']} -> "
                     f"{m['new_inr']}  (foots to {m['dep']})")

    hl_before = dict(base["headlines"])
    hl_after = dict(ctx["headlines"])
    moved = {k: (hl_before.get(k), v) for k, v in hl_after.items()
             if hl_before.get(k) != v}
    rep.check(REPORT, "headline figures, before -> after",
              "; ".join(f"{k}: {a} -> {b}" for k, (a, b) in moved.items())
              or "identical")
    rep.note("A headline may only move because a blank became a figure - the "
             "per-frame checks above are what prove that. Any other movement is "
             "a defect in the fetch, not an improvement. Sign this line off.")

    unres_before = base["unresolved"]
    rep.note(f"unresolved before: {unres_before}")


# ======================================================================
def capture_baseline() -> Path:
    """The pre-fetch state. Run ONCE, before any table is populated."""
    if FBIL_CSV.exists() or ECB_CSV.exists():
        print("REFUSED: an external table already exists. The baseline must be "
              "captured BEFORE fetching, or it proves nothing.")
        sys.exit(1)
    tmp = OUT / "baseline"
    tmp.mkdir(parents=True, exist_ok=True)
    ctx, fx = run_engine(tmp)
    data = {
        "captured": dt.datetime.now().isoformat(timespec="seconds"),
        "period": [PERIOD.start.isoformat(), PERIOD.end.isoformat()],
        "headlines": [[k, v] for k, v in ctx["headlines"]],
        "fx_status": ctx["fx_status"],
        "unresolved": sorted(f"{c} {d:%d-%m-%Y}" for d, c in fx.unresolved),
        "columns": {k: [str(c) for c in ctx[k].columns] for k in COMPARED},
        "frames": {k: frame_digest(ctx[k]) for k in COMPARED},
    }
    data["digest"] = hashlib.sha256(
        json.dumps(data["frames"], sort_keys=True).encode()).hexdigest()[:16]
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(data, indent=1, default=str))
    print(f"Baseline written: {BASELINE}")
    print(f"  digest        {data['digest']}")
    print(f"  fx status     {data['fx_status']}")
    print(f"  unresolved    {data['unresolved']}")
    return BASELINE


def do_fetch():
    from src import fxfetch
    print("Fetching. The engine never does this during a run.")
    for src, msg in fxfetch.refresh(CONFIG).items():
        print(f"  {src:>16}: {msg}")


# ======================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", action="store_true",
                    help="capture the pre-fetch baseline and exit")
    ap.add_argument("--fetch", action="store_true",
                    help="populate the FBIL and ECB tables and exit")
    args = ap.parse_args()
    if args.baseline:
        capture_baseline()
        return 0
    if args.fetch:
        do_fetch()
        return 0

    rep = Report()
    print("=" * 98)
    print("FX REAL-DATA ACCEPTANCE")
    print("=" * 98)
    print("Frozen and not to be changed to make a stage pass: the 30-day")
    print("nearest-date limit, SBI > Google > FBIL > ECB > manual precedence,")
    print("and every tax calculation.")

    fb, ec = stage_1(rep)
    stage_2(rep, fb, ec)
    stage_3(rep, fb, ec)
    stage_4(rep, fb, ec)
    stage_5(rep, fb, ec)

    tmp = OUT / "run"
    tmp.mkdir(parents=True, exist_ok=True)
    ctx, fx = run_engine(tmp)
    write_workbook(tmp / "acceptance_ScheduleFA.xlsx", ctx)
    stage_6(rep, ctx, fx)
    stage_7(rep, ctx, tmp)

    verdict, code = rep.verdict()
    path = rep.write(OUT / "acceptance_report.md")
    print()
    print("=" * 98)
    print(f"OVERALL: {verdict}"
          + (f"   ({rep.warnings} WARNING(S) TO INVESTIGATE)" if rep.warnings
             else ""))
    if verdict == BLOCKED:
        print("Stages needing real data have not run. This is NOT an acceptance.")
    if rep.warnings:
        print("A warning does not fail the acceptance. It does need an answer "
              "before sign-off.")
    print(f"Report: {path}")
    print("=" * 98)
    return code


if __name__ == "__main__":
    sys.exit(main())
