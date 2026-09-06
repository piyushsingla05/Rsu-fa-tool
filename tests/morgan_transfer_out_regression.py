"""Regression tests for the Morgan Stanley TRANSFER_OUT fix.

Prior defect: config/mappings/morgan_stanley_shareplan.yaml maps "Transfer
Out" -> TRANSFER_OUT, but src/ingest/extract.py's _stage_to_events() had no
branch for that event name, so every Morgan Stanley transfer-out silently
vanished - the row was never emitted, the running quantity was never
decremented, and no register flag was raised. Source-account FA-A2/A3
quantities and the reconciliation walk were overstated with no diagnostic.

This is entirely self-contained (synthetic staged rows and synthetic events
frames only) - no private client data, no real broker documents, no network,
no change to the acceptance baseline or harness.

Run: python3 -m tests.morgan_transfer_out_regression
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingest.extract import _stage_to_events               # noqa: E402
from src.compute import build_lots, build_reconciliation      # noqa: E402
from src.models import EVENTS_COLUMNS, Period                 # noqa: E402
from src.review import Register                                # noqa: E402

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
    """A minimal, well-formed events DataFrame from plain dicts."""
    df = pd.DataFrame(rows)
    for c in EVENTS_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return df[EVENTS_COLUMNS]


RULES = {"plan_type": "RSU"}

# ======================================================================
section("1. MORGAN TRANSFER_OUT STAGING - the row is no longer dropped")

staged = [
    {"date": dt.date(2024, 3, 1), "event": "VEST", "qty": 100.0,
     "price": 50.0, "book_value": 5000.0, "source": "p1"},
    {"date": dt.date(2024, 6, 1), "event": "TRANSFER_OUT", "qty": 40.0,
     "price": 0.0, "book_value": 0.0, "source": "p2"},
]
out = _stage_to_events(staged, "AAPL", "MORGAN_STANLEY", "ACC1", False, RULES)

check(len(out) == 2, "both staged rows produce an event (none silently dropped)",
      f"{len(out)} event(s)")
xfer = next((r for r in out if r["event"] == "TRANSFER_OUT"), None)
check(xfer is not None, "the TRANSFER_OUT row is present in the output",
      str([r["event"] for r in out]))
check(xfer is not None and xfer["quantity"] == 40.0,
      "its quantity is preserved exactly", str(xfer["quantity"] if xfer else None))
check(xfer is not None and xfer["symbol"] == "AAPL" and xfer["broker"] == "MORGAN_STANLEY"
      and xfer["account_no"] == "ACC1" and xfer["date"] == dt.date(2024, 6, 1),
      "symbol/broker/account/date are carried through correctly")

# ======================================================================
section("2. RUNNING QUANTITY DECREASES - a later SPLIT sees the reduced holding")

staged2 = [
    {"date": dt.date(2024, 1, 1), "event": "VEST", "qty": 100.0,
     "price": 50.0, "book_value": 5000.0, "source": "p1"},
    {"date": dt.date(2024, 2, 1), "event": "TRANSFER_OUT", "qty": 30.0,
     "price": 0.0, "book_value": 0.0, "source": "p2"},
    {"date": dt.date(2024, 3, 1), "event": "SPLIT", "qty": 70.0,
     "price": 0.0, "book_value": 0.0, "source": "p3"},
]
out2 = _stage_to_events(staged2, "AAPL", "MORGAN_STANLEY", "ACC1", True, RULES)
split_row = next(r for r in out2 if r["event"] == "SPLIT")
# running after VEST(100) - TRANSFER_OUT(30) = 70; the split adds 70 more
# shares, so the ratio the additive-split logic derives is (70+70)/70 = 2.0.
# Before the fix, TRANSFER_OUT never decremented `running`, so the same
# statement would have produced (100+70)/100 = 1.7 instead - proving the
# running total genuinely reflects the transfer-out, not just that the row
# exists.
check(abs(split_row["quantity"] - 2.0) < 1e-9,
      "the split ratio reflects running quantity net of the transfer-out",
      f"ratio={split_row['quantity']} (want 2.0, pre-fix bug would give 1.7)")

# ======================================================================
section("3. TRANSFER-OUT RETAINED AS RECONCILIATION EVIDENCE")

events = events_frame([
    {"date": dt.date(2024, 1, 1), "broker": "MORGAN_STANLEY", "account_no": "ACC1",
     "symbol": "AAPL", "event": "VEST", "quantity": 100.0, "price_fc": 50.0,
     "amount_fc": "", "tax_fc": "", "acquired_on": "", "cost_fc": "",
     "currency": "USD", "notes": ""},
    {"date": dt.date(2024, 6, 1), "broker": "MORGAN_STANLEY", "account_no": "ACC1",
     "symbol": "AAPL", "event": "TRANSFER_OUT", "quantity": 40.0, "price_fc": 0.0,
     "amount_fc": "", "tax_fc": "", "acquired_on": "", "cost_fc": "",
     "currency": "USD", "notes": ""},
])
register = Register()
lots, matches = build_lots(events, register, as_at=dt.date(2024, 12, 31))
remaining = sum(l.remaining for l in lots if l.symbol == "AAPL")
check(abs(remaining - 60.0) < 1e-9,
      "build_lots() reduces the open position by the transfer-out quantity",
      f"remaining={remaining} (want 60.0 = 100 vested - 40 transferred out)")
check(not matches, "no SaleMatch/gain is created by the transfer-out",
      f"{len(matches)} match(es) (want 0)")
check(not register.blockers,
      "no spurious blocker is raised for a well-formed transfer-out")

period = Period(dt.date(2024, 1, 1), dt.date(2024, 12, 31))
recon = build_reconciliation(events, lots, period, register)
row = recon[recon["Symbol"] == "AAPL"].iloc[0]
check(abs(row["Transferred Out"] - 40.0) < 1e-9,
      "build_reconciliation() reports the transfer-out in its own column",
      f"Transferred Out={row['Transferred Out']} (want 40.0)")
check(row["Status"] == "Reconciled",
      "the reconciliation identity foots exactly, transfer-out included",
      f"status={row['Status']!r}, diff={row['Difference']}")

# ======================================================================
section("4. SOURCE TRANSFER-OUT + DESTINATION TRANSFER-IN - no double count")

cross_events = events_frame([
    # Source broker: 100 vested, 40 transferred out.
    {"date": dt.date(2024, 1, 1), "broker": "MORGAN_STANLEY", "account_no": "ACC1",
     "symbol": "AAPL", "event": "VEST", "quantity": 100.0, "price_fc": 50.0,
     "amount_fc": "", "tax_fc": "", "acquired_on": "", "cost_fc": "",
     "currency": "USD", "notes": ""},
    {"date": dt.date(2024, 6, 1), "broker": "MORGAN_STANLEY", "account_no": "ACC1",
     "symbol": "AAPL", "event": "TRANSFER_OUT", "quantity": 40.0, "price_fc": 0.0,
     "amount_fc": "", "tax_fc": "", "acquired_on": "", "cost_fc": "",
     "currency": "USD", "notes": ""},
    # Destination broker: the same 40 shares arrive as an independent
    # TRANSFER_IN, with its own (here, present) cost basis.
    {"date": dt.date(2024, 6, 2), "broker": "SCHWAB", "account_no": "ACC2",
     "symbol": "AAPL", "event": "TRANSFER_IN", "quantity": 40.0, "price_fc": 55.0,
     "amount_fc": "", "tax_fc": "", "acquired_on": "", "cost_fc": "",
     "currency": "USD", "notes": ""},
])
register2 = Register()
lots2, matches2 = build_lots(cross_events, register2, as_at=dt.date(2024, 12, 31))
total_shares = sum(l.remaining for l in lots2 if l.symbol == "AAPL")
by_broker = {}
for l in lots2:
    if l.symbol == "AAPL":
        by_broker[l.broker] = by_broker.get(l.broker, 0.0) + l.remaining
check(abs(total_shares - 100.0) < 1e-9,
      "total AAPL shares across both brokers equal the original vest, "
      "not 140 (which double counting would produce)",
      f"total={total_shares} (want 100.0)")
check(abs(by_broker.get("MORGAN_STANLEY", 0.0) - 60.0) < 1e-9,
      "the source broker correctly holds 60 (100 - 40 transferred out)",
      f"MORGAN_STANLEY={by_broker.get('MORGAN_STANLEY')}")
check(abs(by_broker.get("SCHWAB", 0.0) - 40.0) < 1e-9,
      "the destination broker correctly holds the 40 transferred-in shares",
      f"SCHWAB={by_broker.get('SCHWAB')}")
check(not matches2, "no gain is created on either side of the transfer",
      f"{len(matches2)} match(es) (want 0)")

# ======================================================================
section("5. EXISTING MORGAN BEHAVIOUR IS UNCHANGED FOR EVERY OTHER EVENT TYPE")

staged3 = [
    {"date": dt.date(2024, 1, 1), "event": "VEST", "qty": 100.0,
     "price": 50.0, "book_value": 5000.0, "source": "p1"},
    {"date": dt.date(2024, 2, 1), "event": "BUY", "qty": 10.0,
     "price": 52.0, "book_value": 520.0, "source": "p2"},
    {"date": dt.date(2024, 3, 1), "event": "TRANSFER_IN", "qty": 5.0,
     "price": 51.0, "book_value": 255.0, "source": "p3"},
    {"date": dt.date(2024, 4, 1), "event": "SELL", "qty": 20.0,
     "price": 60.0, "book_value": 1000.0, "source": "p4"},
    {"date": dt.date(2024, 5, 1), "event": "DIV", "qty": 0.0,
     "price": 0.0, "book_value": 15.0, "source": "p5"},
]
out3 = _stage_to_events(staged3, "AAPL", "MORGAN_STANLEY", "ACC1", False, RULES)
kinds = [r["event"] for r in out3]
check(kinds == ["VEST", "BUY", "TRANSFER_IN", "SELL", "DIV"],
      "every non-transfer-out event type still stages 1:1, in order, unchanged",
      str(kinds))
sell_row = next(r for r in out3 if r["event"] == "SELL")
check(sell_row["cost_fc"] == 1000.0 and sell_row["amount_fc"] == 1200.0,
      "SELL staging arithmetic (cost/proceeds) is untouched by this fix",
      f"cost_fc={sell_row['cost_fc']} amount_fc={sell_row['amount_fc']}")
vest_row = next(r for r in out3 if r["event"] == "VEST")
check(vest_row["price_fc"] == 50.0,
      "VEST/BUY/TRANSFER_IN staging arithmetic is untouched by this fix",
      f"price_fc={vest_row['price_fc']}")

# A statement with NO transfer-out at all behaves exactly as before - this
# fix only adds a new branch, it does not touch the fallthrough for any
# other unmapped/unknown event name.
staged4 = [{"date": dt.date(2024, 1, 1), "event": "OTHER", "qty": 1.0,
            "price": 0.0, "book_value": 0.0, "source": "p1"}]
out4 = _stage_to_events(staged4, "AAPL", "MORGAN_STANLEY", "ACC1", False, RULES)
check(out4 == [], "an unmapped/unrecognised event name still produces nothing "
      "(unchanged fallthrough behaviour for names that are not TRANSFER_OUT)")

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
