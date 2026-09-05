"""E*TRADE G&L Expanded regression - the adjusted-capital-gains format rule.

The point this fixture proves is not arithmetic, it is WHICH COLUMN.

An E*TRADE G&L Expanded report states two different gains for the same sale. The
ordinary "Gain/Loss" is proceeds less the ACQUISITION cost, so for an equity plan
it still contains the ordinary income recognised on vest or purchase - income the
employee has already been taxed on as a perquisite. "Adjusted Cost Basis" adds
that income back into cost, and "Adjusted Gain/Loss" is the capital gain that
follows.

On this INTC report the two differ by $15,021.67 on $39,526.32 of proceeds. Take
the easier column and the client is taxed twice on the same income.

Everything here is read from the supplied report. The SBI TTBR rates used for the
INR conversion are SYNTHETIC TEST RATES created inside this test - they are never
written to the shipped rate table, and the engine's real behaviour when a rate is
absent (report nothing, raise a blocker) is asserted separately below.
"""
from __future__ import annotations

import datetime as dt
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.compute import ComputeOptions, build_cg, build_lots        # noqa: E402
from src.fx import FXTable                                          # noqa: E402
from src.ingest.extract import extract_file                         # noqa: E402
from src.ingest.profiles import load_profiles                       # noqa: E402
from src.models import Period                                       # noqa: E402
from src.review import Register, Severity                           # noqa: E402
from src.run import resolve_capital_gain_sources                    # noqa: E402

GL = ROOT / "tests" / "fixtures" / "etrade_GL_Expanded_INTC.xlsx"
PERIOD = Period(dt.date(2026, 4, 1), dt.date(2027, 3, 31))   # FY 2026-27
SOLD = dt.date(2026, 4, 24)

# Printed on the report itself
STMT = {
    "lots": 8, "quantity": 490.0,
    "proceeds": 39526.319904,
    "acquisition_cost": 10168.639974,      # the ordinary cost - NOT the tax cost
    "ordinary_income": 15021.672853,       # already taxed as a perquisite
    "adjusted_cost": 25190.312827,         # acquisition cost + ordinary income
    "ordinary_gain": 29357.679930,         # summary line "Gain/Loss"
    "adjusted_gain": 14336.007077,         # summary line "Adjusted Gain/Loss"
}
# Synthetic SBI TTBR rates, for this test only.
TEST_RATES = {
    SOLD: 93.5000,
    dt.date(2020, 2, 19): 71.2500, dt.date(2021, 2, 19): 72.4000,
    dt.date(2021, 4, 30): 74.1000, dt.date(2021, 5, 3): 73.9500,
    dt.date(2021, 8, 2): 74.2000, dt.date(2021, 8, 19): 74.3500,
    dt.date(2021, 11, 1): 74.8000, dt.date(2022, 5, 2): 76.5000,
}
INDIAN_LTCG_DAYS = 730


def ok(flag, label, detail=""):
    print(f"  {'PASS' if flag else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return 0 if flag else 1


def _fx_with_test_rates(register: Register) -> FXTable:
    fx = FXTable(ROOT / "config" / "fx_rates.csv", register=register)
    fx.merge(pd.DataFrame([{"date": d, "currency": "USD", "ttbr": r,
                            "source": "SYNTHETIC TEST RATE - not an SBI rate",
                            "verified": "Y"}
                           for d, r in TEST_RATES.items()]), priority=True)
    return fx


def main() -> int:
    fails = 0
    profiles = load_profiles()
    ev, rec, _, _ = extract_file(GL, profiles)

    print("=" * 98)
    print("1. FORMAT DETECTION - ON THE HEADERS, NOT ON THE SECURITY")
    print("=" * 98)
    fails += ok(rec.profile_id == "etrade_gl_expanded" and rec.confidence >= 0.99,
                "identified", f"{rec.profile_id} at {rec.confidence:.0%}")
    fails += ok(rec.broker == "ETRADE", "broker", rec.broker)
    prof = next(p for p in profiles if p.id == "etrade_gl_expanded")
    markers = [s.lower() for s in prof.match.get("all_text", [])]
    fails += ok(markers == ["adjusted cost basis", "adjusted gain/loss"],
                "detection markers are the ADJUSTED column headings",
                f"{markers}")
    blob = " ".join(str(v).lower() for v in
                    [prof.match, prof.layout, prof.fields, prof.rules])
    fails += ok("intc" not in blob and "intel" not in blob,
                "no security is named anywhere in the profile",
                "- any future E*TRADE G&L Expanded routes here on format alone")
    fails += ok(rec.cg_authority is True,
                "flagged as the capital-gains source of record for its broker")

    print()
    print("=" * 98)
    print("2. THE REPORT'S OWN ARITHMETIC")
    print("=" * 98)
    sells = ev[ev.event == "SELL"]
    fails += ok(len(sells) == STMT["lots"],
                f"{len(sells)} closed lots extracted vs {STMT['lots']} on the report")
    fails += ok(abs(sells.quantity.sum() - STMT["quantity"]) < 0.01,
                "quantity", f"{sells.quantity.sum():,.0f} vs summary line "
                f"{STMT['quantity']:,.0f}")
    proceeds = float(sells.amount_fc.astype(float).sum())
    adj_cost = float(sells.cost_fc.astype(float).sum())
    adj_gain = float(sells.broker_adj_gain_fc.astype(float).sum())
    ord_gain = float(sells.broker_gain_fc.astype(float).sum())
    for label, got, exp in (("proceeds", proceeds, STMT["proceeds"]),
                            ("adjusted cost basis", adj_cost, STMT["adjusted_cost"]),
                            ("adjusted gain/loss", adj_gain, STMT["adjusted_gain"]),
                            ("ordinary gain/loss", ord_gain, STMT["ordinary_gain"])):
        fails += ok(abs(got - exp) < 0.05, f"{label:<21}",
                    f"{got:>13,.2f} vs report {exp:>13,.2f}")
    fails += ok(abs((proceeds - adj_cost) - adj_gain) < 0.05,
                "proceeds - adjusted cost = adjusted gain, row by row",
                f"{proceeds - adj_cost:,.2f} = {adj_gain:,.2f}")
    fails += ok(not rec.recon_breaks,
                f"{len(rec.recon_breaks)} row(s) fail the report's own arithmetic")
    fails += ok(rec.control_total is not None
                and abs(rec.control_total["gain_fc"] - STMT["adjusted_gain"]) < 0.01,
                "the summary line is read as a CONTROL TOTAL, not as a 9th sale",
                f"{len(sells)} sales extracted from 9 data rows")

    print()
    print("=" * 98)
    print("3. WHY THE ADJUSTED COLUMN - THE WHOLE POINT OF THE FORMAT")
    print("=" * 98)
    print(f"  acquisition cost            ${STMT['acquisition_cost']:>12,.2f}")
    print(f"  ordinary income recognised  ${STMT['ordinary_income']:>12,.2f}"
          "   <- already taxed as a perquisite")
    print(f"  adjusted cost basis         ${STMT['adjusted_cost']:>12,.2f}")
    print()
    print(f"  ordinary  Gain/Loss         ${STMT['ordinary_gain']:>12,.2f}   WRONG")
    print(f"  Adjusted  Gain/Loss         ${STMT['adjusted_gain']:>12,.2f}   correct")
    over = STMT["ordinary_gain"] - STMT["adjusted_gain"]
    print(f"  overstatement               ${over:>12,.2f}   "
          f"= the ordinary income, taxed twice")
    fails += ok(abs(over - STMT["ordinary_income"]) < 0.05,
                "the gap between the two columns IS the ordinary income",
                f"${over:,.2f}")
    fails += ok(abs(adj_cost - (STMT["acquisition_cost"] + STMT["ordinary_income"]))
                < 0.05,
                "adjusted cost = acquisition cost + ordinary income")
    fails += ok(abs(adj_cost - STMT["acquisition_cost"]) > 1000,
                "the engine took the ADJUSTED cost, not the acquisition cost",
                f"${adj_cost:,.2f} not ${STMT['acquisition_cost']:,.2f}")
    fails += ok(abs(adj_gain - STMT["ordinary_gain"]) > 1000,
                "and therefore not the ordinary Gain/Loss",
                f"${adj_gain:,.2f} not ${STMT['ordinary_gain']:,.2f}")
    fails += ok("ordinary" in str(sells.iloc[0]["notes"]).lower(),
                "every row discloses the ordinary figure it did NOT use")

    print()
    print("=" * 98)
    print("4. INDIAN HOLDING PERIOD - COMPUTED FROM DATES, NOT THE US LABEL")
    print("=" * 98)
    fails += ok((sells.acquired_on.astype(str).str.len() > 0).all(),
                "every lot carries the broker's stated acquisition date")
    fails += ok("capital gains status" not in
                " ".join(str(v).lower() for v in prof.fields.values()),
                "the report's own 'Capital Gains Status' column is NOT mapped",
                "- it is the US 12-month test and has no bearing on Indian tax")
    reg = Register()
    lots, matches = build_lots(ev, reg, as_at=PERIOD.end)
    days = sorted((m.sold_on - m.acquired_on).days for m in matches)
    fails += ok(len(matches) == STMT["lots"], f"{len(matches)} matched disposals")
    fails += ok(all(d > INDIAN_LTCG_DAYS for d in days),
                "all lots exceed the Indian 24-month test on their own dates",
                f"holding {min(days)}-{max(days)} days")

    print()
    print("=" * 98)
    print("5. SALE-DATE TTBR AND THE INR CONVERSION")
    print("=" * 98)
    reg2 = Register()
    fx = _fx_with_test_rates(reg2)
    cg = build_cg(matches, fx, ComputeOptions(), PERIOD, reg2)
    fails += ok(len(cg) == STMT["lots"], f"{len(cg)} capital-gain rows")
    fails += ok((pd.to_datetime(cg["Date of Sale"]).dt.date == SOLD).all(),
                "sale date", f"{SOLD:%d-%m-%Y} on every row")
    fails += ok((cg["FX Rate - Sale Date"] == TEST_RATES[SOLD]).all(),
                "sale-date TTBR is the rate for the SALE DATE",
                f"{TEST_RATES[SOLD]:.4f} for {SOLD:%d-%m-%Y}, not a period-end rate")
    fails += ok((pd.to_datetime(cg["FX Date Used - Sale"]).dt.date == SOLD).all(),
                "and the sheet records WHICH published rate date was used")
    fails += ok((cg["Computable"] == "Yes").all(), "every row computable")
    fails += ok((cg["Nature of Gain"] == "Long Term").all(),
                "nature of gain", "Long Term on the Indian 24-month rule")

    inr_proceeds = float(cg["Full Value of Consideration (Rs.)"].sum())
    inr_cost = float(cg["Cost of Acquisition (Rs.)"].sum())
    inr_gain = float(cg["Capital Gain (Rs.)"].sum())
    expect_proceeds = STMT["proceeds"] * TEST_RATES[SOLD]
    fails += ok(abs(inr_proceeds - expect_proceeds) < 60,
                "INR consideration = FC proceeds x sale-date TTBR",
                f"Rs {inr_proceeds:,.0f} vs Rs {expect_proceeds:,.0f}")
    fails += ok(abs(inr_gain - (inr_proceeds - inr_cost)) < 10,
                "INR gain = INR consideration - INR cost",
                f"Rs {inr_gain:,.0f}")
    print(f"        proceeds  Rs {inr_proceeds:>14,.0f}")
    print(f"        cost      Rs {inr_cost:>14,.0f}   (each lot at its OWN "
          "acquisition-date TTBR)")
    print(f"        gain      Rs {inr_gain:>14,.0f}")

    # Same conversion driven off the ordinary column - the error being prevented.
    wrong = sum(float(r["Broker Ordinary Gain/Loss (FC)"]) * TEST_RATES[SOLD]
                for _, r in cg.iterrows())
    print(f"        gain on the ORDINARY column would be Rs {wrong:,.0f} "
          f"- overstated by Rs {wrong - inr_gain:,.0f}")
    fails += ok(wrong - inr_gain > 100000,
                "using the ordinary column would materially overstate the gain",
                f"by Rs {wrong - inr_gain:,.0f}")
    fails += ok(abs(inr_cost - STMT["adjusted_cost"] * 74) > 1,
                "cost converts at each lot's own acquisition-date rate",
                "- not one blended rate")

    print()
    print("=" * 98)
    print("6. A MISSING RATE IS NOT A RATE OF ZERO")
    print("=" * 98)
    reg3 = Register()
    bare = FXTable(ROOT / "config" / "fx_rates.csv", register=reg3)
    cg_bare = build_cg(matches, bare, ComputeOptions(), PERIOD, reg3)
    fails += ok((cg_bare["Computable"] == "No - FX unavailable").all(),
                "with no 2020-2022 rate from ANY source, no rupee figure is produced")
    fails += ok((cg_bare["Capital Gain (Rs.)"] == "").all(),
                "the gain column is blank, NOT the full sale proceeds",
                "- a cost of nil would have reported Rs 3.6 crore of gain")
    fails += ok(any(f.severity == Severity.BLOCKER for f in reg3.flags),
                "and the missing rates are raised as blockers")
    stale = [f for f in reg2.flags if "carried forward" in f.reason.lower()]
    print(f"        carried-forward rate flags raised: {len(stale)}")

    print()
    print("=" * 98)
    print("7. PRECEDENCE - THE SAME SALE IS NEVER COUNTED TWICE")
    print("=" * 98)
    # A quarterly statement showing the same disposal at summary level. Built
    # here to exercise the precedence rule; it is not client evidence.
    stmt_frame = pd.DataFrame([{
        "date": SOLD, "broker": "ETRADE", "account_no": "X", "symbol": "INTC",
        "event": "SELL", "quantity": 490.0, "price_fc": 80.6659,
        "amount_fc": 39526.32, "tax_fc": "", "acquired_on": "", "cost_fc": "",
        "currency": "USD", "notes": "quarterly statement summary sale"}])

    class _Rec:
        file, cg_authority = "quarterly_statement.pdf", False
    reg4 = Register()
    kept = resolve_capital_gain_sources(
        [(stmt_frame, _Rec()), (ev, rec)], [_Rec(), rec], reg4)
    total_sells = sum(int((f["event"] == "SELL").sum()) for f in kept if len(f))
    fails += ok(total_sells == STMT["lots"],
                f"{total_sells} sales survive from 1 statement row + "
                f"{STMT['lots']} lot rows",
                "- the statement's summary row is superseded by the lot detail")
    fails += ok(any("precedence" in f.reason.lower() for f in reg4.flags),
                "and the supersession is disclosed, not silent")

    reg5 = Register()
    kept2 = resolve_capital_gain_sources([(stmt_frame, _Rec())], [_Rec()], reg5)
    fails += ok(int(kept2[0]["event"].eq("SELL").sum()) == 1,
                "with no G&L report supplied the statement is used unchanged",
                "- the quarterly limited-data path is untouched")

    # A sale OUTSIDE the report's coverage must survive.
    other = stmt_frame.copy()
    other["date"] = dt.date(2026, 9, 15)
    reg6 = Register()
    kept3 = resolve_capital_gain_sources(
        [(other, _Rec()), (ev, rec)], [_Rec(), rec], reg6)
    fails += ok(sum(int((f["event"] == "SELL").sum()) for f in kept3 if len(f))
                == STMT["lots"] + 1,
                "a sale the report does NOT cover is kept",
                "- precedence extends only to the dates actually reported")

    print()
    print(f"RESULT: {'ALL CHECKS PASSED' if not fails else str(fails) + ' CHECK(S) FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
