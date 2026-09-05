"""UBS regression against the statements' own printed figures.

UBS brings three things no earlier broker did:

  1. FIVE monthly statements inside ONE downloaded PDF, each with its own
     as-at date. Read as a single statement, four fifths of the year vanishes.
  2. A Form 1042-S that prints the DATE of every dividend payment. The
     aggregate period-end basis exists because dates are usually missing - when
     they are stated, each payment converts at its own date.
  3. A stock option exercise, where the broker's own "capital gain" includes the
     perquisite already taxed as salary. Same trap as the E*TRADE G&L Expanded
     ordinary-vs-adjusted columns, in a different shape.

Every figure asserted here is printed on the statements.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.compute import ComputeOptions, build_cg, build_lots      # noqa: E402
from src.fx import FXTable                                        # noqa: E402
from src.ingest.extract import extract_file                       # noqa: E402
from src.ingest.profiles import load_profiles                     # noqa: E402
from src.models import Period                                     # noqa: E402
from src.review import Register, Severity                         # noqa: E402
from src.run import (                                             # noqa: E402
    flag_equity_plan_evidence, ingest_documents, resolve_positions,
)

SRC = Path("/home/claude/brokers/RSU brokers/UBS")
PERIOD = Period(dt.date(2025, 1, 1), dt.date(2025, 12, 31))

STMT_PDF = SRC / "DHOnlineStatementsDisplayServlet.pdf"          # Sep 25 - Jan 26
STMT_PDF2 = SRC / "DHOnlineStatementsDisplayServlet (2).pdf"     # Apr 25 - Aug 25
F1042 = SRC / "DHOnlineStatementsDisplayServlet (1).pdf"         # 2025 Form 1042-S

# ---- printed on the statements -------------------------------------------
DEC = {"shares": 7.0, "acquired": dt.date(2025, 4, 1), "cost_per_share": 151.300,
       "cost_total": 1059.10, "close": 173.490, "value": 1214.43, "cash": 0.00}
# Form 1042-S detail schedule, income code 06
DIVIDENDS = [(dt.date(2025, 2, 11), 42.16, 10.54),
             (dt.date(2025, 5, 13), 51.68, 12.92),
             (dt.date(2025, 8, 12), 19.04, 4.76),
             (dt.date(2025, 11, 12), 31.24, 7.81)]
DIV_SUBTOTAL = (144.12, 36.03)          # the form's own Subtotal line
DIV_FORM_FACE = (144.00, 36.00)         # box 2 / box 7a, rounded to dollars
# Closed lots in 2025, from the monthly realised gain/loss tables
CLOSED_2025 = [
    (dt.date(2025, 5, 15), 7.0, dt.date(2024, 7, 1), 1314.15, 1157.87),
    (dt.date(2025, 5, 15), 6.0, dt.date(2024, 10, 1), 1126.42, 1028.16),
    (dt.date(2025, 5, 15), 7.0, dt.date(2025, 1, 2), 1314.15, 1112.37),
    (dt.date(2025, 5, 15), 5.0, dt.date(2023, 4, 3), 938.68, 782.70),
    (dt.date(2025, 5, 15), 6.0, dt.date(2023, 7, 3), 1126.42, 916.80),
    (dt.date(2025, 7, 8), 27.0, dt.date(2025, 7, 8), 5855.92, 4700.70),
    (dt.date(2025, 7, 8), 40.0, dt.date(2025, 7, 8), 8675.43, 6696.80),
    (dt.date(2025, 12, 30), 7.0, dt.date(2025, 7, 1), 1225.91, 1252.23),
    (dt.date(2025, 12, 30), 8.0, dt.date(2025, 10, 1), 1401.05, 1226.72),
]
# "Company sponsored stock plan(s) - Realized gains", July statement.
# The grant/reference numbers below must match the literal text printed in
# the real statement PDF byte-for-byte, so they live in a local, gitignored
# support file (clients/CLIENT_UBS_01/exercise_refs.json) rather than in
# committed source - see claude/rsu-fa-github-privacy-review.md. Everything
# else about this test (figures, logic, expected results) is unchanged.
_EXERCISE_REF_FILE = ROOT / "clients" / "CLIENT_UBS_01" / "exercise_refs.json"
_EXERCISE_FIGURES = [(27.0, 4700.70, 5860.02, 1159.32),
                      (40.0, 6696.80, 8681.52, 1984.72)]


def _load_exercise_refs() -> list[str]:
    if not _EXERCISE_REF_FILE.exists():
        raise SystemExit(
            f"missing local fixture: {_EXERCISE_REF_FILE}\n"
            "This test needs the real UBS exercise reference numbers "
            "(as printed on the statement), which are never committed to "
            "source control. Create the file with:\n"
            '  {"refs": ["<first exercise reference>", '
            '"<second exercise reference>"]}')
    refs = json.loads(_EXERCISE_REF_FILE.read_text())["refs"]
    if len(refs) != len(_EXERCISE_FIGURES):
        raise SystemExit(f"{_EXERCISE_REF_FILE} must list exactly "
                          f"{len(_EXERCISE_FIGURES)} reference numbers, "
                          f"got {len(refs)}")
    return refs


EXERCISES = [(ref, *figs)
             for ref, figs in zip(_load_exercise_refs(), _EXERCISE_FIGURES)]
PERQUISITE = 3144.04                    # the two exercises' stated realised gain
UBS_JULY_CAPITAL_GAIN = 3133.85         # what UBS calls the capital gain
YTD_AUG = {"short": 3590.17, "long": 365.60, "total": 3955.77}
CASH = {dt.date(2025, 4, 30): 80.58, dt.date(2025, 5, 30): 0.00,
        dt.date(2025, 8, 29): 14.28, dt.date(2025, 9, 30): 14.28,
        dt.date(2025, 10, 31): 14.28, dt.date(2025, 11, 28): 37.71,
        dt.date(2025, 12, 31): 0.00}
PLAN_VALUE_JAN26 = 99995.53             # options 11,835.58 + RSUs 88,159.95
INDIAN_LTCG_DAYS = 730


def ok(flag, label, detail=""):
    print(f"  {'PASS' if flag else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return 0 if flag else 1


def main() -> int:
    fails = 0
    profiles = load_profiles()

    print("=" * 98)
    print("1. DOCUMENT DETECTION, DUPLICATES, AND FIVE STATEMENTS PER FILE")
    print("=" * 98)
    reg = Register()
    events, sources, _, cash_rows = ingest_documents(SRC, reg, PERIOD)
    fails += ok(len(list(SRC.iterdir())) == 4 and len(sources) == 3,
                "4 files supplied, 3 counted",
                "- two of them are byte-identical copies of the 1042-S")
    by_id = {s.profile_id for s in sources}
    fails += ok(by_id == {"ubs_investment_account", "ubs_form_1042s"},
                "profiles used", ", ".join(sorted(by_id)))
    fails += ok(all(s.confidence >= 0.99 for s in sources),
                "every file profiled at 100% confidence")
    fails += ok(all(s.broker == "UBS" for s in sources), "broker UBS on every file")

    stmts = sorted((s for s in sources if s.profile_id == "ubs_investment_account"),
                   key=lambda s: s.period_start)
    for s in stmts:
        print(f"        {s.file[:44]:<46} {s.coverage}")
    fails += ok(len(stmts) == 2
                and stmts[0].period_start == dt.date(2025, 4, 1)
                and stmts[1].period_end == dt.date(2026, 1, 31),
                "each combined PDF reports the WHOLE span it contains",
                "- 5 monthly statements per file, not just the first")
    fails += ok(any("Duplicate" in f.reason for f in reg.flags),
                "the byte-identical copy is detected and ignored")

    print()
    print("=" * 98)
    print("2. PERIOD-END HOLDING - LOT LEVEL, WITH THE LOT'S OWN TRADE DATE")
    print("=" * 98)
    reg2 = Register()
    resolved = resolve_positions(events, PERIOD, reg2)
    lots, matches = build_lots(resolved, Register(), as_at=PERIOD.end)
    held = [l for l in lots if l.remaining > 1e-9]
    fails += ok(len(held) == 1, f"{len(held)} lot held at the period end")
    if held:
        l = held[0]
        fails += ok(abs(l.remaining - DEC["shares"]) < 0.001, "quantity",
                    f"{l.remaining:,.0f} vs December statement {DEC['shares']:,.0f}")
        fails += ok(l.acquired == DEC["acquired"],
                    "acquisition date is the LOT's trade date, not the statement date",
                    f"{l.acquired:%d-%m-%Y} - the December statement is dated 31-12-2025")
        fails += ok(abs(l.price_fc - DEC["cost_per_share"]) < 0.001, "cost per share",
                    f"{l.price_fc:,.3f} vs statement {DEC['cost_per_share']:,.3f}")
        fails += ok(abs(l.remaining * l.price_fc - DEC["cost_total"]) < 0.02,
                    "cost basis", f"{l.remaining * l.price_fc:,.2f} vs statement "
                    f"{DEC['cost_total']:,.2f}")
    print(f"        statement value {DEC['shares']:,.0f} x ${DEC['close']:.3f} = "
          f"${DEC['shares'] * DEC['close']:,.2f} vs stated ${DEC['value']:,.2f}")
    fails += ok(abs(DEC["shares"] * DEC["close"] - DEC["value"]) < 0.01,
                "and the statement's own value ties to its own price")

    print()
    print("=" * 98)
    print("3. CASH FOR FA-A2 - ONE BALANCE PER STATEMENT, EACH AT ITS OWN DATE")
    print("=" * 98)
    got = {r["date"]: round(float(r["balance_fc"]), 2) for _, r in
           pd.DataFrame(cash_rows).iterrows()} if len(cash_rows) else {}
    for d, expect in sorted(CASH.items()):
        fails += ok(abs(got.get(d, -1) - expect) < 0.01, f"{d:%d-%m-%Y} closing cash",
                    f"{got.get(d, 0):,.2f} vs statement {expect:,.2f}")
    in_period = {d: v for d, v in got.items() if d <= PERIOD.end}
    fails += ok(abs(max(in_period.values()) - 80.58) < 0.01,
                "A2 peak balance", f"${max(in_period.values()):,.2f}")
    fails += ok(abs(in_period[PERIOD.end] - 0.00) < 0.01,
                "A2 closing balance", "$0.00 - the account was emptied")

    print()
    print("=" * 98)
    print("4. FORM 1042-S - EVERY PAYMENT CARRIES ITS OWN DATE")
    print("=" * 98)
    f_ev, f_rec, _, _ = extract_file(F1042, profiles)
    div = f_ev[f_ev.event == "DIV"] if len(f_ev) else pd.DataFrame()
    fails += ok(len(div) == 4, f"{len(div)} dated payments extracted")
    for d, gross, tax in DIVIDENDS:
        row = div[div["date"] == d]
        good = (len(row) == 1 and abs(float(row.iloc[0]["amount_fc"]) - gross) < 0.01
                and abs(float(row.iloc[0]["tax_fc"]) - tax) < 0.01)
        fails += ok(good, f"{d:%d-%m-%Y}", f"${gross:,.2f} gross / ${tax:,.2f} withheld")
    g, t = float(div.amount_fc.sum()), float(div.tax_fc.sum())
    fails += ok(abs(g - DIV_SUBTOTAL[0]) < 0.01 and abs(t - DIV_SUBTOTAL[1]) < 0.01,
                "the detail rows tie to the form's own Subtotal",
                f"${g:,.2f} / ${t:,.2f}")
    fails += ok(abs(t / g - 0.25) < 0.0005, "withholding is the 25% treaty rate",
                f"{t / g:.2%}")
    fails += ok(abs(g - DIV_FORM_FACE[0]) > 0.10,
                "the detail beats the form face, which is rounded to whole dollars",
                f"detail ${g:,.2f} vs box 2 ${DIV_FORM_FACE[0]:,.2f}")
    fails += ok((f_rec.period_start, f_rec.period_end)
                == (dt.date(2025, 1, 1), dt.date(2025, 12, 31)),
                "an annual form covers its whole calendar year", f_rec.coverage)

    print()
    print("=" * 98)
    print("5. CLOSED LOTS - EVERY 2025 DISPOSAL THE STATEMENTS STATE")
    print("=" * 98)
    sells = events[(events.event == "SELL") & (events.date <= PERIOD.end)]
    fails += ok(len(sells) == len(CLOSED_2025),
                f"{len(sells)} closed lots vs {len(CLOSED_2025)} on the statements")
    for sold, qty, acq, proceeds, cost in CLOSED_2025:
        row = sells[(sells.date == sold) & (abs(sells.quantity - qty) < 1e-6)
                    & (sells.acquired_on.astype(str) == acq.isoformat())]
        good = (len(row) == 1
                and abs(float(row.iloc[0]["amount_fc"]) - proceeds) < 0.01)
        fails += ok(good, f"{sold:%d-%m-%Y}  {qty:>5,.0f} sh acquired {acq:%d-%m-%Y}",
                    f"proceeds ${proceeds:,.2f}")
    fails += ok((sells.quantity > 0).all(),
                "a disposal printed with a negative quantity is read as a positive one")

    print()
    print("=" * 98)
    print("6. OPTION EXERCISE - PERQUISITE FIRST, CAPITAL GAIN SECOND")
    print("=" * 98)
    ex_rec = next(s for s in sources
                  if s.profile_id == "ubs_investment_account" and s.exercises)
    fails += ok(len(ex_rec.exercises) == 2, f"{len(ex_rec.exercises)} exercises stated")
    for grant, qty, opt_cost, fmv, gain in EXERCISES:
        e = next((x for x in ex_rec.exercises if x["grant_no"] == grant), None)
        good = (e and abs(e["quantity"] - qty) < 1e-6
                and abs(e["option_cost_fc"] - opt_cost) < 0.01
                and abs(e["fmv_at_exercise_fc"] - fmv) < 0.01)
        fails += ok(good, f"{grant}  {qty:,.0f} sh",
                    f"option cost ${opt_cost:,.2f}, value at exercise ${fmv:,.2f}")
    print()
    print(f"  option cost                 ${sum(e[2] for e in EXERCISES):>10,.2f}")
    print(f"  value at time of exercise   ${sum(e[3] for e in EXERCISES):>10,.2f}")
    print(f"  perquisite (salary)         ${PERQUISITE:>10,.2f}   <- already taxed")
    ex_sells = sells[sells.date == dt.date(2025, 7, 8)]
    used_cost = float(ex_sells.cost_fc.astype(float).sum())
    proceeds = float(ex_sells.amount_fc.astype(float).sum())
    fails += ok(abs(used_cost - sum(e[3] for e in EXERCISES)) < 0.02,
                "the engine uses the VALUE AT EXERCISE as cost of acquisition",
                f"${used_cost:,.2f} not ${sum(e[2] for e in EXERCISES):,.2f}")
    engine_gain = proceeds - used_cost
    print(f"  UBS 'capital gain'          ${UBS_JULY_CAPITAL_GAIN:>10,.2f}   WRONG "
          "- it contains the perquisite")
    print(f"  Indian capital gain         ${engine_gain:>10,.2f}   correct "
          "- proceeds less value at exercise")
    fails += ok(abs((UBS_JULY_CAPITAL_GAIN - engine_gain) - PERQUISITE) < 0.05,
                "the gap between the two IS the perquisite",
                f"${UBS_JULY_CAPITAL_GAIN - engine_gain:,.2f}")
    fails += ok(engine_gain < 0,
                "the real capital result is a small LOSS - the brokerage",
                f"${engine_gain:,.2f}")
    reg3 = Register()
    flag_equity_plan_evidence(sources, reg3, PERIOD)
    perq_flags = [f for f in reg3.flags if "perquisite" in f.reason.lower()]
    fails += ok(len(perq_flags) == 1 and perq_flags[0].severity == Severity.REVIEW,
                "and the perquisite is raised for review, not quietly dropped")
    fails += ok(f"{PERQUISITE:,.2f}" in perq_flags[0].detail,
                "naming the amount that belongs in salary income",
                f"${PERQUISITE:,.2f}")

    print()
    print("=" * 98)
    print("7. INDIAN HOLDING PERIOD - NOT THE BROKER'S US LABEL")
    print("=" * 98)
    fx = FXTable(ROOT / "config" / "fx_rates.csv", register=Register())
    cg = build_cg(matches, fx, ComputeOptions(), PERIOD, Register())
    terms = {}
    for _, r in cg.iterrows():
        a = r["Date of Acquisition"]
        if a:
            terms[(pd.Timestamp(a).date(), r["Quantity"])] = r["Nature of Gain"]
    long_apr23 = terms.get((dt.date(2023, 4, 3), 5.0))
    long_jul23 = terms.get((dt.date(2023, 7, 3), 6.0))
    print(f"  acquired 03-04-2023, sold 15-05-2025 -> 773 days -> {long_apr23}")
    print(f"  acquired 03-07-2023, sold 15-05-2025 -> 682 days -> {long_jul23}")
    fails += ok(long_apr23 == "Long Term" and long_jul23 == "Short Term",
                "UBS puts BOTH in its long-term table; India splits them",
                "- 682 days fails the 24-month test")
    print(f"        UBS states long-term realised gains of ${YTD_AUG['long']:,.2f} "
          "for the year to August; part of that is short term for Indian tax")

    print()
    print("=" * 98)
    print("8. UNVESTED AWARDS ARE DISCLOSED, NEVER TREATED AS HELD")
    print("=" * 98)
    award_flags = [f for f in reg3.flags if "Unvested awards" in f.reason]
    fails += ok(len(award_flags) >= 1, "raised as a disclosure note")
    fails += ok(all("NOT reported in Schedule FA" in f.detail for f in award_flags),
                "saying plainly that they are not reported")
    a3_symbols = {l.symbol for l in held}
    fails += ok(len(held) == 1 and abs(sum(l.remaining for l in held) - 7.0) < 1e-6,
                "Schedule FA carries only the 7 shares actually owned",
                f"not the 409 unvested units worth ${PLAN_VALUE_JAN26:,.2f} "
                "of plan value")
    print("        the statement itself says the plan value is NOT part of the "
          "account value")

    print()
    print("=" * 98)
    print("9. GENERIC vs PROFILED")
    print("=" * 98)
    for label, path in (("statements Sep-Jan", STMT_PDF),
                        ("statements Apr-Aug", STMT_PDF2),
                        ("Form 1042-S", F1042)):
        pev, prec, _, _ = extract_file(path, profiles)
        gev, grec, _, _ = extract_file(path, [])
        print(f"  {label:<20} profiled {len(pev):>3} rows ({prec.profile_id:<24}) "
              f"generic {len(gev):>3} rows, broker {grec.broker}, "
              f"conf {grec.confidence:.0%}")
        fails += ok(len(gev) == 0,
                    f"{label}: generic invents nothing from a laid-out PDF")
        fails += ok(grec.broker == "UBS",
                    f"{label}: generic still identifies the institution")
    print("        A UBS statement is a laid-out PDF with no extractable tables, so")
    print("        the generic pass yields no rows - but it names the broker, so an")
    print("        unprofiled UBS file is flagged for mapping, never mis-attributed.")

    print()
    print(f"RESULT: {'ALL CHECKS PASSED' if not fails else str(fails) + ' CHECK(S) FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
