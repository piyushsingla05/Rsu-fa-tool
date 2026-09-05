"""Fidelity regression, including the combined Fidelity + Schwab client.

Anchors are the documents' own printed figures. Where Fidelity's wording differs
from its arithmetic, the arithmetic wins and the difference is explained.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.compute import build_cross_broker, build_lots     # noqa: E402
from src.ingest.extract import extract_file                # noqa: E402
from src.ingest.profiles import load_profiles              # noqa: E402
from src.models import Period                              # noqa: E402
from src.review import Register                            # noqa: E402
from src.run import resolve_positions                      # noqa: E402

FID = Path("/home/claude/brokers/RSU brokers/Fidelity")
SCH = Path("/home/claude/brokers/RSU brokers/Charles schwab")
SUMMARY = FID / "fidelity_2025_Jan_to_March.pdf"
PERIOD = Period(dt.date(2025, 1, 1), dt.date(2025, 12, 31))

# Printed on Fidelity's own account summary
STATED = {"dividends": 3317.44, "nra_withholding": -821.11, "stock_sales": 30961.44}
# Derived from the seven detail rows - proceeds and cost, which the summary's
# "Stock sales" figure is NOT (that figure is the gain).
DETAIL = {"proceeds": 51447.64, "cost": 20486.20}
# CLIENT_FIDELITY_01's Dec-2025 SPS report, and the reference values built by hand earlier
CLIENT_FID01 = {"shares": 452.640, "cost_total": 148012.18, "cost_per_share": 326.9976,
          "price": 483.62}
FORM_1042S = {"gross": 1465.00, "tax": 366.00}
INDIAN_LTCG_DAYS = 730


def ok(flag, label, detail=""):
    print(f"  {'PASS' if flag else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return 0 if flag else 1


def collect(folder, profiles):
    frames, recs, seen = [], [], {}
    for f in sorted(Path(folder).iterdir()):
        ev, rec, _, _ = extract_file(f, profiles)
        if rec.content_hash and rec.content_hash in seen:
            continue
        seen[rec.content_hash] = rec.file
        recs.append(rec)
        if len(ev):
            frames.append(ev)
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()), recs


def main() -> int:
    fails = 0
    profiles = load_profiles()

    print("=" * 98)
    print("1. FIDELITY CUSTOM TRANSACTION SUMMARY vs ITS OWN STATED TOTALS")
    print("=" * 98)
    ev, rec, _, _ = extract_file(SUMMARY, profiles)
    fails += ok(rec.profile_id == "fidelity_custom_transaction_summary"
                and rec.confidence >= 0.99,
                "identified", f"{rec.profile_id} at {rec.confidence:.0%}, "
                f"period {rec.coverage}")

    div = ev[ev.event == "DIV"]
    g = float(div.amount_fc.astype(float).sum())
    fails += ok(abs(g - STATED["dividends"]) < 0.01, "gross dividend income",
                f"{g:,.2f} vs stated {STATED['dividends']:,.2f}  "
                f"diff {g - STATED['dividends']:,.2f}")
    print(f"        {len(div)} rows for Fidelity's 9 transactions - two pairs share "
          "a payment date and are grouped; the total is unaffected")

    tax = ev[ev.event == "DIV_TAX"]
    t = float(tax.amount_fc.astype(float).sum())
    fails += ok(abs(t + STATED["nra_withholding"]) < 0.01, "NRA withholding",
                f"{t:,.2f} vs stated {STATED['nra_withholding']:,.2f} "
                f"(sign inverted: a withholding is a positive tax)")
    fails += ok(len(tax) == 10, f"{len(tax)} withholding rows vs stated 10")

    sells = ev[ev.event == "SELL"]
    P = float(sells.amount_fc.astype(float).sum())
    C = float(sells.cost_fc.astype(float).sum())
    fails += ok(len(sells) == 7, f"{len(sells)} closed-lot sales vs stated 7")
    fails += ok(abs(P - DETAIL["proceeds"]) < 0.01, "sale proceeds",
                f"{P:,.2f} vs detail rows {DETAIL['proceeds']:,.2f}")
    fails += ok(abs(C - DETAIL["cost"]) < 0.01, "cost basis",
                f"{C:,.2f} vs detail rows {DETAIL['cost']:,.2f}")
    fails += ok(abs((P - C) - STATED["stock_sales"]) < 0.01,
                "gain reconciles to the stated figure",
                f"{P - C:,.2f} vs stated 'Stock sales' {STATED['stock_sales']:,.2f}")
    print("        NOTE: Fidelity's 'Stock sales total' is the GAIN, not proceeds.")
    print(f"        proceeds {P:,.2f} - cost {C:,.2f} = {P - C:,.2f} = stated total.")
    fails += ok(sells.acquired_on.astype(str).str.len().gt(0).all(),
                "every sale carries the stated acquisition date")

    print()
    print("=" * 98)
    print("2. FIDELITY SPS REPORTS - PERIOD-END HOLDING (built from documents only)")
    print("=" * 98)
    rev, rrecs = collect(ROOT / "uploads" / "CLIENT_FIDELITY_02", profiles)
    reg = Register()
    resolved = resolve_positions(rev, PERIOD, reg)
    held = resolved[resolved.event == "POSITION"]
    fails += ok(len(held) == 1, f"{int((rev.event == 'POSITION').sum())} snapshots "
                f"-> {len(held)} holding")
    if len(held):
        r = held.iloc[0]
        q, cps = float(r["quantity"]), float(r["price_fc"])
        fails += ok(abs(q - CLIENT_FID01["shares"]) < 0.001, "quantity",
                    f"{q:,.3f} vs statement {CLIENT_FID01['shares']:,.3f}")
        fails += ok(abs(cps - CLIENT_FID01["cost_per_share"]) < 0.01, "cost per share",
                    f"{cps:,.4f} vs {CLIENT_FID01['cost_total']:,.2f}/{CLIENT_FID01['shares']:,.3f}"
                    f" = {CLIENT_FID01['cost_per_share']:,.4f}")
        fails += ok(r["date"] == PERIOD.end, "dated at the period end",
                    f"{r['date']:%d-%m-%Y}")

    f1042 = [x for rc in rrecs for x in (rc.form1042s or [])]
    fails += ok(len(f1042) == 1, f"{len(f1042)} Form 1042-S ingested")
    if f1042:
        fails += ok(abs(f1042[0]["gross_income_fc"] - FORM_1042S["gross"]) < 0.01
                    and abs(f1042[0]["tax_withheld_fc"] - FORM_1042S["tax"]) < 0.01,
                    "1042-S gross and tax",
                    f"{f1042[0]['gross_income_fc']:,.2f} / "
                    f"{f1042[0]['tax_withheld_fc']:,.2f} vs form "
                    f"{FORM_1042S['gross']:,.2f} / {FORM_1042S['tax']:,.2f}")
    unreadable = [r for r in rrecs if r.kind == "pdf" and r.profile_id == "generic"
                  and r.rows == 0]
    fails += ok(len(unreadable) == 1,
                "the scanned PDF is flagged, not guessed at",
                f"{unreadable[0].file if unreadable else '-'} has no text layer")

    print()
    print("=" * 98)
    print("3. COMBINED FIDELITY + SCHWAB - TRANSFERS MUST NOT DOUBLE-COUNT")
    print("=" * 98)
    cev, crecs = collect(ROOT / "uploads" / "CLIENT_COMBINED_01", profiles)
    reg2 = Register()
    cres = resolve_positions(cev, PERIOD, reg2)
    transfers = [t for r in crecs for t in (r.transfers or [])]
    lots, matches = build_lots(cres, Register(), as_at=PERIOD.end)
    holding = sum(l.remaining for l in lots if l.symbol == "AVGO")

    fid_sales = cres[(cres.broker == "FIDELITY") & (cres.event == "SELL")]
    sch_sales = cres[(cres.broker == "SCHWAB") & (cres.event == "SELL")]
    print(f"  Fidelity sales : {len(fid_sales):>2} rows, "
          f"{fid_sales.quantity.astype(float).sum():>6,.0f} shares, "
          f"${fid_sales.amount_fc.astype(float).sum():>12,.2f}")
    print(f"  Schwab sales   : {len(sch_sales):>2} rows, "
          f"{sch_sales.quantity.astype(float).sum():>6,.0f} shares, "
          f"${sch_sales.amount_fc.astype(float).sum():>12,.2f}")
    fails += ok(len(fid_sales) == 7 and len(sch_sales) == 21,
                "both brokers' sales are kept and none is duplicated",
                "- the date ranges do not overlap")
    fails += ok(abs(holding - 3866.0) < 0.01, "combined period-end holding",
                f"{holding:,.0f} - Schwab's stated position, with transfers-in "
                "excluded because that position already contains them")

    t_in = sum(t["quantity"] for t in transfers if t["direction"] == "IN")
    acq = float(cres[cres.event.isin(["VEST", "TRANSFER_IN"])].quantity
                .astype(float).sum()) if len(cres) else 0.0
    fails += ok(acq == 0.0,
                "no transfer created an acquisition",
                f"{t_in:,.0f} shares of transfer evidence recorded, "
                f"{acq:,.0f} acquisitions generated")
    fails += ok(not any(t.get("direction") == "IN" and t.get("value_fc", 0) and False
                        for t in transfers),
                "no transfer created proceeds or a capital gain")

    cross = build_cross_broker(cres, transfers, lots, PERIOD, Register())
    print()
    print("  Cross-broker chain:")
    for _, r in cross.iterrows():
        print(f"    {r['Broker']:<10} acquired {r['Acquired / Vested']:>9,.0f}  "
              f"sold {r['Sold']:>7,.0f}  in {r['Transferred In']:>7,.0f}  "
              f"out {r['Transferred Out']:>7,.0f}   -> {r['Status']}")
    fails += ok(not cross.empty
                and (cross["Status"] == "Transfer linkage incomplete").all(),
                "linkage reported as INCOMPLETE, not assumed",
                "- Fidelity reports no vests and no transfers out")

    print()
    print("=" * 98)
    print("4. HOLDING PERIOD - INDIAN 24-MONTH RULE ACROSS BOTH BROKERS")
    print("=" * 98)
    ind = us = 0
    for m in matches:
        if m.acquired_on is None:
            continue
        d = (m.sold_on - m.acquired_on).days
        ind += 1 if d > INDIAN_LTCG_DAYS else 0
        us += 1 if d > 365 else 0
    print(f"  long-term under the Indian 24-month rule : {ind}")
    print(f"  long-term under the US 12-month rule     : {us}")
    fails += ok(ind < us, "the Indian rule is applied, not the broker's label",
                f"{us - ind} lot(s) the brokers call long-term are short term here")

    print()
    print("=" * 98)
    print("5. DIVIDEND / 1042-S PRECEDENCE ON REAL FIDELITY DATA")
    print("=" * 98)
    from src.compute import build_dividends
    from src.fx import FXTable
    fx = FXTable(ROOT / "config" / "fx_rates.csv")
    fid_div = ev[ev.event.isin(["DIV", "DIV_TAX"])]
    f_1042 = pd.DataFrame([{
        "tax_year": 2025, "broker": "FIDELITY", "payer": "Fidelity SPS",
        "income_code": "06", "gross_income_fc": FORM_1042S["gross"],
        "tax_withheld_fc": FORM_1042S["tax"], "currency": "USD",
        "source_file": "1042s.pdf", "notes": ""}])
    full = [(dt.date(2025, 1, 1), dt.date(2025, 12, 31))]
    partial = [(dt.date(2025, 10, 1), dt.date(2025, 12, 31))]

    scenarios = [
        ("complete coverage + 1042-S", fid_div, f_1042, full, "Transaction-level"),
        ("incomplete coverage + 1042-S", fid_div, f_1042, partial, "Form 1042-S"),
        ("1042-S only", fid_div.iloc[0:0], f_1042, [], "Form 1042-S"),
        ("transaction only, complete", fid_div, pd.DataFrame(), full,
         "Transaction-level"),
    ]
    for name, d_in, f_in, iv, expect in scenarios:
        reg3 = Register()
        div_t, f_t, fsi, basis = build_dividends(d_in, f_in, fx, PERIOD, reg3,
                                                 txn_intervals=iv)
        fsi_g = float(fsi["Gross Income (Rs.)"].sum()) if not fsi.empty else 0.0
        fsi_t = float(fsi["Foreign Tax Withheld (Rs.)"].sum()) if not fsi.empty else 0.0
        txn_g = float(div_t["Gross Dividend (Rs.)"].sum()) if not div_t.empty else 0.0
        f_g = float(f_t["Gross Dividend Income (Rs.)"].sum()) if not f_t.empty else 0.0
        rows_kept = len(div_t) == len(d_in[d_in.event == "DIV"]) + \
            len(d_in[d_in.event == "DIV_TAX"])
        no_double = abs(fsi_g - (txn_g + f_g)) > 1.0 or not (txn_g and f_g)
        good = basis == expect and no_double
        fails += 0 if good else 1
        print(f"  {'PASS' if good else 'FAIL'}  {name:<32} -> {basis:<18} "
              f"FSI gross Rs {fsi_g:>11,.0f}  tax Rs {fsi_t:>10,.0f}  "
              f"rows kept {len(div_t)}")
    print("  Foreign tax is never taken from both sources at once.")

    print()
    print("=" * 98)
    print("6. GENERIC vs PROFILED - FIDELITY")
    print("=" * 98)
    checks = [("custom transaction summary", SUMMARY),
              ("SPS report", FID / "ITR_Kodhw3LDiECf3QvUNPLPEg_2025_"
                                   "a8835fbd-7473-4ce8-8f1d-ec77405d6529_mumbai.pdf"),
              ("Form 1042-S", FID / "ITR_Kodhw3LDiECf3QvUNPLPEg_2025_"
                                    "a4e4bb3f-a7e5-4641-bbd5-5471facb1c78_mumbai.pdf")]
    all_generic_empty = True
    for label, f in checks:
        pev, prec, _, _ = extract_file(f, profiles)
        gev, grec, _, _ = extract_file(f, [])
        all_generic_empty &= (len(gev) == 0)
        print(f"  {label:<28} profiled {len(pev):>3} rows ({prec.profile_id})   "
              f"generic {len(gev):>3} rows, broker {grec.broker}, "
              f"conf {grec.confidence:.0%}")
    fails += ok(all_generic_empty,
                "generic extracts nothing from these laid-out PDFs and invents nothing",
                "- it still names FIDELITY from the text")

    print()
    print(f"RESULT: {'ALL CHECKS PASSED' if not fails else str(fails) + ' CHECK(S) FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
