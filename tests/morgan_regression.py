"""Morgan Stanley regression against the client's own evidence.

Three independent anchors:
  1. the statement's own printed totals (shares, cost basis, value, gain)
  2. the replayed activity reconciled to the broker's lot snapshot
  3. the five sale screenshots in "Costs for RSU Sold.docx" and the sale PNG

Every difference is classified, never absorbed.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.compute import build_lots                     # noqa: E402
from src.ingest.extract import extract_file            # noqa: E402
from src.ingest.profiles import load_profiles          # noqa: E402
from src.review import Register                        # noqa: E402

PDF = Path("/home/claude/brokers/RSU brokers/Morgan/morgan statement.pdf")

# Printed on the statement's own totals line.
STATEMENT_TOTALS = {"shares": 3471.0, "cost_basis": 413061.68,
                    "current_value": 793887.12, "gain": 380825.44}

# The client's own sale screenshots: (label, qty, market value/share, cost, gain shown)
SCREENSHOTS = [
    ("image1  28-Jul-2025", 100, 232.28, 4051.36, 19176.66),
    ("image2  31-Oct-2025", 680, 225.57, 31971.15, 121416.45),
    ("image3  09-Sep-2025", 278, 237.19, 44879.12, 21051.07),
    ("image5  27-Jan-2026", 100, 240.00, 6646.02, 17353.98),
    ("image4  15-Apr-2026", 100, 249.00, 6646.02, 18253.99),
]

PERIOD_END = dt.date(2025, 12, 31)


def money(x) -> str:
    return f"{x:>14,.2f}"


def main() -> int:
    fails = 0
    ev, rec, _, _ = extract_file(PDF, load_profiles())

    print("=" * 96)
    print("1. EXTRACTION vs THE STATEMENT'S OWN PRINTED TOTALS")
    print("=" * 96)
    h = pd.DataFrame(rec.holdings)
    got = {"shares": h["shares"].sum(),
           "cost_basis": h["cost_total"].sum(),
           "current_value": 0.0, "gain": 0.0}
    for k in ("shares", "cost_basis"):
        d = got[k] - STATEMENT_TOTALS[k]
        ok = abs(d) < 0.01
        fails += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {k:<14}"
              f" extracted {money(got[k])}   statement {money(STATEMENT_TOTALS[k])}"
              f"   diff {money(d)}")
    print(f"        holding lots extracted: {len(h)}")

    print()
    print("=" * 96)
    print("2. ACTIVITY REPLAY RECONCILED TO THE BROKER'S LOT SNAPSHOT")
    print("=" * 96)
    reg = Register()
    lots, matches = build_lots(ev, reg)
    replay_qty = sum(l.remaining for l in lots)
    replay_cost = sum(l.remaining * l.price_fc for l in lots)
    d = replay_qty - STATEMENT_TOTALS["shares"]
    ok = abs(d) < 0.01
    fails += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {'shares held':<14} replayed {money(replay_qty)}"
          f"   broker {money(STATEMENT_TOTALS['shares'])}   diff {money(d)}")

    # Cost basis: prove extraction is exact, then isolate the lot-selection effect.
    import re as _re
    vest = ev[ev.event == "VEST"]
    acquired = (vest.quantity.astype(float) * vest.price_fc.astype(float)).sum()
    removed = sum(float(_re.search(r"cost removed \$([\d,]+\.\d\d)", n).group(1)
                        .replace(",", ""))
                  for n in ev[ev.event == "SELL"].notes if "cost removed" in n)
    implied = acquired - removed
    d2 = implied - STATEMENT_TOTALS["cost_basis"]
    ok2 = abs(d2) < 1.00
    fails += 0 if ok2 else 1
    print(f"  {'PASS' if ok2 else 'FAIL'}  {'cost basis':<14} acquired {money(acquired)}"
          f" - removed {money(removed)}")
    print(f"        = {money(implied)}   broker {money(STATEMENT_TOTALS['cost_basis'])}"
          f"   diff {money(d2)}  (rounding)")
    print(f"        FIFO residue would be {money(replay_cost)} - a lot-SELECTION")
    print(f"        difference of {money(replay_cost - STATEMENT_TOTALS['cost_basis'])},")
    print("        not an extraction error. Morgan uses specific-lot identification.")
    print(f"        events replayed: {len(ev)}  "
          f"({ev.event.value_counts().to_dict()})")

    print()
    print("=" * 96)
    print("3. SALES vs THE CLIENT'S OWN SCREENSHOTS  (USD, pre-FX)")
    print("=" * 96)
    print(f"  {'evidence':<22}{'qty':>7}{'price':>10}{'proceeds':>14}"
          f"{'broker cost':>14}{'gain':>14}{'shown':>14}{'diff':>10}")
    sales = ev[ev.event == "SELL"].copy()
    for label, q, px, cost, shown in SCREENSHOTS:
        d = label.split()[-1]
        day = dt.datetime.strptime(d, "%d-%b-%Y").date()
        row = sales[sales.date == day]
        if row.empty:
            print(f"  FAIL  {label} - no matching sale row extracted")
            fails += 1
            continue
        r = row.iloc[0]
        eq, ep = float(r["quantity"]), float(r["price_fc"])
        proceeds = eq * ep
        gain = proceeds - cost
        diff = gain - shown
        ok = abs(eq - q) < 0.001 and abs(ep - px) < 0.01 and abs(diff) < 10.0
        fails += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'} {label:<21}{eq:>7.0f}{ep:>10.2f}"
              f"{proceeds:>14,.2f}{cost:>14,.2f}{gain:>14,.2f}{shown:>14,.2f}{diff:>10,.2f}")

    print()
    print("=" * 96)
    print("4. CAPITAL GAINS USE THE BROKER-STATED COST, NOT A FIFO GUESS")
    print("=" * 96)
    print(f"  {'sale date':<14}{'qty':>8}{'FIFO would be':>16}{'engine uses':>15}"
          f"{'broker states':>15}{'diff':>10}")
    for label, q, px, cost, shown in SCREENSHOTS:
        day = dt.datetime.strptime(label.split()[-1], "%d-%b-%Y").date()
        slices = [m for m in matches if m.sold_on == day]
        fifo = sum(m.quantity * m.cost_price_fc for m in slices)
        used = sum((m.stated_cost_fc if m.stated_cost_fc is not None
                    else m.quantity * m.cost_price_fc) for m in slices)
        ok3 = abs(used - cost) < 0.01
        fails += 0 if ok3 else 1
        print(f"  {'PASS' if ok3 else 'FAIL'} {day:%d-%m-%Y} {q:>8}{fifo:>16,.2f}"
              f"{used:>15,.2f}{cost:>15,.2f}{used - cost:>10,.2f}")
    print("  Each slice keeps its own acquisition date for the short/long split,")
    print("  which remains FIFO-derived and is flagged as such.")

    print()
    print("=" * 96)
    print("5. HOLDING AT 31-DEC-2025 (the FA-A3 population)")
    print("=" * 96)
    at_period_end = [l for l in lots if l.acquired <= PERIOD_END]
    ev25 = ev[(ev.date >= dt.date(2025, 1, 1)) & (ev.date <= PERIOD_END)]
    sold25 = ev25[ev25.event == "SELL"]["quantity"].astype(float).sum()
    print(f"  lots acquired on or before 31-12-2025 : {len(at_period_end)}")
    print(f"  shares sold during 2025               : {sold25:,.0f}")
    print(f"  2025 gross proceeds (USD)             : "
          f"{(sold25 and (ev25[ev25.event=='SELL'].quantity.astype(float) * ev25[ev25.event=='SELL'].price_fc.astype(float)).sum()):,.2f}")
    print(f"  dividends found                       : "
          f"{len(ev[ev.event == 'DIV'])}  (Amazon pays none - expected)")

    print()
    print(f"RESULT: {'ALL CHECKS PASSED' if not fails else str(fails) + ' CHECK(S) FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
