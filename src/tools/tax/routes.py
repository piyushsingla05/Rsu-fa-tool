"""HTTP routes for the Tax / Foreign Assets working paper tool.

Everything here is this tool's own. It is mounted by the application shell
under the prefix the registry declares, and it knows nothing about the shell
beyond the session dependency it is handed.

STILL NO TAX LOGIC. Every route moves a file, starts a run, or hands back part
of the dict `build_context()` already produced.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ...excel_out import ENGINE_VERSION
from ...models import Period
from ...session import require_session
from . import api

router = APIRouter(tags=["tax"])

MAX_UPLOAD_MB = 60


def _job(job_id: str) -> api.Job:
    job = api.STORE.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    return job


@router.get("/info")
def info(_: str = Depends(require_session)):
    return {"tool": "tax", "engine_version": ENGINE_VERSION,
            "sections": list(api.SECTIONS)}


# ---------------------------------------------------------------- jobs
@router.get("/jobs")
def list_jobs(_: str = Depends(require_session)):
    api.STORE.purge_expired()
    return {"jobs": [j.public() for j in api.STORE.list()]}


@router.post("/jobs")
def create_job(client: str = Form(...), period_start: str = Form(...),
               period_end: str = Form(...), limited_statement: bool = Form(False),
               entity_wise: bool = Form(False), _: str = Depends(require_session)):
    try:
        period = Period(dt.date.fromisoformat(period_start),
                        dt.date.fromisoformat(period_end))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Bad period: {exc}")
    if period.end < period.start:
        raise HTTPException(status_code=400, detail="Period ends before it starts")
    return api.STORE.create(client.strip() or "Client", period,
                            limited_statement, entity_wise).public()


@router.get("/jobs/{job_id}")
def get_job(job_id: str, _: str = Depends(require_session)):
    return _job(job_id).public()


@router.delete("/jobs/{job_id}")
def close_job(job_id: str, _: str = Depends(require_session)):
    """Purge the job and every source document it holds."""
    return {"purged": api.STORE.close(job_id)}


@router.post("/jobs/{job_id}/documents")
async def upload(job_id: str, files: list[UploadFile] = File(...),
                 _: str = Depends(require_session)):
    """Broker statements, portfolio Excel, 1042-S, SBI TTBR, Google FX - any mix.

    Each file is identified as it arrives by the same code the run uses, so the
    preparer sees what the engine made of it before committing to a run.
    """
    job = _job(job_id)
    out = []
    for f in files:
        name = Path(f.filename or "upload").name
        dest = job.uploads / name
        size = 0
        with dest.open("wb") as fh:
            while chunk := await f.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"{name} is larger than {MAX_UPLOAD_MB} MB")
                fh.write(chunk)
        out.append(api.analyse_upload(dest))
    if job.state == api.NEW:
        job.state = api.READY
    return {"files": out, "job": job.public()}


@router.delete("/jobs/{job_id}/documents/{name}")
def remove_document(job_id: str, name: str, _: str = Depends(require_session)):
    job = _job(job_id)
    target = job.uploads / Path(name).name
    if not target.exists():
        raise HTTPException(status_code=404, detail="No such document")
    target.unlink()
    return {"removed": name, "job": job.public()}


@router.post("/jobs/{job_id}/run")
def run(job_id: str, _: str = Depends(require_session)):
    """Run synchronously.

    v1 is single-user and a run takes seconds to a minute. The state machine
    already reports RUNNING/DONE/FAILED, so putting a queue behind this later
    changes no client code.
    """
    job = _job(job_id)
    api.run_job(job)
    if job.state == api.FAILED:
        return JSONResponse(status_code=500,
                            content={"job": job.public(), "error": job.error})
    return {"job": job.public()}


@router.get("/jobs/{job_id}/status")
def status(job_id: str, _: str = Depends(require_session)):
    job = _job(job_id)
    return {"state": job.state, "step": job.step, "message": job.message,
            "error": job.error, "run_count": job.run_count,
            "workbook_ready": bool(job.workbook and job.workbook.exists())}


# ---------------------------------------------------------------- reading
@router.get("/jobs/{job_id}/section/{name}")
def read_section(job_id: str, name: str, _: str = Depends(require_session)):
    job = _job(job_id)
    try:
        return api.section(job, name)
    except KeyError as exc:
        raise HTTPException(status_code=409 if job.ctx is None else 404,
                            detail=str(exc))


@router.get("/jobs/{job_id}/workbook")
def workbook(job_id: str, _: str = Depends(require_session)):
    job = _job(job_id)
    if not (job.workbook and job.workbook.exists()):
        raise HTTPException(status_code=409, detail="Run the job first")
    return FileResponse(
        job.workbook, filename=job.workbook.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------- FX round-trip
@router.get("/jobs/{job_id}/fx/requests")
def fx_requests(job_id: str, _: str = Depends(require_session)):
    job = _job(job_id)
    try:
        return api.google_requests(job)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/jobs/{job_id}/fx/rates")
async def fx_rates(job_id: str, text: str = Form(""),
                   file: UploadFile | None = File(None),
                   _: str = Depends(require_session)):
    """Completed rates back, by paste or CSV.

    Merged into the GOOGLE provider only - never into the SBI table - and
    applied on the next run.
    """
    job = _job(job_id)
    body = text or ""
    if file is not None:
        body = (body + "\n" + (await file.read()).decode("utf-8", "ignore")).strip()
    if not body.strip():
        raise HTTPException(status_code=400, detail="Nothing to import")
    return api.import_google_rates(job, body)


@router.delete("/jobs/{job_id}/fx/rates")
def clear_fx_rates(job_id: str, _: str = Depends(require_session)):
    job = _job(job_id)
    job.google_fx.unlink(missing_ok=True)
    return {"cleared": True}
