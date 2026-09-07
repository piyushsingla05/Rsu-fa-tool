"""Orchestrator. Canonical CSVs in, Schedule FA working paper out.

prepare() is pure - inputs in, dataframes out, no disk writes - so the same call
serves the CLI today and the web endpoint later. write_workbook() is the only
function that touches the filesystem.
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd

from .compute import (
    ComputeOptions, ComputeResult, build_a2, build_a3, build_cg, build_dividends,
    build_lots, build_reconciliation, build_cross_broker, coverage_gaps,
)
from .excel_out import ENGINE_VERSION, write_workbook
from .fx import FXTable
from .ingest.extract import extract_file
from .ingest.profiles import FORM_1042S, TTBR_TABLE, load_profiles
from .marketdata import MarketData
from .models import (
    ACCOUNTS_COLUMNS, CASH_COLUMNS, ENTITIES_COLUMNS, EVENTS_COLUMNS,
    FORM1042S_COLUMNS, FX_SAME_DAY, Period, empty,
)
from .review import Register, UNVERIFIED_FX
from .review import CG_CONTROL_TOTAL, CG_SOURCE_PRECEDENCE, STALE_FX
from .review import INVALID_NUMERIC_FIELD
from .review import Severity

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FX = ROOT / "config" / "fx_rates.csv"
DEFAULT_MARKET = ROOT / "config" / "market_prices.csv"
# Consolidated Google Finance FX fallback table. Optional: absent simply means
# no secondary FX source, and unresolved rates stay unresolved.
DEFAULT_GOOGLE_FX = ROOT / "config" / "fx_google_finance.csv"
# External historical reference tables, fetched deliberately by src/fxfetch.py
# and never during a run. Absent simply means that source has nothing to offer.
DEFAULT_FBIL_FX = ROOT / "config" / "fx_fbil.csv"
DEFAULT_ECB_FX = ROOT / "config" / "fx_ecb.csv"
NUMERIC = ("quantity", "price_fc", "amount_fc", "tax_fc", "cost_fc", "balance_fc",
           "gross_income_fc", "tax_withheld_fc")
OPTIONAL = ("cost_fc", "acquired_on")

CONVENTIONS = [
    ("A3 initial value", "Held qty x vested/cost price x SBI TTBR on the vest date."),
    ("A3 peak value", "Held qty at period end x highest price during the period x "
                      "SBI TTBR as on the period-end date."),
    ("A3 closing value", "Held qty x period-end closing price x SBI TTBR as on the "
                         "period-end date."),
    ("A3 scope", "One row per lot still held at period end. Lots fully sold during "
                 "the period appear only through gross proceeds."),
    ("Capital gains", "Sale at sale-date TTBR; cost at vest-date TTBR, falling back "
                      "to the sale-date TTBR where the vest date is unavailable."),
    ("Dividends", "Gross, before foreign withholding. Tax withheld is carried to "
                  "Schedule FSI separately from the credit claimed."),
    ("Lot matching", "FIFO. Long term above 24 months."),
    ("Country code", "Not reported. Country name only."),
]


def _read(path: Path, columns, required=False) -> pd.DataFrame:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Required input missing: {path}")
        return empty(columns)
    df = pd.read_csv(path)
    for c in OPTIONAL:
        if c in columns and c not in df.columns:
            df[c] = ""
    missing = set(columns) - set(df.columns)
    if missing:
        raise ValueError(f"{path.name} is missing columns: {sorted(missing)}")
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in NUMERIC:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def resolve_positions(events: pd.DataFrame, period: Period, register: Register):
    """Collapse broker-stated position snapshots into one holding per security.

    A monthly statement states the SAME shares every month. Only the latest
    snapshot on or before the period end may become a holding; the rest are
    superseded, and any snapshot after the period end is out of scope.
    """
    if events.empty or "event" not in events:
        return events
    pos = events[events["event"] == "POSITION"]
    if pos.empty:
        return events

    # Account extraction is never perfect across brokers. Where some snapshots of
    # the same broker and security carry an account number and others do not, the
    # blanks belong to that same account - otherwise they form a phantom second
    # holding and the position is counted twice.
    pos = pos.copy()
    pos["account_no"] = pos["account_no"].fillna("").astype(str).str.strip()
    for (b, sym), grp in pos.groupby(["broker", "symbol"], dropna=False):
        named = sorted({a for a in grp["account_no"] if a})
        if len(named) == 1:
            blanks = grp.index[grp["account_no"] == ""]
            if len(blanks):
                pos.loc[blanks, "account_no"] = named[0]
                register.note(
                    "Security/ticker mapping missing", f"{b} {sym}",
                    f"{len(blanks)} position snapshot(s) carried no account number "
                    f"and were attributed to {named[0]}, the only account this "
                    "broker reports for the security.",
                    "Position resolution", "Confirm there is only one account.")

    keep_idx, dropped, late = [], 0, 0
    for key, grp in pos.groupby(["broker", "account_no", "symbol"], dropna=False):
        eligible = grp[grp["date"] <= period.end]
        late += len(grp) - len(eligible)
        if eligible.empty:
            register.review(
                "Limited statement coverage", str(key[2]),
                f"Every stated position for {key[2]} is dated after "
                f"{period.end:%d-%m-%Y}; none can serve as the period-end holding.",
                "Position snapshots", "Supply a statement at or before the period end.")
            continue
        latest = eligible["date"].max()
        chosen = eligible[eligible["date"] == latest]
        keep_idx += list(chosen.index)
        dropped += len(eligible) - len(chosen)
        if latest != period.end:
            register.review(
                "Limited statement coverage", str(key[2]),
                f"The latest stated position is at {latest:%d-%m-%Y}, not the period "
                f"end {period.end:%d-%m-%Y}. It is used as the period-end holding.",
                "Position snapshots",
                "Supply the period-end statement to remove the approximation.")
    if dropped or late:
        register.note(
            "Duplicate transaction", "Position snapshots",
            f"{dropped} superseded and {late} out-of-period position snapshot(s) "
            "were set aside so the holding is counted once, not once per statement.",
            "Position resolution", "None.")

    out = events.copy()
    out.loc[pos.index, "account_no"] = pos["account_no"]     # carry the fill through
    drop = [i for i in pos.index if i not in keep_idx]

    # The stated position already contains every share that vested into or was
    # transferred into that account. Adding those acquisitions again would count
    # the same shares twice, so they are set aside as evidence.
    superseded = 0
    for i in keep_idx:
        row = pos.loc[i]
        same = out[(out["broker"] == row["broker"])
                   & (out["account_no"].astype(str) == str(row["account_no"]))
                   & (out["symbol"] == row["symbol"])
                   & (out["event"].isin(["VEST", "BUY", "TRANSFER_IN"]))
                   & (out["date"] <= row["date"])]
        if len(same):
            drop += list(same.index)
            superseded += len(same)
    if superseded:
        register.note(
            "Duplicate transaction", "Position snapshots",
            f"{superseded} acquisition row(s) were superseded by the broker's own "
            "stated position, which already includes those shares.",
            "Position resolution", "None - prevents a double count.")

    # POSITION is kept as its own event so downstream logic can tell a stated
    # holding apart from a transaction that built one up.
    out = out.drop(index=[i for i in set(drop) if i in out.index])
    return out.reset_index(drop=True)


def resolve_capital_gain_sources(frames, sources, register: Register):
    """Decide which document states each broker's realised gains, so no sale counts twice.

    Same shape as the frozen dividend rule: precedence is decided on ACTUAL
    COVERAGE, not on amounts. A broker's realised gain/loss report is a complete
    closed-lot record - quantity, acquisition date, sale date, proceeds and the
    cost the broker actually removed. Where one is supplied, it is the capital-
    gains source of record for that broker over the dates it covers, and any
    disposal of the same security on the same date from another document of that
    broker is the same sale seen twice.

    It supersedes only what it actually covers. A sale outside the report's dates
    is still taken from the statement, so a quarterly statement keeps working
    exactly as before alongside a part-year report.
    """
    authorities = [r for r in sources if getattr(r, "cg_authority", False)
                   and r.period_start and r.period_end]
    if not authorities:
        return [f for f, _ in frames]

    # What the authority states, keyed by broker -> security -> dates covered.
    covered: dict[tuple[str, str], list[tuple]] = {}
    for rec in authorities:
        for lot in (rec.closed_lots or []):
            covered.setdefault((str(lot["broker"]), str(lot["symbol"])), []).append(
                (lot["date"], rec))

    out, suppressed = [], {}
    for frame, rec in frames:
        if getattr(rec, "cg_authority", False) or frame.empty:
            out.append(frame)
            continue
        drop = []
        for i, r in frame.iterrows():
            if str(r.get("event")) != "SELL":
                continue
            hits = covered.get((str(r.get("broker")), str(r.get("symbol"))), [])
            if any(d == r.get("date") for d, _ in hits):
                drop.append(i)
        if drop:
            auth = hits[0][1].file if hits else "the realised gain/loss report"
            key = (rec.file, auth)
            g = suppressed.setdefault(key, {"rows": 0, "proceeds": 0.0})
            g["rows"] += len(drop)
            g["proceeds"] += float(
                pd.to_numeric(frame.loc[drop, "amount_fc"], errors="coerce").fillna(0).sum())
            frame = frame.drop(index=drop)
        out.append(frame)

    for (file, auth), g in suppressed.items():
        register.note(
            CG_SOURCE_PRECEDENCE, file,
            f"{g['rows']} sale row(s) in {file} totalling ${g['proceeds']:,.2f} of "
            f"proceeds are the same disposals stated lot by lot in {auth}. The "
            "lot-level report is the capital-gains source of record, so the "
            "statement's rows are held as evidence and excluded from the "
            "capital-gains computation. Nothing is counted twice.",
            file, "Confirm the two documents describe the same sales.")
    return out


def flag_closed_lot_reports(sources, register: Register):
    """Check each realised gain/loss report against its own stated control total."""
    for rec in sources:
        for b in (getattr(rec, "recon_breaks", None) or []):
            register.review(
                "G&L adjusted figures do not reconcile", b["symbol"],
                b["detail"] + " The broker's figures are reported as stated and "
                "have not been altered to force agreement.",
                b["source"], "Confirm the adjusted figures with the broker.")

        ctrl = getattr(rec, "control_total", None)
        lots = getattr(rec, "closed_lots", None) or []
        if not ctrl or not lots:
            continue
        qty = sum(float(l["quantity"]) for l in lots)
        gain = sum(float(l["broker_adj_gain_fc"]) for l in lots)
        ok_qty = abs(qty - float(ctrl.get("quantity") or 0)) < 0.01
        ok_gain = abs(gain - float(ctrl.get("gain_fc") or 0)) < 0.05
        if ok_qty and ok_gain:
            register.note(
                CG_CONTROL_TOTAL, rec.file,
                f"{len(lots)} closed lots totalling {qty:,.0f} shares and an "
                f"adjusted gain of ${gain:,.2f} agree with the report's own summary "
                f"line. The report's ordinary Gain/Loss of "
                f"${float(ctrl.get('ordinary_gain_fc') or 0):,.2f} is stated for "
                "reconciliation only - it excludes the ordinary income already "
                "taxed as a perquisite and is not the Indian capital gain.",
                ctrl.get("source", rec.file), "None.")
        else:
            register.blocker(
                CG_CONTROL_TOTAL, rec.file,
                f"The extracted lots total {qty:,.0f} shares and ${gain:,.2f} of "
                f"adjusted gain, but the report's own summary line states "
                f"{float(ctrl.get('quantity') or 0):,.0f} shares and "
                f"${float(ctrl.get('gain_fc') or 0):,.2f}. Some rows may not have "
                "been read.",
                ctrl.get("source", rec.file),
                "Reconcile the report to its summary line before filing.")


def flag_invalid_quantities(sources, register: Register):
    """A quantity/shares cell that was present but failed to parse.

    extract.py never drops such a row silently and never treats it as a real
    zero - it excludes the row from every downstream figure (the holding, a
    transfer, a disposal, a perquisite adjustment) and records what it saw on
    the SourceRecord instead, exactly like the existing recon_breaks pattern
    above. This turns each of those into a visible, actionable register entry
    naming the broker, the event/role, the source coordinates and the raw
    value that could not be parsed.
    """
    for rec in sources:
        for iv in (getattr(rec, "invalid_quantities", None) or []):
            register.blocker(
                INVALID_NUMERIC_FIELD, iv.get("symbol", ""),
                f"A {iv.get('role', '')} row for "
                f"{iv.get('event') or 'this event'} has an unparseable/"
                f"invalid quantity ({iv.get('raw', '')}) and has been "
                "excluded rather than treated as zero or guessed. Any "
                "holding, transfer, disposal or perquisite figure that would "
                "have included this row is understated until this is "
                "corrected.",
                iv.get("broker", ""),
                "Correct the quantity in the source data and re-run.")


def flag_equity_plan_evidence(sources, register: Register, period=None):
    """Disclose what a statement says about the plan but does NOT put in the account.

    Two things a company-sponsored plan statement states that a reviewer must
    see, and that Schedule FA must NOT silently swallow:

    * Unvested awards and unexercised options. The employee does not own them
      yet, so they are not a foreign asset to report - but they are large, they
      are printed on the statement, and a reviewer who cannot see why they were
      left out cannot sign the file.
    * The perquisite inside an option exercise. The spread between the option
      cost and the market value at exercise is SALARY, already taxed, and it is
      declared on a different head of income. The engine has removed it from the
      capital gain; someone still has to put it where it belongs.
    """
    for rec in sources:
        awards = getattr(rec, "unvested_awards", None) or []
        if period is not None:
            # A plan position stated after the period end says nothing about
            # what was held during it.
            awards = [a for a in awards
                      if a.get("as_at") and a["as_at"] <= period.end]
        if awards:
            latest = max((a["as_at"] for a in awards if a.get("as_at")), default=None)
            current = [a for a in awards if a.get("as_at") == latest]
            units = sum(a["unvested"] for a in current)
            value = sum(a["unvested_value_fc"] for a in current)
            pending = sum(a.get("vested_pending") or 0 for a in current)
            register.note(
                "Unvested awards excluded from Schedule FA",
                f"{rec.broker} {current[0]['symbol'] if current else ''}".strip(),
                f"{units:,.0f} unvested award/unit(s) with a stated potential value "
                f"of ${value:,.2f}"
                + (f", plus {pending:,.0f} vested but pending lapse" if pending else "")
                + f", as stated at {latest:%d-%m-%Y}. Unvested awards are not owned "
                "and are NOT reported in Schedule FA. The statement itself excludes "
                "them from the account value.",
                rec.file,
                "Confirm none of these vested on or before the period end.")

        exercises = getattr(rec, "exercises", None) or []
        applied = [e for e in exercises if e.get("applied_to")]
        if applied:
            perq = sum(e["fmv_at_exercise_fc"] - e["option_cost_fc"] for e in applied)
            register.review(
                "Option exercise perquisite - salary, not capital gain",
                f"{rec.broker} {applied[0]['symbol']}",
                f"{len(applied)} option exercise(s) totalling "
                f"${sum(e['quantity'] for e in applied):,.0f} shares. The spread "
                f"between the option cost and the market value at exercise is "
                f"${perq:,.2f}. That is a PERQUISITE taxed as salary, so the cost "
                "of acquisition used for capital gains is the value at exercise, "
                "not the option cost. Using the broker's stated capital gain "
                "would tax the same income twice.",
                rec.file,
                "Confirm the perquisite is included in salary income and that "
                "employer withholding has been credited. It is not part of the "
                "capital-gains figure on this working paper.")


def ingest_documents(doc_dir: Path, register: Register, period=None):
    """Upload folder -> normalised events, source registry, TTBR rows.

    An unrecognised broker is processed by the generic pass and flagged, never
    rejected. Anything that cannot be mapped becomes a review item.
    """
    profiles = load_profiles()
    frames, sources, ttbr_frames, unmapped, cash_rows = [], [], [], [], []
    seen_hashes: dict[str, str] = {}
    for f in sorted(Path(doc_dir).iterdir()):
        if f.is_dir() or f.name.startswith("."):
            continue
        try:
            ev, rec, un, rates = extract_file(f, profiles)
        except Exception as exc:
            register.blocker(
                "Unknown document format", f.name,
                f"The file could not be read: {type(exc).__name__}: {exc}",
                f.name, "Inspect the file, or supply the data as canonical CSV.")
            continue
        # The same statement uploaded twice - "(1)" copies are routine - must not
        # be counted twice.
        if rec.content_hash and rec.content_hash in seen_hashes:
            register.note(
                "Duplicate transaction", rec.file,
                f"Byte-identical to {seen_hashes[rec.content_hash]}; ignored so its "
                "figures are not counted twice.",
                rec.file, "None - confirm the duplicate upload was unintended.")
            continue
        if rec.content_hash:
            seen_hashes[rec.content_hash] = rec.file

        sources.append(rec)
        cash_rows += getattr(rec, "cash", []) or []
        if len(rates):
            ttbr_frames.append(rates)
        if len(ev):
            frames.append((ev, rec))
        unmapped += [(rec.file, cols) for _, cols in un]

        # A rate table or a tax form is not a broker statement, so an unknown
        # broker flag would be noise.
        if rec.profile_id == "generic" and rec.document_type not in (
                TTBR_TABLE, FORM_1042S):
            register.review(
                "Unknown broker", rec.broker or rec.file,
                f"No configured profile matched {rec.file}. Generic extraction was "
                f"used and produced {rec.rows} rows "
                f"(confidence {rec.confidence:.0%}, securities: "
                f"{', '.join(rec.securities) or 'none identified'}).",
                rec.file,
                "Confirm the extracted rows, then promote the mapping to a profile "
                "in config/mappings/ so future statements process automatically.")
        if rec.securities and "UNKNOWN" in rec.securities:
            register.review(
                "Security/ticker mapping missing", rec.file,
                "One or more rows could not be tied to a ticker.",
                rec.file, "Add the security to the profile's symbol_from_text map.")
    for f, cols in unmapped:
        if cols:
            register.note(
                "Low-confidence extraction", f,
                "Columns present in the source but not mapped: " + ", ".join(cols[:10]),
                f, "Confirm nothing material was left behind.")

    flag_invalid_quantities(sources, register)
    flag_closed_lot_reports(sources, register)
    flag_equity_plan_evidence(sources, register, period)
    kept = resolve_capital_gain_sources(frames, sources, register)
    kept = [f for f in kept if len(f)]
    events = (pd.concat(kept, ignore_index=True) if kept
              else empty(EVENTS_COLUMNS))
    ttbr = (pd.concat(ttbr_frames, ignore_index=True) if ttbr_frames
            else pd.DataFrame())
    cash = pd.DataFrame(cash_rows) if cash_rows else pd.DataFrame()
    return events, sources, ttbr, cash


def prepare(client_dir: Path, period: Period, fx_path: Path, market_path: Path,
            opts: ComputeOptions | None = None, doc_dir: Path | None = None,
            google_fx_path: Path | None = None,
            fbil_fx_path: Path | None = None, ecb_fx_path: Path | None = None):
    opts = opts or ComputeOptions()
    register = Register()
    gfx = Path(google_fx_path) if google_fx_path else DEFAULT_GOOGLE_FX
    bfx = Path(fbil_fx_path) if fbil_fx_path else DEFAULT_FBIL_FX
    efx = Path(ecb_fx_path) if ecb_fx_path else DEFAULT_ECB_FX
    fx = FXTable(fx_path, register=register,
                 google_path=gfx if Path(gfx).exists() else None,
                 fbil_path=bfx if Path(bfx).exists() else None,
                 ecb_path=efx if Path(efx).exists() else None)
    md = MarketData(market_path, register)

    sources = []
    if doc_dir and Path(doc_dir).exists():
        events, sources, uploaded_ttbr, ingested_cash = ingest_documents(
            Path(doc_dir), register, period)
        events = resolve_positions(events, period, register)
        # A returned Google Finance FX table is a SECONDARY source. It is merged
        # into the Google provider, never into the SBI table - the two are not
        # interchangeable and the workbook must keep saying which is which.
        for rec in sources:
            g = getattr(rec, "google_fx", None)
            if g is not None and len(g):
                fx.merge_google(g)
                register.note(
                    "FX source", "Google Finance fallback",
                    f"{len(g)} Google Finance FX rate(s) loaded from {rec.file}. "
                    "They are used ONLY where SBI TTBR is unavailable for the "
                    "date, and every use is labelled on FX_WORKING.",
                    rec.file, "Confirm the rates before filing.")
        if len(uploaded_ttbr):
            # A user-supplied SBI TTBR table outranks the engine's own default.
            fx.merge(uploaded_ttbr, priority=True)
            register.note(
                "FX source", "SBI TTBR",
                f"{len(uploaded_ttbr)} user-supplied TTBR rows loaded and given "
                "priority over the engine's default table.",
                "User upload", "None.")
    else:
        events = _read(client_dir / "events.csv", EVENTS_COLUMNS, required=True)
        ingested_cash = pd.DataFrame()
    cash = _read(client_dir / "cash.csv", CASH_COLUMNS)
    if len(ingested_cash):
        cash = pd.concat([cash, ingested_cash[CASH_COLUMNS]], ignore_index=True)
    entities = _read(client_dir / "entities.csv", ENTITIES_COLUMNS)
    accounts = _read(client_dir / "accounts.csv", ACCOUNTS_COLUMNS)
    f1042_in = _read(client_dir / "form1042s.csv", FORM1042S_COLUMNS)
    ingested_1042 = [r for rec in sources for r in (getattr(rec, "form1042s", []) or [])]
    if ingested_1042:
        f1042_in = pd.concat([f1042_in, pd.DataFrame(ingested_1042)[FORM1042S_COLUMNS]],
                             ignore_index=True)

    lots, matches = build_lots(events, register, as_at=period.end)
    a3 = build_a3(events, lots, entities, md, fx, period, opts, register)
    a2 = build_a2(cash, accounts, events, fx, period, register)
    sales = build_cg(matches, fx, opts, period, register)
    # Only documents that could actually report dividends count toward dividend
    # coverage. A realised gain/loss summary is not a dividend record.
    div_intervals = [(r.period_start, r.period_end) for r in sources
                     if r.document_type != FORM_1042S
                     and getattr(r, "reports_dividends", True)]
    if not sources:
        # Canonical-CSV mode: coverage is asserted by the preparer.
        div_intervals = [(period.start, period.end)]
    dividends, form1042s, fsi, div_basis = build_dividends(
        events, f1042_in, fx, period, register, txn_intervals=div_intervals)
    recon = build_reconciliation(events, lots, period, register)
    transfers = [t for r in sources for t in (getattr(r, "transfers", []) or [])]
    cross = build_cross_broker(events, transfers, lots, period, register)

    vesting = pd.DataFrame([{
        "Symbol": l.symbol, "Date of Acquisition": l.acquired, "Event": l.event,
        "Quantity Acquired": round(l.quantity, 4),
        "Price per Share (FC)": l.price_fc, "Currency": l.currency,
        # A rate of zero is not a rate. Where none is on file the cell is
        # blank, exactly as it is everywhere else in the workbook.
        "SBI TTBR - Acquisition Date": (
            fx.rate(l.acquired, l.currency, FX_SAME_DAY,
                    purpose="Vesting register") or ""),
        "Quantity Still Held": round(l.remaining, 4),
        "Broker": l.broker, "Account": l.account_no,
        "Source Document": "",
    } for l in lots])

    unverified = {u.used_date for u in fx.uses if not u.verified}
    if unverified:
        register.review(
            UNVERIFIED_FX, "All conversions",
            f"{len(unverified)} of the SBI TTBR dates used are not verified against "
            "an SBI source.", fx_path.name,
            "Verify against SBI and set verified=Y in the FX table.")

    result = ComputeResult(a3=a3, a2=a2, vesting=vesting, sales=sales,
                           dividends=dividends, form1042s=form1042s, fsi=fsi,
                           reconciliation=recon, dividend_basis=div_basis)
    result.summary = {"sources": sources, "events": events, "cross_broker": cross,
                      "transfers": transfers}
    return result, fx, md, register


def build_context(client, period, result, fx, md, register, opts, sources):
    def total(df, col):
        """Sum a column, ignoring rows the engine declined to compute.

        A blank means "not computable - the rate is missing", not zero, so it is
        excluded from the total rather than silently treated as nil.
        """
        if df.empty or col not in df:
            return 0.0
        return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())

    master = result.summary.get("events", pd.DataFrame())
    brokers = sorted({str(b) for b in master.get("broker", []) if str(b).strip()})
    securities = sorted({str(x) for x in master.get("symbol", []) if str(x).strip()})

    src_table = pd.DataFrame([{
        "File": r.file, "Type": r.document_type, "Broker": r.broker,
        "Mapping": r.profile_id, "Confidence": f"{r.confidence:.0%}",
        "Period covered": r.coverage, "Rows": r.rows,
        "Securities": ", ".join(r.securities),
    } for r in sources])

    # Coverage is reported on documents that could carry INCOME, which is what a
    # gap actually threatens. A realised gain/loss summary spans the year but
    # reports no dividends, so counting it would overstate coverage.
    gaps = coverage_gaps([(r.period_start, r.period_end) for r in sources
                          if r.document_type != FORM_1042S
                          and getattr(r, "reports_dividends", True)],
                         period) if sources else []
    coverage_status = ("Complete for the reporting period" if (sources and not gaps)
                       else ("Income-document gaps: "
                             + "; ".join(f"{a:%d-%m-%Y} to {b:%d-%m-%Y}"
                                         for a, b in gaps)) if sources
                       else "Canonical CSV input - coverage asserted by preparer")

    fx_unver = len({u.used_date for u in fx.uses if not u.verified})
    fx_status = fx.fx_status
    if fx_unver and "SBI TT buying rate throughout" in fx_status:
        fx_status = f"SBI TTBR throughout; {fx_unver} rate date(s) unverified"
    approx = [p.ticker for p in md.used if p.is_approximate]
    mkt_status = ("All prices verified" if not approx
                  else f"Approximate annual high for: {', '.join(sorted(set(approx)))}")

    headlines = [
        ("A3 - total closing value (Rs.)", total(result.a3, "Closing Value (Rs.)")),
        ("A3 - total peak value (Rs.)",
         total(result.a3, "Peak Value of Investment During the Period (Rs.)")),
        ("A2 - total closing balance (Rs.)", total(result.a2, "Closing Balance (Rs.)")),
        ("Capital gain (Rs.)", total(result.sales, "Capital Gain (Rs.)")),
        ("Gross dividend income (Rs.)", total(result.fsi, "Gross Income (Rs.)")),
        ("Foreign tax withheld (Rs.)", total(result.fsi, "Foreign Tax Withheld (Rs.)")),
    ]
    return {
        "client": client, "period": period, "register": register,
        "master": master, "brokers": ", ".join(brokers) or "-",
        "securities": ", ".join(securities) or "-",
        "source_table": src_table, "coverage_status": coverage_status,
        "fx_status": fx_status, "market_status": mkt_status,
        "fx_google_requests": fx.google_request_frame(),
        "a3": result.a3, "a2": result.a2, "vesting": result.vesting,
        "sales": result.sales, "dividends": result.dividends,
        "form1042s": result.form1042s, "fsi": result.fsi,
        "reconciliation": result.reconciliation,
        "cross_broker": result.summary.get("cross_broker", pd.DataFrame()),
        "fx_audit": fx.audit_frame(), "fx_sources": fx.source_frame(),
        "market_audit": md.audit_frame(),
        "dividend_basis": result.dividend_basis,
        "statement_basis": ("LIMITED_DATA - period-end valuation basis; historical "
                            "acquisition FX not established by the documents"
                            if opts.limited_statement else "Full statement set"),
        "headlines": headlines, "sources": sources, "conventions": CONVENTIONS,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Generate an RSU / Schedule FA working paper.")
    p.add_argument("client_dir")
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--to", dest="end", required=True)
    p.add_argument("--client", default="")
    p.add_argument("--fx", default=str(DEFAULT_FX))
    p.add_argument("--market", default=str(DEFAULT_MARKET))
    p.add_argument("--google-fx", default="",
                   help="consolidated Google Finance FX fallback table (CSV: "
                        "date, base_currency, quote_currency, rate). Used only "
                        "where SBI TTBR is unavailable for the date.")
    p.add_argument("--entity-wise", action="store_true")
    p.add_argument("--limited-statement", action="store_true",
                   help="statement set cannot establish historical FX; convert the "
                        "initial value at the period-end TTBR")
    p.add_argument("--docs", default="",
                   help="folder of uploaded documents to ingest instead of "
                        "canonical CSVs (statements, portfolio Excel, 1042-S, TTBR)")
    p.add_argument("--out", default="")
    args = p.parse_args()

    client_dir = Path(args.client_dir)
    period = Period(dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end))
    client = args.client or client_dir.name
    opts = ComputeOptions(a3_granularity="entity" if args.entity_wise else "lot",
                          limited_statement=args.limited_statement)

    result, fx, md, register = prepare(
        client_dir, period, Path(args.fx), Path(args.market), opts,
        doc_dir=Path(args.docs) if args.docs else None,
        google_fx_path=Path(args.google_fx) if args.google_fx else None)
    sources = result.summary.get("sources", [])
    ctx = build_context(client, period, result, fx, md, register, opts, sources)
    out = Path(args.out) if args.out else (
        ROOT / "output" / f"{client}_ScheduleFA_{period.calendar_year}.xlsx")
    write_workbook(out, ctx)

    counts = register.counts()
    print(f"Written: {out}  (engine {ENGINE_VERSION})")
    print(f"  Blockers {counts['Blocker']} | Review {counts['Review']} | Note {counts['Note']}")
    for f in register.flags:
        print(f"  [{f.severity.value:8}] {f.reason} - {f.subject}")


if __name__ == "__main__":
    main()
