"""Permanent tests for the dividend coverage rule and generic/profile parity."""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.compute import build_dividends, coverage_gaps          # noqa: E402
from src.fx import FXTable                                      # noqa: E402
from src.ingest.extract import extract_file                     # noqa: E402
from src.ingest.profiles import load_profiles                   # noqa: E402
from src.models import Period                                   # noqa: E402
from src.review import Register                                 # noqa: E402

D = dt.date
PERIOD = Period(D(2025, 1, 1), D(2025, 12, 31))


def _events():
    return pd.DataFrame([
        {"date": D(2025, 3, 14), "broker": "B", "account_no": "1", "symbol": "X",
         "event": "DIV", "quantity": "", "price_fc": "", "amount_fc": 100.0,
         "tax_fc": 25.0, "cost_fc": "", "currency": "USD", "notes": ""},
        {"date": D(2025, 9, 12), "broker": "B", "account_no": "1", "symbol": "X",
         "event": "DIV", "quantity": "", "price_fc": "", "amount_fc": 100.0,
         "tax_fc": 25.0, "cost_fc": "", "currency": "USD", "notes": ""},
    ])


def _f1042():
    return pd.DataFrame([{
        "tax_year": 2025, "broker": "B", "payer": "X Corp", "income_code": "06",
        "gross_income_fc": 400.0, "tax_withheld_fc": 100.0, "currency": "USD",
        "source_file": "1042s.pdf", "notes": ""}])


CASES = [
    ("complete period coverage",
     [(D(2025, 1, 1), D(2025, 12, 31))], "Transaction-level"),
    ("Q4 only - partial coverage",
     [(D(2025, 10, 1), D(2025, 12, 31))], "Form 1042-S"),
    ("four complete quarters",
     [(D(2025, 1, 1), D(2025, 3, 31)), (D(2025, 4, 1), D(2025, 6, 30)),
      (D(2025, 7, 1), D(2025, 9, 30)), (D(2025, 10, 1), D(2025, 12, 31))],
     "Transaction-level"),
    ("missing a quarter",
     [(D(2025, 1, 1), D(2025, 3, 31)), (D(2025, 7, 1), D(2025, 12, 31))],
     "Form 1042-S"),
    ("no transaction coverage at all", [], "Form 1042-S"),
]


def dividend_coverage() -> int:
    print("=" * 92)
    print("DIVIDEND COVERAGE PRECEDENCE  (period 01-01-2025 to 31-12-2025)")
    print("=" * 92)
    fx = FXTable(ROOT / "config" / "fx_rates.csv")
    fails = 0
    for name, intervals, expected in CASES:
        reg = Register()
        div, f1042, fsi, basis = build_dividends(
            _events(), _f1042(), fx, PERIOD, reg, txn_intervals=intervals)
        fsi_gross = float(fsi["Gross Income (Rs.)"].sum()) if not fsi.empty else 0.0
        txn_gross = float(div["Gross Dividend (Rs.)"].sum()) if not div.empty else 0.0
        f_gross = float(f1042["Gross Dividend Income (Rs.)"].sum()) if not f1042.empty else 0.0
        expected_gross = txn_gross if expected == "Transaction-level" else f_gross

        ok = basis == expected and abs(fsi_gross - expected_gross) < 1.0
        # Supporting rows must survive even when the 1042-S wins.
        rows_kept = len(div) == 2
        no_double = abs(fsi_gross - (txn_gross + f_gross)) > 1.0
        ok = ok and rows_kept and no_double
        fails += 0 if ok else 1
        gaps = coverage_gaps(intervals, PERIOD)
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<32} -> {basis:<18} "
              f"FSI {fsi_gross:>12,.0f}  txn rows kept {len(div)}  "
              f"gaps {len(gaps)}")
    print("  (no double count: FSI never equals transaction + 1042-S combined)")
    return fails


def threshold_removed() -> int:
    print()
    print("=" * 92)
    print("THE OLD 95% THRESHOLD MUST NOT EXIST ANYWHERE")
    print("=" * 92)
    hits = subprocess.run(
        ["grep", "-rn", "-e", "COVERAGE_THRESHOLD", "-e", "0[.]95", "--include=*.py",
         "--include=*.yaml", "--exclude=test_coverage_and_parity.py",
         "src", "config", "tests"],
        cwd=ROOT, capture_output=True, text=True).stdout.strip()
    ok = not hits
    print(f"  {'PASS' if ok else 'FAIL'}  "
          f"{'no threshold logic found' if ok else hits}")
    return 0 if ok else 1


def generic_parity() -> int:
    print()
    print("=" * 92)
    print("GENERIC vs PROFILED EXTRACTION - Morgan Stanley")
    print("=" * 92)
    pdf = Path("/home/claude/brokers/RSU brokers/Morgan/morgan statement.pdf")
    prof_ev, prof_rec, _, _ = extract_file(pdf, load_profiles())
    gen_ev, gen_rec, _, _ = extract_file(pdf, [])          # profile disabled

    print(f"  profiled : {len(prof_ev):>3} events, broker {prof_rec.broker}, "
          f"{len(prof_rec.holdings)} holding lots, confidence {prof_rec.confidence:.0%}")
    print(f"  generic  : {len(gen_ev):>3} events, broker {gen_rec.broker}, "
          f"{len(gen_rec.holdings)} holding lots, confidence {gen_rec.confidence:.0%}")
    print("  Explanation: the generic pass reads TABLES. A laid-out PDF has none,")
    print("  so it correctly extracts nothing and flags the document for review")
    print("  rather than inventing rows. The profile supplies the row regexes.")

    # The generic path must still work on a tabular source after the PDF profile exists.
    xlsx = Path("/home/claude/brokers/RSU brokers/Computershare/"
                "PortfolioDetails_CLIENT_COMPUTERSHARE_01.xlsx")
    a, _, _, _ = extract_file(xlsx, load_profiles())
    b, _, _, _ = extract_file(xlsx, [])
    ok = len(a) == len(b) == 35 and set(a.symbol) == set(b.symbol)
    print(f"  {'PASS' if ok else 'FAIL'}  generic adapter still intact: "
          f"Computershare profiled {len(a)} rows vs generic {len(b)} rows, "
          f"symbols {sorted(set(b.symbol))}")
    # The statement never names Morgan Stanley anywhere in its text - only
    # "Amazon.com Inc." and an MS- account prefix. UNKNOWN_BROKER is therefore
    # the correct answer for the generic pass: inferring the institution from an
    # account-number prefix would be exactly the invented name the rules forbid.
    ok2 = gen_rec.broker == "UNKNOWN_BROKER" and len(gen_ev) == 0
    print(f"  {'PASS' if ok2 else 'FAIL'}  generic reports {gen_rec.broker} and "
          f"invents nothing (the PDF never names the institution)")

    print()
    print("=" * 92)
    print("GENERIC vs PROFILED EXTRACTION - Charles Schwab")
    print("=" * 92)
    schwab = Path("/home/claude/brokers/RSU brokers/Charles schwab")
    dec = schwab / "Brokerage Statement_2025-12-31_527.PDF"
    ye = schwab / "Year-End Summary - 2025_2026-02-06_527.PDF"
    rows = []
    for label, f in (("Dec statement", dec), ("Year-End Summary", ye)):
        pev, prec, _, _ = extract_file(f, load_profiles())
        gev, grec, _, _ = extract_file(f, [])
        rows.append((label, len(pev), prec.profile_id, len(gev), grec.broker,
                     grec.confidence))
        print(f"  {label:<18} profiled {len(pev):>3} rows ({prec.profile_id})  "
              f"generic {len(gev):>3} rows, broker {grec.broker}, "
              f"conf {grec.confidence:.0%}")
    print("  Explanation: Schwab statements are laid-out PDFs with no extractable")
    print("  tables, so the generic pass yields nothing - but unlike Morgan it DOES")
    print("  name the institution, so the broker is identified without a profile.")
    ok3 = all(r[3] == 0 for r in rows) and all(r[4] == "SCHWAB" for r in rows)
    print(f"  {'PASS' if ok3 else 'FAIL'}  generic identifies SCHWAB from the text "
          f"and invents no transactions")
    return 0 if (ok and ok2 and ok3) else 1


if __name__ == "__main__":
    f = dividend_coverage() + threshold_removed() + generic_parity()
    print()
    print(f"RESULT: {'ALL PASSED' if not f else str(f) + ' FAILED'}")
    sys.exit(0 if not f else 1)
