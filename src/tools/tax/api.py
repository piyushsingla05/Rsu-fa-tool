"""Tax / Foreign Assets working paper - the tool's own service layer.

THIS FILE CONTAINS NO TAX LOGIC, AND MUST NOT.

Its whole job is job lifecycle and serialisation. Every figure it hands to the
UI is read out of the dict `build_context()` already returns - the same dict
`write_workbook()` renders - so a number on screen and the same number in the
downloaded workbook cannot disagree. If a screen ever needs a figure that is not
in that dict, that is a question for the engine, never a calculation here.

Five integration points, all pre-existing:

    extract_file      per-file preview on upload
    ingest_documents  folder-level ingestion (called inside prepare)
    prepare           the whole computation - pure, no disk writes
    build_context     everything the UI renders
    write_workbook    the .xlsx download

Retention: a job's uploads live in its own working directory and nowhere else.
They are purged when the job is closed or expires. Nothing is written outside
the job directory, and nothing leaves the machine.
"""
from __future__ import annotations

import datetime as dt
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ...compute import ComputeOptions
from ...excel_out import ENGINE_VERSION, write_workbook
from ...ingest.extract import extract_file
from ...ingest.profiles import load_profiles
from ...models import Period
from ...run import DEFAULT_FX, DEFAULT_GOOGLE_FX, DEFAULT_MARKET, ROOT, build_context, prepare

WORK_ROOT = ROOT / "work" / "tax"   # this tool's jobs, nobody else's

# state machine
NEW, READY, RUNNING, DONE, FAILED = "new", "ready", "running", "done", "failed"

# How long a job's working directory - including its uploaded source documents -
# may live before it is purged. The uploads exist for the working run only.
JOB_TTL = dt.timedelta(hours=8)


# ----------------------------------------------------------------------
# Serialisation: a DataFrame as the UI sees it. Values only - no formatting
# decisions, no derived columns, no arithmetic.
# ----------------------------------------------------------------------
def _cell(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (pd.Timestamp, dt.datetime)):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (int, float)):
        return v
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return str(v)


def frame_to_json(df: pd.DataFrame | None) -> dict:
    """A frame exactly as the engine produced it.

    Backend columns - the grey ones the workbook shows beside the filing
    columns - are named so the UI can style them the same way, but every column
    is sent. Hiding a column the workbook shows would let the two disagree.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        cols = list(df.columns) if isinstance(df, pd.DataFrame) else []
        return {"columns": cols, "rows": [], "backend_from": None}
    cols = [str(c) for c in df.columns]
    backend = None
    for marker in ("Currency", "Ref: Symbol", "FX Source"):
        if marker in cols:
            backend = cols.index(marker)
            break
    rows = [[_cell(r[c]) for c in df.columns] for _, r in df.iterrows()]
    return {"columns": cols, "rows": rows, "backend_from": backend}


# ----------------------------------------------------------------------
@dataclass
class Job:
    id: str
    client: str
    period: Period
    limited_statement: bool = False
    entity_wise: bool = False
    state: str = NEW
    step: str = ""
    message: str = ""
    error: str = ""
    created: dt.datetime = field(default_factory=dt.datetime.utcnow)
    finished: dt.datetime | None = None
    ctx: dict | None = field(default=None, repr=False)
    workbook: Path | None = None
    run_count: int = 0

    @property
    def workdir(self) -> Path:
        return WORK_ROOT / self.id

    @property
    def uploads(self) -> Path:
        return self.workdir / "uploads"

    @property
    def client_dir(self) -> Path:
        return self.workdir / "client"

    @property
    def google_fx(self) -> Path:
        return self.workdir / "google_fx.csv"

    @property
    def expired(self) -> bool:
        return dt.datetime.utcnow() - self.created > JOB_TTL

    def public(self) -> dict:
        return {
            "id": self.id, "client": self.client,
            "period_start": self.period.start.isoformat(),
            "period_end": self.period.end.isoformat(),
            "limited_statement": self.limited_statement,
            "entity_wise": self.entity_wise,
            "state": self.state, "step": self.step, "message": self.message,
            "error": self.error, "run_count": self.run_count,
            "engine_version": ENGINE_VERSION,
            "created": self.created.isoformat() + "Z",
            "finished": (self.finished.isoformat() + "Z") if self.finished else None,
            "documents": sorted(p.name for p in self.uploads.glob("*")
                                if p.is_file()) if self.uploads.exists() else [],
            "has_google_fx": self.google_fx.exists(),
            "workbook_ready": bool(self.workbook and self.workbook.exists()),
        }


class JobStore:
    """In-memory jobs plus their working directories.

    Deliberately simple for a single-user self-hosted v1. Everything a
    multi-user version would need to change lives behind this class - swap it
    for a database-backed store and nothing else moves.
    """

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, client, period, limited_statement=False, entity_wise=False) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], client=client, period=period,
                  limited_statement=limited_statement, entity_wise=entity_wise)
        job.uploads.mkdir(parents=True, exist_ok=True)
        job.client_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._jobs[job.id] = job
        self.purge_expired()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def close(self, job_id: str) -> bool:
        """Purge a job and every source document it holds."""
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        shutil.rmtree(job.workdir, ignore_errors=True)
        return True

    def purge_expired(self) -> int:
        gone = [j.id for j in list(self._jobs.values()) if j.expired]
        for jid in gone:
            self.close(jid)
        return len(gone)


STORE = JobStore()


# ----------------------------------------------------------------------
def analyse_upload(path: Path) -> dict:
    """What the identifier makes of one file, before any run.

    Straight from extract_file - the same identification the run itself uses.
    """
    try:
        events, rec, unmapped, ttbr = extract_file(path, load_profiles())
    except Exception as exc:                       # never fail an upload
        return {"file": path.name, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"}
    return {
        "file": rec.file, "ok": True, "kind": rec.kind,
        "document_type": rec.document_type, "profile": rec.profile_id,
        "broker": rec.broker, "confidence": round(float(rec.confidence), 2),
        "coverage": rec.coverage, "rows": int(rec.rows),
        "securities": list(rec.securities),
        "holdings": len(rec.holdings or []), "cash_rows": len(rec.cash or []),
        "closed_lots": len(rec.closed_lots or []),
        "exercises": len(getattr(rec, "exercises", []) or []),
        "ttbr_rows": int(len(ttbr)) if ttbr is not None else 0,
        "is_google_fx": getattr(rec, "google_fx", None) is not None,
        "notes": rec.notes,
    }


def run_job(job: Job) -> Job:
    """Compute, then build the context and the workbook. No logic of its own."""
    job.state, job.error = RUNNING, ""
    job.step, job.message = "prepare", "Reading documents and computing"
    try:
        opts = ComputeOptions(
            a3_granularity="entity" if job.entity_wise else "lot",
            limited_statement=job.limited_statement)
        docs = job.uploads if any(job.uploads.glob("*")) else None
        result, fx, md, register = prepare(
            job.client_dir, job.period, DEFAULT_FX, DEFAULT_MARKET, opts,
            doc_dir=docs,
            google_fx_path=job.google_fx if job.google_fx.exists() else DEFAULT_GOOGLE_FX)

        job.step, job.message = "context", "Assembling the working paper"
        ctx = build_context(job.client, job.period, result, fx, md, register,
                            opts, result.summary.get("sources", []))

        job.step, job.message = "workbook", "Writing the workbook"
        out = job.workdir / f"{_safe(job.client)}_ScheduleFA.xlsx"
        write_workbook(out, ctx)

        job.ctx, job.workbook = ctx, out
        job.state, job.step = DONE, "done"
        job.run_count += 1
        job.message = f"Run {job.run_count} complete"
    except Exception as exc:
        job.state, job.step = FAILED, "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        job.message = "The run did not complete"
    job.finished = dt.datetime.utcnow()
    return job


def _safe(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in " -_") else "_" for c in name).strip()
    return (keep or "client").replace(" ", "_")[:60]


# ----------------------------------------------------------------------
# Sections. Each one names the ctx keys it renders - nothing else exists.
# ----------------------------------------------------------------------
SECTIONS = {
    "summary": ["headlines", "brokers", "securities", "dividend_basis",
                "statement_basis", "fx_status", "market_status",
                "coverage_status", "conventions"],
    "fa_a2": ["a2"],
    "fa_a3": ["a3"],
    "capital_gains": ["sales"],
    "dividends": ["dividends", "form1042s", "fsi", "dividend_basis"],
    "fx_working": ["fx_audit", "fx_sources", "fx_google_requests",
                   "market_audit", "fx_status"],
    "reconciliation": ["reconciliation", "cross_broker"],
    "review": ["register"],
    "sources": ["source_table", "master"],
    "vesting": ["vesting"],
}


def section(job: Job, name: str) -> dict:
    """One screen's data, read straight out of the engine's own context."""
    if job.ctx is None:
        raise KeyError("this job has not been run yet")
    if name not in SECTIONS:
        raise KeyError(f"unknown section {name!r}")
    ctx = job.ctx
    out: dict = {"section": name}

    if name == "summary":
        counts = ctx["register"].counts()
        out.update({
            "client": ctx["client"],
            "period_start": ctx["period"].start.isoformat(),
            "period_end": ctx["period"].end.isoformat(),
            "engine_version": ENGINE_VERSION,
            "headlines": [{"label": k, "value": v} for k, v in ctx["headlines"]],
            "brokers": ctx["brokers"], "securities": ctx["securities"],
            "dividend_basis": ctx["dividend_basis"],
            "statement_basis": ctx["statement_basis"],
            "fx_status": ctx["fx_status"], "market_status": ctx["market_status"],
            "coverage_status": ctx["coverage_status"],
            "counts": {k: int(counts.get(k, 0))
                       for k in ("Blocker", "Review", "Note")},
            "verdict": ("NOT READY TO FILE - blockers outstanding"
                        if counts.get("Blocker")
                        else "No blockers. Review items still require sign-off."),
            "conventions": [{"item": a, "basis": b} for a, b in ctx["conventions"]],
        })
        return out

    if name == "review":
        out["register"] = frame_to_json(ctx["register"].frame())
        out["counts"] = {k: int(v) for k, v in ctx["register"].counts().items()}
        return out

    for key in SECTIONS[name]:
        v = ctx.get(key)
        out[key] = frame_to_json(v) if isinstance(v, pd.DataFrame) else v
    return out


def all_sections(job: Job) -> dict:
    return {n: section(job, n) for n in SECTIONS}


# ----------------------------------------------------------------------
# Google Finance round-trip
# ----------------------------------------------------------------------
GOOGLE_IMPORT_COLUMNS = ["date", "base_currency", "quote_currency", "rate"]


def google_requests(job: Job) -> dict:
    """The consolidated FX request table, plus what is still unresolved.

    One row per currency/date pair the primary table could not supply -
    deduplicated by the engine, not here.
    """
    if job.ctx is None:
        raise KeyError("this job has not been run yet")
    req = job.ctx.get("fx_google_requests")
    return {
        "requests": frame_to_json(req),
        "count": int(len(req)) if isinstance(req, pd.DataFrame) else 0,
        "fx_status": job.ctx.get("fx_status", ""),
        "have_rates": job.google_fx.exists(),
        "tsv": _requests_tsv(req),
    }


def _requests_tsv(req) -> str:
    """The request table as tab-separated text, ready to paste into Sheets.

    Column A is the date, so the GOOGLEFINANCE formula's cell reference lines up
    when it lands in row 2 onwards.
    """
    if not isinstance(req, pd.DataFrame) or req.empty:
        return ""
    cols = ["date", "base_currency", "quote_currency", "rate",
            "google_finance_formula"]
    cols = [c for c in cols if c in req.columns]
    lines = ["\t".join(cols)]
    for _, r in req.iterrows():
        lines.append("\t".join("" if pd.isna(r[c]) else str(_cell(r[c]) or "")
                               for c in cols))
    return "\n".join(lines)


def import_google_rates(job: Job, text: str) -> dict:
    """Accept completed rates pasted back from Sheets, or uploaded as CSV/TSV.

    Parsed permissively - a paste may or may not carry a header row, and may
    carry the formula column back with it. A row without a usable positive rate
    is SKIPPED, never read as zero: Google returning nothing for a date must
    leave that date unresolved.
    """
    rows, skipped = [], 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in (line.split("\t") if "\t" in line
                                     else line.split(","))]
        if len(parts) < 2:
            continue
        if parts[0].lower().startswith("date"):
            continue                                    # header
        date = pd.to_datetime(parts[0], errors="coerce", dayfirst=False)
        if pd.isna(date):
            continue
        base = (parts[1] or "USD").upper()[:3] if len(parts) > 1 else "USD"
        quote = (parts[2] or "INR").upper()[:3] if len(parts) > 2 else "INR"
        rate = pd.to_numeric(parts[3].replace(",", ""), errors="coerce") \
            if len(parts) > 3 else None
        if rate is None or pd.isna(rate) or float(rate) <= 0:
            skipped += 1
            continue
        rows.append({"date": date.date().isoformat(), "base_currency": base,
                     "quote_currency": quote, "rate": float(rate),
                     "source": "Google Finance historical FX (supplied via UI)",
                     "retrieved_on": dt.date.today().isoformat(),
                     "status": "SUPPLIED", "notes": ""})
    if not rows:
        return {"imported": 0, "skipped": skipped,
                "message": "No usable rate rows were found."}

    existing = (pd.read_csv(job.google_fx) if job.google_fx.exists()
                else pd.DataFrame())
    df = pd.DataFrame(rows)
    if len(existing):
        df = (pd.concat([df, existing], ignore_index=True)
              .drop_duplicates(subset=["date", "base_currency", "quote_currency"],
                               keep="first"))
    df.to_csv(job.google_fx, index=False)
    return {"imported": len(rows), "skipped": skipped, "total_on_file": len(df),
            "message": (f"{len(rows)} rate(s) accepted"
                        + (f", {skipped} row(s) skipped with no usable rate"
                           if skipped else "")
                        + ". Re-run to apply them.")}
