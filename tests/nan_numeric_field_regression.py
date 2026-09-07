"""Regression tests for the NaN/unparsed price & quantity diagnostic fix.

Prior defect (investigated read-only, then confirmed): an unparseable
price_fc or quantity in events.csv is coerced to a real NaN by
pd.to_numeric(errors="coerce") in src/run.py's _read(). NaN is truthy in
Python, so the existing `float(r["price_fc"] or 0)` / `float(r["quantity"]
or 0)` idioms in build_lots() let it flow straight into a Lot with no
diagnostic raised anywhere:

  - A NaN price left "_init_ok"/"Computable" reporting True/Yes next to a
    blank (NaN, blanked only by pd.isna() at render time) Initial Value /
    Cost of Acquisition / Capital Gain.
  - A NaN quantity made `l.remaining > 1e-9` evaluate False (NaN comparisons
    are always False), so the lot silently vanished from build_a3()'s `held`
    filter - not blank, simply absent, with no diagnostic from build_a3()
    itself (build_reconciliation() happened to also break, but with an
    uninformative "nan vs nan" message that didn't name the cause).

Fix (src/compute.py, src/review.py): explicit NaN checks (never truthiness)
at the point a Lot is built from an ACQUIRING_EVENTS row, plus at the two
downstream consumers of a possibly-NaN price (build_a3()'s _init_ok,
build_cg()'s Computable/cost gating). A NaN quantity is refused - no Lot is
built - and flagged as INVALID_NUMERIC_FIELD. A NaN price still builds the
Lot (its quantity is valid and must still count toward the holding/
reconciliation) but is flagged, and every value gated on that price is
corrected to reflect it rather than reporting a false "ok". No FX, tax
formula, FA-A2/A3 formula, baseline, harness, UI/API, or broker-config file
is touched, and a legitimate price=0 / quantity=0 is left completely alone.

Entirely self-contained (synthetic events + a controlled FXTable/MarketData)
- no private data, no network, no change to the acceptance baseline.

Run: python3 -m tests.nan_numeric_field_regression
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.compute import build_a3, build_cg, build_lots, ComputeOptions  # noqa: E402
from src.fx import FXTable                                              # noqa: E402
from src.marketdata import MarketData                                   # noqa: E402
from src.models import EVENTS_COLUMNS, Period                           # noqa: E402
from src.review import INVALID_NUMERIC_FIELD, Register                  # noqa: E402

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


def ev(date, broker, account, symbol, event, quantity, price_fc=0.0, **extra):
    row = {"date": date, "broker": broker, "account_no": account, "symbol": symbol,
           "event": event, "quantity": quantity, "price_fc": price_fc,
           "amount_fc": "", "tax_fc": "", "acquired_on": "", "cost_fc": "",
           "currency": "USD", "notes": ""}
    row.update(extra)
    return row


def coerced(rows):
    """Reproduce _read()'s CSV-events path: pd.to_numeric(errors="coerce")."""
    raw = pd.DataFrame(rows)
    raw["price_fc"] = pd.to_numeric(raw["price_fc"], errors="coerce")
    raw["quantity"] = pd.to_numeric(raw["quantity"], errors="coerce")
    return events_frame(raw.to_dict("records"))


def controlled_fx(register):
    """FXTable seeded with exact-date rates so FX resolution never confounds
    a test whose target variable is the NaN price/quantity handling."""
    fx = FXTable(ROOT / "config" / "fx_rates.csv", register=register)
    manual = pd.DataFrame([
        {"date": "2024-01-01", "currency": "USD", "ttbr": 83.0, "verified": "Y"},
        {"date": "2024-02-01", "currency": "USD", "ttbr": 83.5, "verified": "Y"},
        {"date": "2024-06-01", "currency": "USD", "ttbr": 83.8, "verified": "Y"},
        {"date": "2024-12-31", "currency": "USD", "ttbr": 84.0, "verified": "Y"},
    ])
    fx.merge(manual, priority=True)
    return fx


def controlled_market(tmp_name):
    mkt = pd.DataFrame([
        {"ticker": "AAPL", "security_name": "Apple Inc", "exchange": "NASDAQ",
         "price_kind": "ANNUAL_HIGH", "price": 200.0, "price_date": "2024-12-31",
         "currency": "USD", "basis": "DAILY_CLOSE_HIGH", "source_type": "PRIMARY",
         "source_name": "test", "retrieved_on": "2024-12-31",
         "confidence": "HIGH", "verified": "Y", "notes": ""},
        {"ticker": "AAPL", "security_name": "Apple Inc", "exchange": "NASDAQ",
         "price_kind": "PERIOD_END_CLOSE", "price": 195.0, "price_date": "2024-12-31",
         "currency": "USD", "basis": "DAILY_CLOSE_HIGH", "source_type": "PRIMARY",
         "source_name": "test", "retrieved_on": "2024-12-31",
         "confidence": "HIGH", "verified": "Y", "notes": ""},
    ])
    path = Path(f"/tmp/{tmp_name}")
    mkt.to_csv(path, index=False)
    return path


PERIOD = Period(dt.date(2024, 1, 1), dt.date(2024, 12, 31))


# ======================================================================
section("1. NaN ACQUISITION QUANTITY - explicit diagnostic, no silent A3 "
        "disappearance, reconciliation stays consistent with build_lots()")

events1 = coerced([
    ev(dt.date(2024, 1, 1), "X", "1", "AAPL", "VEST", "garbled_qty", "40.0"),
    ev(dt.date(2024, 2, 1), "X", "1", "AAPL", "VEST", "20", "55.0"),
])
reg1 = Register()
lots1, matches1 = build_lots(events1, reg1, as_at=dt.date(2024, 12, 31))

check(len(lots1) == 1 and lots1[0].acquired == dt.date(2024, 2, 1),
      "the NaN-quantity row builds NO Lot at all - not a zero-quantity Lot, "
      "not a guessed one",
      f"{len(lots1)} lot(s): {[(l.acquired, l.quantity) for l in lots1]}")

blockers1 = [f for f in reg1.blockers if f.reason == INVALID_NUMERIC_FIELD]
check(len(blockers1) == 1 and "2024" in blockers1[0].detail,
      "an explicit INVALID_NUMERIC_FIELD blocker is raised for the "
      "unparseable quantity",
      blockers1[0].detail if blockers1 else "no blocker raised")

fx1 = controlled_fx(reg1)
md1 = MarketData(controlled_market("diag_market_1.csv"), reg1)
opts = ComputeOptions()
entities = pd.DataFrame(columns=["symbol"])
a3_1 = build_a3(events1, lots1, entities, md1, fx1, PERIOD, opts, reg1)
check(len(a3_1) == 1 and a3_1.iloc[0]["_qty"] == 20.0,
      "FA-A3 shows exactly the one genuinely-known 20-share lot - the "
      "NaN-quantity row is accounted for by the earlier blocker, not by "
      "silently vanishing with zero explanation",
      f"{len(a3_1)} row(s), qty={list(a3_1['_qty']) if len(a3_1) else []}")

from src.compute import build_reconciliation  # noqa: E402
recon1 = build_reconciliation(events1, lots1, PERIOD, reg1)
row1 = recon1[recon1["Symbol"] == "AAPL"].iloc[0]
# Prior to the malformed-quantity ingestion hardening audit,
# build_reconciliation()'s own quantity walk had no NaN guard of its own -
# `r["quantity"] or 0` does not catch NaN (NaN is truthy in Python), so the
# excluded row's NaN silently propagated into `computed`, forcing a
# coincidental BREAK. That walk now explicitly excludes the same
# already-flagged NaN row build_lots() excluded (via the new _qty_or_zero()
# helper - see src/compute.py), so the two independent totals agree with
# each other again and the walk correctly stays reconciled for the shares
# that ARE known. The malformed row is never silently lost either way - it
# is the INVALID_NUMERIC_FIELD blocker just above, not a coincidental
# reconciliation break, that is the actionable signal for it.
check(row1["Status"] == "Reconciled",
      "reconciliation is consistent with build_lots()'s own exclusion of "
      "the NaN-quantity row, rather than coincidentally breaking on "
      "unguarded NaN arithmetic",
      f"status={row1['Status']!r}")


# ======================================================================
section("2. NaN ACQUISITION PRICE - explicit diagnostic, Initial Value "
        "unresolved/blank, _init_ok=False")

events2 = coerced([
    ev(dt.date(2024, 1, 1), "X", "1", "AAPL", "VEST", "50", "garbled$$"),
    ev(dt.date(2024, 2, 1), "X", "1", "AAPL", "VEST", "20", "55.0"),
])
reg2 = Register()
lots2, matches2 = build_lots(events2, reg2, as_at=dt.date(2024, 12, 31))

check(len(lots2) == 2,
      "unlike NaN quantity, the NaN-price row STILL builds a Lot - its "
      "quantity is valid and must still count toward the holding",
      f"{len(lots2)} lot(s)")
price_lot = next(l for l in lots2 if l.acquired == dt.date(2024, 1, 1))
check(price_lot.quantity == 50.0 and price_lot.price_fc != price_lot.price_fc,
      "that Lot's quantity is exactly 50 and its price is NaN (not silently "
      "zeroed)",
      f"quantity={price_lot.quantity} price_fc={price_lot.price_fc}")

blockers2 = [f for f in reg2.blockers if f.reason == INVALID_NUMERIC_FIELD]
check(len(blockers2) == 1 and "AAPL" in blockers2[0].subject,
      "an explicit INVALID_NUMERIC_FIELD blocker is raised for the "
      "unparseable price",
      blockers2[0].detail if blockers2 else "no blocker raised")

fx2 = controlled_fx(reg2)
md2 = MarketData(controlled_market("diag_market_2.csv"), reg2)
a3_2 = build_a3(events2, lots2, entities, md2, fx2, PERIOD, opts, reg2)
row_jan = a3_2[a3_2["Date of Acquiring the Interest"] == dt.date(2024, 1, 1)].iloc[0]
row_feb = a3_2[a3_2["Date of Acquiring the Interest"] == dt.date(2024, 2, 1)].iloc[0]

check(row_jan["Initial Value of the Investment (Rs.)"] == "",
      "the NaN-priced lot's Initial Value is left BLANK ('', not a NaN "
      "float, not a guessed number)",
      repr(row_jan["Initial Value of the Investment (Rs.)"]))
check(not row_jan["_init_ok"],
      "_init_ok is now correctly False for that lot - it no longer lies "
      "about a value that can't actually be computed",
      f"_init_ok={row_jan['_init_ok']!r}")
check(isinstance(row_feb["Initial Value of the Investment (Rs.)"], float)
      and row_feb["Initial Value of the Investment (Rs.)"] > 0
      and bool(row_feb["_init_ok"]),
      "the OTHER (valid-price) lot is completely unaffected - per-lot "
      "granularity is the default, so one bad lot does not poison another",
      f"Initial Value={row_feb['Initial Value of the Investment (Rs.)']} "
      f"_init_ok={row_feb['_init_ok']!r}")


# ======================================================================
section("3. NaN COST BASIS ON A LATER SALE - Computable != Yes, no numeric "
        "Cost/Gain produced, explicit diagnostic")

events3 = coerced([
    ev(dt.date(2024, 1, 1), "X", "1", "AAPL", "VEST", "50", "garbled$$"),
    ev(dt.date(2024, 6, 1), "X", "1", "AAPL", "SELL", "30", "70.0"),
])
reg3 = Register()
lots3, matches3 = build_lots(events3, reg3, as_at=dt.date(2024, 12, 31))
fx3 = controlled_fx(reg3)
cg3 = build_cg(matches3, fx3, opts, PERIOD, reg3)

check(len(cg3) == 1, "exactly one capital-gain row for the sale", f"{len(cg3)} row(s)")
row_cg = cg3.iloc[0]
check(row_cg["Computable"] == "No - invalid cost basis",
      "Computable explicitly names the invalid cost basis rather than "
      "saying 'Yes', and rather than the generic 'No - FX unavailable' "
      "(FX resolved fine here - only the cost failed to parse)",
      f"Computable={row_cg['Computable']!r}")
check(row_cg["Cost of Acquisition (Rs.)"] == "" and row_cg["Capital Gain (Rs.)"] == "",
      "Cost of Acquisition and Capital Gain are both left blank, never a "
      "guessed rupee figure",
      f"cost={row_cg['Cost of Acquisition (Rs.)']!r} "
      f"gain={row_cg['Capital Gain (Rs.)']!r}")

blockers3 = [f for f in reg3.blockers if f.reason == INVALID_NUMERIC_FIELD]
check(len(blockers3) == 2,
      "two INVALID_NUMERIC_FIELD blockers exist: one from the lot's own "
      "build (root cause), one from the sale that matched it (point of "
      "consequence) - both are legitimate per-defect diagnostics, not "
      "duplicates of unrelated things",
      f"{len(blockers3)} blocker(s): "
      f"{[b.subject for b in blockers3]}")


# ======================================================================
section("4. VALID NUMERIC ZERO - zero is never treated as NaN/missing")

events4 = coerced([
    ev(dt.date(2024, 1, 1), "X", "1", "AAPL", "POSITION", "50", "0.0",
       notes="opening position, cost basis not on file"),
    ev(dt.date(2024, 2, 1), "X", "1", "AAPL", "VEST", "0", "60.0",
       notes="zero-share vest line, e.g. a placeholder/cancelled row"),
])
reg4 = Register()
lots4, matches4 = build_lots(events4, reg4, as_at=dt.date(2024, 12, 31))

check(not [f for f in reg4.blockers if f.reason == INVALID_NUMERIC_FIELD],
      "neither a price=0 nor a quantity=0 row raises INVALID_NUMERIC_FIELD",
      str(reg4.blockers))
zero_price_lot = next((l for l in lots4 if l.acquired == dt.date(2024, 1, 1)), None)
check(zero_price_lot is not None and zero_price_lot.price_fc == 0.0
      and zero_price_lot.quantity == 50.0,
      "the price=0 lot is built normally, with its real 50-share quantity",
      f"{zero_price_lot}")
zero_qty_lot = next((l for l in lots4 if l.acquired == dt.date(2024, 2, 1)), None)
check(zero_qty_lot is not None and zero_qty_lot.quantity == 0.0,
      "the quantity=0 lot is ALSO built (not excluded like a NaN quantity "
      "is) - zero is a legitimate value, not a missing one",
      f"{zero_qty_lot}")

fx4 = controlled_fx(reg4)
md4 = MarketData(controlled_market("diag_market_4.csv"), reg4)
a3_4 = build_a3(events4, lots4, entities, md4, fx4, PERIOD, opts, reg4)
row4 = a3_4[a3_4["Date of Acquiring the Interest"] == dt.date(2024, 1, 1)].iloc[0]
check(bool(row4["_init_ok"]) and row4["Initial Value of the Investment (Rs.)"] == 0,
      "the price=0 lot's Initial Value computes to a real, numeric 0 - "
      "_init_ok stays True, this is not confused with the NaN case",
      f"_init_ok={row4['_init_ok']!r} "
      f"Initial Value={row4['Initial Value of the Investment (Rs.)']!r}")


# ======================================================================
section("5. NORMAL VALID ACQUISITION - existing output completely unchanged")

events5 = coerced([
    ev(dt.date(2024, 1, 1), "X", "1", "AAPL", "VEST", "50", "40.0"),
    ev(dt.date(2024, 6, 1), "X", "1", "AAPL", "SELL", "20", "70.0"),
])
reg5 = Register()
lots5, matches5 = build_lots(events5, reg5, as_at=dt.date(2024, 12, 31))
check(not [f for f in reg5.blockers if f.reason == INVALID_NUMERIC_FIELD],
      "no INVALID_NUMERIC_FIELD blocker for entirely clean, valid data",
      str(reg5.blockers))
fx5 = controlled_fx(reg5)
md5 = MarketData(controlled_market("diag_market_5.csv"), reg5)
a3_5 = build_a3(events5, lots5, entities, md5, fx5, PERIOD, opts, reg5)
row5 = a3_5.iloc[0]
check(bool(row5["_init_ok"])
      and row5["Initial Value of the Investment (Rs.)"] == round(30 * 40.0 * 83.0, 0),
      "the surviving 30-share lot's Initial Value is the same figure this "
      "fix's changes did not touch: qty x price x vest-date TTBR",
      f"Initial Value={row5['Initial Value of the Investment (Rs.)']!r}")
cg5 = build_cg(matches5, fx5, opts, PERIOD, reg5)
row_cg5 = cg5.iloc[0]
check(row_cg5["Computable"] == "Yes"
      and row_cg5["Capital Gain (Rs.)"] == round(
          20 * 70.0 * 83.8 - 20 * 40.0 * 83.0, 0),
      "the sale's Computable/Capital Gain are unchanged: the ordinary "
      "gain formula, untouched by this fix",
      f"Computable={row_cg5['Computable']!r} "
      f"Capital Gain={row_cg5['Capital Gain (Rs.)']!r}")


# ======================================================================
section("6. MIXED A3 ENTITY-GRANULARITY - a NaN lot cannot silently "
        "contaminate or disappear from a pooled valuation")

events6 = coerced([
    ev(dt.date(2024, 1, 1), "X", "1", "AAPL", "VEST", "50", "garbled$$"),
    ev(dt.date(2024, 2, 1), "X", "1", "AAPL", "VEST", "20", "55.0"),
])
reg6 = Register()
lots6, matches6 = build_lots(events6, reg6, as_at=dt.date(2024, 12, 31))
fx6 = controlled_fx(reg6)
md6 = MarketData(controlled_market("diag_market_6.csv"), reg6)
opts_entity = ComputeOptions(a3_granularity="entity")
a3_6 = build_a3(events6, lots6, entities, md6, fx6, PERIOD, opts_entity, reg6)

check(len(a3_6) == 1, "entity mode pools both lots into a single row",
      f"{len(a3_6)} row(s)")
row6 = a3_6.iloc[0]
check(row6["_qty"] == 70.0,
      "the pooled row's quantity STILL includes both lots' shares (50 + "
      "20) - the NaN-priced lot does not silently disappear from the "
      "pooled quantity",
      f"_qty={row6['_qty']}")
check(not row6["_init_ok"]
      and row6["Initial Value of the Investment (Rs.)"] == "",
      "the pooled Initial Value is blank, not a weighted average silently "
      "computed from only the valid lot (which would misreport the true "
      "blended cost) and not a wrong/plausible number either",
      f"_init_ok={row6['_init_ok']!r} "
      f"Initial Value={row6['Initial Value of the Investment (Rs.)']!r}")


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
