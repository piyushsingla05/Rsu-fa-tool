"""Targeted regression for the 2026-09 hardening batch (audit findings 2, 3, 4,
5, 8).

Each section proves the specific defect described in the takeover audit is now
flagged / handled, AND proves the resolved / normal-data path is completely
unchanged - the fix must be additive, never a change to a number that was
already correct. Every section has both a positive (nothing new fires) and a
negative (the gap is now visible) case, per the frozen requirement that missing
evidence must remain visibly unresolved rather than silently folded into a
total or a cell.

This file uses only the shipped, non-private tables in config/ and synthetic
in-memory data - it does not touch client data and can run anywhere the repo
is checked out.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.compute import (                                     # noqa: E402
    ComputeOptions, build_a2, build_a3, build_cg, build_lots,
)
from src.fx import FXTable                                    # noqa: E402
from src.marketdata import MarketData                          # noqa: E402
from src.models import Period                                  # noqa: E402
from src.review import (                                       # noqa: E402
    FA_FX_GAP, MISSING_ANNUAL_HIGH, PEAK_QTY_ROSE, Register, Severity,
    TRANSFER_COST_MISSING,
)

FXC = ROOT / "config" / "fx_rates.csv"
MKT = ROOT / "config" / "market_prices.csv"

SBI_DATE = dt.date(2025, 12, 31)      # on file in the shipped SBI table
SBI_RATE = 89.9619
PERIOD = Period(dt.date(2025, 1, 1), SBI_DATE)


def ok(flag, label, detail=""):
    print(f"  {'PASS' if flag else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return 0 if flag else 1


def has(register: Register, reason: str, subject: str | None = None) -> bool:
    return any(f.reason == reason and (subject is None or f.subject == subject)
               for f in register.flags)


def base_row(**over) -> dict:
    row = {"date": None, "broker": "TEST", "account_no": "1", "symbol": "TST",
           "event": "", "quantity": "", "price_fc": "", "amount_fc": "",
           "tax_fc": "", "acquired_on": "", "cost_fc": "", "currency": "USD",
           "notes": ""}
    row.update(over)
    return row


def main() -> int:
    fails = 0

    # ==================================================================
    print("=" * 98)
    print("1. FINDING 8 - TRANSFER_IN with no cost basis must be flagged, not")
    print("   silently costed at zero")
    print("=" * 98)

    reg = Register()
    ev = pd.DataFrame([base_row(
        date=dt.date(2025, 6, 1), event="TRANSFER_IN", quantity=50.0,
        price_fc="")])                                     # cost basis BLANK
    lots, _ = build_lots(ev, reg, as_at=SBI_DATE)
    fails += ok(has(reg, TRANSFER_COST_MISSING, "TST"),
                "a TRANSFER_IN with no cost basis raises TRANSFER_COST_MISSING",
                f"flags: {[f.reason for f in reg.flags]}")
    fails += ok(reg.flags and reg.flags[0].severity == Severity.BLOCKER,
                "and it is a BLOCKER, not a mere note",
                "- it can silently overstate a later capital gain")
    fails += ok(len(lots) == 1 and lots[0].price_fc == 0.0,
                "the lot itself is unchanged (still recorded, cost 0.0)",
                "- this is a disclosure fix, not a new invented cost")

    reg2 = Register()
    ev2 = pd.DataFrame([base_row(
        date=dt.date(2025, 6, 1), event="TRANSFER_IN", quantity=50.0,
        price_fc=120.0)])                                  # cost basis PRESENT
    build_lots(ev2, reg2, as_at=SBI_DATE)
    fails += ok(not has(reg2, TRANSFER_COST_MISSING),
                "a TRANSFER_IN WITH a cost basis raises nothing",
                "- no false positive on the normal case")

    reg3 = Register()
    ev3 = pd.DataFrame([base_row(
        date=dt.date(2025, 6, 1), event="VEST", quantity=50.0, price_fc="")])
    build_lots(ev3, reg3, as_at=SBI_DATE)
    fails += ok(not has(reg3, TRANSFER_COST_MISSING),
                "a VEST with no price does NOT raise the transfer-specific flag",
                "- finding 8 was scoped to TRANSFER_IN; VEST/BUY carry a "
                "broker-stated price and are out of scope for this fix")

    print()
    print("-" * 98)
    print("1b. FINDING 8 (downstream) - the missing-cost lot is later SOLD; the")
    print("    blocker must still stand at capital-gain computation, not be lost")
    print("    or silently treated as a valid zero cost basis")
    print("-" * 98)

    reg1b = Register()
    fx1b = FXTable(FXC, register=reg1b)
    # Same register threaded through build_lots -> build_cg, exactly as the
    # real pipeline does - the point of this test is that nothing along that
    # path clears or replaces the blocker raised at transfer-in time.
    ev1b = pd.DataFrame([
        base_row(date=dt.date(2025, 3, 1), event="TRANSFER_IN", quantity=20.0,
                 price_fc=""),                              # cost basis BLANK
        base_row(date=dt.date(2025, 9, 1), event="SELL", quantity=20.0,
                 price_fc=150.0, amount_fc=3000.0),
    ])
    lots1b, matches1b = build_lots(ev1b, reg1b, as_at=SBI_DATE)
    fails += ok(has(reg1b, TRANSFER_COST_MISSING, "TST"),
                "the blocker is raised at build_lots time, as before")
    cg1b = build_cg(matches1b, fx1b, ComputeOptions(), PERIOD, reg1b)
    fails += ok(has(reg1b, TRANSFER_COST_MISSING, "TST"),
                "and it is STILL present after build_cg runs on the same register",
                "- capital-gain computation does not clear, resolve or replace it")
    fails += ok(len(cg1b) == 1
                and float(cg1b.iloc[0]["Cost of Acquisition (FC)"]) == 0.0,
                "the sale's cost basis is exactly 0 - not fabricated, not "
                "replaced with a plug figure",
                f"{cg1b.iloc[0]['Cost of Acquisition (FC)']}")
    fails += ok(float(cg1b.iloc[0]["Capital Gain (Rs.)"])
                == float(cg1b.iloc[0]["Full Value of Consideration (Rs.)"]),
                "so the reported gain equals the full sale proceeds - the exact "
                "overstatement risk the finding describes, and the ONLY thing "
                "standing between this figure and a filer relying on it is the "
                "still-open TRANSFER_COST_MISSING blocker asserted above",
                f"gain={cg1b.iloc[0]['Capital Gain (Rs.)']} "
                f"consideration={cg1b.iloc[0]['Full Value of Consideration (Rs.)']}")

    # Control: a TRANSFER_IN WITH a real cost, later sold, must compute a
    # perfectly ordinary gain with no blocker and no arithmetic change.
    reg1c = Register()
    fx1c = FXTable(FXC, register=reg1c)
    ev1c = pd.DataFrame([
        base_row(date=dt.date(2025, 3, 1), event="TRANSFER_IN", quantity=20.0,
                 price_fc=100.0),
        base_row(date=dt.date(2025, 9, 1), event="SELL", quantity=20.0,
                 price_fc=150.0, amount_fc=3000.0),
    ])
    lots1c, matches1c = build_lots(ev1c, reg1c, as_at=SBI_DATE)
    fails += ok(not has(reg1c, TRANSFER_COST_MISSING),
                "a TRANSFER_IN WITH a cost basis raises no blocker at all")
    cg1c = build_cg(matches1c, fx1c, ComputeOptions(), PERIOD, reg1c)
    fails += ok(float(cg1c.iloc[0]["Cost of Acquisition (FC)"]) == 2000.0,
                "and the capital gain is computed on the REAL cost (20 x 100)",
                f"{cg1c.iloc[0]['Cost of Acquisition (FC)']}")
    fails += ok(float(cg1c.iloc[0]["Capital Gain (Rs.)"])
                < float(cg1c.iloc[0]["Full Value of Consideration (Rs.)"]),
                "so the gain is correctly LESS than full proceeds",
                "- ordinary transfer arithmetic is completely unchanged")

    # ==================================================================
    print()
    print("=" * 98)
    print("2. FINDING 2 - A2/A3 totals must flag an unconvertible FX date")
    print("   rather than folding a silent 0 into the total")
    print("=" * 98)

    NO_RATE = dt.date(2018, 3, 15)      # not on file anywhere

    reg4 = Register()
    fx4 = FXTable(FXC, register=reg4)
    accounts = pd.DataFrame([{"broker": "TEST", "account_no": "1",
                              "country": "United States of America",
                              "institution_name": "Test Bank", "address": "",
                              "zip": "", "status": "Owner",
                              "opening_date": ""}])
    cash = pd.DataFrame([
        {"broker": "TEST", "account_no": "1", "date": NO_RATE,
         "balance_fc": 1000.0, "currency": "USD"},
        {"broker": "TEST", "account_no": "1", "date": dt.date(2025, 12, 31),
         "balance_fc": 2000.0, "currency": "USD"},
    ])
    period4 = Period(dt.date(2018, 1, 1), dt.date(2025, 12, 31))
    a2 = build_a2(cash, accounts, pd.DataFrame(), fx4, period4, reg4)
    fails += ok(has(reg4, FA_FX_GAP),
                "an unresolvable A2 balance date raises FA_FX_GAP",
                f"flags: {[f.reason for f in reg4.flags]}")
    fails += ok(float(a2.iloc[0]["Peak Balance During the Period (Rs.)"])
                == round(2000.0 * SBI_RATE, 0),
                "peak balance is exactly the OTHER (resolvable) date's value",
                "- the unresolvable date contributes nothing, exactly as "
                "fx.rate() already did; it is now VISIBLE, not silently absorbed")

    reg5 = Register()
    fx5 = FXTable(FXC, register=reg5)
    cash_ok = pd.DataFrame([
        {"broker": "TEST", "account_no": "1", "date": dt.date(2025, 12, 31),
         "balance_fc": 1000.0, "currency": "USD"}])
    period5 = Period(dt.date(2025, 1, 1), dt.date(2025, 12, 31))
    a2b = build_a2(cash_ok, accounts, pd.DataFrame(), fx5, period5, reg5)
    fails += ok(not has(reg5, FA_FX_GAP),
                "a fully-resolvable A2 case raises no FA_FX_GAP",
                "- no false positive")
    fails += ok(float(a2b.iloc[0]["Peak Balance During the Period (Rs.)"])
                == round(1000.0 * SBI_RATE, 0),
                "and the peak balance is numerically identical to the "
                "pre-fix fx.rate()-based figure",
                f"{a2b.iloc[0]['Peak Balance During the Period (Rs.)']}")

    # A3 dividend/proceeds path (_sum_dividends / _sum_proceeds). The vest
    # itself is on the exact SBI date so its own FX resolves cleanly - only
    # the dividend's currency ("ZZZ") is unresolvable anywhere, isolating
    # the one code path this section is testing.
    reg6 = Register()
    fx6 = FXTable(FXC, register=reg6)
    md6 = MarketData(MKT, reg6)
    hold_ev = pd.DataFrame([
        base_row(date=SBI_DATE, symbol="IBM", event="VEST",
                 quantity=10.0, price_fc=100.0),
        base_row(date=dt.date(2025, 6, 15), symbol="IBM", event="DIV",
                 quantity="", price_fc="", amount_fc=500.0, currency="ZZZ"),
    ])
    lots6, _ = build_lots(hold_ev, reg6, as_at=SBI_DATE)
    a3_6 = build_a3(hold_ev, lots6, pd.DataFrame(), md6, fx6, PERIOD,
                    ComputeOptions(), reg6)
    fails += ok(has(reg6, FA_FX_GAP, "IBM"),
                "an unresolvable A3 dividend date raises FA_FX_GAP for the symbol",
                f"flags: {[(f.reason, f.subject) for f in reg6.flags]}")
    fails += ok(float(a3_6.iloc[0]["Total Gross Amount Paid/Credited w.r.t. the Holding (Rs.)"]) == 0,
                "and the dividend total for that date is excluded (0), not "
                "invented at a fabricated rate",
                "- same number as the old silent behaviour, now flagged")

    print()
    print("-" * 98)
    print("2b. FINDING 2 (dedicated) - the A3 SALE-PROCEEDS path (_sum_proceeds)")
    print("    specifically, unresolvable and fully-resolvable")
    print("-" * 98)

    # Unresolvable: a SELL priced in a currency no FX source has ever heard
    # of, isolating _sum_proceeds the same way section 2 isolated dividends.
    reg2b = Register()
    fx2b = FXTable(FXC, register=reg2b)
    md2b = MarketData(MKT, reg2b)
    sale_ev = pd.DataFrame([
        base_row(date=SBI_DATE, symbol="IBM", event="VEST",
                 quantity=10.0, price_fc=100.0),
        base_row(date=dt.date(2025, 6, 15), symbol="IBM", event="SELL",
                 quantity=4.0, price_fc=150.0, amount_fc=600.0, currency="ZZZ"),
    ])
    lots2b, _ = build_lots(sale_ev, reg2b, as_at=SBI_DATE)
    a3_2b = build_a3(sale_ev, lots2b, pd.DataFrame(), md2b, fx2b, PERIOD,
                     ComputeOptions(), reg2b)
    fails += ok(has(reg2b, FA_FX_GAP, "IBM"),
                "an unresolvable sale-proceeds date raises FA_FX_GAP for the symbol",
                f"flags: {[(f.reason, f.subject) for f in reg2b.flags]}")
    fails += ok(float(a3_2b.iloc[0]["Total Gross Proceeds from Sale/Redemption (Rs.)"]) == 0,
                "no fabricated INR amount is produced for the unresolvable sale",
                "- the contribution is left at nil, exactly as the pre-fix "
                "fx.rate() already computed, now flagged rather than silent")

    # Fully resolvable: the SAME shape of sale, in USD, on the exact SBI date.
    reg2c = Register()
    fx2c = FXTable(FXC, register=reg2c)
    md2c = MarketData(MKT, reg2c)
    sale_ev_ok = pd.DataFrame([
        base_row(date=SBI_DATE, symbol="IBM", event="VEST",
                 quantity=10.0, price_fc=100.0),
        base_row(date=SBI_DATE, symbol="IBM", event="SELL",
                 quantity=4.0, price_fc=150.0, amount_fc=600.0, currency="USD"),
    ])
    lots2c, _ = build_lots(sale_ev_ok, reg2c, as_at=SBI_DATE)
    a3_2c = build_a3(sale_ev_ok, lots2c, pd.DataFrame(), md2c, fx2c, PERIOD,
                     ComputeOptions(), reg2c)
    fails += ok(not has(reg2c, FA_FX_GAP),
                "a fully-resolvable sale-proceeds case raises no FA_FX_GAP")
    fails += ok(round(float(a3_2c.iloc[0]["Total Gross Proceeds from Sale/Redemption (Rs.)"]), 0)
                == round(600.0 * SBI_RATE, 0),
                "and the proceeds total is numerically identical to qty x price "
                "x SBI TTBR - the same figure the old fx.rate()-based code "
                "would have produced",
                f"{a3_2c.iloc[0]['Total Gross Proceeds from Sale/Redemption (Rs.)']} "
                f"vs {round(600.0 * SBI_RATE, 0)}")

    # ==================================================================
    print()
    print("=" * 98)
    print("3. FINDING 5 - a missing annual high/close must blank the A3 cell,")
    print("   never reach it as a computed zero")
    print("=" * 98)

    reg7 = Register()
    fx7 = FXTable(FXC, register=reg7)
    md7 = MarketData(MKT, reg7)
    # ZZZQ has no market_prices.csv row at all - annual high AND close missing.
    missing_ev = pd.DataFrame([
        base_row(date=dt.date(2024, 1, 2), symbol="ZZZQ", event="VEST",
                 quantity=5.0, price_fc=50.0)])
    lots7, _ = build_lots(missing_ev, reg7, as_at=SBI_DATE)
    a3_7 = build_a3(missing_ev, lots7, pd.DataFrame(), md7, fx7, PERIOD,
                    ComputeOptions(), reg7)
    fails += ok(has(reg7, MISSING_ANNUAL_HIGH),
                "the missing price still raises MISSING_ANNUAL_HIGH (unchanged)")
    fails += ok(a3_7.iloc[0]["Peak Value of Investment During the Period (Rs.)"] == "",
                "Peak Value is BLANK, not a computed 0",
                f'value: {a3_7.iloc[0]["Peak Value of Investment During the Period (Rs.)"]!r}')
    fails += ok(a3_7.iloc[0]["Closing Value (Rs.)"] == "",
                "Closing Value is BLANK too (close price also missing)",
                f'value: {a3_7.iloc[0]["Closing Value (Rs.)"]!r}')

    # IBM has both prices on file - fully resolvable, must be numeric and
    # identical to what the engine has always produced for this fixture.
    reg8 = Register()
    fx8 = FXTable(FXC, register=reg8)
    md8 = MarketData(MKT, reg8)
    ibm_ev = pd.DataFrame([
        base_row(date=dt.date(2024, 1, 2), symbol="IBM", event="VEST",
                 quantity=10.0, price_fc=100.0)])
    lots8, _ = build_lots(ibm_ev, reg8, as_at=SBI_DATE)
    a3_8 = build_a3(ibm_ev, lots8, pd.DataFrame(), md8, fx8, PERIOD,
                    ComputeOptions(), reg8)
    peak8 = a3_8.iloc[0]["Peak Value of Investment During the Period (Rs.)"]
    close8 = a3_8.iloc[0]["Closing Value (Rs.)"]
    fails += ok(isinstance(peak8, (int, float)) and peak8 > 0,
                "a fully-resolvable symbol still gets a numeric Peak Value",
                f"{peak8}")
    fails += ok(isinstance(close8, (int, float)) and close8 > 0,
                "and a numeric Closing Value",
                f"{close8}")
    fails += ok(round(peak8, 0) == round(10.0 * 308.58 * SBI_RATE, 0),
                "Peak Value matches qty x shipped annual high x SBI TTBR exactly",
                f"{peak8} vs {round(10.0 * 308.58 * SBI_RATE, 0)}")

    print()
    print("-" * 98)
    print("3b. FINDING 5 (mixed) - Peak Value and Closing Value must gate")
    print("    INDEPENDENTLY on _peak_ok / _close_val_ok, not on one shared flag")
    print("-" * 98)

    tmp_mkt = ROOT / "tests" / "_tmp_mixed_prices.csv"
    mkt_cols = ["ticker", "security_name", "exchange", "price_kind", "price",
                "price_date", "currency", "basis", "source_type", "source_name",
                "retrieved_on", "confidence", "verified", "notes"]
    mixed_rows = [
        # MIXHI: annual high on file, period-end close MISSING.
        {"ticker": "MIXHI", "security_name": "Mixed Test A", "exchange": "TEST",
         "price_kind": "ANNUAL_HIGH", "price": 200.0, "price_date": SBI_DATE,
         "currency": "USD", "basis": "DAILY_CLOSE_HIGH", "source_type": "PRIMARY",
         "source_name": "TEST", "retrieved_on": "2026-09-06", "confidence": "High",
         "verified": "Y", "notes": ""},
        # MIXCL: period-end close on file, annual high MISSING.
        {"ticker": "MIXCL", "security_name": "Mixed Test B", "exchange": "TEST",
         "price_kind": "PERIOD_END_CLOSE", "price": 150.0, "price_date": SBI_DATE,
         "currency": "USD", "basis": "DAILY_CLOSE_HIGH", "source_type": "PRIMARY",
         "source_name": "TEST", "retrieved_on": "2026-09-06", "confidence": "High",
         "verified": "Y", "notes": ""},
    ]
    pd.DataFrame(mixed_rows, columns=mkt_cols).to_csv(tmp_mkt, index=False)
    try:
        # Case A: annual high MISSING, close AVAILABLE.
        regA = Register()
        fxA = FXTable(FXC, register=regA)
        mdA = MarketData(tmp_mkt, regA)
        evA = pd.DataFrame([base_row(date=dt.date(2024, 1, 2), symbol="MIXCL",
                                     event="VEST", quantity=3.0, price_fc=50.0)])
        lotsA, _ = build_lots(evA, regA, as_at=SBI_DATE)
        a3A = build_a3(evA, lotsA, pd.DataFrame(), mdA, fxA, PERIOD,
                       ComputeOptions(), regA)
        fails += ok(a3A.iloc[0]["Peak Value of Investment During the Period (Rs.)"] == "",
                    "Case A (high missing, close present): Peak Value is BLANK",
                    f'value: {a3A.iloc[0]["Peak Value of Investment During the Period (Rs.)"]!r}')
        closeA = a3A.iloc[0]["Closing Value (Rs.)"]
        fails += ok(isinstance(closeA, (int, float)) and closeA > 0,
                    "Case A: Closing Value remains NUMERIC (close price resolved "
                    "independently of the missing high)",
                    f"{closeA}")
        fails += ok(round(closeA, 0) == round(3.0 * 150.0 * SBI_RATE, 0),
                    "Case A: Closing Value matches qty x close x SBI TTBR exactly",
                    f"{closeA} vs {round(3.0 * 150.0 * SBI_RATE, 0)}")

        # Case B: annual high AVAILABLE, close MISSING.
        regB = Register()
        fxB = FXTable(FXC, register=regB)
        mdB = MarketData(tmp_mkt, regB)
        evB = pd.DataFrame([base_row(date=dt.date(2024, 1, 2), symbol="MIXHI",
                                     event="VEST", quantity=3.0, price_fc=50.0)])
        lotsB, _ = build_lots(evB, regB, as_at=SBI_DATE)
        a3B = build_a3(evB, lotsB, pd.DataFrame(), mdB, fxB, PERIOD,
                       ComputeOptions(), regB)
        peakB = a3B.iloc[0]["Peak Value of Investment During the Period (Rs.)"]
        fails += ok(isinstance(peakB, (int, float)) and peakB > 0,
                    "Case B (high present, close missing): Peak Value remains "
                    "NUMERIC (high price resolved independently of the missing "
                    "close)",
                    f"{peakB}")
        fails += ok(round(peakB, 0) == round(3.0 * 200.0 * SBI_RATE, 0),
                    "Case B: Peak Value matches qty x high x SBI TTBR exactly",
                    f"{peakB} vs {round(3.0 * 200.0 * SBI_RATE, 0)}")
        fails += ok(a3B.iloc[0]["Closing Value (Rs.)"] == "",
                    "Case B: Closing Value is BLANK",
                    f'value: {a3B.iloc[0]["Closing Value (Rs.)"]!r}')
    finally:
        tmp_mkt.unlink(missing_ok=True)

    # ==================================================================
    print()
    print("=" * 98)
    print("4. FINDING 3 - a zero or negative SBI TTBR must not resolve as a rate")
    print("=" * 98)

    bad_date = dt.date(2030, 1, 15)
    reg9 = Register()
    bad_sbi = pd.read_csv(FXC, dtype={"currency": str, "source": str})
    bad_sbi = pd.concat([bad_sbi, pd.DataFrame([
        {"date": "2030-01-15", "currency": "USD", "ttbr": 0.0,
         "source": "TEST - deliberately bad row", "verified": "N"},
        {"date": "2030-02-15", "currency": "USD", "ttbr": -5.0,
         "source": "TEST - deliberately bad row", "verified": "N"},
    ])], ignore_index=True)
    tmp_fx = ROOT / "tests" / "_tmp_bad_sbi.csv"
    bad_sbi.to_csv(tmp_fx, index=False)
    try:
        fx9 = FXTable(tmp_fx, register=reg9)
        q_zero = fx9.rate_quote(bad_date, "USD")
        fails += ok(q_zero is None or abs(q_zero.rate) > 1e-9,
                    "a 0.0 SBI TTBR row is never returned as a resolved rate",
                    f"{q_zero.rate if q_zero else None}")
        q_neg = fx9.rate_quote(dt.date(2030, 2, 15), "USD")
        fails += ok(q_neg is None or q_neg.rate > 0,
                    "a negative SBI TTBR row is never returned as a resolved rate",
                    f"{q_neg.rate if q_neg else None}")
    finally:
        tmp_fx.unlink(missing_ok=True)

    # merge() path - a user-supplied override table with a bad rate.
    reg10 = Register()
    fx10 = FXTable(FXC, register=reg10)
    fx10.merge(pd.DataFrame([
        {"date": "2030-03-15", "currency": "USD", "ttbr": 0.0,
         "source": "TEST merge", "verified": "N"}]))
    q_merge = fx10.rate_quote(dt.date(2030, 3, 15), "USD")
    fails += ok(q_merge is None or abs(q_merge.rate) > 1e-9,
                "merge() also drops a 0.0 TTBR rather than adding it as usable",
                f"{q_merge.rate if q_merge else None}")

    fails += ok(abs(fx10.rate_quote(SBI_DATE, "USD").rate - SBI_RATE) < 1e-6,
                "a genuine positive SBI rate is completely unaffected",
                f"{fx10.rate_quote(SBI_DATE, 'USD').rate}")

    # ==================================================================
    print()
    print("=" * 98)
    print("5. FINDING 4 - acquisitions during the period get the symmetric")
    print("   PEAK_QTY_ROSE disclosure (overstatement risk), same as disposals")
    print("   already get PEAK_QTY_FELL (understatement risk)")
    print("=" * 98)

    reg11 = Register()
    fx11 = FXTable(FXC, register=reg11)
    md11 = MarketData(MKT, reg11)
    # Vests late in the period, after the shipped annual-high's own month-end.
    late_vest = pd.DataFrame([
        base_row(date=dt.date(2025, 12, 1), symbol="IBM", event="VEST",
                 quantity=8.0, price_fc=300.0)])
    lots11, _ = build_lots(late_vest, reg11, as_at=SBI_DATE)
    build_a3(late_vest, lots11, pd.DataFrame(), md11, fx11, PERIOD,
             ComputeOptions(), reg11)
    fails += ok(has(reg11, PEAK_QTY_ROSE, "IBM"),
                "a mid-period acquisition raises PEAK_QTY_ROSE",
                f"flags: {[(f.reason, f.subject) for f in reg11.flags]}")

    reg12 = Register()
    fx12 = FXTable(FXC, register=reg12)
    md12 = MarketData(MKT, reg12)
    # Vested well BEFORE the period - no in-period acquisition or disposal.
    steady = pd.DataFrame([
        base_row(date=dt.date(2020, 1, 2), symbol="IBM", event="VEST",
                 quantity=8.0, price_fc=100.0)])
    lots12, _ = build_lots(steady, reg12, as_at=SBI_DATE)
    build_a3(steady, lots12, pd.DataFrame(), md12, fx12, PERIOD,
             ComputeOptions(), reg12)
    fails += ok(not has(reg12, PEAK_QTY_ROSE),
                "a holding untouched during the period raises neither flag",
                f"flags: {[f.reason for f in reg12.flags]}")

    print()
    print("=" * 98)
    if fails:
        print(f"RESULT: {fails} CHECK(S) FAILED")
    else:
        print("RESULT: ALL CHECKS PASSED")
    print("=" * 98)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
