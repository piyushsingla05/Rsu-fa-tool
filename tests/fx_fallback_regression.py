"""Universal FX fallback regression - SBI TTBR, then Google Finance, then nothing.

The rule this proves, for every broker and every conversion in the engine:

    1. SBI TT buying rate
    2. Google Finance historical FX, ONLY where SBI has no rate for the date
    3. any further configured provider
    4. no rate -> the INR figure is BLANK and raised as FX_UNAVAILABLE

Never a rate of zero. Never an unrelated period-end rate. Never an estimate. And
a Google Finance rate is never presented as though it were an SBI TTBR.

The Google rates used here are TEST VALUES defined in this file. They are not
written to any shipped table.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.compute import (                                     # noqa: E402
    ComputeOptions, build_a3, build_cg, build_dividends, build_lots,
)
from src.fx import FXTable                                    # noqa: E402
from src.fxsources import (                                   # noqa: E402
    FALLBACK_BANNER, GOOGLE_FINANCE, SBI_NOT_AVAILABLE, SBI_TTBR,
    google_formula, pair_symbol,
)
from src.marketdata import MarketData                         # noqa: E402
from src.models import Period                                 # noqa: E402
from src.review import FX_FALLBACK, Register, Severity                     # noqa: E402

FXC = ROOT / "config" / "fx_rates.csv"
MKT = ROOT / "config" / "market_prices.csv"

SBI_DATE = dt.date(2025, 12, 31)      # on file in the shipped SBI table
SBI_RATE = 89.9619                    # what the shipped table says for it

# Dates the SBI table does NOT cover, across several currencies and years.
GOOGLE_ROWS = [
    (dt.date(2019, 5, 1), "USD", 69.5820),
    (dt.date(2021, 8, 19), "USD", 74.2450),
    (dt.date(2022, 5, 2), "USD", 76.5150),
    (dt.date(2023, 6, 30), "GBP", 104.2100),
    (dt.date(2024, 1, 31), "CHF", 96.4400),
    (dt.date(2022, 3, 11), "EUR", 83.7710),
    (SBI_DATE, "USD", 111.1111),      # deliberately wrong - SBI must still win
]
NO_RATE_ANYWHERE = dt.date(2018, 3, 15)


def ok(flag, label, detail=""):
    print(f"  {'PASS' if flag else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return 0 if flag else 1


def google_frame(rows=None):
    return pd.DataFrame([
        {"date": d, "base_currency": c, "quote_currency": "INR", "rate": r,
         "source": "Google Finance historical FX", "retrieved_on": "2026-09-04",
         "status": "OK", "notes": pair_symbol(c)}
        for d, c, r in (GOOGLE_ROWS if rows is None else rows)])


def main() -> int:
    fails = 0

    print("=" * 98)
    print("1. PRIORITY - SBI TTBR WINS WHENEVER IT HAS THE DATE")
    print("=" * 98)
    reg = Register()
    fx = FXTable(FXC, register=reg, google_frame=google_frame())
    q = fx.rate_quote(SBI_DATE, "USD")
    fails += ok(q is not None and abs(q.rate - SBI_RATE) < 1e-6,
                "SBI rate used", f"{q.rate} for {SBI_DATE:%d-%m-%Y}")
    fails += ok(q.source_type == SBI_TTBR, "source", q.source_type)
    fails += ok(abs(q.rate - 111.1111) > 1,
                "the Google row for the same date was NOT used",
                "- Google says 111.1111 and is deliberately ignored")
    fails += ok(not q.is_fallback and "SBI TT buying rate" in q.basis,
                "and it is labelled as an SBI TT buying rate")

    print()
    print("=" * 98)
    print("2. FALLBACK - GOOGLE FINANCE WHERE SBI HAS NOTHING")
    print("=" * 98)
    reg2 = Register()
    fx2 = FXTable(FXC, register=reg2, google_frame=google_frame())
    q2 = fx2.rate_quote(dt.date(2021, 8, 19), "USD")
    fails += ok(q2 is not None and abs(q2.rate - 74.2450) < 1e-6,
                "Google rate used", f"{q2.rate} for 19-08-2021")
    fails += ok(q2.source_type == GOOGLE_FINANCE, "source", q2.source_type)
    fails += ok(q2.sbi_availability == SBI_NOT_AVAILABLE,
                "SBI availability recorded", q2.sbi_availability)
    fails += ok(FALLBACK_BANNER in q2.basis,
                "basis states the fallback in words", f'"{q2.basis}"')
    flags = [f for f in reg2.flags if f.reason == FX_FALLBACK]
    fails += ok(len(flags) == 1 and flags[0].severity == Severity.REVIEW,
                "raised as a REVIEW item, not applied silently")
    fails += ok("not an sbi" in flags[0].detail.lower()
                or "NOT an SBI" in flags[0].detail,
                "and the flag says plainly it is not an SBI rate")

    print()
    print("=" * 98)
    print("3. NEITHER SOURCE HAS IT - BLANK, NEVER ZERO")
    print("=" * 98)
    reg3 = Register()
    fx3 = FXTable(FXC, register=reg3, google_frame=google_frame())
    q3 = fx3.rate_quote(NO_RATE_ANYWHERE, "JPY")
    fails += ok(q3 is None, "no quote returned", f"{NO_RATE_ANYWHERE:%d-%m-%Y} JPY")
    fails += ok((NO_RATE_ANYWHERE, "JPY") in fx3.unresolved, "recorded as unresolved")
    blockers = [f for f in reg3.flags if f.severity == Severity.BLOCKER]
    fails += ok(any("FX_UNAVAILABLE" in f.reason for f in blockers),
                "raised as an FX_UNAVAILABLE blocker")
    fails += ok("blank" in blockers[0].detail.lower()
                and "zero" in blockers[0].detail.lower(),
                "the flag states the figure is blank and not converted at zero")
    audit = fx3.audit_frame()
    row = audit[audit["Date required"] == NO_RATE_ANYWHERE].iloc[0]
    fails += ok(row["Rate"] == "" and row["FX source"] == "NONE",
                "the audit row carries no rate at all, not a 0.0000")

    print()
    print("=" * 98)
    print("4. MULTIPLE CURRENCIES, BUILT DYNAMICALLY - NEVER HARDCODED")
    print("=" * 98)
    for cur, expect in (("USD", "CURRENCY:USDINR"), ("EUR", "CURRENCY:EURINR"),
                        ("GBP", "CURRENCY:GBPINR"), ("CHF", "CURRENCY:CHFINR"),
                        ("SGD", "CURRENCY:SGDINR"), ("AUD", "CURRENCY:AUDINR")):
        fails += ok(pair_symbol(cur) == expect, f"{cur} pair", pair_symbol(cur))
    f = google_formula("EUR", "A2")
    fails += ok(f == '=INDEX(GOOGLEFINANCE("CURRENCY:EURINR","price",A2),2,2)',
                "formula shape", f)
    src = (ROOT / "src" / "fxsources.py").read_text()
    fails += ok('"CURRENCY:EURINR"' not in src and '"CURRENCY:USDINR"' not in src,
                "no currency pair is hardcoded anywhere in the source")

    reg4 = Register()
    fx4 = FXTable(FXC, register=reg4, google_frame=google_frame())
    for d, cur, rate in [(dt.date(2023, 6, 30), "GBP", 104.2100),
                         (dt.date(2024, 1, 31), "CHF", 96.4400),
                         (dt.date(2022, 3, 11), "EUR", 83.7710)]:
        qq = fx4.rate_quote(d, cur)
        fails += ok(qq is not None and abs(qq.rate - rate) < 1e-6
                    and qq.source_type == GOOGLE_FINANCE,
                    f"{cur} resolves through Google", f"{qq.rate if qq else None}")

    print()
    print("=" * 98)
    print("5. MULTIPLE HISTORICAL DATES, NOT JUST A PERIOD END")
    print("=" * 98)
    reg5 = Register()
    fx5 = FXTable(FXC, register=reg5, google_frame=google_frame())
    got = {}
    for d, cur, rate in GOOGLE_ROWS:
        if cur != "USD" or d == SBI_DATE:
            continue
        qq = fx5.rate_quote(d, cur)
        got[d] = qq.rate if qq else None
    fails += ok(len(got) == 3 and all(v for v in got.values()),
                f"{len(got)} distinct historical dates resolved across 2019-2022",
                ", ".join(f"{d:%Y}={v}" for d, v in sorted(got.items())))
    fails += ok(len(set(got.values())) == len(got),
                "each date got its OWN rate - not one rate reused")

    print()
    print("=" * 98)
    print("6/7. DATE INTEGRITY - SALE DATE FOR SALES, VEST DATE FOR COST")
    print("=" * 98)
    sold, vested = dt.date(2022, 5, 2), dt.date(2021, 8, 19)
    ev = pd.DataFrame([
        {"date": vested, "broker": "TEST", "account_no": "1", "symbol": "TST",
         "event": "VEST", "quantity": 100.0, "price_fc": 50.0, "amount_fc": "",
         "tax_fc": "", "acquired_on": "", "cost_fc": "", "currency": "USD",
         "notes": "vest"},
        {"date": sold, "broker": "TEST", "account_no": "1", "symbol": "TST",
         "event": "SELL", "quantity": 100.0, "price_fc": 80.0, "amount_fc": 8000.0,
         "tax_fc": "", "acquired_on": "", "cost_fc": "", "currency": "USD",
         "notes": "sale"},
    ])
    reg6 = Register()
    fx6 = FXTable(FXC, register=reg6, google_frame=google_frame())
    _, matches = build_lots(ev, Register(), as_at=dt.date(2022, 12, 31))
    cg = build_cg(matches, fx6, ComputeOptions(),
                  Period(dt.date(2022, 1, 1), dt.date(2022, 12, 31)), reg6)
    r = cg.iloc[0]
    fails += ok(abs(float(r["FX Rate - Sale Date"]) - 76.5150) < 1e-6,
                "sale converted at the SALE date's rate",
                f"{r['FX Rate - Sale Date']} for {sold:%d-%m-%Y}")
    fails += ok(abs(float(r["FX Rate - Vest Date"]) - 74.2450) < 1e-6,
                "cost converted at the VEST date's rate",
                f"{r['FX Rate - Vest Date']} for {vested:%d-%m-%Y}")
    fails += ok(r["FX Rate - Sale Date"] != r["FX Rate - Vest Date"],
                "the two are genuinely different rates, not one applied twice")
    fails += ok(pd.to_datetime(r["FX Date Used - Sale"]).date() == sold
                and pd.to_datetime(r["FX Date Used - Cost"]).date() == vested,
                "and each row records the rate DATE it actually used")
    fails += ok(r["FX Source - Sale"] == GOOGLE_FINANCE
                and r["FX Source - Cost"] == GOOGLE_FINANCE,
                "each row names its FX source", "both GOOGLE_FINANCE here")
    fails += ok(FALLBACK_BANNER in str(r["Cost Conversion Basis"]),
                "the basis does NOT claim these were TTBR rates",
                f'"{str(r["Cost Conversion Basis"])[:70]}..."')

    print()
    print("=" * 98)
    print("8. DIVIDENDS - PAYMENT DATE FOR TRANSACTIONS, PERIOD END FOR 1042-S")
    print("=" * 98)
    paid = dt.date(2022, 5, 2)
    dev = pd.DataFrame([
        {"date": paid, "broker": "TEST", "account_no": "1", "symbol": "TST",
         "event": "DIV", "quantity": "", "price_fc": "", "amount_fc": 1000.0,
         "tax_fc": 250.0, "acquired_on": "", "cost_fc": "", "currency": "USD",
         "notes": "dividend"}])
    reg7 = Register()
    fx7 = FXTable(FXC, register=reg7, google_frame=google_frame())
    period = Period(dt.date(2022, 1, 1), dt.date(2022, 12, 31))
    div, f1042, fsi, basis = build_dividends(
        dev, pd.DataFrame(), fx7, period, reg7,
        txn_intervals=[(period.start, period.end)])
    d0 = div.iloc[0]
    fails += ok(abs(float(d0["FX Rate"]) - 76.5150) < 1e-6,
                "dividend converted at its PAYMENT date's rate",
                f"{d0['FX Rate']} for {paid:%d-%m-%Y}")
    fails += ok(d0["FX Source"] == GOOGLE_FINANCE, "through the fallback",
                d0["FX Source"])
    fails += ok(abs(float(d0["Foreign Tax Withheld (Rs.)"]) - 250.0 * 76.5150) < 2,
                "the foreign tax withheld uses the same date's rate",
                f"Rs {float(d0['Foreign Tax Withheld (Rs.)']):,.0f}")

    reg8 = Register()
    fx8 = FXTable(FXC, register=reg8, google_frame=google_frame())
    f1042_in = pd.DataFrame([{
        "tax_year": 2025, "broker": "TEST", "payer": "TEST", "income_code": "06",
        "gross_income_fc": 1000.0, "tax_withheld_fc": 250.0, "currency": "USD",
        "source_file": "1042s.pdf", "notes": ""}])
    p25 = Period(dt.date(2025, 1, 1), dt.date(2025, 12, 31))
    _, f_out, _, _ = build_dividends(pd.DataFrame(columns=dev.columns), f1042_in,
                                     fx8, p25, reg8, txn_intervals=[])
    fr = f_out.iloc[0]
    fails += ok(abs(float(fr["Conversion Rate (period-end TTBR)"]) - SBI_RATE) < 1e-6,
                "1042-S still converts in aggregate at the period-end rate",
                f"{fr['Conversion Rate (period-end TTBR)']} - the agreed "
                "methodology, unchanged")
    fails += ok("no payment dates" in str(fr["Basis"]).lower()
                or "aggregate" in str(fr["Basis"]).lower(),
                "and the row states why", f'"{str(fr["Basis"])[:60]}..."')

    print()
    print("=" * 98)
    print("9/10. NO ZERO CONVERSION, NO UNRELATED PERIOD-END SUBSTITUTION")
    print("=" * 98)
    reg9 = Register()
    fx9 = FXTable(FXC, register=reg9)            # no Google table at all
    ev2 = ev.copy()
    _, m2 = build_lots(ev2, Register(), as_at=dt.date(2022, 12, 31))
    cg2 = build_cg(m2, fx9, ComputeOptions(),
                   Period(dt.date(2022, 1, 1), dt.date(2022, 12, 31)), reg9)
    r2 = cg2.iloc[0]
    fails += ok(r2["Computable"].startswith("No"), "row marked not computable",
                r2["Computable"])
    fails += ok(r2["Capital Gain (Rs.)"] == "" and r2["Cost of Acquisition (Rs.)"] == "",
                "gain and cost are BLANK, not zero and not a full-proceeds gain")
    fails += ok(float(r2["Proceeds (FC)"]) == 8000.0,
                "the foreign-currency substance is still reported",
                f"${float(r2['Proceeds (FC)']):,.2f}")
    # The shipped SBI table has 2024-2026 rates. None may be borrowed for 2022.
    fails += ok(r2["FX Rate - Sale Date"] == "",
                "no later period-end rate was borrowed for a 2022 date",
                "- the 2024-2026 rates on file were NOT used")
    fails += ok(r2["FX Rate - Sale Date"] != 0 and r2["FX Rate - Vest Date"] != 0,
                "and the rate column itself is BLANK, not a rate of zero",
                "- a rate of nil is not a rate")

    print()
    print("=" * 98)
    print("11-14. THE WORKBOOK ITSELF")
    print("=" * 98)
    reg10 = Register()
    fx10 = FXTable(FXC, register=reg10, google_frame=google_frame())
    fx10.rate_quote(dt.date(2021, 8, 19), "USD")     # a fallback
    fx10.rate_quote(NO_RATE_ANYWHERE, "JPY")         # an unresolved pair
    a = fx10.audit_frame()
    need = ["Date required", "Rate date used", "Gap (days)", "Rate date is",
            "Currency", "Converted to", "Currency pair", "Rate", "FX source",
            "SBI TTBR availability", "Retrieval status",
            "Basis stated in the working paper", "Source", "Review flag"]
    missing = [c for c in need if c not in a.columns]
    fails += ok(not missing, "FX_WORKING carries every provenance column",
                f"{len(need)} columns" if not missing else f"missing {missing}")
    fb = a[a["FX source"] == GOOGLE_FINANCE].iloc[0]
    fails += ok(fb["SBI TTBR availability"] == SBI_NOT_AVAILABLE
                and FALLBACK_BANNER in str(fb["Review flag"]),
                "a fallback row states SBI NOT_AVAILABLE and the banner")
    fails += ok(FALLBACK_BANNER in fx10.fx_status,
                "the workbook's FX status line says it too", fx10.fx_status)

    req = fx10.google_request_frame()
    fails += ok(len(req) == 1, "the request table holds only what is still missing",
                f"{len(req)} row for the unresolved JPY pair, not one per lookup")
    fails += ok(req.iloc[0]["google_pair"] == "CURRENCY:JPYINR",
                "with its pair built from the source currency",
                req.iloc[0]["google_pair"])
    fails += ok("GOOGLEFINANCE" in req.iloc[0]["google_finance_formula"],
                "and the GOOGLEFINANCE formula for that row",
                req.iloc[0]["google_finance_formula"])

    # Consolidation: many lookups of the same pair produce ONE request row.
    reg11 = Register()
    fx11 = FXTable(FXC, register=reg11)
    for _ in range(50):
        fx11.rate_quote(NO_RATE_ANYWHERE, "USD")
    fails += ok(len(fx11.google_request_frame()) == 1,
                "50 lookups of one currency/date pair -> 1 request row",
                "- consolidated, not one GOOGLEFINANCE call per transaction")

    print()
    print("=" * 98)
    print("15. UNIVERSAL - NOT TIED TO ANY BROKER OR ANY SECURITY")
    print("=" * 98)
    import re as _re
    from src.ingest import adjustments                              # noqa: E402

    fx_layer = ((ROOT / "src" / "fxsources.py").read_text()
                + (ROOT / "src" / "fx.py").read_text())
    brokers = ["etrade", "schwab", "fidelity", "morgan", "computershare", "ubs",
               "shareworks"]
    hits = [b for b in brokers
            if _re.search(rf"\b{b}\b", fx_layer, _re.I)]
    fails += ok(not hits, "no broker is named anywhere in the FX layer",
                f"checked {len(brokers)} broker names" if not hits else f"found {hits}")
    tickers = ["intc", "intel", "qcom", "qualcomm", "avgo", "msft", "amzn", "ibm"]
    thits = [t for t in tickers if _re.search(rf"\b{t}\b", fx_layer, _re.I)]
    fails += ok(not thits, "and no security either", f"found {thits}" if thits else "")

    fails += ok(not adjustments.REGISTRY,
                "the broker-adjustment extension point still has ZERO hooks",
                "- no broker-specific Google Finance hook was introduced")

    profiles = sorted((ROOT / "config" / "mappings").glob("*.y*ml"))
    gprof = [p.name for p in profiles if "google" in p.read_text().lower()]
    fails += ok(not gprof, f"none of the {len(profiles)} broker profiles mention Google",
                f"found {gprof}" if gprof else "- FX is resolved below the profile layer")

    # End to end on a broker that is not E*TRADE and a security that is not INTC.
    reg12 = Register()
    fx12 = FXTable(FXC, register=reg12, google_frame=google_frame())
    other = pd.DataFrame([
        {"date": dt.date(2019, 5, 1), "broker": "ANY_OTHER_BROKER", "account_no": "9",
         "symbol": "ZZZ", "event": "VEST", "quantity": 10.0, "price_fc": 100.0,
         "amount_fc": "", "tax_fc": "", "acquired_on": "", "cost_fc": "",
         "currency": "USD", "notes": "vest"},
        {"date": dt.date(2021, 8, 19), "broker": "ANY_OTHER_BROKER", "account_no": "9",
         "symbol": "ZZZ", "event": "SELL", "quantity": 10.0, "price_fc": 150.0,
         "amount_fc": 1500.0, "tax_fc": "", "acquired_on": "", "cost_fc": "",
         "currency": "USD", "notes": "sale"},
    ])
    _, m12 = build_lots(other, Register(), as_at=dt.date(2021, 12, 31))
    cg12 = build_cg(m12, fx12, ComputeOptions(),
                    Period(dt.date(2021, 1, 1), dt.date(2021, 12, 31)), reg12)
    r12 = cg12.iloc[0]
    fails += ok(r12["FX Source - Sale"] == GOOGLE_FINANCE
                and r12["FX Source - Cost"] == GOOGLE_FINANCE,
                "an unrelated broker and ticker get the same fallback",
                "ANY_OTHER_BROKER / ZZZ resolved through Google Finance")
    fails += ok(abs(float(r12["FX Rate - Cost"] if "FX Rate - Cost" in r12
                          else r12["FX Rate - Vest Date"]) - 69.5820) < 1e-6,
                "at its own vest date's rate", "69.5820 for 01-05-2019")
    fails += ok(r12["Computable"] == "Yes"
                and float(r12["Capital Gain (Rs.)"]) > 0,
                "and the row computes",
                f"Rs {float(r12['Capital Gain (Rs.)']):,.0f}")

    # A3 holdings, not just capital gains - the fallback reaches valuation too.
    reg13 = Register()
    fx13 = FXTable(FXC, register=reg13, google_frame=google_frame())
    md13 = MarketData(MKT, reg13)
    hold = pd.DataFrame([
        {"date": dt.date(2021, 8, 19), "broker": "ANY_OTHER_BROKER", "account_no": "9",
         "symbol": "IBM", "event": "VEST", "quantity": 10.0, "price_fc": 100.0,
         "amount_fc": "", "tax_fc": "", "acquired_on": "", "cost_fc": "",
         "currency": "USD", "notes": "vest"}])
    lots13, _ = build_lots(hold, Register(), as_at=SBI_DATE)
    a3 = build_a3(hold, lots13, pd.DataFrame(), md13, fx13,
                  Period(dt.date(2025, 1, 1), SBI_DATE), ComputeOptions(), reg13)
    fails += ok(len(a3) == 1 and FALLBACK_BANNER in str(a3.iloc[0]["_basis"]),
                "A3 initial value also names the fallback in its basis",
                f'"{str(a3.iloc[0]["_basis"])[:64]}..."')
    fails += ok(bool(a3.iloc[0]["_init_ok"])
                and float(a3.iloc[0]["Initial Value of the Investment (Rs.)"]) > 0
                and abs(float(a3.iloc[0]["_rate_vest"]) - 74.2450) < 1e-6
                and abs(float(a3.iloc[0]["_rate_end"]) - SBI_RATE) < 1e-6,
                "vest-date FX from Google, period-end FX from SBI, in ONE row",
                f"vest {float(a3.iloc[0]['_rate_vest']):.4f} (Google) / period-end "
                f"{float(a3.iloc[0]['_rate_end']):.4f} (SBI)")

    print()
    print("=" * 98)
    print("16. NEAREST AVAILABLE GOOGLE DATE - WEEKENDS, HOLIDAYS, NON-TRADING DAYS")
    print("=" * 98)
    from src.fxsources import (                                      # noqa: E402
        MAX_CARRY_DAYS, MAX_NEAREST_DAYS, STATUS_EXACT, STATUS_NEAREST,
    )

    # EUR carries no SBI rate at all, so the Google path is exercised cleanly.
    # Fri 26-12 and Mon 29-12 quoted; the weekend between them is not.
    CAL = [(dt.date(2025, 12, 24), "EUR", 94.80),   # Wed
           (dt.date(2025, 12, 26), "EUR", 95.10),   # Fri (25th is a holiday)
           (dt.date(2025, 12, 29), "EUR", 95.90),   # Mon (27-28 is the weekend)
           (dt.date(2025, 6, 1), "EUR", 90.00)]     # isolated, far from the rest
    reg14 = Register()
    fx14 = FXTable(FXC, register=reg14, google_frame=google_frame(CAL))

    q_exact = fx14.rate_quote(dt.date(2025, 12, 26), "EUR")
    fails += ok(q_exact is not None and q_exact.status == STATUS_EXACT
                and q_exact.gap_days == 0 and abs(q_exact.rate - 95.10) < 1e-6,
                "exact date available -> exact rate, gap 0",
                f"{q_exact.rate} on {q_exact.used_date:%d-%m-%Y}")

    q_hol = fx14.rate_quote(dt.date(2025, 12, 25), "EUR")     # Christmas Day
    fails += ok(q_hol is not None and q_hol.status == STATUS_NEAREST
                and q_hol.used_date == dt.date(2025, 12, 24) and q_hol.gap_days == 1,
                "holiday -> nearest available date",
                f"25-12 required, {q_hol.used_date:%d-%m-%Y} used, gap "
                f"{q_hol.gap_days}d {q_hol.direction}")

    q_sat = fx14.rate_quote(dt.date(2025, 12, 28), "EUR")     # Sunday
    fails += ok(q_sat is not None and q_sat.status == STATUS_NEAREST
                and q_sat.used_date == dt.date(2025, 12, 29) and q_sat.gap_days == 1,
                "weekend -> nearest available date",
                f"28-12 required, {q_sat.used_date:%d-%m-%Y} used, gap "
                f"{q_sat.gap_days}d {q_sat.direction}")

    # 27-12 sits exactly between 26-12 and 28-12... but 28-12 is not quoted, so
    # the true tie is built from the quoted dates 26-12 and 29-12 either side of
    # a midpoint. Use 26-12 / 28-12 quotes for an unambiguous 1-day tie.
    TIE = [(dt.date(2025, 12, 26), "EUR", 95.10),
           (dt.date(2025, 12, 28), "EUR", 95.90)]
    reg15 = Register()
    fx15 = FXTable(FXC, register=reg15, google_frame=google_frame(TIE))
    q_tie = fx15.rate_quote(dt.date(2025, 12, 27), "EUR")
    fails += ok(q_tie is not None and q_tie.used_date == dt.date(2025, 12, 26)
                and q_tie.gap_days == 1 and q_tie.direction == "prior",
                "equally distant prior and next -> PRIOR wins",
                f"26-12 and 28-12 are both 1 day from 27-12; "
                f"{q_tie.used_date:%d-%m-%Y} was taken")
    fails += ok(abs(q_tie.rate - 95.10) < 1e-6,
                "and it is the prior date's RATE that was used", f"{q_tie.rate}")

    print()
    print("  provenance on a nearest-date quote:")
    for label, val in (("required FX date", f"{q_hol.requested_date:%d-%m-%Y}"),
                       ("Google rate date", f"{q_hol.used_date:%d-%m-%Y}"),
                       ("currency pair", pair_symbol(q_hol.base_currency)),
                       ("rate", f"{q_hol.rate}"),
                       ("source", q_hol.source_type),
                       ("status", q_hol.status),
                       ("gap (days)", f"{q_hol.gap_days}"),
                       ("SBI availability", q_hol.sbi_availability)):
        print(f"    {label:<20} {val}")
    fails += ok(q_hol.requested_date != q_hol.used_date
                and q_hol.status == STATUS_NEAREST,
                "the substituted date is NOT presented as the date required")
    fails += ok(("exact Google Finance date unavailable" in q_hol.basis
                 and "nearest available prior date" in q_hol.basis),
                "and the basis says so in words", f'"{q_hol.basis[:76]}..."')

    a14 = fx14.audit_frame()
    hrow = a14[a14["Date required"] == dt.date(2025, 12, 25)].iloc[0]
    for col, want in (("Rate date used", dt.date(2025, 12, 24)),
                      ("Gap (days)", 1), ("Retrieval status", STATUS_NEAREST),
                      ("Currency pair", "CURRENCY:EURINR"),
                      ("FX source", GOOGLE_FINANCE),
                      ("SBI TTBR availability", SBI_NOT_AVAILABLE)):
        fails += ok(hrow[col] == want, f"FX_WORKING records {col}", f"{hrow[col]}")
    fails += ok("prior" in str(hrow["Rate date is"]),
                "and which side of the required date it came from",
                str(hrow["Rate date is"]))
    fails += ok("nearest available date used" in str(hrow["Review flag"]),
                "with a review flag naming the substitution",
                str(hrow["Review flag"]))

    print()
    print("  materially distant vs a routine weekend/holiday carry:")
    q_far = fx14.rate_quote(dt.date(2025, 6, 20), "EUR")
    fails += ok(q_far is not None and q_far.gap_days == 19
                and q_far.is_materially_distant,
                "a 19-day substitution is flagged MATERIALLY DISTANT",
                f"{q_far.used_date:%d-%m-%Y} used for 20-06-2025")
    fails += ok(not q_hol.is_materially_distant,
                f"a 1-day holiday carry is not (threshold {MAX_CARRY_DAYS} days)",
                "- normal weekend/holiday behaviour is unchanged")
    far_flags = [f for f in reg14.flags
                 if "MATERIALLY DISTANT" in f.detail and f.severity == Severity.REVIEW]
    fails += ok(len(far_flags) == 1,
                "and it raises REVIEW_REQUIRED rather than passing silently")
    fails += ok("MATERIALLY DISTANT" in q_far.basis,
                "the calculation basis carries the same warning")

    q_none = fx14.rate_quote(dt.date(2025, 8, 15), "EUR")
    fails += ok(q_none is None,
                f"beyond {MAX_NEAREST_DAYS} days no rate is used at all",
                "- an unrelated rate is worse than no rate")
    fails += ok((dt.date(2025, 8, 15), "EUR") in fx14.unresolved
                and any(f.reason == "FX_UNAVAILABLE" and f.severity == Severity.BLOCKER
                        for f in reg14.flags),
                "it becomes blank + FX_UNAVAILABLE")

    # Precedence is untouched by any of this.
    reg16 = Register()
    fx16 = FXTable(FXC, register=reg16,
                   google_frame=google_frame([(SBI_DATE, "USD", 111.1111)]))
    q_sbi = fx16.rate_quote(SBI_DATE, "USD")
    fails += ok(q_sbi.source_type == SBI_TTBR and abs(q_sbi.rate - SBI_RATE) < 1e-6,
                "SBI still wins outright where it has the date",
                "- nearest-date search never runs for it")
    # 02-01-2026 is 2 days after the 31-12 SBI rate: inside SBI's normal carry
    # window, so SBI answers and Google is never consulted.
    q_sbi2 = fx16.rate_quote(dt.date(2026, 1, 2), "USD")
    fails += ok(q_sbi2 is not None and q_sbi2.source_type == SBI_TTBR
                and q_sbi2.used_date == SBI_DATE and q_sbi2.gap_days == 2,
                "SBI's own weekend/holiday carry is unchanged",
                f"02-01 -> {q_sbi2.used_date:%d-%m-%Y} at {q_sbi2.gap_days}d, "
                "still SBI")
    # But where SBI's nearest is far and Google has a NEARER date, Google wins -
    # that is the point of the fallback, not a regression.
    q_mix = fx16.rate_quote(dt.date(2025, 12, 27), "USD")
    fails += ok(q_mix is not None and q_mix.source_type == GOOGLE_FINANCE
                and q_mix.gap_days == 4,
                "where SBI's nearest is 16 days off, Google's 4-day quote wins",
                f"27-12 -> {q_mix.used_date:%d-%m-%Y} via {q_mix.source_type}, "
                "not SBI's 11-12")

    print()
    print("=" * 98)
    print("17. UNIVERSAL NEAREST-AVAILABLE RULE ON THE PRIMARY (SBI) TABLE")
    print("=" * 98)
    from src.fxsources import MAX_NEAREST_PRIMARY_DAYS                # noqa: E402

    reg17 = Register()
    fx17 = FXTable(FXC, register=reg17)          # no secondary source at all
    # The shipped table's earliest row is 31-07-2024. A date days BEFORE it had
    # no prior rate to carry forward, so it could not resolve at all.
    q_fwd = fx17.rate_quote(dt.date(2024, 7, 27), "USD")
    fails += ok(q_fwd is not None and q_fwd.used_date == dt.date(2024, 7, 31)
                and q_fwd.gap_days == 4 and q_fwd.direction == "later",
                "a date with no PRIOR rate resolves to the nearest LATER one",
                f"27-07-2024 -> {q_fwd.used_date:%d-%m-%Y}, {q_fwd.gap_days}d later"
                if q_fwd else "unresolved")
    fails += ok(q_fwd is not None and q_fwd.source_type == SBI_TTBR
                and q_fwd.status == "NEAREST_AVAILABLE_DATE",
                "recorded as SBI, NEAREST_AVAILABLE_DATE - not as the date itself")
    near_flags = [f for f in reg17.flags if "carried forward" in f.reason.lower()]
    fails += ok(any(f"{MAX_NEAREST_PRIMARY_DAYS}-day" in f.detail for f in near_flags),
                "and raised for review naming the window",
                f"{MAX_NEAREST_PRIMARY_DAYS}-day nearest-available window")

    # Just outside the window: not a rate for this date.
    reg18 = Register()
    fx18 = FXTable(FXC, register=reg18)
    q_out = fx18.rate_quote(dt.date(2024, 7, 1), "USD")
    fails += ok(q_out is None,
                f"a gap of 30 days is outside the {MAX_NEAREST_PRIMARY_DAYS}-day "
                "window and does NOT resolve",
                "- 01-07-2024, one of the three UBS dates")
    fails += ok(any(f.reason == "FX_UNAVAILABLE" and f.severity == Severity.BLOCKER
                    for f in reg18.flags),
                "it stays an FX_UNAVAILABLE blocker",
                "- an input-data gap, not something the engine may paper over")

    # Precedence is untouched: an exact SBI rate still wins outright.
    reg19 = Register()
    fx19 = FXTable(FXC, register=reg19)
    q_ex = fx19.rate_quote(SBI_DATE, "USD")
    fails += ok(q_ex.status == "EXACT_DATE" and abs(q_ex.rate - SBI_RATE) < 1e-6,
                "an exact SBI rate is unaffected by the rule", f"{q_ex.rate}")
    # And the ordinary weekend carry still behaves as before.
    q_carry = fx19.rate_quote(dt.date(2026, 1, 2), "USD")
    fails += ok(q_carry is not None and q_carry.used_date == SBI_DATE
                and q_carry.gap_days == 2,
                "the normal weekend/holiday carry is unchanged",
                f"02-01-2026 -> {q_carry.used_date:%d-%m-%Y}")

    print()
    print(f"RESULT: {'ALL CHECKS PASSED' if not fails else str(fails) + ' CHECK(S) FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
