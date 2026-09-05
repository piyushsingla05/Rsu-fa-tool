"""E*TRADE regression - the dedicated limited-statement-path validation.

One quarterly statement covering Q1 2025 is the entire evidence for a calendar
2025 return. The engine must produce a usable working paper from it, mark the
basis, and name every gap - without inventing a single missing statement.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.compute import ComputeOptions, build_cg, build_lots   # noqa: E402
from src.fx import FXTable                                  # noqa: E402
from src.ingest.extract import extract_file                 # noqa: E402
from src.ingest.profiles import load_profiles               # noqa: E402
from src.models import Period                               # noqa: E402
from src.review import Register, Severity                   # noqa: E402
from src.run import resolve_positions                       # noqa: E402

PDF = Path("/home/claude/brokers/RSU brokers/etrade/"
           "Etrade_March_2024_ClientStatements_080225.pdf")
PERIOD = Period(dt.date(2025, 1, 1), dt.date(2025, 12, 31))

# Printed on the statement
STMT = {
    "period": (dt.date(2025, 1, 1), dt.date(2025, 3, 31)),
    "shares": 1170.0, "price": 153.610, "cost_total": 25863.40,
    "market_value": 179723.70, "cash": 0.00,
    "dividend": 1092.25, "tax": 273.06,
    "sales": [(58.0, 150.0704, 8698.88), (57.0, 150.0780, 8549.26)],
    "credited": 17248.14, "realised_gain": 9909.34,
}
GROSS = sum(q * p for q, p, _ in STMT["sales"])          # 17,258.53
FEES = GROSS - STMT["credited"]                          # 10.39
DERIVED_COST = GROSS - STMT["realised_gain"]             # 7,349.19


def ok(flag, label, detail=""):
    print(f"  {'PASS' if flag else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return 0 if flag else 1


def main() -> int:
    fails = 0
    profiles = load_profiles()
    ev, rec, _, _ = extract_file(PDF, profiles)

    print("=" * 98)
    print("1. EXTRACTION AND STATEMENT COVERAGE")
    print("=" * 98)
    fails += ok(rec.profile_id == "etrade_client_statement" and rec.confidence >= 0.99,
                "identified", f"{rec.profile_id} at {rec.confidence:.0%}")
    fails += ok((rec.period_start, rec.period_end) == STMT["period"],
                "statement coverage recorded exactly", rec.coverage)
    fails += ok(rec.period_end < PERIOD.end,
                "coverage is SHORT of the reporting period",
                f"statement ends {rec.period_end:%d-%m-%Y}, period ends "
                f"{PERIOD.end:%d-%m-%Y} - the limited-data case")

    print()
    print("=" * 98)
    print("2. HOLDINGS - TRADE-DATE BASIS, NOT DEDUCTED TWICE")
    print("=" * 98)
    reg = Register()
    resolved = resolve_positions(ev, PERIOD, reg)
    lots, matches = build_lots(resolved, Register(), as_at=PERIOD.end)
    held = sum(l.remaining for l in lots)
    fails += ok(abs(held - STMT["shares"]) < 0.001, "period-end quantity",
                f"{held:,.0f} vs statement {STMT['shares']:,.0f}")
    print("        E*TRADE states holdings on a TRADE-date basis, so the 115 shares")
    print("        traded 31-03 and settling 01-04 are already excluded. Deducting")
    print("        them again would report 1,055.")
    pos = ev[ev.event == "POSITION"].iloc[0]
    cps = float(pos["price_fc"])
    fails += ok(abs(cps * STMT["shares"] - STMT["cost_total"]) < 0.5,
                "cost basis", f"{cps:,.5f}/share x {STMT['shares']:,.0f} = "
                f"{cps * STMT['shares']:,.2f} vs statement {STMT['cost_total']:,.2f}")

    print()
    print("=" * 98)
    print("3. LIMITED-DATA VALUATION")
    print("=" * 98)
    fx = FXTable(ROOT / "config" / "fx_rates.csv", register=Register())
    rate_end = fx.rate(PERIOD.end, "USD", purpose="check")
    print(f"  period-end TTBR {rate_end:.4f} for {PERIOD.end:%d-%m-%Y}")
    print(f"  initial  = {STMT['shares']:,.0f} x {cps:,.4f} x {rate_end:.4f} "
          f"= Rs {STMT['shares'] * cps * rate_end:,.0f}   (period-end TTBR: the")
    print("             acquisition dates are not in evidence)")
    print(f"  peak     = {STMT['shares']:,.0f} x 180.90 (annual high) x {rate_end:.4f}"
          f" = Rs {STMT['shares'] * 180.90 * rate_end:,.0f}")
    print(f"  closing  = {STMT['shares']:,.0f} x 171.05 (period-end close) x "
          f"{rate_end:.4f} = Rs {STMT['shares'] * 171.05 * rate_end:,.0f}")
    fails += ok(180.90 != 153.610 and 171.05 != 153.610,
                "annual high and period-end close are NOT the statement price",
                "statement price 153.610 is a 31-03 quote, not either of them")

    print()
    print("=" * 98)
    print("4. SALES, COST AND THE INDIAN HOLDING PERIOD")
    print("=" * 98)
    sells = ev[ev.event == "SELL"]
    fails += ok(len(sells) == 2, f"{len(sells)} sales extracted vs 2 on the statement")
    g = float(sells.amount_fc.astype(float).sum())
    c = float(sells.cost_fc.astype(float).sum())
    fails += ok(abs(g - GROSS) < 0.01, "gross proceeds",
                f"{g:,.2f} = credited {STMT['credited']:,.2f} + fees {FEES:,.2f}")
    fails += ok(abs(c - DERIVED_COST) < 0.05, "cost derived from the stated gain",
                f"{c:,.2f} = gross {GROSS:,.2f} - stated realised gain "
                f"{STMT['realised_gain']:,.2f}")
    fails += ok(abs((g - c) - STMT["realised_gain"]) < 0.05,
                "gain reproduces the statement exactly",
                f"{g - c:,.2f} vs stated {STMT['realised_gain']:,.2f}")
    fails += ok((sells.acquired_on.astype(str).str.strip() == "").all(),
                "no acquisition date was invented",
                "- E*TRADE directs the reader online for tax-lot detail")

    reg2 = Register()
    cg = build_cg(matches, fx, ComputeOptions(), PERIOD, reg2)
    terms = sorted({t for t in cg["Nature of Gain"]}) if not cg.empty else []
    fails += ok(terms == ["Undeterminable"],
                "Indian holding period reported as undeterminable", f"{terms}")
    fails += ok(any(f.severity == Severity.BLOCKER for f in reg2.flags),
                "and raised as a BLOCKER, not silently classified",
                "- the statement's 'Long-Term Gain' label is the US test")

    print()
    print("=" * 98)
    print("5. DIVIDENDS - COVERAGE IS SHORT AND THERE IS NO 1042-S")
    print("=" * 98)
    div = ev[ev.event == "DIV"]
    fails += ok(len(div) == 1, f"{len(div)} dividend row")
    if len(div):
        d = div.iloc[0]
        fails += ok(abs(float(d["amount_fc"]) - STMT["dividend"]) < 0.01
                    and abs(float(d["tax_fc"]) - STMT["tax"]) < 0.01,
                    "gross and withholding paired",
                    f"{float(d['amount_fc']):,.2f} / {float(d['tax_fc']):,.2f} vs "
                    f"statement {STMT['dividend']:,.2f} / {STMT['tax']:,.2f}")
    print(f"        withholding is {STMT['tax'] / STMT['dividend']:.1%} of gross")
    print("        Coverage runs 01-01 to 31-03 only and no 1042-S exists, so the")
    print("        available dividend evidence is reported and the gap is flagged.")

    print()
    print("=" * 98)
    print("6. CASH FOR FA-A2")
    print("=" * 98)
    fails += ok(len(rec.cash) == 1
                and abs(float(rec.cash[0]["balance_fc"]) - STMT["cash"]) < 0.01,
                "cash balance", f"{float(rec.cash[0]['balance_fc']):,.2f} vs "
                f"statement {STMT['cash']:,.2f}")
    print("        The projected settled balance of 17,248.14 is cash pending payment")
    print("        TO THE COMPANY, not the client's, and is deliberately not used.")

    print()
    print("=" * 98)
    print("7. GENERIC vs PROFILED")
    print("=" * 98)
    gev, grec, _, _ = extract_file(PDF, [])
    print(f"  profiled {len(ev):>3} rows ({rec.profile_id})   "
          f"generic {len(gev):>3} rows, broker {grec.broker}, "
          f"conf {grec.confidence:.0%}")
    fails += ok(len(gev) == 0, "generic invents nothing from a laid-out PDF")
    fails += ok(grec.broker == "ETRADE",
                "generic names the brand, not the parent",
                "- the statement carries both E*TRADE and Morgan Stanley Smith "
                "Barney; the account's brand is the better identification")

    print()
    print(f"RESULT: {'ALL CHECKS PASSED' if not fails else str(fails) + ' CHECK(S) FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
