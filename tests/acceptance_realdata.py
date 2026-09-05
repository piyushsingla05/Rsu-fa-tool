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
    ECB, FBIL, GOOGLE_FINANCE, MANUAL, MAX_NEAREST_DAYS,
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
            q = src.lookup(gap_day, cur)
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


def stage_7(rep, ctx, tmp):
    rep.stage(7, "NOTHING ELSE MOVED - EVERY FIGURE AGAINST THE PRE-FETCH BASELINE")
    if not BASELINE.exists():
        rep.check(BLOCKED, "a pre-fetch baseline exists to compare against",
                  "run `python3 tests/acceptance_realdata.py --baseline` BEFORE "
                  "fetching")
        return
    base = json.loads(BASELINE.read_text())
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
        filled, changed = 0, []
        for i, (a, b) in enumerate(zip(want, got)):
            for j, (x, y) in enumerate(zip(a, b)):
                if x == y:
                    continue
                # The ONLY permitted change: a cell that was blank because a
                # rate was missing now holds a figure.
                if x == "" and y != "":
                    filled += 1
                else:
                    changed.append((i, base["columns"][key][j], x, y))
        rep.check(PASS if not changed else FAIL,
                  f"{key:<15} unchanged except newly convertible cells",
                  f"{filled} cell(s) filled, 0 altered" if not changed
                  else f"{len(changed)} ALTERED: {changed[:3]}")

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
