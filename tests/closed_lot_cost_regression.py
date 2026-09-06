"""Regression tests for the closed-lot-record hardcoded-zero-cost fix.

Prior defect (investigated read-only, then confirmed): build_lots()'s
DISPOSING_EVENTS handler built SaleMatch objects with `cost_price_fc=0.0`
hardcoded at two sites - the "closed-lot record" branch (a disposal that
states its own acquisition date, e.g. from a realised-gain/loss statement
table) and the final unmatched-disposal catch-all - regardless of whether
any acquisition cost was actually known. Whenever no cost was ever supplied
(a blank cost_fc, or an unmatched disposal with nothing to draw a cost
from), the hardcoded 0.0 flowed straight into build_cg() as a real,
legitimate-looking cost of Rs.0, reporting the ENTIRE sale proceeds as a
taxable capital gain - a materially wrong, overstated tax figure, not a
blank cell.

Fix (src/compute.py, src/review.py): both sites now construct the
SaleMatch with `cost_price_fc=float("nan")` instead of a placeholder zero.
Where a cost IS genuinely stated (`stated is not None`), it is still
applied via `m.stated_cost_fc` exactly as before - the NaN is only ever
"seen" downstream when no cost was ever supplied at all, at which point the
build_cg() `cost_ok` gate already added by commit 1412859 keeps Cost of
Acquisition/Capital Gain unresolved instead of computing a false gain.  A
new register category, ACQUISITION_COST_MISSING (distinct from both
TRANSFER_COST_MISSING, which is worded specifically for TRANSFER_IN, and
INVALID_NUMERIC_FIELD, which means a value was present but failed to
parse), is raised at the closed-lot-record site the moment a stated
acquisition date carries no cost. No FX, tax formula, FA-A2/A3 formula,
baseline, harness, or commit d2377c0/320c53b/1412859 is touched.

Entirely self-contained (synthetic events + a controlled FXTable) - no
private data, no network, no change to the acceptance baseline.

Run: python3 -m tests.closed_lot_cost_regression
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.compute import (                                                # noqa: E402
    build_cg, build_lots, build_reconciliation, ComputeOptions,
)
from src.fx import FXTable                                               # noqa: E402
from src.models import EVENTS_COLUMNS, Period                            # noqa: E402
from src.review import (                                                 # noqa: E402
    ACQUISITION_COST_MISSING, MISSING_VEST_DATE, Register,
)

PASS, FAIL = "PASS", "FAIL"
_fails = 0


def check(ok, label, detail=""):
    global _fails
    status = PASS if ok else FAIL
    print(f"  {status:<6} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        _fails += 1


def section(title):
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def events_frame(rows):
    df = pd.DataFrame(rows)
    for c in EVENTS_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return df[EVENTS_COLUMNS]


def controlled_fx(register):
    fx = FXTable(ROOT / "config" / "fx_rates.csv", register=register)
    manual = pd.DataFrame([
        {"date": "2024-01-01", "currency": "USD", "ttbr": 83.0, "verified": "Y"},
        {"date": "2024-06-01", "currency": "USD", "ttbr": 83.8, "verified": "Y"},
    ])
    fx.merge(manual, priority=True)
    return fx


PERIOD = Period(dt.date(2024, 1, 1), dt.date(2024, 12, 31))
OPTS = ComputeOptions()
FULL_PROCEEDS = round(30 * 70.0 * 83.8, 0)     # = 175980 - what a false-zero-cost
                                                 # sale would wrongly report as gain


def run_case(sell_extra, with_prior_vest=False):
    rows = []
    if with_prior_vest:
        rows.append({"date": dt.date(2024, 1, 1), "broker": "X", "account_no": "1",
                      "symbol": "AAPL", "event": "VEST", "quantity": 50.0,
                      "price_fc": 40.0, "amount_fc": "", "tax_fc": "",
                      "acquired_on": "", "cost_fc": "", "currency": "USD",
                      "notes": ""})
    base = {"date": dt.date(2024, 6, 1), "broker": "X", "account_no": "1",
            "symbol": "AAPL", "event": "SELL", "quantity": 30.0, "price_fc": 70.0,
            "amount_fc": "", "tax_fc": "", "acquired_on": "", "cost_fc": "",
            "currency": "USD", "notes": ""}
    base.update(sell_extra)
    rows.append(base)
    events = events_frame(rows)
    register = Register()
    lots, matches = build_lots(events, register, as_at=dt.date(2024, 12, 31))
    fx = controlled_fx(register)
    cg = build_cg(matches, fx, OPTS, PERIOD, register)
    recon = build_reconciliation(events, lots, PERIOD, register)
    return events, lots, matches, cg, register, recon


# ======================================================================
section("A. STATED ACQUISITION DATE + VALID STATED COST - unchanged")

_, _, matches_a, cg_a, reg_a, _ = run_case({"acquired_on": "2024-01-01",
                                             "cost_fc": "1200.0"})
row_a = cg_a.iloc[0]
check(matches_a[0].stated_cost_fc == 1200.0,
      "the stated cost is applied to the SaleMatch exactly as before",
      f"stated_cost_fc={matches_a[0].stated_cost_fc!r}")
check(row_a["Cost of Acquisition (Rs.)"] == round(1200.0 * 83.0, 0)
      and row_a["Capital Gain (Rs.)"] == round(30 * 70 * 83.8 - 1200.0 * 83.0, 0)
      and row_a["Computable"] == "Yes",
      "Cost of Acquisition / Capital Gain / Computable are the same "
      "figures a valid stated cost has always produced",
      f"cost={row_a['Cost of Acquisition (Rs.)']!r} "
      f"gain={row_a['Capital Gain (Rs.)']!r} computable={row_a['Computable']!r}")
check(not [f for f in reg_a.blockers if f.reason == ACQUISITION_COST_MISSING],
      "no ACQUISITION_COST_MISSING blocker when a real cost was stated",
      str(reg_a.blockers))


# ======================================================================
section("B. STATED ACQUISITION DATE + BLANK COST - no artificial zero")

_, _, matches_b, cg_b, reg_b, _ = run_case({"acquired_on": "2024-01-01",
                                             "cost_fc": ""})
row_b = cg_b.iloc[0]
check(matches_b[0].cost_price_fc != matches_b[0].cost_price_fc,
      "the SaleMatch's cost_price_fc is NaN, not 0.0 - never an invented "
      "placeholder cost",
      f"cost_price_fc={matches_b[0].cost_price_fc!r}")
check(matches_b[0].stated_cost_fc is None,
      "no stated_cost_fc either - genuinely nothing was supplied",
      f"stated_cost_fc={matches_b[0].stated_cost_fc!r}")
check(row_b["Cost of Acquisition (Rs.)"] == "" and row_b["Capital Gain (Rs.)"] == "",
      "Cost of Acquisition and Capital Gain are BLANK, not a false Rs.0 "
      f"cost / Rs.{FULL_PROCEEDS:,.0f} full-proceeds gain",
      f"cost={row_b['Cost of Acquisition (Rs.)']!r} "
      f"gain={row_b['Capital Gain (Rs.)']!r}")
check(row_b["Computable"] != "Yes",
      "Computable is NOT 'Yes'", f"Computable={row_b['Computable']!r}")
blockers_b = [f for f in reg_b.blockers if f.reason == ACQUISITION_COST_MISSING]
check(len(blockers_b) == 1 and "no acquisition" in blockers_b[0].detail,
      "an explicit ACQUISITION_COST_MISSING blocker identifies the gap - "
      "not called INVALID_NUMERIC_FIELD, since nothing failed to parse, "
      "nothing was ever supplied",
      blockers_b[0].detail if blockers_b else "no blocker raised")


# ======================================================================
section("C. STATED ACQUISITION DATE + MALFORMED COST REACHING THIS LAYER "
        "AS A LITERAL ZERO - must not be silently trusted as real")

# generic.py::to_number() is explicitly out of scope for this fix (a separate,
# subsequent fix); this reproduces its CURRENT behaviour - a malformed cost
# string is already coerced to a literal "0.0" by the time it reaches this
# layer, so cost_fc here is indistinguishable from case G's evidenced zero.
# This is the known, documented boundary of what this fix can address on
# its own (see report item 8/D).
_, _, matches_c, cg_c, reg_c, _ = run_case({"acquired_on": "2024-01-01",
                                             "cost_fc": "0.0"})
row_c = cg_c.iloc[0]
check(matches_c[0].stated_cost_fc == 0.0,
      "cost_fc='0.0' is treated as a STATED (evidenced-by-the-data) cost "
      "under the current schema's convention - identical to case G below, "
      "since this layer cannot yet tell a to_number()-zeroed malformed "
      "value apart from a genuinely-verified zero (that gap belongs to "
      "the separate to_number() fix, not this one)",
      f"stated_cost_fc={matches_c[0].stated_cost_fc!r}")
check(row_c["Cost of Acquisition (Rs.)"] == 0.0
      and row_c["Capital Gain (Rs.)"] == FULL_PROCEEDS,
      "documenting current behaviour: a cost_fc of '0.0', however it "
      "arrived, is used as a real Rs.0 cost - this is the pre-existing, "
      "unresolved-until-to_number()-is-fixed edge this suite documents "
      "rather than silently passes over",
      f"cost={row_c['Cost of Acquisition (Rs.)']!r} "
      f"gain={row_c['Capital Gain (Rs.)']!r}")


# ======================================================================
section("D. UNMATCHED DISPOSAL, NO ACQUISITION DATE, NO COST - "
        "MISSING_VEST_DATE remains, CG row must NOT show full proceeds as gain")

_, _, matches_d, cg_d, reg_d, _ = run_case({"acquired_on": ""})
row_d = cg_d.iloc[0]
check(matches_d[0].cost_price_fc != matches_d[0].cost_price_fc,
      "the unmatched SaleMatch's cost_price_fc is NaN, not 0.0",
      f"cost_price_fc={matches_d[0].cost_price_fc!r}")
blockers_mvd = [f for f in reg_d.blockers if f.reason == MISSING_VEST_DATE]
check(len(blockers_mvd) == 1,
      "the existing MISSING_VEST_DATE blocker still fires, unchanged",
      str(blockers_mvd))
check(row_d["Cost of Acquisition (Rs.)"] == "" and row_d["Capital Gain (Rs.)"] == "",
      f"the CG row does NOT show Rs.{FULL_PROCEEDS:,.0f} (full proceeds) as "
      "a computed gain any more",
      f"cost={row_d['Cost of Acquisition (Rs.)']!r} "
      f"gain={row_d['Capital Gain (Rs.)']!r}")
check(row_d["Computable"] != "Yes",
      "Computable is NOT 'Yes' for this row",
      f"Computable={row_d['Computable']!r}")


# ======================================================================
section("E. VALID NORMAL LOT MATCH - completely unchanged")

_, lots_e, matches_e, cg_e, reg_e, recon_e = run_case({"acquired_on": ""},
                                                        with_prior_vest=True)
row_e = cg_e.iloc[0]
check(matches_e[0].cost_price_fc == 40.0,
      "the ordinary FIFO match still costs the sale at the real matched "
      "lot's price - completely untouched by this fix",
      f"cost_price_fc={matches_e[0].cost_price_fc!r}")
check(row_e["Cost of Acquisition (Rs.)"] == round(30 * 40.0 * 83.0, 0)
      and row_e["Capital Gain (Rs.)"] == round(30 * 70 * 83.8 - 30 * 40.0 * 83.0, 0)
      and row_e["Computable"] == "Yes",
      "cost/gain/computable exactly match the pre-fix formula",
      f"cost={row_e['Cost of Acquisition (Rs.)']!r} "
      f"gain={row_e['Capital Gain (Rs.)']!r}")
check(not reg_e.blockers, "no blocker at all for a clean, fully-matched sale",
      str(reg_e.blockers))
row_recon_e = recon_e[recon_e["Symbol"] == "AAPL"].iloc[0]
check(row_recon_e["Status"] == "Reconciled",
      "reconciliation is unaffected - still reconciles for a normal match",
      f"status={row_recon_e['Status']!r}")


# ======================================================================
section("F. STATED ACQUISITION DATE + STATED COST VIA THE remaining > 1e-6 "
        "BRANCH - unchanged (this site was never defective)")

# Two SELLs at the same broker/symbol: the first consumes the only real lot,
# the second states its own acquisition date/cost with nothing left to match
# against a Lot, exercising the `remaining > 1e-6 and stated is not None`
# site specifically (not the final catch-all).
events_f = events_frame([
    {"date": dt.date(2024, 1, 1), "broker": "X", "account_no": "1", "symbol": "AAPL",
     "event": "VEST", "quantity": 10.0, "price_fc": 40.0, "amount_fc": "",
     "tax_fc": "", "acquired_on": "", "cost_fc": "", "currency": "USD", "notes": ""},
    {"date": dt.date(2024, 6, 1), "broker": "X", "account_no": "1", "symbol": "AAPL",
     "event": "SELL", "quantity": 10.0, "price_fc": 70.0, "amount_fc": "",
     "tax_fc": "", "acquired_on": "", "cost_fc": "", "currency": "USD", "notes": ""},
    {"date": dt.date(2024, 6, 2), "broker": "X", "account_no": "1", "symbol": "AAPL",
     "event": "SELL", "quantity": 20.0, "price_fc": 70.0, "amount_fc": "",
     "tax_fc": "", "acquired_on": "2023-01-01", "cost_fc": "600.0",
     "currency": "USD", "notes": ""},
])
reg_f = Register()
lots_f, matches_f = build_lots(events_f, reg_f, as_at=dt.date(2024, 12, 31))
fx_f = controlled_fx(reg_f)
cg_f = build_cg(matches_f, fx_f, OPTS, PERIOD, reg_f)
second_sell_match = [m for m in matches_f if m.sold_on == dt.date(2024, 6, 2)]
check(len(second_sell_match) == 1
      and second_sell_match[0].stated_cost_fc == 600.0,
      "the second SELL's 20 unmatched shares still get their broker-stated "
      "cost applied via the remaining > 1e-6 / stated-is-not-None branch",
      f"{second_sell_match[0] if second_sell_match else None}")
check(not [f for f in reg_f.blockers if f.reason == ACQUISITION_COST_MISSING],
      "no ACQUISITION_COST_MISSING blocker for that site - it was never "
      "defective and remains untouched",
      str(reg_f.blockers))


# ======================================================================
section("G. LEGITIMATE, EXPLICITLY EVIDENCED ZERO COST - "
        "remains legitimate, not falsely treated as missing")

# The current schema has no separate "verified zero" flag distinct from a
# plain parseable "0" in cost_fc; the existing, pre-existing (unrelated to
# this fix) convention throughout build_lots() is that ANY value which
# successfully parses via `stated = float(stated) if str(stated).strip()
# not in ("", "nan") else None` - including "0" - counts as evidence, and
# is applied via stated_cost_fc exactly like any other real figure. This
# fix relies on, and preserves, that existing convention: it changes
# behaviour ONLY when `stated` ends up None (nothing parsed at all), never
# when `stated` is a real, if numerically zero, value. No new semantic
# field is introduced here - see report item 8/D for why not.
_, _, matches_g, cg_g, reg_g, _ = run_case({"acquired_on": "2024-01-01",
                                             "cost_fc": "0",
                                             "notes": "broker confirms Rs.0 cost basis"})
row_g = cg_g.iloc[0]
check(matches_g[0].stated_cost_fc == 0.0,
      "a data-evidenced cost_fc='0' is still applied as stated_cost_fc, "
      "exactly as before this fix",
      f"stated_cost_fc={matches_g[0].stated_cost_fc!r}")
check(row_g["Cost of Acquisition (Rs.)"] == 0.0
      and row_g["Capital Gain (Rs.)"] == FULL_PROCEEDS
      and row_g["Computable"] == "Yes",
      "the legitimate zero-cost sale still computes a real cost of Rs.0 "
      "and a real (not blanked) capital gain - it is NOT falsely treated "
      "as a missing-cost case",
      f"cost={row_g['Cost of Acquisition (Rs.)']!r} "
      f"gain={row_g['Capital Gain (Rs.)']!r} "
      f"computable={row_g['Computable']!r}")
check(not [f for f in reg_g.blockers if f.reason == ACQUISITION_COST_MISSING],
      "no ACQUISITION_COST_MISSING blocker for a value that WAS supplied",
      str(reg_g.blockers))


# ======================================================================
print()
print("=" * 92)
if _fails:
    print(f"RESULT: {_fails} CHECK(S) FAILED")
else:
    print("RESULT: ALL CHECKS PASSED")
print("=" * 92)

if __name__ == "__main__":
    sys.exit(1 if _fails else 0)
