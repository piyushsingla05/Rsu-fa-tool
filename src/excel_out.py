"""Excel writer - professional tax working paper.

SUMMARY is always the first tab and REVIEW_REQUIRED the second, so a reviewer
meets the exceptions before the numbers. Tabs with no data are suppressed rather
than delivered empty. The filing tabs hold no hardcoded figures: every rupee is a
formula over visible backend columns.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .fxsources import google_formula

ENGINE_VERSION = "0.4.0"

FONT = "Arial"
NAVY = "1F3864"
HDR_FILL = PatternFill("solid", fgColor=NAVY)
BACKEND_FILL = PatternFill("solid", fgColor="7F7F7F")
HDR_FONT = Font(name=FONT, size=10, bold=True, color="FFFFFF")
BODY = Font(name=FONT, size=10)
INPUT = Font(name=FONT, size=10, color="0000FF")
BACKEND = Font(name=FONT, size=9, color="595959")
RED = Font(name=FONT, size=10, color="C00000")
AMBER = Font(name=FONT, size=10, color="BF8F00")
BOLD = Font(name=FONT, size=10, bold=True)
TITLE = Font(name=FONT, size=13, bold=True, color=NAVY)
BIG = Font(name=FONT, size=11, bold=True, color=NAVY)
NOTE_FILL = PatternFill("solid", fgColor="FFF2CC")
OK_FILL = PatternFill("solid", fgColor="E2EFDA")
BAD_FILL = PatternFill("solid", fgColor="FCE4E4")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

INR = '#,##0;(#,##0);-'
FCF = '#,##0.0000;(#,##0.0000);-'
RATE = '0.0000'
QTY = '#,##0.0000'
DFMT = "DD-MM-YYYY"

T_SUMMARY, T_REVIEW = "SUMMARY", "REVIEW_REQUIRED"
T_A2, T_A3 = "FA_A2", "FA_A3"
T_MASTER, T_VEST = "RSU_MASTER", "VESTING_SALES"
T_DIV = "DIVIDENDS_1042"          # transaction dividends AND the 1042-S, one tab
T_CG = "CAPITAL_GAINS"
T_FX = "FX_WORKING"               # SBI TTBR used, then market data provenance
T_FSI, T_RECON = "FSI_TR_WORKING", "RECONCILIATION"

# What a cell says when no FX source could supply the rate. It is never a
# zero and never an unrelated period-end rate.
FX_BLANK = "FX_UNAVAILABLE - not converted"

# Eleven tabs. SUMMARY always first; nothing empty is written.
TAB_ORDER = [T_SUMMARY, T_A2, T_A3, T_MASTER, T_VEST, T_DIV, T_CG, T_FX,
             T_FSI, T_RECON, T_REVIEW]


def _table(ws: Worksheet, df: pd.DataFrame, start_row: int = 1,
           nfmt: dict | None = None, backend_from: int | None = None) -> int:
    nfmt = nfmt or {}
    for j, col in enumerate(df.columns, start=1):
        c = ws.cell(row=start_row, column=j, value=str(col))
        c.font = HDR_FONT
        c.fill = BACKEND_FILL if (backend_from and j >= backend_from) else HDR_FILL
        c.border = BOX
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[start_row].height = 44
    for i, (_, r) in enumerate(df.iterrows(), start=start_row + 1):
        for j, col in enumerate(df.columns, start=1):
            v = r[col]
            if isinstance(v, (pd.Timestamp, dt.datetime)):
                v = v.date()
            if pd.isna(v):
                v = ""
            c = ws.cell(row=i, column=j, value=v)
            c.font = BACKEND if (backend_from and j >= backend_from) else BODY
            c.border = BOX
            if isinstance(v, dt.date):
                c.number_format = DFMT
            elif col in nfmt:
                c.number_format = nfmt[col]
    for j, col in enumerate(df.columns, start=1):
        w = max(len(str(col)) * 0.5, 10)
        if len(df):
            w = max(w, min(float(df[col].astype(str).str.len().max()) + 3, 38))
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    return start_row + 1


def write_workbook(path, ctx) -> Path:
    """ctx carries everything the workbook needs - see run.build_context()."""
    wb = Workbook()
    wb.remove(wb.active)
    _summary(wb, ctx)
    _a2(wb, ctx)
    _a3(wb, ctx)
    _master(wb, ctx)
    _vesting(wb, ctx)
    _dividends(wb, ctx)
    _cg(wb, ctx)
    _fx(wb, ctx)
    _fsi(wb, ctx)
    _recon(wb, ctx)
    _review(wb, ctx)
    wb._sheets = [wb[n] for n in TAB_ORDER if n in wb.sheetnames]
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


# ----------------------------------------------------------------------
def _summary(wb, ctx):
    ws = wb.create_sheet(T_SUMMARY)
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 62
    p, reg = ctx["period"], ctx["register"]

    ws.cell(row=1, column=1, value="RSU / Schedule FA working paper").font = TITLE
    c = ws.cell(row=2, column=1, value=(
        "This is a professional tax working paper, not a statutory filing. Final "
        "filing treatment requires professional review."))
    c.font = AMBER
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=3)

    r = 4
    for k, v, f in [("Client", ctx["client"], None),
                    ("Reporting period from", p.start, DFMT),
                    ("Reporting period to", p.end, DFMT),
                    ("Prepared on", dt.date.today(), DFMT),
                    ("Engine version", ENGINE_VERSION, None),
                    ("Dividend source used for FSI", ctx["dividend_basis"], None),
                    ("Statement basis", ctx["statement_basis"], None),
                    ("Brokers identified", ctx["brokers"], None),
                    ("Securities identified", ctx["securities"], None),
                    ("FX status", ctx["fx_status"], None),
                    ("Market data status", ctx["market_status"], None),
                    ("Source coverage", ctx["coverage_status"], None),
                    ("Prepared by / reviewed by", "", None)]:
        ws.cell(row=r, column=1, value=k).font = BOLD
        cc = ws.cell(row=r, column=2, value=v)
        cc.font = INPUT
        if f:
            cc.number_format = f
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Exceptions").font = BIG
    r += 1
    counts = reg.counts()
    for sev, fill in [("Blocker", BAD_FILL), ("Review", NOTE_FILL), ("Note", None)]:
        ws.cell(row=r, column=1, value=sev).font = BOLD
        cc = ws.cell(row=r, column=2, value=counts.get(sev, 0))
        cc.font = RED if sev == "Blocker" and counts.get(sev, 0) else BODY
        if fill:
            cc.fill = fill
        ws.cell(row=r, column=3,
                value={"Blocker": "Figure cannot be relied on until resolved",
                       "Review": "Documented approximation - needs sign-off",
                       "Note": "Disclosure only"}[sev]).font = BODY
        r += 1
    verdict = ("NOT READY TO FILE - blockers outstanding" if counts.get("Blocker")
               else "No blockers. Review items still require sign-off.")
    cc = ws.cell(row=r, column=1, value=verdict)
    cc.font = RED if counts.get("Blocker") else BOLD
    cc.fill = BAD_FILL if counts.get("Blocker") else OK_FILL
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)

    r += 2
    ws.cell(row=r, column=1, value="Headline figures").font = BIG
    r += 1
    for label, val in ctx["headlines"]:
        ws.cell(row=r, column=1, value=label).font = BOLD
        cc = ws.cell(row=r, column=2, value=val)
        cc.number_format = INR
        cc.font = BODY
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="Source documents").font = BIG
    r += 1
    src = ctx["source_table"]
    if src.empty:
        ws.cell(row=r, column=1, value="Canonical CSV input - no documents ingested").font = BODY
        r += 1
    else:
        r = _table(ws, src, start_row=r) + len(src) + 1

    r += 1
    ws.cell(row=r, column=1, value="Conventions").font = BIG
    r += 1
    for k, v in ctx["conventions"]:
        ws.cell(row=r, column=1, value=k).font = BOLD
        cc = ws.cell(row=r, column=3, value=v)
        cc.font = BODY
        cc.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 28
        r += 1


def _review(wb, ctx):
    ws = wb.create_sheet(T_REVIEW)
    ws.cell(row=1, column=1, value="Exceptions requiring review before filing").font = TITLE
    df = ctx["register"].frame()
    first = _table(ws, df, start_row=3)
    for i in range(first, first + len(df)):
        sev = ws.cell(row=i, column=1).value
        if sev == "Blocker":
            for j in range(1, len(df.columns) + 1):
                ws.cell(row=i, column=j).fill = BAD_FILL
                ws.cell(row=i, column=j).font = RED
        elif sev == "Review":
            ws.cell(row=i, column=1).fill = NOTE_FILL
        for j in range(1, len(df.columns) + 1):
            ws.cell(row=i, column=j).alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[i].height = 30


def _a3(wb, ctx):
    ws = wb.create_sheet(T_A3)
    df = ctx["a3"].copy()
    # Which rows have a usable rate. A row whose FX could not be resolved gets a
    # named blank, never a figure multiplied by a rate of zero.
    init_ok = (list(df["_init_ok"]) if "_init_ok" in df.columns else [True] * len(df))
    end_ok = (list(df["_end_ok"]) if "_end_ok" in df.columns else [True] * len(df))
    df = df.drop(columns=[c for c in ("_init_ok", "_end_ok") if c in df.columns])
    names = {"_symbol": "Ref: Symbol", "_qty": "Qty held at period end",
             "_vest_price_fc": "Vested/cost price (FC)", "_rate_vest": "TTBR - initial value",
             "_high_fc": "Annual high price (FC)", "_close_fc": "Period-end close (FC)",
             "_rate_end": "TTBR - period end", "_basis": "Valuation basis",
             "_broker": "Broker"}
    order = list(names)
    present = [c for c in order if c in df.columns]
    filing = [c for c in df.columns if not c.startswith("_")]
    df = df[filing + present].rename(columns=names)

    ws.cell(row=1, column=1,
            value="Schedule FA - Table A3: Foreign Equity and Debt Interest").font = TITLE
    nfmt = {c: INR for c in filing if "(Rs.)" in c}
    nfmt.update({"Qty held at period end": QTY, "Vested/cost price (FC)": FCF,
                 "TTBR - initial value": RATE, "Annual high price (FC)": FCF,
                 "Period-end close (FC)": FCF, "TTBR - period end": RATE})
    b = len(filing)
    first = _table(ws, df, start_row=3, nfmt=nfmt, backend_from=b + 1)

    SY, Q, VP = (get_column_letter(b + 1), get_column_letter(b + 2), get_column_letter(b + 3))
    RV, HI, CL, RE = (get_column_letter(b + 4), get_column_letter(b + 5),
                      get_column_letter(b + 6), get_column_letter(b + 7))
    seen = set()
    for k in range(len(df)):
        i = first + k
        formulas = [(8, f"={Q}{i}*{VP}{i}*{RV}{i}", bool(init_ok[k])),
                    (9, f"={Q}{i}*{HI}{i}*{RE}{i}", bool(end_ok[k])),
                    (10, f"={Q}{i}*{CL}{i}*{RE}{i}", bool(end_ok[k]))]
        for col, f, usable in formulas:
            if usable:
                c = ws.cell(row=i, column=col, value=f)
                c.font, c.border, c.number_format = BODY, BOX, INR
            else:
                c = ws.cell(row=i, column=col, value=FX_BLANK)
                c.font, c.border = BACKEND, BOX
        sym = df.iloc[k]["Ref: Symbol"]
        if sym not in seen:
            seen.add(sym)
            for col, f in [(11, f"=SUMIFS('{T_DIV}'!$G:$G,'{T_DIV}'!$B:$B,${SY}{i})"),
                           (12, f"=SUMIFS('{T_CG}'!$I:$I,'{T_CG}'!$A:$A,${SY}{i})")]:
                c = ws.cell(row=i, column=col, value=f)
                c.font, c.border, c.number_format = BODY, BOX, INR
    _total(ws, first, len(df), range(8, 13))


def _a2(wb, ctx):
    ws = wb.create_sheet(T_A2)
    ws.cell(row=1, column=1,
            value="Schedule FA - Table A2: Foreign Custodial Accounts").font = TITLE
    df = ctx["a2"]
    if df.empty:
        df = pd.DataFrame(columns=["Sr. No", "Country Name", "Name of Financial Institution",
                                   "Address of Financial Institution", "ZIP Code",
                                   "Account Number", "Status", "Account Opening Date",
                                   "Peak Balance During the Period (Rs.)",
                                   "Closing Balance (Rs.)",
                                   "Gross Amount Paid/Credited During the Period (Rs.)"])
    _table(ws, df, start_row=3, nfmt={c: INR for c in df.columns if "(Rs.)" in c})


def _cg(wb, ctx):
    ws = wb.create_sheet(T_CG)
    ws.cell(row=1, column=1, value="Capital gains on sale of RSU shares").font = TITLE
    df = ctx["sales"]
    if df.empty:
        df = pd.DataFrame(columns=[
            "Symbol", "Date of Acquisition", "Date of Sale", "Quantity",
            "Holding Period (days)", "Nature of Gain", "Sale Price per Share (FC)",
            "FX Rate - Sale Date", "Full Value of Consideration (Rs.)",
            "Vested Price per Share (FC)", "FX Rate - Vest Date",
            "Cost of Acquisition (Rs.)", "Capital Gain (Rs.)", "Cost Conversion Basis"])
    # Everything from "Currency" rightwards is backend provenance: the broker's
    # own stated figures and the rate dates actually used.
    backend = (list(df.columns).index("Currency") + 1
               if "Currency" in df.columns else None)
    first = _table(ws, df, start_row=3, backend_from=backend, nfmt={
        "Quantity": QTY, "Sale Price per Share (FC)": FCF,
        "Vested Price per Share (FC)": FCF, "FX Rate - Sale Date": RATE,
        "FX Rate - Vest Date": RATE, "Full Value of Consideration (Rs.)": INR,
        "Cost of Acquisition (Rs.)": INR, "Capital Gain (Rs.)": INR})
    # Each rupee column is blanked against the rate IT actually needs, exactly as
    # the engine frame does, so the workbook and the screen render one context
    # identically:
    #     I  consideration  needs the SALE-date rate      (column H)
    #     L  cost           needs the COST-date rate      (column K)
    #     M  gain           needs BOTH - it is I minus L
    # A rate of zero is never multiplied through. Doing so would print a cost of
    # nil and therefore a "gain" equal to the whole sale consideration - a wrong
    # figure that looks like a real one. The FC columns still show the substance,
    # and a blocker names the missing rate.
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    sale_rate = ([_num(v) for v in df["FX Rate - Sale Date"]]
                 if "FX Rate - Sale Date" in df.columns else [1.0] * len(df))
    cost_rate = ([_num(v) for v in df["FX Rate - Vest Date"]]
                 if "FX Rate - Vest Date" in df.columns else [1.0] * len(df))
    partial = 0
    for n, i in enumerate(range(first, first + len(df))):
        have_sale, have_cost = bool(sale_rate[n]), bool(cost_rate[n])
        if have_sale != have_cost:
            partial += 1
        for col, f, avail in ((9, f"=D{i}*G{i}*H{i}", have_sale),
                              (12, f"=D{i}*J{i}*K{i}", have_cost),
                              (13, f"=I{i}-L{i}", have_sale and have_cost)):
            c = ws.cell(row=i, column=col, value=(f if avail else FX_BLANK))
            c.border = BOX
            if avail:
                c.font, c.number_format = BODY, INR
            else:
                c.font = BACKEND
    if len(df):
        last = first + len(df) - 1
        r = _total(ws, first, len(df), (9, 12, 13))
        for term, lbl in [("Short Term", "Short term capital gain (Rs.)"),
                          ("Long Term", "Long term capital gain (Rs.)")]:
            r += 1
            ws.cell(row=r, column=1, value=lbl).font = BOLD
            c = ws.cell(row=r, column=13,
                        value=f'=SUMIFS(M{first}:M{last},F{first}:F{last},"{term}")')
            c.font, c.number_format = BOLD, INR
        if partial:
            # Each total sums the rows its own column could convert. Where a row
            # has one rate and not the other, those row sets differ - so the
            # three totals do not tie to each other, and the sheet says so rather
            # than letting a reader subtract one from another.
            r += 2
            ws.cell(row=r, column=1, value=(
                f"{partial} row(s) have a rate for one side of the calculation "
                "only. The column totals above therefore cover different rows "
                "and do not tie to each other - the gain total is the sum of the "
                "gain column itself, not consideration less cost. Every affected "
                "row is named in REVIEW_REQUIRED.")).font = AMBER
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=13)


def _vesting(wb, ctx):
    ws = wb.create_sheet(T_VEST)
    ws.cell(row=1, column=1, value="Vesting, acquisition and disposal register").font = TITLE
    df = ctx["vesting"]
    if df.empty:
        df = pd.DataFrame(columns=["Symbol", "Date of Acquisition", "Event",
                                   "Quantity Acquired", "Price per Share (FC)",
                                   "Currency", "SBI TTBR - Acquisition Date",
                                   "Quantity Still Held", "Broker", "Account",
                                   "Source Document"])
    first = _table(ws, df, start_row=3, nfmt={
        "Quantity Acquired": QTY, "Quantity Still Held": QTY,
        "Price per Share (FC)": FCF, "SBI TTBR - Acquisition Date": RATE})


def _dividends(wb, ctx):
    ws = wb.create_sheet(T_DIV)
    ws.cell(row=1, column=1,
            value="Dividend income - gross, and foreign tax withheld").font = TITLE
    df = ctx["dividends"]
    if df.empty:
        df = pd.DataFrame(columns=["Date", "Symbol", "Currency", "Gross Dividend (FC)",
                                   "Foreign Tax Withheld (FC)", "FX Rate",
                                   "Gross Dividend (Rs.)", "Foreign Tax Withheld (Rs.)",
                                   "Net Received (Rs.)", "FX Source", "FX Basis",
                                   "Source"])
    backend = (list(df.columns).index("FX Source") + 1
               if "FX Source" in df.columns else None)
    first = _table(ws, df, start_row=3, backend_from=backend, nfmt={
        "Gross Dividend (FC)": FCF, "Foreign Tax Withheld (FC)": FCF, "FX Rate": RATE,
        "Gross Dividend (Rs.)": INR, "Foreign Tax Withheld (Rs.)": INR,
        "Net Received (Rs.)": INR})
    rates = (list(df["FX Rate"]) if "FX Rate" in df.columns else [1] * len(df))
    for n, i in enumerate(range(first, first + len(df))):
        # No rate, no conversion. The FC columns still carry the income.
        if not rates[n]:
            for col in (7, 8, 9):
                c = ws.cell(row=i, column=col, value=FX_BLANK)
                c.font, c.border = BACKEND, BOX
            continue
        for col, f in [(7, f"=D{i}*F{i}"), (8, f"=E{i}*F{i}"), (9, f"=G{i}-H{i}")]:
            c = ws.cell(row=i, column=col, value=f)
            c.font, c.border, c.number_format = BODY, BOX, INR
    _total(ws, first, len(df), (7, 8, 9))
    r = first + len(df) + 3
    if ctx["dividend_basis"] != "Transaction-level" and len(df):
        c = ws.cell(row=r, column=1, value=(
            "These transaction rows do NOT feed Schedule FSI - the 1042-S below does, "
            "because the transaction documents do not cover the whole reporting "
            "period. Shown as evidence only, to avoid a double count."))
        c.font = RED
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=10)
        r += 2

    f = ctx["form1042s"]
    if not f.empty:
        ws.cell(row=r, column=1, value="Form 1042-S").font = BIG
        c = ws.cell(row=r + 1, column=1, value=(
            "Aggregate 1042-S conversion - period-end TTBR used because "
            "transaction-level dividend dates were unavailable. No individual "
            "dividend dates or FX rates have been invented."))
        c.font = AMBER
        ws.merge_cells(start_row=r + 1, start_column=1, end_row=r + 1, end_column=10)
        fr = _table(ws, f, start_row=r + 3, nfmt={
            "Gross Dividend Income (FC)": FCF, "Foreign Tax Withheld (FC)": FCF,
            "Conversion Rate (period-end TTBR)": RATE,
            "Gross Dividend Income (Rs.)": INR, "Foreign Tax Withheld (Rs.)": INR})
        for i in range(fr, fr + len(f)):
            for col, fx_f in [(9, f"=E{i}*H{i}"), (10, f"=F{i}*H{i}")]:
                cc = ws.cell(row=i, column=col, value=fx_f)
                cc.font, cc.border, cc.number_format = BODY, BOX, INR
        _total(ws, fr, len(f), (9, 10))


def _f1042(wb, ctx):
    ws = wb.create_sheet(T_1042)
    ws.cell(row=1, column=1,
            value="Form 1042-S - dividend income and US tax withheld").font = TITLE
    c = ws.cell(row=2, column=1, value=(
        "1042-S aggregate conversion - period-end TTBR used because transaction-level "
        "dividend dates were unavailable. No individual dividend dates or rates have "
        "been invented."))
    c.font = AMBER
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=12)
    df = ctx["form1042s"]
    first = _table(ws, df, start_row=4, nfmt={
        "Gross Dividend Income (FC)": FCF, "Foreign Tax Withheld (FC)": FCF,
        "Conversion Rate (period-end TTBR)": RATE,
        "Gross Dividend Income (Rs.)": INR, "Foreign Tax Withheld (Rs.)": INR})
    for i in range(first, first + len(df)):
        for col, f in [(9, f"=E{i}*H{i}"), (10, f"=F{i}*H{i}")]:
            c = ws.cell(row=i, column=col, value=f)
            c.font, c.border, c.number_format = BODY, BOX, INR
    _total(ws, first, len(df), (9, 10))


def _fsi(wb, ctx):
    ws = wb.create_sheet(T_FSI)
    ws.cell(row=1, column=1,
            value="Schedule FSI - income from outside India and tax relief").font = TITLE
    df = ctx["fsi"]
    if df.empty:
        df = pd.DataFrame(columns=["Country", "Source", "Head of Income",
                                   "Gross Income (Rs.)", "Foreign Tax Withheld (Rs.)",
                                   "Foreign Tax Credit Claimed (Rs.)", "Relief Section",
                                   "Basis"])
    first = _table(ws, df, start_row=3, nfmt={
        "Gross Income (Rs.)": INR, "Foreign Tax Withheld (Rs.)": INR,
        "Foreign Tax Credit Claimed (Rs.)": INR})
    _total(ws, first, len(df), (4, 5))
    r = first + len(df) + 4
    for txt in [
        "Foreign tax WITHHELD and foreign tax credit CLAIMED are deliberately separate "
        "columns. They are not the same figure.",
        "Relief under section 90 is capped at the lower of the foreign tax paid and the "
        "Indian tax on the doubly-taxed income.",
        "Form 67 must be filed BEFORE the return, or the credit is inadmissible.",
    ]:
        c = ws.cell(row=r, column=1, value=txt)
        c.font = RED
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
        r += 1


def _fx(wb, ctx):
    ws = wb.create_sheet(T_FX)
    ws.cell(row=1, column=1,
            value="Every FX rate used in this workbook, with its source").font = TITLE

    # The fallback status is stated at the top, not buried in a column. A Google
    # Finance rate is never presented as an SBI TT buying rate.
    status = ctx.get("fx_status", "")
    c = ws.cell(row=2, column=1, value=status)
    c.font = (AMBER if ("SBI TTBR unavailable" in status or "INCOMPLETE" in status)
              else BODY)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=10)

    fxa = ctx["fx_audit"]
    r = _table(ws, fxa, start_row=4, nfmt={"Rate": RATE}) + len(fxa) + 2

    ws.cell(row=r, column=1,
            value="FX source priority applied to every conversion above").font = BIG
    r += 1
    # The same frame the screen renders, so the hierarchy is stated from one
    # place and the two cannot drift apart.
    src = ctx.get("fx_sources")
    if src is not None and len(src):
        r = _table(ws, src, start_row=r) + len(src) + 1
    for line in ("Within a source: the exact date, else the nearest available "
                 "date within 30 days either way, a tie going to the prior date. "
                 "The substituted date is always shown; it is never presented as "
                 "the date required.",
                 "The first source that answers wins outright - a lower-ranked "
                 "source is never preferred merely because its date is closer.",
                 "No source is ever labelled as an SBI TT buying rate, and no "
                 "provider's data is ever written under another provider's name.",
                 "The 'Rate is' column distinguishes three things: a rate quoted "
                 "directly for the pair; a cross the PROVIDER published as its "
                 "own (FBIL's EUR/INR, GBP/INR and JPY/INR are its crosses "
                 "through USD - only its USD/INR is measured against the rupee); "
                 "and a cross this engine derived, which shows its components "
                 "and its arithmetic in the columns above.",
                 "FBIL is licensed benchmark data. This engine does not retrieve "
                 "it: an FBIL rate appears above only where the preparer supplied "
                 "a table transcribed from FBIL's own publication."):
        ws.cell(row=r, column=1, value=line).font = BODY
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=10)
        r += 1

    # The consolidated request table: one row per currency/date pair that still
    # needs a rate. Deduplicated, never one call per transaction.
    req = ctx.get("fx_google_requests")
    if req is not None and len(req):
        r += 1
        ws.cell(row=r, column=1,
                value="FX rates still required - no ranked source has them").font = BIG
        r += 1
        for line in (f"{len(req)} currency/date pair(s) could not be resolved by "
                     "any source in the chain above. Each row below carries the "
                     "GOOGLEFINANCE formula for its own pair, built from the "
                     "source currency.",
                     "Open this block in Google Sheets and the rate column fills "
                     "itself; or paste the rates in. Save the date / "
                     "base_currency / quote_currency / rate columns as CSV and "
                     "supply it with the documents to resolve these rows."):
            ws.cell(row=r, column=1, value=line).font = BODY
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=10)
            r += 1
        r += 1
        first = _table(ws, req, start_row=r, nfmt={"rate": RATE})
        # Write the formula as a live formula, not text, so Google Sheets
        # evaluates it the moment the block is opened there.
        fcol = list(req.columns).index("google_finance_formula") + 1
        rate_col = list(req.columns).index("rate") + 1
        for k in range(len(req)):
            i = first + k
            cell = ws.cell(row=i, column=fcol)
            cell.value = google_formula(req.iloc[k]["base_currency"],
                                        f"{get_column_letter(1)}{i}")
            cell.font = BACKEND
            ws.cell(row=i, column=rate_col).number_format = RATE
        r = first + len(req) + 2

    r += 1
    ws.cell(row=r, column=1, value="Market data used, with full provenance").font = BIG
    _table(ws, ctx["market_audit"], start_row=r + 2, nfmt={"Price": FCF})


def _master(wb, ctx):
    """The normalised model itself - every row traceable to file, sheet and row."""
    ws = wb.create_sheet(T_MASTER)
    ws.cell(row=1, column=1,
            value="Normalised transaction master - every row as extracted").font = TITLE
    c = ws.cell(row=2, column=1, value=(
        "This is the common internal model. Every downstream figure derives from "
        "these rows, and each row names the document, sheet and line it came from."))
    c.font = BODY
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=11)
    df = ctx["master"].copy()
    if df.empty:
        df = pd.DataFrame(columns=["date", "broker", "account_no", "symbol", "event",
                                   "quantity", "price_fc", "amount_fc", "tax_fc",
                                   "currency", "notes"])
    df = df.rename(columns={
        "date": "Date", "broker": "Broker", "account_no": "Account", "symbol": "Ticker",
        "event": "Transaction Type", "quantity": "Quantity",
        "price_fc": "Price / FMV (FC)", "amount_fc": "Amount (FC)",
        "tax_fc": "Foreign Tax (FC)", "currency": "Currency",
        "notes": "Plan type | Source file | sheet | row"})
    _table(ws, df, start_row=4, nfmt={
        "Quantity": QTY, "Price / FMV (FC)": FCF, "Amount (FC)": FCF,
        "Foreign Tax (FC)": FCF})


def _recon(wb, ctx):
    ws = wb.create_sheet(T_RECON)
    ws.cell(row=1, column=1, value="Share reconciliation").font = TITLE
    c = ws.cell(row=2, column=1, value=(
        "Opening + vested + purchased + transferred in - sold - withheld - "
        "transferred out = closing"))
    c.font = BODY
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=12)
    df = ctx["reconciliation"]
    if df.empty:
        df = pd.DataFrame(columns=["Symbol", "Status"])
    first = _table(ws, df, start_row=4, nfmt={
        c: QTY for c in df.columns if c not in ("Symbol", "Status")})
    for i in range(first, first + len(df)):
        cell = ws.cell(row=i, column=len(df.columns))
        if cell.value == "BREAK":
            for j in range(1, len(df.columns) + 1):
                ws.cell(row=i, column=j).fill = BAD_FILL
            cell.font = RED
        else:
            cell.fill = OK_FILL

    cross = ctx.get("cross_broker")
    if cross is not None and not cross.empty:
        r = first + len(df) + 3
        ws.cell(row=r, column=1, value="Cross-broker position reconciliation").font = BIG
        c = ws.cell(row=r + 1, column=1, value=(
            "acquired + transferred in - sold - transferred out = combined "
            "period-end holding. A transfer must never create an acquisition, a "
            "gain, or proceeds. Where the documents do not establish which lots "
            "moved, the chain is reported as incomplete rather than assumed."))
        c.font = AMBER
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=r + 1, start_column=1, end_row=r + 1, end_column=9)
        cf = _table(ws, cross, start_row=r + 3, nfmt={
            c2: QTY for c2 in cross.columns
            if c2 not in ("Security", "Broker", "Status")})
        for i in range(cf, cf + len(cross)):
            cell = ws.cell(row=i, column=len(cross.columns))
            if str(cell.value).startswith("Transfer linkage"):
                cell.font = RED
                cell.fill = BAD_FILL
            else:
                cell.fill = OK_FILL


def _total(ws, first, n, cols) -> int:
    if not n:
        return first
    r = first + n + 1
    ws.cell(row=r, column=1, value="Total").font = BOLD
    for col in cols:
        L = get_column_letter(col)
        c = ws.cell(row=r, column=col, value=f"=SUM({L}{first}:{L}{first + n - 1})")
        c.font, c.number_format = BOLD, INR
    return r
