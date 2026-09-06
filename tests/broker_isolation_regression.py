"""Regression tests for the build_lots() broker-isolation fix.

Prior defect: open_lots in src/compute.py's build_lots() was keyed by symbol
alone. A disposal (SELL/TRANSFER_OUT/TAX_WITHHOLD) at one broker, if that
broker's own tracked lots ran short, would silently consume another broker's
same-symbol lots rather than raising the existing MISSING_VEST_DATE /
unmatched-disposal blocker - misattributing cost basis and holding period,
understating the other broker's own remaining position, and going
undetected by both build_reconciliation() (which only checks the combined
total) and build_cross_broker() (whose aggregate happened to still balance).

Fix: open_lots is now keyed by (broker, symbol). SPLIT remains symbol-wide
across every broker's lots for that symbol, since a corporate action affects
the security regardless of which broker holds it.

Entirely self-contained (synthetic events only) - no private data, no
network, no change to the acceptance baseline or harness.

Run: python3 -m tests.broker_isolation_regression
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.compute import build_lots, build_reconciliation             # noqa: E402
from src.models import EVENTS_COLUMNS, Period                        # noqa: E402
from src.review import MISSING_VEST_DATE, Register, Severity          # noqa: E402

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


# ======================================================================
section("A. CROSS-BROKER SHORTFALL - Broker A sells more than it owns, "
        "Broker B's lot must not be borrowed")

events_a = events_frame([
    ev(dt.date(2024, 1, 1), "BROKER_A", "A1", "AAPL", "VEST", 50.0, 40.0),
    ev(dt.date(2024, 2, 1), "BROKER_B", "B1", "AAPL", "VEST", 100.0, 45.0),
    ev(dt.date(2024, 6, 1), "BROKER_A", "A1", "AAPL", "SELL", 80.0, 60.0),
])
reg_a = Register()
lots_a, matches_a = build_lots(events_a, reg_a, as_at=dt.date(2024, 12, 31))
by_broker_a = {l.broker: l.remaining for l in lots_a}

check(abs(by_broker_a.get("BROKER_A", -1) - 0.0) < 1e-9,
      "Broker A consumes exactly its own 50 shares (remaining 0)",
      f"BROKER_A remaining={by_broker_a.get('BROKER_A')}")
check(abs(by_broker_a.get("BROKER_B", -1) - 100.0) < 1e-9,
      "Broker B's lot is completely untouched - still 100",
      f"BROKER_B remaining={by_broker_a.get('BROKER_B')}")

blockers_a = [f for f in reg_a.blockers if f.reason == MISSING_VEST_DATE]
check(len(blockers_a) == 1 and "30" in blockers_a[0].detail,
      "the unmatched 30 shares raise the existing MISSING_VEST_DATE blocker",
      blockers_a[0].detail if blockers_a else "no blocker raised")

cross_lot_matches = [m for m in matches_a if m.acquired_on == dt.date(2024, 2, 1)]
check(not cross_lot_matches,
      "no SaleMatch for Broker A's sale carries Broker B's acquisition date "
      "(2024-02-01) - Broker B's lot never appears in Broker A's matches",
      str([(m.acquired_on, m.quantity) for m in matches_a]))

unmatched = [m for m in matches_a if m.acquired_on is None]
check(len(unmatched) == 1 and abs(unmatched[0].quantity - 30.0) < 1e-9
      and unmatched[0].cost_price_fc == 0.0,
      "the unmatched 30 shares are recorded with no acquisition date and no "
      "invented cost - no gain/loss is silently calculated for them",
      f"{unmatched[0].quantity if unmatched else None} qty, "
      f"cost={unmatched[0].cost_price_fc if unmatched else None}")

matched_50 = [m for m in matches_a if m.acquired_on == dt.date(2024, 1, 1)]
check(len(matched_50) == 1 and abs(matched_50[0].quantity - 50.0) < 1e-9
      and matched_50[0].cost_price_fc == 40.0,
      "the genuinely-matched 50 shares still use Broker A's own cost/date "
      "correctly", f"{matched_50[0] if matched_50 else None}")

period = Period(dt.date(2024, 1, 1), dt.date(2024, 12, 31))
recon_a = build_reconciliation(events_a, lots_a, period, reg_a)
row_a = recon_a[recon_a["Symbol"] == "AAPL"].iloc[0]
check(row_a["Status"] == "BREAK" and abs(row_a["Closing per Lots"] - 100.0) < 1e-9,
      "reconciliation now correctly surfaces the break instead of falsely "
      "reconciling (closing-per-lots is the true 100, not the pre-fix 70)",
      f"status={row_a['Status']!r} closing_per_lots={row_a['Closing per Lots']}")

# ======================================================================
section("B. NORMAL SAME-BROKER DISPOSAL - unchanged behaviour, control case")

events_b = events_frame([
    ev(dt.date(2024, 1, 1), "BROKER_A", "A1", "AAPL", "VEST", 50.0, 40.0),
    ev(dt.date(2024, 2, 1), "BROKER_B", "B1", "AAPL", "VEST", 100.0, 45.0),
    ev(dt.date(2024, 6, 1), "BROKER_A", "A1", "AAPL", "SELL", 30.0, 60.0),
])
reg_b = Register()
lots_b, matches_b = build_lots(events_b, reg_b, as_at=dt.date(2024, 12, 31))
by_broker_b = {l.broker: l.remaining for l in lots_b}
check(abs(by_broker_b.get("BROKER_A", -1) - 20.0) < 1e-9,
      "Broker A correctly retains 20 (50 - 30)",
      f"BROKER_A remaining={by_broker_b.get('BROKER_A')}")
check(abs(by_broker_b.get("BROKER_B", -1) - 100.0) < 1e-9,
      "Broker B is untouched, exactly as before this fix",
      f"BROKER_B remaining={by_broker_b.get('BROKER_B')}")
check(len(matches_b) == 1 and matches_b[0].acquired_on == dt.date(2024, 1, 1)
      and matches_b[0].cost_price_fc == 40.0,
      "the sale matches only Broker A's own lot, correct cost/date",
      str(matches_b[0]) if matches_b else "no match")
check(not reg_b.blockers, "no blocker for a well-formed, sufficient disposal")

# ======================================================================
section("C. TRANSFER_OUT ISOLATION - same shortfall shape, disposing event "
        "is TRANSFER_OUT instead of SELL")

events_c = events_frame([
    ev(dt.date(2024, 1, 1), "BROKER_A", "A1", "AAPL", "VEST", 50.0, 40.0),
    ev(dt.date(2024, 2, 1), "BROKER_B", "B1", "AAPL", "VEST", 100.0, 45.0),
    ev(dt.date(2024, 6, 1), "BROKER_A", "A1", "AAPL", "TRANSFER_OUT", 80.0, 0.0),
])
reg_c = Register()
lots_c, matches_c = build_lots(events_c, reg_c, as_at=dt.date(2024, 12, 31))
by_broker_c = {l.broker: l.remaining for l in lots_c}
check(abs(by_broker_c.get("BROKER_A", -1) - 0.0) < 1e-9,
      "Broker A's TRANSFER_OUT consumes only its own 50 shares",
      f"BROKER_A remaining={by_broker_c.get('BROKER_A')}")
check(abs(by_broker_c.get("BROKER_B", -1) - 100.0) < 1e-9,
      "Broker B's lot is NOT consumed by Broker A's transfer-out",
      f"BROKER_B remaining={by_broker_c.get('BROKER_B')}")
blockers_c = [f for f in reg_c.blockers if f.reason == MISSING_VEST_DATE]
check(len(blockers_c) == 1 and "30" in blockers_c[0].detail,
      "the 30-share shortfall raises the existing unmatched-disposal blocker "
      "for TRANSFER_OUT exactly as it does for SELL",
      blockers_c[0].detail if blockers_c else "no blocker raised")
check(not matches_c,
      "TRANSFER_OUT never creates a SaleMatch/gain regardless of shortfall "
      "(PROCEEDS_EVENTS excludes TRANSFER_OUT - unaffected by this fix)",
      f"{len(matches_c)} match(es)")

# ======================================================================
section("D. SPLIT ACROSS BROKERS - remains symbol-wide, no broker skipped")

events_d = events_frame([
    ev(dt.date(2024, 1, 1), "BROKER_A", "A1", "AAPL", "VEST", 50.0, 40.0),
    ev(dt.date(2024, 2, 1), "BROKER_B", "B1", "AAPL", "VEST", 100.0, 45.0),
    ev(dt.date(2024, 3, 1), "BROKER_A", "A1", "AAPL", "SPLIT", 2.0, 0.0),
])
reg_d = Register()
lots_d, matches_d = build_lots(events_d, reg_d, as_at=dt.date(2024, 12, 31))
by_broker_d = {l.broker: (l.quantity, l.remaining, l.price_fc) for l in lots_d}
check(by_broker_d.get("BROKER_A") == (100.0, 100.0, 20.0),
      "Broker A's lot receives the 2:1 split (qty/remaining doubled, price halved)",
      str(by_broker_d.get("BROKER_A")))
check(by_broker_d.get("BROKER_B") == (200.0, 200.0, 22.5),
      "Broker B's lot ALSO receives the same split, though the SPLIT row was "
      "reported on Broker A's statement - no broker is skipped because of "
      "the new (broker, symbol) key",
      str(by_broker_d.get("BROKER_B")))

# ======================================================================
section("E. EXISTING MORGAN TRANSFER_OUT REGRESSION - must remain fully green")

import subprocess
result = subprocess.run([sys.executable, "-m", "tests.morgan_transfer_out_regression"],
                        cwd=str(ROOT), capture_output=True, text=True)
check(result.returncode == 0 and "ALL CHECKS PASSED" in result.stdout,
      "tests.morgan_transfer_out_regression is unaffected by this fix",
      "exit=" + str(result.returncode))

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
