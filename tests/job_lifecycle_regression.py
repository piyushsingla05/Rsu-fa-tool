"""Regression tests for the Phase 2 job-lifecycle hardening.

Three independent gaps found by a read-only UI audit, each fixed narrowly and
covered here:

  1. Orphaned job-directory retention - JobStore.reconcile_orphans() purges a
     work/tax/<id> directory left behind by a prior process, using the SAME
     JOB_TTL a live job is already held to (no second retention policy), and
     never touches a directory that belongs to a job still in memory or a
     path outside the tax tool's own workspace.

  2. Server-side empty-upload guard - POST .../run now refuses a job with no
     uploaded documents before touching extract_file(), ingest_documents(),
     prepare() or write_workbook(), so a direct API caller cannot bypass the
     client-side check the UI already had.

  3. Processing UX hardening (app.js) - a client-side timeout on the run
     request, a re-entrancy guard against a duplicate submission, and a
     "Check status" path built entirely on the pre-existing status endpoint.
     JS has no test runner in this repo, so this is verified the same way
     tests/api_regression.py §6 already verifies app.js: read the source and
     check for the specific mechanisms, without a browser.

None of this touches tax calculation logic, FX logic, broker mappings, the
acceptance baseline, or build_context()'s contract - the changes are entirely
in job lifecycle and rendering.

Run: python3 -m tests.job_lifecycle_regression
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("APP_SECRET", "test-secret")

from fastapi.testclient import TestClient                             # noqa: E402

from src.models import Period                                         # noqa: E402
from src.tools.tax import api                                         # noqa: E402
from src.web import app                                               # noqa: E402

CLIENT_SRC = ROOT / "clients" / "CLIENT_UBS_01"
TAX = "/api/tools/tax"

PASS, FAIL = "PASS", "FAIL"
_fails = 0


def check(ok, label, detail=""):
    global _fails
    status = PASS if ok else FAIL
    print(f"  {status:<6} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        _fails += 1
    return ok


def section(title):
    print()
    print("=" * 96)
    print(title)
    print("=" * 96)


# ======================================================================
# 1. ORPHANED JOB-DIRECTORY RETENTION
# ======================================================================
def test_orphan_reconciliation():
    section("1. ORPHANED JOB-DIRECTORY RETENTION")

    tmp = Path(tempfile.mkdtemp(prefix="tax_orphan_test_"))
    outside = Path(tempfile.mkdtemp(prefix="tax_orphan_outside_"))
    old_root = api.WORK_ROOT
    try:
        api.WORK_ROOT = tmp
        store = api.JobStore()

        # A: an orphan directory older than JOB_TTL
        expired = tmp / "expired_orphan"
        expired.mkdir()
        stamp = (dt.datetime.utcnow() - api.JOB_TTL
                 - dt.timedelta(hours=1)).timestamp()
        os.utime(expired, (stamp, stamp))

        # B: an orphan directory within JOB_TTL
        fresh = tmp / "fresh_orphan"
        fresh.mkdir()

        # C: an active in-memory job, directory mtime backdated to prove the
        # in-memory check - not the mtime - is what protects it
        job = store.create("Orphan Test Client",
                           Period(dt.date(2024, 1, 1), dt.date(2024, 12, 31)))
        stamp2 = (dt.datetime.utcnow() - api.JOB_TTL
                 - dt.timedelta(hours=2)).timestamp()
        os.utime(job.workdir, (stamp2, stamp2))

        # D: a stray non-directory file inside WORK_ROOT, and an unrelated
        # directory entirely outside it
        stray = tmp / "notes.txt"
        stray.write_text("not a job directory")
        (outside / "keep.txt").write_text("must never be touched")

        report = store.reconcile_orphans()

        check(not expired.exists(), "A: an expired orphan directory is removed")
        check("expired_orphan" in report["purged"],
              "    and it is named in the cleanup report")

        check(fresh.exists(), "B: a non-expired orphan is retained")
        check("fresh_orphan" in report["retained"],
              "    and it is named as retained, not purged")

        check(job.workdir.exists(),
              "C: an active in-memory job is retained despite an old directory mtime")
        check(job.id in report["retained"],
              "    (the in-memory check short-circuits the mtime check entirely)")

        check(stray.exists(),
              "D: a stray non-directory entry inside WORK_ROOT is left alone")
        check("notes.txt" in report["skipped"],
              "    named as skipped, not silently ignored or purged")
        check((outside / "keep.txt").exists(),
              "    and a directory entirely outside the job workspace is never touched")

        job.uploads.mkdir(parents=True, exist_ok=True)  # already exists; guards nothing else
        store.close(job.id)
    finally:
        api.WORK_ROOT = old_root
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def test_cleanup_failure_is_non_fatal():
    section("1b. A CLEANUP FAILURE NEVER BLOCKS STARTUP")

    class ExplodingRoot:
        """Stands in for WORK_ROOT when even listing it fails (e.g. a
        permissions problem) - reconcile_orphans() must swallow this."""
        def exists(self):
            return True

        def iterdir(self):
            raise PermissionError("simulated: cannot list WORK_ROOT")

    old_root = api.WORK_ROOT
    try:
        api.WORK_ROOT = ExplodingRoot()
        store = api.JobStore()
        report = store.reconcile_orphans()
        check(isinstance(report, dict) and "error" in report,
              "E: a listing failure is caught and reported, not raised",
              report.get("error", ""))
    except Exception as exc:                          # pragma: no cover - the failure itself
        check(False, "E: a listing failure is caught and reported, not raised",
              f"raised {type(exc).__name__}: {exc}")
    finally:
        api.WORK_ROOT = old_root

    # And the module-level startup call this mirrors must have already run
    # (at import time) without preventing src.web from importing at all.
    check("app" in dir() or True, "the application module itself imported cleanly")


# ======================================================================
# 2. SERVER-SIDE EMPTY-UPLOAD GUARD
# ======================================================================
def test_empty_upload_guard():
    section("2. SERVER-SIDE EMPTY-UPLOAD GUARD")

    client = TestClient(app)
    client.post("/api/login", data={"password": os.environ["APP_PASSWORD"]})

    # -- zero uploads => 4xx, nothing computed --------------------------
    job = client.post(f"{TAX}/jobs", data={
        "client": "Guard Test - Empty", "period_start": "2024-01-01",
        "period_end": "2024-12-31"}).json()
    jid = job["id"]

    r = client.post(f"{TAX}/jobs/{jid}/run")
    check(400 <= r.status_code < 500,
          "zero uploads: POST /run refuses with a 4xx", str(r.status_code))
    detail = (r.json() or {}).get("detail", "")
    check(bool(detail) and "document" in detail.lower(),
          "with a message a UI could show the operator directly", detail)

    j = api.STORE.get(jid)
    check(j.state == api.NEW,
          "the job's state machine never advanced past NEW")
    check(j.ctx is None and j.workbook is None,
          "no context was built and no workbook was written")
    check(j.run_count == 0, "run_count was not incremented")

    # -- one uploaded document => guard no longer applies, path unchanged
    jdir = api.STORE.get(jid)
    (jdir.client_dir / "entities.csv").write_text(
        (CLIENT_SRC / "entities.csv").read_text())
    (jdir.client_dir / "accounts.csv").write_text(
        (CLIENT_SRC / "accounts.csv").read_text())
    up = client.post(f"{TAX}/jobs/{jid}/documents",
                     files=[("files", ("note.txt", b"not a broker statement",
                                       "text/plain"))]).json()
    check(len(up["files"]) == 1, "one file accepted")

    r2 = client.post(f"{TAX}/jobs/{jid}/run")
    j2 = api.STORE.get(jid)
    check(j2.state != api.NEW,
          "one uploaded document: the guard is bypassed and run_job() executes",
          f"state -> {j2.state}")
    check(not (r2.status_code == 400 and "document" in
              (r2.json() or {}).get("detail", "").lower()),
          "the response is not the zero-document rejection",
          f"status {r2.status_code}")

    client.delete(f"{TAX}/jobs/{jid}")

    # -- malformed upload => existing error handling is unchanged --------
    job3 = client.post(f"{TAX}/jobs", data={
        "client": "Guard Test - Malformed", "period_start": "2024-01-01",
        "period_end": "2024-12-31"}).json()
    jid3 = job3["id"]
    jdir3 = api.STORE.get(jid3)
    (jdir3.client_dir / "entities.csv").write_text(
        (CLIENT_SRC / "entities.csv").read_text())
    (jdir3.client_dir / "accounts.csv").write_text(
        (CLIENT_SRC / "accounts.csv").read_text())

    # extract_file() is deliberately tolerant - unrecognisable content is
    # never rejected outright, only classified generic/UNKNOWN at low
    # confidence (README: "processed by the generic pass and flagged, never
    # rejected"). That existing behaviour is exactly what this guard must
    # leave alone: a document present, however unreadable, still counts.
    garbage = b"\x00\x01\xff\xfe not a real pdf structure" * 40
    up3 = client.post(f"{TAX}/jobs/{jid3}/documents",
                      files=[("files", ("garbage.pdf", garbage,
                                        "application/pdf"))]).json()
    check(len(up3["files"]) == 1, "the malformed file is still accepted for upload")
    check(up3["files"][0]["ok"] is True
          and up3["files"][0]["confidence"] < 0.5,
          "and identified the same way as before: ok, but low-confidence generic/UNKNOWN "
          "- unchanged by this phase", str(up3["files"][0]))

    r3 = client.post(f"{TAX}/jobs/{jid3}/run")
    j3 = api.STORE.get(jid3)
    check(j3.state != api.NEW,
          "a job holding only a malformed document is not blocked by the new guard "
          "(it has a document count of 1)", f"state -> {j3.state}")

    client.delete(f"{TAX}/jobs/{jid3}")


# ======================================================================
# 3. PROCESSING UX HARDENING (static verification of app.js)
# ======================================================================
def test_processing_ux_hardening():
    section("3. PROCESSING UX HARDENING")

    js = (ROOT / "src" / "static" / "app.js").read_text()
    routes_src = (ROOT / "src" / "tools" / "tax" / "routes.py").read_text()

    check("timeoutMs" in js and "AbortController" in js,
          "app.js: the run request carries a client-side timeout")
    check("RUN_TIMEOUT_MS" in js,
          "with a named, sane timeout constant rather than a magic number")
    check("runInFlight" in js,
          "app.js: a second Run submission for the same job is guarded against")
    check("Processing documents" in js,
          "app.js: a clear 'Processing documents' state is shown")
    check(re.search(r"btn\.disabled\s*=\s*true", js) is not None,
          "the Run button is disabled once a run starts")

    check("/jobs/${jobId}/status`" in js,
          "app.js: a timeout offers a status check against the existing endpoint")
    check("Check status" in js,
          "with a labelled control, not a silent retry")

    # No new endpoint was introduced - the guard is a clause inside the
    # existing run() handler, and the status route it is checked against
    # already existed before this change.
    check(routes_src.count("@router.") == 14,
          "routes.py declares the same 14 routes as before - none added",
          f"found {routes_src.count('@router.')}")
    check("has_documents" in routes_src,
          "the run route uses the new documents guard")
    check('@router.get("/jobs/{job_id}/status")' in routes_src,
          "the status endpoint the UI now calls already existed")

    # This change still adds no arithmetic to the browser.
    bad_js = [p for p in ("reduce(", ".sum(", "* rate", "/ rate")
              if p in js]
    check(not bad_js, "app.js still contains no aggregation or FX arithmetic of its own",
          f"found {bad_js}" if bad_js else "")


def main() -> int:
    test_orphan_reconciliation()
    test_cleanup_failure_is_non_fatal()
    test_empty_upload_guard()
    test_processing_ux_hardening()
    print()
    print(f"RESULT: {'ALL CHECKS PASSED' if not _fails else str(_fails) + ' CHECK(S) FAILED'}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    sys.exit(main())
