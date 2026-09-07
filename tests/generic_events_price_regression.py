"""Regression tests for the generic-broker acquisition-price zero-coercion fix.

Prior defect: src/ingest/extract.py::_extract_events() - the fallback used for
an unrecognised broker (no profile) and for a profile-driven table whose
layout is neither pdf_sections nor closed_lot_gains - called
G.to_number(r.get(c.get("price_fc"))) with NO on_invalid="nan", and its
price -> cost_total_fc fallback guard was plain `if not price and ...`.

A price/cost cell that was genuinely PRESENT but failed to parse (garbled
text, a stray footnote) therefore became a silent 0.0 that reached
build_lots() as a real, trusted VEST/BUY price - never NaN, so the existing
1412859 NaN-price diagnostic (compute.py, `_price != _price`) never saw it
and never fired. A later sale against that lot would report the FULL sale
proceeds as capital gain against a fabricated Rs.0 acquisition cost, with no
blocker anywhere in the chain.

Fix: G.to_number(..., on_invalid="nan") for both price_fc and cost_total_fc,
with the fallback guard rewritten as `(not price or not price_ok)` (price_ok
= price == price) so a malformed (NaN) price - not just a blank/zero one -
still falls through to the cost_total_fc fallback exactly as before. The
DIVIDEND_REINVESTMENT guard is also rewritten (`and price and price == price`)
since NaN is truthy in Python and would otherwise fabricate a DIV row with an
unresolved amount_fc.

Entirely self-contained (synthetic tables via the Table/Document dataclasses
that _extract_events() itself consumes) - no private data, no network, no
change to any broker profile, the acceptance baseline, or the five prior
commits.

Run: python3 -m tests.generic_events_price_regression
"""
from __future__ import annotations

import datetime as dt
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingest.extract import _extract_events                        # noqa: E402
from src.ingest.tabular import Table, Document                        # noqa: E402
from src.compute import build_lots, build_cg, ComputeOptions          # noqa: E402
from src.fx import FXTable                                            # noqa: E402
from src.models import EVENTS_COLUMNS, Period                         # noqa: E402
from src.review import Register, INVALID_NUMERIC_FIELD                # noqa: E402

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
    try:
        return isinstance(x, float) and math.isnan(x)
    except Exception:
        return False


class _Rec:
    """Minimal stand-in for the rec object _extract_events() writes into."""
    def __init__(self):
        self.invalid_quantities = []


def make_doc(rows, price_col="Price", cost_col=None):
    """A single-table Document with the generic synonym headers _extract_events()
    relies on map_columns() to find."""
    headers = {"Date Acquired": [], "Quantity": [], price_col: []}
    if cost_col:
        headers[cost_col] = []
    for r in rows:
        headers["Date Acquired"].append(r.get("date", "01/01/2024"))
        headers["Quantity"].append(r.get("qty", ""))
        headers[price_col].append(r.get("price", ""))
        if cost_col:
            headers[cost_col].append(r.get("cost_total", ""))
    df = pd.DataFrame(headers)
    t = Table(df=df, header_row=0, sheet="Sheet1", source_file="synthetic.xlsx",
             raw=None)
    return Document(path=Path("synthetic.xlsx"), kind="excel", text="",
                    tables=[t])


def run_extract(rows, cost_col="Total Cost Basis"):
    doc = make_doc(rows, cost_col=cost_col)
    rec = _Rec()
    events, unmapped = _extract_events(doc, None, "GENERIC_BROKER", rec)
    return events


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


# ======================================================================
section("0. INPUT-COMBINATION MATRIX (via _extract_events())")

cases = [
    ("valid price", {"date": "01/01/2024", "qty": "50", "price": "40.00"}),
    ("legitimate zero price, no cost_total mapped away", {"date": "01/01/2024", "qty": "50", "price": "0"}),
    ("blank price, valid cost_total", {"date": "01/01/2024", "qty": "50", "price": "", "cost_total": "2000.00"}),
    ("malformed price, valid cost_total", {"date": "01/01/2024", "qty": "50", "price": "garbled$$", "cost_total": "2000.00"}),
    ("blank price, blank cost_total", {"date": "01/01/2024", "qty": "50", "price": "", "cost_total": ""}),
    ("malformed price, malformed cost_total", {"date": "01/01/2024", "qty": "50", "price": "garbled$$", "cost_total": "N/A - see note"}),
    ("malformed cost_total alone (price blank)", {"date": "01/01/2024", "qty": "50", "price": "", "cost_total": "not a number"}),
    ("valid quantity", {"date": "01/01/2024", "qty": "50", "price": "40.00"}),
    ("malformed quantity", {"date": "01/01/2024", "qty": "garbled$$", "price": "40.00"}),
]

for label, row in cases:
    ev = run_extract([row])
    if label == "malformed quantity":
        check(len(ev) == 0, f"{label}: row excluded, not fabricated as a "
              "zero-quantity event (the visible diagnostic this now raises "
              "is covered by tests/malformed_quantity_regression.py)",
              f"{len(ev)} row(s)")
        continue
    check(len(ev) == 1, f"{label}: exactly one event row produced", f"{len(ev)} row(s)")
    if len(ev) != 1:
        continue
    price_fc = ev.iloc[0]["price_fc"]
    if label == "valid price":
        check(price_fc == 40.0, f"{label}: price_fc is the real value", f"{price_fc!r}")
    elif label.startswith("legitimate zero"):
        check(price_fc == 0.0, f"{label}: price_fc is a real, valid zero (not blocked, not NaN)",
              f"{price_fc!r}")
    elif label == "blank price, valid cost_total":
        check(price_fc == 40.0, f"{label}: fallback computes 2000/50 = 40.0", f"{price_fc!r}")
    elif label == "malformed price, valid cost_total":
        check(price_fc == 40.0, f"{label}: fallback STILL triggers for a malformed "
              "(not just blank) price - 2000/50 = 40.0", f"{price_fc!r}")
    elif label == "blank price, blank cost_total":
        check(price_fc == 0.0, f"{label}: both genuinely blank -> real 0.0, unchanged "
              "pre-existing convention", f"{price_fc!r}")
    elif label == "malformed price, malformed cost_total":
        check(is_nan(price_fc), f"{label}: BOTH malformed -> unresolved NaN, never a "
              "trusted zero", f"{price_fc!r}")
    elif label == "malformed cost_total alone (price blank)":
        check(is_nan(price_fc), f"{label}: blank price falls back to a malformed "
              "cost_total -> unresolved NaN, not a fabricated zero", f"{price_fc!r}")
    elif label == "valid quantity":
        check(ev.iloc[0]["quantity"] == 50.0, f"{label}: quantity preserved", "")

# ======================================================================
section("A. DANGEROUS REPRODUCTION - generic/unknown broker, malformed "
        "acquisition price, later sale: no false Rs.0 basis / full-proceeds gain")

# No cost_total_fc column mapped at all - the single-price-column case that
# is the exact shape of the original HIGH finding: a malformed price with no
# fallback source available anywhere in the table.
acquire_row = {"date": "01/01/2024", "qty": "100", "price": "garbled$$"}
doc_acq = make_doc([acquire_row], cost_col=None)
rec = _Rec()
acq_events, _ = _extract_events(doc_acq, None, "GENERIC_BROKER", rec)
check(len(acq_events) == 1, "generic acquisition row extracted", f"{len(acq_events)} row(s)")
acq_price = acq_events.iloc[0]["price_fc"]
check(is_nan(acq_price), "acquisition price_fc is unresolved NaN, not a trusted zero",
      f"price_fc={acq_price!r}")

sale_row = pd.DataFrame([{
    "date": dt.date(2024, 6, 1), "broker": "GENERIC_BROKER", "account_no": "",
    "symbol": acq_events.iloc[0]["symbol"], "event": "SELL", "quantity": 100.0,
    "price_fc": 60.0, "amount_fc": "", "tax_fc": "", "acquired_on": "",
    "cost_fc": "", "currency": "USD", "notes": "",
}])
all_events = pd.concat([acq_events.assign(broker="GENERIC_BROKER"), sale_row],
                       ignore_index=True)
all_events = events_frame(all_events.to_dict("records"))

reg = Register()
lots, matches = build_lots(all_events, reg, as_at=dt.date(2024, 12, 31))
lot_price = next((l.price_fc for l in lots if l.event in ("VEST", "BUY")), None)
check(is_nan(lot_price), "the resulting Lot's price_fc is NaN, not a placeholder "
      "zero, once it reaches build_lots()", f"lot price_fc={lot_price!r}")

price_blockers = [f for f in reg.blockers if f.reason == INVALID_NUMERIC_FIELD]
check(len(price_blockers) >= 1, "the existing 1412859 NaN-price diagnostic fires "
      "automatically - no new parallel mechanism was invented",
      f"{len(price_blockers)} blocker(s)")

fx = controlled_fx(reg)
opts = ComputeOptions()
period = Period(dt.date(2024, 1, 1), dt.date(2024, 12, 31))
cg = build_cg(matches, fx, opts, period, reg)
row = cg.iloc[0]
check(row["Cost of Acquisition (Rs.)"] == "", "Cost of Acquisition is BLANK, not a "
      "false Rs.0", f"Cost={row['Cost of Acquisition (Rs.)']!r}")
check(row["Capital Gain (Rs.)"] == "", "Capital Gain is BLANK - no false "
      "full-proceeds gain fabricated from the malformed acquisition price",
      f"Gain={row['Capital Gain (Rs.)']!r}")
check(row["Computable"] == "No - invalid cost basis", "Computable correctly names "
      "the invalid cost basis", f"Computable={row['Computable']!r}")

# ======================================================================
section("B. LEGITIMATE ZERO ACQUISITION PRICE - remains a real, unblocked zero")

zero_row = {"date": "01/01/2024", "qty": "100", "price": "0", "cost_total": ""}
doc_zero = make_doc([zero_row], cost_col="Total Cost Basis")
rec2 = _Rec()
zero_events, _ = _extract_events(doc_zero, None, "GENERIC_BROKER", rec2)
zero_price = zero_events.iloc[0]["price_fc"]
check(zero_price == 0.0, "a genuinely evidenced zero price stays a real 0.0, not NaN",
      f"price_fc={zero_price!r}")

zero_events2 = events_frame(zero_events.assign(broker="GENERIC_BROKER").to_dict("records"))
reg_z = Register()
lots_z, _ = build_lots(zero_events2, reg_z, as_at=dt.date(2024, 12, 31))
z_blockers = [f for f in reg_z.blockers if f.reason == INVALID_NUMERIC_FIELD]
check(not z_blockers, "no INVALID_NUMERIC_FIELD blocker for a genuinely evidenced "
      "zero price - it is not incorrectly treated as malformed", f"{z_blockers}")
check(abs(lots_z[0].price_fc - 0.0) < 1e-9, "the Lot's price_fc is a real 0.0",
      f"{lots_z[0].price_fc!r}")

# ======================================================================
section("C. DIVIDEND_REINVESTMENT GUARD - malformed price does not fabricate a "
        "phantom DIV row with an unresolved amount")

div_row = {"date": "01/01/2024", "qty": "10", "price": "garbled$$", "cost_total": ""}
doc_div = make_doc([div_row], price_col="Reinvest Price", cost_col="Total Cost Basis")
# Force DIVIDEND_REINVESTMENT classification via the descriptor text column.
doc_div.tables[0].df["Description"] = ["Dividend Reinvestment Purchase"]
rec3 = _Rec()
div_events, _ = _extract_events(doc_div, None, "GENERIC_BROKER", rec3)
div_rows = div_events[div_events["event"] == "DIV"]
check(len(div_rows) == 0, "a malformed (NaN) price does NOT fabricate a phantom "
      "DIV income row (price == price explicitly excludes NaN, which is truthy "
      "in Python and would otherwise pass the old `and price` guard)",
      f"{len(div_rows)} DIV row(s)")

div_row_valid = {"date": "01/01/2024", "qty": "10", "price": "40.00", "cost_total": ""}
doc_div2 = make_doc([div_row_valid], price_col="Reinvest Price", cost_col="Total Cost Basis")
doc_div2.tables[0].df["Description"] = ["Dividend Reinvestment Purchase"]
rec4 = _Rec()
div_events2, _ = _extract_events(doc_div2, None, "GENERIC_BROKER", rec4)
div_rows2 = div_events2[div_events2["event"] == "DIV"]
check(len(div_rows2) == 1 and abs(div_rows2.iloc[0]["amount_fc"] - 400.0) < 1e-9,
      "a genuinely valid price still creates the DIV row with the correct amount, "
      "unchanged", f"{len(div_rows2)} DIV row(s), "
      f"amount={div_rows2.iloc[0]['amount_fc'] if len(div_rows2) else None}")

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
