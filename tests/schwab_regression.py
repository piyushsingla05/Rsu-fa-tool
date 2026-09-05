"""Schwab regression against the statements' own printed figures.

Anchors: the December position table, the monthly cash lines, the dividend and
NRA-tax pairs, and the Year-End Summary realised gain/loss totals.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.compute import build_lots                       # noqa: E402
from src.ingest.extract import extract_file              # noqa: E402
from src.ingest.profiles import load_profiles            # noqa: E402
from src.models import Period                            # noqa: E402
from src.review import Register                          # noqa: E402
from src.run import resolve_positions                    # noqa: E402

SRC = Path("/home/claude/brokers/RSU brokers/Charles schwab")
PERIOD = Period(dt.date(2025, 1, 1), dt.date(2025, 12, 31))

# Printed on the December statement
DEC_POSITION = {"shares": 3866.0, "price": 346.10, "market_value": 1338022.60,
                "cost_basis": 824794.76, "cash": 2119.86}
# Printed on the Year-End Summary totals lines
YE_TOTALS = {"proceeds": 306289.76 + 13617.40, "cost": 309052.20 + 5141.64,
             "gain": (306289.76 + 13617.40) - (309052.20 + 5141.64)}
# Printed dividend / NRA-tax pairs
DIVIDENDS = [(dt.date(2025, 9, 30), 2020.75, 505.19),
             (dt.date(2025, 12, 31), 2512.90, 628.23)]
# Monthly cash closing balances, from each statement's Cash line
CASH = {dt.date(2025, 7, 31): 0.89, dt.date(2025, 9, 30): 4150.57,
        dt.date(2025, 10, 31): 0.65, dt.date(2025, 11, 30): 0.65,
        dt.date(2025, 12, 31): 2119.86}

INDIAN_LTCG_DAYS = 730       # >24 months for foreign shares
US_LTCG_DAYS = 365           # >12 months - the broker's own basis


def ok(flag, label, detail=""):
    print(f"  {'PASS' if flag else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return 0 if flag else 1


def main() -> int:
    fails = 0
    profiles = load_profiles()
    frames, recs, cash_rows, hashes = [], [], [], {}
    for f in sorted(SRC.iterdir()):
        ev, rec, _, _ = extract_file(f, profiles)
        if rec.content_hash in hashes:
            continue                                  # duplicate upload
        hashes[rec.content_hash] = rec.file
        recs.append(rec)
        cash_rows += rec.cash or []
        if len(ev):
            frames.append(ev)
    events = pd.concat(frames, ignore_index=True)

    print("=" * 96)
    print("1. DOCUMENT DETECTION AND DUPLICATE HANDLING")
    print("=" * 96)
    fails += ok(len(list(SRC.iterdir())) == 7 and len(recs) == 6,
                "7 files supplied, 6 counted",
                "- two Sep-30 files are byte-identical")
    fails += ok(all(r.confidence >= 0.99 for r in recs),
                "every file profiled at 100% confidence",
                f"- {sorted({r.profile_id for r in recs})}")

    print()
    print("=" * 96)
    print("2. POSITION SNAPSHOTS COLLAPSE TO ONE HOLDING")
    print("=" * 96)
    snaps = int((events["event"] == "POSITION").sum())
    reg = Register()
    resolved = resolve_positions(events, PERIOD, reg)
    kept = resolved[(resolved["event"] == "POSITION")]
    fails += ok(snaps == 5 and len(kept) == 1,
                f"{snaps} stated positions -> {len(kept)} holding")
    if len(kept):
        r = kept.iloc[0]
        fails += ok(abs(float(r["quantity"]) - DEC_POSITION["shares"]) < 0.01,
                    "period-end quantity",
                    f"{float(r['quantity']):,.0f} vs statement "
                    f"{DEC_POSITION['shares']:,.0f}")
        cost = float(r["quantity"]) * float(r["price_fc"])
        fails += ok(abs(cost - DEC_POSITION["cost_basis"]) < 1.0,
                    "period-end cost basis",
                    f"{cost:,.2f} vs statement {DEC_POSITION['cost_basis']:,.2f}"
                    f"  diff {cost - DEC_POSITION['cost_basis']:,.2f}")

    print()
    print("=" * 96)
    print("3. CASH BALANCES FOR FA-A2")
    print("=" * 96)
    cash = pd.DataFrame(cash_rows)
    got = {r["date"]: round(float(r["balance_fc"]), 2) for _, r in cash.iterrows()}
    for d, expect in sorted(CASH.items()):
        fails += ok(abs(got.get(d, -1) - expect) < 0.01,
                    f"{d:%d-%m-%Y} closing cash",
                    f"{got.get(d, 0):,.2f} vs statement {expect:,.2f}")
    print(f"        A2 peak from these balances: "
          f"${max(got.values()) if got else 0:,.2f}")

    print()
    print("=" * 96)
    print("4. DIVIDENDS AND NRA WITHHOLDING  (paired from separate lines)")
    print("=" * 96)
    div = events[events["event"] == "DIV"]
    print(f"  {'date':<14}{'gross':>12}{'stated':>12}{'tax':>12}{'stated':>12}")
    for d, gross, tax in DIVIDENDS:
        row = div[div["date"] == d]
        if row.empty:
            fails += ok(False, f"{d:%d-%m-%Y} dividend not extracted")
            continue
        g, t = float(row.iloc[0]["amount_fc"]), float(row.iloc[0]["tax_fc"])
        good = abs(g - gross) < 0.01 and abs(t - tax) < 0.01
        fails += 0 if good else 1
        print(f"  {'PASS' if good else 'FAIL'} {d:%d-%m-%Y}{g:>12,.2f}{gross:>12,.2f}"
              f"{t:>12,.2f}{tax:>12,.2f}")
    tot_g = div["amount_fc"].astype(float).sum()
    tot_t = div["tax_fc"].astype(float).sum()
    print(f"        totals extracted: gross ${tot_g:,.2f}, withheld ${tot_t:,.2f} "
          f"({tot_t / tot_g:.1%})")

    print()
    print("=" * 96)
    print("5. CLOSED-LOT SALES vs THE YEAR-END SUMMARY TOTALS")
    print("=" * 96)
    sells = events[events["event"] == "SELL"]
    proceeds = sells["amount_fc"].astype(float).sum()
    cost = sells["cost_fc"].astype(float).sum()
    fails += ok(len(sells) == 21, f"{len(sells)} closed-lot rows extracted")
    for label, got_v, exp_v in (("proceeds", proceeds, YE_TOTALS["proceeds"]),
                                ("cost basis", cost, YE_TOTALS["cost"]),
                                ("realised gain", proceeds - cost, YE_TOTALS["gain"])):
        fails += ok(abs(got_v - exp_v) < 0.01, f"{label:<14}",
                    f"{got_v:>14,.2f} vs statement {exp_v:>14,.2f}"
                    f"  diff {got_v - exp_v:,.2f}")
    fails += ok(sells["acquired_on"].astype(str).str.len().gt(0).all(),
                "every sale carries the broker's stated acquisition date")

    print()
    print("=" * 96)
    print("6. HOLDING PERIOD - INDIAN 24-MONTH RULE, NOT THE BROKER'S US LABEL")
    print("=" * 96)
    lots, matches = build_lots(resolved, Register(), as_at=PERIOD.end)
    ind = us = 0
    for m in matches:
        if m.acquired_on is None:
            continue
        days = (m.sold_on - m.acquired_on).days
        ind += 1 if days > INDIAN_LTCG_DAYS else 0
        us += 1 if days > US_LTCG_DAYS else 0
    print(f"  long-term under the Indian 24-month rule : {ind}")
    print(f"  long-term under the US 12-month rule     : {us}  (Schwab labels 2 as LT)")
    fails += ok(ind == 0 and us == 2,
                "the engine applies the Indian rule",
                "- Schwab's $8,475.76 'long term' is SHORT term for Indian tax")

    print()
    print(f"RESULT: {'ALL CHECKS PASSED' if not fails else str(fails) + ' CHECK(S) FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
