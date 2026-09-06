"""Regression tests for the to_number() zero-coercion fix.

Prior defect (investigated read-only across three earlier passes, then
approved for a fix): src/ingest/generic.py::to_number() converted EVERY
failure mode - None, blank, a bare "-"/"n/a", garbled text, an unrelated
footnote like "N/A - see attached", and a genuine NaN/pd.NA - to the same
0.0 a real, valid zero would also produce. At several tax-consequential
call sites in src/ingest/extract.py, that false zero reached the compute
layer as a trusted figure: a false Rs.0 cash balance (FA-A2), a false Rs.0
acquisition cost (fabricating a full-proceeds capital gain), or a false
Rs.0 sale price/proceeds (fabricating a loss) - all with zero diagnostic,
and all completely invisible to the NaN-aware diagnostics already added by
commits 1412859 and a877a1b, since to_number() never actually produced a
NaN for them to catch.

Fix:
  - src/ingest/generic.py: to_number(v, on_invalid="zero") gains an opt-in
    parameter. The default ("zero") is byte-for-byte the historic
    behaviour - every caller that does not pass it is unaffected.
    on_invalid="nan" returns NaN instead, but ONLY for a value that was
    genuinely PRESENT and failed to parse; a genuinely blank/absent value
    (None, NaN, pd.NA, "", whitespace, a bare "-"/"--", "n/a"/"not
    applicable") still always returns 0.0, in both modes.
  - src/ingest/extract.py: the cash, closed_lot_gains (PDF role and its
    spreadsheet twin _extract_closed_lot_table), sales, and
    holdings_as_acquisition/holdings call sites are retrofitted to
    on_invalid="nan" for their tax-consequential fields (balance, cost,
    proceeds, price/amount). Every existing fallback ("if not cost_total:
    use cost_per_share", "if not price: use credited") is preserved by
    explicitly checking `x == x` alongside truthiness, so a malformed
    (NaN) value still falls through to the fallback exactly as a
    genuinely blank/zero one already did - only when BOTH figures are
    unparseable does the result end up genuinely unresolved.
    _extract_closed_lot_table's own proceeds-minus-cost-equals-gain
    reconciliation check is made NaN-aware too, so a malformed figure
    there is treated as a break rather than silently passing.
  - src/compute.py: build_cg() gains a sale_ok gate (the sale-price/
    proceeds sibling of the existing cost_ok gate from 1412859) so a NaN
    sale_price_fc blanks Consideration/Capital Gain and sets
    Computable != "Yes", instead of fabricating a loss. build_a2() gains
    an invalid-cash-balance check: a NaN balance_fc raises
    INVALID_NUMERIC_FIELD and blanks that account's Peak/Closing Balance
    for the WHOLE period (the true peak could have fallen on exactly the
    missing date), rather than silently reporting Rs.0.

None of this touches FX hierarchy, tax formulas, the acceptance baseline,
the acceptance harness, or commits d2377c0/320c53b/1412859/a877a1b.

Entirely self-contained (synthetic events/cash + a controlled FXTable) -
no private data, no network, no PDF/document extraction pipeline (which
this suite does not attempt to drive end-to-end; the sales-role and
holdings_as_acquisition-role fallback conditions are exercised by mirroring
the exact conditional expressions now in src/ingest/extract.py, called out
by name below, since that arithmetic is not separately factored into an
importable helper).

Run: python3 -m tests.to_number_zero_coercion_regression
"""
from __future__ import annotations

import datetime as dt
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.compute import build_a2, build_cg, build_lots, ComputeOptions  # noqa: E402
from src.fx import FXTable                                              # noqa: E402
from src.ingest.generic import to_number                                # noqa: E402
from src.models import EVENTS_COLUMNS, Period                           # noqa: E402
from src.review import (                                                # noqa: E402
    ACQUISITION_COST_MISSING, INVALID_NUMERIC_FIELD, Register,
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


def is_nan(x):
    return isinstance(x, float) and math.isnan(x)


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
        {"date": "2024-12-31", "currency": "USD", "ttbr": 84.0, "verified": "Y"},
    ])
    fx.merge(manual, priority=True)
    return fx


PERIOD = Period(dt.date(2024, 1, 1), dt.date(2024, 12, 31))
OPTS = ComputeOptions()


# ======================================================================
section("0. to_number() INPUT MATRIX - default mode unchanged, nan mode "
        "distinguishes blank/placeholder from a genuine parse failure")

CASES = [
    ("valid positive '1234.56'", "1234.56", 1234.56),
    ("valid negative '-42.5'", "-42.5", -42.5),
    ("currency '$1,234.56'", "$1,234.56", 1234.56),
    ("comma-formatted '1,234.56'", "1,234.56", 1234.56),
    ("parentheses negative '(123.45)'", "(123.45)", -123.45),
    ("numeric int 0", 0, 0.0),
    ("numeric float 0.0", 0.0, 0.0),
    ("string '0'", "0", 0.0),
    ("string '0.0'", "0.0", 0.0),
]
for label, v, expect in CASES:
    z = to_number(v)
    n = to_number(v, on_invalid="nan")
    check(z == expect and n == expect,
          f"{label}: SAME real value in both modes ({expect})",
          f"zero-mode={z!r} nan-mode={n!r}")

BLANK_CASES = [
    ("None", None), ("empty string", ""), ("whitespace '   '", "   "),
    ("bare dash '-'", "-"), ("double dash '--'", "--"), ("'n/a'", "n/a"),
    ("'not applicable'", "not applicable"), ("real float NaN", float("nan")),
    ("pandas NA", pd.NA),
]
for label, v in BLANK_CASES:
    z = to_number(v)
    n = to_number(v, on_invalid="nan")
    check(z == 0.0 and n == 0.0,
          f"{label}: genuinely blank/absent -> 0.0 in BOTH modes (unchanged)",
          f"zero-mode={z!r} nan-mode={n!r}")

MALFORMED_CASES = [
    ("malformed 'garbled$$'", "garbled$$"),
    ("malformed '12.34.56'", "12.34.56"),
    ("unrelated text 'N/A - see attached'", "N/A - see attached"),
]
for label, v in MALFORMED_CASES:
    z = to_number(v)
    n = to_number(v, on_invalid="nan")
    check(z == 0.0, f"{label}: default mode still returns 0.0 (unchanged)",
          f"zero-mode={z!r}")
    check(is_nan(n), f"{label}: nan mode returns NaN, distinguishing it "
          "from a genuine zero/blank",
          f"nan-mode={n!r}")


# ======================================================================
section("A. MALFORMED CASH BALANCE - no false Rs.0 FA-A2; blocker + blank")

accounts = pd.DataFrame([{"broker": "X", "account_no": "1",
                          "country": "United States of America",
                          "institution_name": "Test Bank", "address": "",
                          "zip": "", "status": "Owner"}])
empty_events = events_frame([])

cash_a = pd.DataFrame([{"date": dt.date(2024, 12, 31), "broker": "X",
                        "account_no": "1",
                        "balance_fc": to_number("N/A - see statement",
                                                on_invalid="nan"),
                        "currency": "USD", "source": "test"}])
reg_a = Register()
fx_a = controlled_fx(reg_a)
a2_a = build_a2(cash_a, accounts, empty_events, fx_a, PERIOD, reg_a)
row_a = a2_a.iloc[0]
check(row_a["Peak Balance During the Period (Rs.)"] == ""
      and row_a["Closing Balance (Rs.)"] == "",
      "Peak and Closing Balance are BLANK, not a false Rs.0",
      f"peak={row_a['Peak Balance During the Period (Rs.)']!r} "
      f"closing={row_a['Closing Balance (Rs.)']!r}")
blockers_a = [f for f in reg_a.blockers if f.reason == INVALID_NUMERIC_FIELD]
check(len(blockers_a) == 1 and "31-12-2024" in blockers_a[0].detail,
      "an explicit INVALID_NUMERIC_FIELD blocker identifies the unparseable "
      "balance and its date",
      blockers_a[0].detail if blockers_a else "no blocker raised")


# ======================================================================
section("B. GENUINE ZERO CASH BALANCE - remains a valid Rs.0")

cash_b = pd.DataFrame([{"date": dt.date(2024, 12, 31), "broker": "X",
                        "account_no": "1",
                        "balance_fc": to_number("0", on_invalid="nan"),
                        "currency": "USD", "source": "test"}])
reg_b = Register()
fx_b = controlled_fx(reg_b)
a2_b = build_a2(cash_b, accounts, empty_events, fx_b, PERIOD, reg_b)
row_b = a2_b.iloc[0]
check(row_b["Peak Balance During the Period (Rs.)"] == 0.0
      and row_b["Closing Balance (Rs.)"] == 0.0,
      "a genuinely evidenced zero balance still computes a real Rs.0, not "
      "blanked",
      f"peak={row_b['Peak Balance During the Period (Rs.)']!r} "
      f"closing={row_b['Closing Balance (Rs.)']!r}")
check(not [f for f in reg_b.blockers if f.reason == INVALID_NUMERIC_FIELD],
      "no INVALID_NUMERIC_FIELD blocker for a value that WAS parseable",
      str(reg_b.blockers))


# ======================================================================
section("C. MALFORMED CLOSED-LOT COST (simulating the retrofitted "
        "closed_lot_gains/_extract_closed_lot_table ingestion) - no false gain")

events_c = events_frame([
    {"date": dt.date(2024, 6, 1), "broker": "X", "account_no": "1",
     "symbol": "AAPL", "event": "SELL", "quantity": 30.0, "price_fc": 70.0,
     "amount_fc": "", "tax_fc": "", "acquired_on": "2024-01-01",
     # exactly what the retrofitted `cost = G.to_number(r.get("cost"),
     # on_invalid="nan")` now produces for a malformed source cell:
     "cost_fc": to_number("garbled$$", on_invalid="nan"),
     "currency": "USD", "notes": ""},
])
reg_c = Register()
lots_c, matches_c = build_lots(events_c, reg_c, as_at=dt.date(2024, 12, 31))
fx_c = controlled_fx(reg_c)
cg_c = build_cg(matches_c, fx_c, OPTS, PERIOD, reg_c)
row_c = cg_c.iloc[0]
check(matches_c[0].stated_cost_fc is None,
      "a malformed (NaN) cost_fc is excluded by build_lots()'s existing "
      "blank/NaN 'stated' check (a877a1b) - never applied as a real zero",
      f"stated_cost_fc={matches_c[0].stated_cost_fc!r}")
check(row_c["Cost of Acquisition (Rs.)"] == "" and row_c["Capital Gain (Rs.)"] == "",
      "Cost of Acquisition and Capital Gain are blank, not a false full-"
      "proceeds gain",
      f"cost={row_c['Cost of Acquisition (Rs.)']!r} "
      f"gain={row_c['Capital Gain (Rs.)']!r}")
check(row_c["Computable"] != "Yes",
      "Computable is not 'Yes'", f"Computable={row_c['Computable']!r}")
check(any(f.reason == ACQUISITION_COST_MISSING for f in reg_c.blockers),
      "the existing ACQUISITION_COST_MISSING blocker fires (the malformed "
      "cost is indistinguishable, at this layer, from a genuinely missing "
      "one - both correctly refuse to invent a cost)",
      str([f.reason for f in reg_c.blockers]))


# ======================================================================
section("D. GENUINE ZERO CLOSED-LOT COST - remains a legitimate zero")

events_d = events_frame([
    {"date": dt.date(2024, 6, 1), "broker": "X", "account_no": "1",
     "symbol": "AAPL", "event": "SELL", "quantity": 30.0, "price_fc": 70.0,
     "amount_fc": "", "tax_fc": "", "acquired_on": "2024-01-01",
     "cost_fc": to_number("0", on_invalid="nan"),
     "currency": "USD", "notes": ""},
])
reg_d = Register()
lots_d, matches_d = build_lots(events_d, reg_d, as_at=dt.date(2024, 12, 31))
fx_d = controlled_fx(reg_d)
cg_d = build_cg(matches_d, fx_d, OPTS, PERIOD, reg_d)
row_d = cg_d.iloc[0]
check(matches_d[0].stated_cost_fc == 0.0,
      "a genuinely evidenced cost_fc='0' is still applied as a real "
      "stated_cost_fc",
      f"stated_cost_fc={matches_d[0].stated_cost_fc!r}")
check(row_d["Cost of Acquisition (Rs.)"] == 0.0 and row_d["Computable"] == "Yes",
      "the legitimate zero-cost sale still computes a real Rs.0 cost and a "
      "real (non-blank) capital gain",
      f"cost={row_d['Cost of Acquisition (Rs.)']!r} "
      f"computable={row_d['Computable']!r}")


# ======================================================================
section("E. MALFORMED SALES PRICE + VALID CREDITED - the credited-amount "
        "fallback still works")

# Mirrors src/ingest/extract.py's "sales" role exactly (price/credited/
# gross/fee block): price is retrofitted to on_invalid="nan", and the
# fallback condition explicitly excludes NaN (not merely falsy) so a
# malformed price still falls through to `credited`.
qty = 20.0
price = to_number("N/A - see attached", on_invalid="nan")
credited = to_number("1500.00", on_invalid="nan")
price_ok = price == price
credited_ok = credited == credited
gross = qty * price if (price and price_ok) else credited
fee = (round(gross - credited, 2)
       if (price and price_ok and credited and credited_ok) else 0.0)
check(is_nan(price), "the malformed price itself is NaN", f"price={price!r}")
check(gross == credited == 1500.0,
      "gross correctly falls back to the valid credited amount - the "
      "fallback is NOT disabled by the malformed price",
      f"gross={gross!r} credited={credited!r}")
check(fee == 0.0, "fee is not computed from a NaN price (stays 0.0, no "
      "nonsensical NaN fee note)", f"fee={fee!r}")


# ======================================================================
section("F. MALFORMED SALES PRICE + MALFORMED CREDITED - unresolved, not "
        "a fabricated zero proceeds")

price_f = to_number("garbled$$", on_invalid="nan")
credited_f = to_number("N/A", on_invalid="nan")   # bare "N/A" -> still 0.0,
                                                   # both modes (blank marker)
check(credited_f == 0.0,
      "a bare 'N/A' credited amount is a genuine BLANK marker, not a "
      "malformed one, and stays 0.0 even in nan mode",
      f"credited_f={credited_f!r}")
# Use a genuinely malformed (not blank-marker) credited value instead, to
# exercise the both-unparseable case the requirement actually describes:
credited_f2 = to_number("N/A - see attached", on_invalid="nan")
price_ok_f = price_f == price_f
credited_ok_f = credited_f2 == credited_f2
gross_f = qty * price_f if (price_f and price_ok_f) else credited_f2
check(is_nan(gross_f),
      "when BOTH price and credited are genuinely unparseable, gross ends "
      "up unresolved (NaN), never a fabricated 0.0",
      f"gross_f={gross_f!r}")

events_f = events_frame([
    {"date": dt.date(2024, 6, 1), "broker": "X", "account_no": "1",
     "symbol": "AAPL", "event": "SELL", "quantity": qty,
     "price_fc": round(price_f, 6), "amount_fc": round(gross_f, 2),
     "tax_fc": "", "acquired_on": "", "cost_fc": "", "currency": "USD",
     "notes": ""},
    {"date": dt.date(2024, 1, 1), "broker": "X", "account_no": "1",
     "symbol": "AAPL", "event": "VEST", "quantity": qty, "price_fc": 40.0,
     "amount_fc": "", "tax_fc": "", "acquired_on": "", "cost_fc": "",
     "currency": "USD", "notes": ""},
])
reg_f = Register()
lots_f, matches_f = build_lots(events_f, reg_f, as_at=dt.date(2024, 12, 31))
fx_f = controlled_fx(reg_f)
cg_f = build_cg(matches_f, fx_f, OPTS, PERIOD, reg_f)
row_f = cg_f.iloc[0]
check(row_f["Full Value of Consideration (Rs.)"] == ""
      and row_f["Capital Gain (Rs.)"] == "",
      "Consideration and Capital Gain are blank, not a fabricated Rs.0 "
      "proceeds / false large loss",
      f"consideration={row_f['Full Value of Consideration (Rs.)']!r} "
      f"gain={row_f['Capital Gain (Rs.)']!r}")
check(row_f["Computable"] == "No - invalid sale price/proceeds",
      "Computable explicitly names the invalid sale price/proceeds",
      f"Computable={row_f['Computable']!r}")
check(any(f.reason == INVALID_NUMERIC_FIELD for f in reg_f.blockers),
      "an explicit INVALID_NUMERIC_FIELD blocker is raised for the sale "
      "side (the sale_ok gate)",
      str([f.reason for f in reg_f.blockers]))


# ======================================================================
section("G. MALFORMED holdings_as_acquisition COST (both cost_total AND "
        "cost_per_share unparseable) - no false Rs.0 acquisition value")

# Mirrors the retrofitted holdings_as_acquisition branch: `if not
# cost_total or cost_total != cost_total:` falls through to the
# cost_per_share fallback exactly as a blank/zero cost_total already did.
shares_g = 50.0
cost_total_g = to_number("garbled$$", on_invalid="nan")
if not cost_total_g or cost_total_g != cost_total_g:
    cps_g = to_number("also garbled", on_invalid="nan")
    cost_total_g = cps_g * shares_g
check(is_nan(cost_total_g),
      "with BOTH figures unparseable, cost_total stays unresolved (NaN), "
      "never a fabricated Rs.0",
      f"cost_total_g={cost_total_g!r}")

events_g = events_frame([
    {"date": dt.date(2024, 1, 1), "broker": "X", "account_no": "1",
     "symbol": "AAPL", "event": "POSITION", "quantity": shares_g,
     "price_fc": round(cost_total_g / shares_g, 6), "amount_fc": "",
     "tax_fc": "", "acquired_on": "", "cost_fc": "", "currency": "USD",
     "notes": ""},
])
reg_g = Register()
lots_g, matches_g = build_lots(events_g, reg_g, as_at=dt.date(2024, 12, 31))
check(lots_g[0].quantity == 50.0 and is_nan(lots_g[0].price_fc),
      "the POSITION Lot is still built (quantity is valid) with its price "
      "left NaN, not a false Rs.0",
      f"quantity={lots_g[0].quantity} price_fc={lots_g[0].price_fc!r}")
check(any(f.reason == INVALID_NUMERIC_FIELD for f in reg_g.blockers),
      "the existing 1412859 price-NaN diagnostic fires automatically once "
      "the retrofitted ingestion value reaches build_lots()",
      str([f.reason for f in reg_g.blockers]))


# ======================================================================
section("H. VALID holdings_as_acquisition FALLBACK (cost_total malformed, "
        "cost_per_share VALID) - fallback unchanged, real cost computed")

shares_h = 50.0
cost_total_h = to_number("garbled$$", on_invalid="nan")
if not cost_total_h or cost_total_h != cost_total_h:
    cps_h = to_number("45.00", on_invalid="nan")
    cost_total_h = cps_h * shares_h
check(cost_total_h == 45.0 * 50.0,
      "with cost_per_share genuinely valid, the fallback still computes "
      "the real cost_total exactly as it always did",
      f"cost_total_h={cost_total_h!r}")

events_h = events_frame([
    {"date": dt.date(2024, 1, 1), "broker": "X", "account_no": "1",
     "symbol": "AAPL", "event": "POSITION", "quantity": shares_h,
     "price_fc": round(cost_total_h / shares_h, 6), "amount_fc": "",
     "tax_fc": "", "acquired_on": "", "cost_fc": "", "currency": "USD",
     "notes": ""},
])
reg_h = Register()
lots_h, matches_h = build_lots(events_h, reg_h, as_at=dt.date(2024, 12, 31))
check(lots_h[0].price_fc == 45.0,
      "the Lot's price is the real, fallback-derived Rs.45/share, not NaN "
      "and not a fabricated zero",
      f"price_fc={lots_h[0].price_fc!r}")
check(not reg_h.blockers, "no blocker at all - this is clean, valid data",
      str(reg_h.blockers))


# ======================================================================
section("I. MALFORMED _extract_closed_lot_table COST/PROCEEDS - the "
        "report's own reconciliation check is NOT silently defeated")

# Mirrors the retrofitted `broke` computation exactly: NaN in proceeds,
# cost, or gain is now explicitly treated as a break, since NaN
# comparisons are always False and would otherwise let a malformed row
# silently pass this self-consistency check.
tol = 0.01
proceeds_i = to_number("garbled$$", on_invalid="nan")
cost_i = 2100.0
gain_i = proceeds_i - cost_i   # no separate gain_fc column configured
broke_i = ((proceeds_i != proceeds_i) or (cost_i != cost_i)
           or (gain_i != gain_i) or abs((proceeds_i - cost_i) - gain_i) > tol)
check(broke_i, "a malformed proceeds figure is caught as a reconciliation "
      "break, not silently passed (the pre-fix `abs(NaN) > tol` would have "
      "been False - a silent pass - without this explicit NaN check)",
      f"broke_i={broke_i!r}")

events_i = events_frame([
    {"date": dt.date(2024, 6, 1), "broker": "X", "account_no": "1",
     "symbol": "AAPL", "event": "SELL", "quantity": 30.0,
     "price_fc": round(proceeds_i / 30.0, 6), "amount_fc": round(proceeds_i, 2),
     "tax_fc": "", "acquired_on": "2024-01-01", "cost_fc": round(cost_i, 2),
     "currency": "USD", "notes": ""},
])
reg_i = Register()
lots_i, matches_i = build_lots(events_i, reg_i, as_at=dt.date(2024, 12, 31))
fx_i = controlled_fx(reg_i)
cg_i = build_cg(matches_i, fx_i, OPTS, PERIOD, reg_i)
row_i = cg_i.iloc[0]
check(row_i["Full Value of Consideration (Rs.)"] == ""
      and row_i["Capital Gain (Rs.)"] == "",
      "downstream, the malformed proceeds also blanks Consideration and "
      "Capital Gain via the sale_ok gate - blocked/unresolved, not a "
      "computed figure",
      f"consideration={row_i['Full Value of Consideration (Rs.)']!r} "
      f"gain={row_i['Capital Gain (Rs.)']!r}")


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
