"""Regression tests for the Phase 3B production-hardening changes.

Four independent, narrowly-scoped changes, each covered here:

  1. openapi_url=None (src/web.py) - /openapi.json, /docs and /redoc are all
     unreachable. This is a private tool, not a published API.

  2. The session cookie now carries Secure whenever the request reached the
     app over HTTPS directly, or via a reverse proxy forwarding
     X-Forwarded-Proto: https - and is UNCHANGED (no Secure) for a plain-HTTP
     request, so this project's own httpx-based test suite (and a Codespaces
     dev run) keeps working exactly as before.

  3. requirements.txt - exactly the six packages the application's own
     import graph requires, pinned to versions verified end-to-end in
     Phase 3, plus documentation of the one OS-level (non-pip) dependency,
     poppler-utils.

  4. main() (i.e. actually running the server) sets a restrictive process
     umask before doing anything else, so job/upload/workbook directories
     default to owner-only regardless of the host's own umask - scoped to
     main(), so importing this module for tests is unaffected.

None of this touches tax calculation logic, FX logic, broker mappings, the
acceptance baseline, build_context()'s contract, the UI, or JobStore/
single-worker architecture.

Run: python3 -m tests.production_hardening_regression
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("APP_SECRET", "test-secret")

from fastapi.testclient import TestClient                             # noqa: E402

import src.web as web                                                 # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_fails = 0


def check(ok, label, detail=""):
    global _fails
    status = PASS if ok else FAIL
    print(f"  {status:<6} {label}{('  ' + str(detail)) if detail else ''}")
    if not ok:
        _fails += 1
    return ok


def section(title):
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


# ======================================================================
# 1. OPENAPI EXPOSURE CLOSED
# ======================================================================
def test_openapi_disabled():
    section("1. /openapi.json, /docs AND /redoc ARE ALL UNREACHABLE")

    check(web.app.openapi_url is None,
          "the FastAPI app itself declares openapi_url=None")

    client = TestClient(web.app)
    for path in ("/openapi.json", "/docs", "/redoc"):
        r = client.get(path)
        check(r.status_code == 404, f"GET {path:<14} -> 404", r.status_code)

    # And this must hold with no authentication at all - the point is that
    # the schema is not reachable by anyone, signed in or not.
    r = client.get("/openapi.json")
    check("openapi" not in r.text.lower(),
          "the 404 body carries no schema fragment", r.text[:80])


# ======================================================================
# 2. PRODUCTION-SAFE SECURE SESSION COOKIE
# ======================================================================
def test_secure_cookie_behaviour():
    section("2. SECURE SESSION COOKIE - ONLY WHEN ACTUALLY SERVED OVER HTTPS")

    pw = os.environ["APP_PASSWORD"]

    # Plain HTTP (this project's own test suite, a Codespaces dev run):
    # behaviour must be BYTE-FOR-BYTE unchanged from before this phase.
    plain = TestClient(web.app)
    r = plain.post("/api/login", data={"password": pw})
    cookie = r.headers.get("set-cookie", "")
    check(r.status_code == 200, "plain-HTTP login still succeeds")
    check("HttpOnly" in cookie, "plain-HTTP: HttpOnly is present", cookie)
    check("SameSite=strict" in cookie, "plain-HTTP: SameSite=strict is present", cookie)
    check("Secure" not in cookie,
          "plain-HTTP: Secure is NOT set (would break this same test suite "
          "and any plain-HTTP dev run otherwise)", cookie)

    # A reverse proxy terminating TLS and forwarding the standard header.
    proxied = TestClient(web.app)
    r2 = proxied.post("/api/login", data={"password": pw},
                      headers={"X-Forwarded-Proto": "https"})
    cookie2 = r2.headers.get("set-cookie", "")
    check(r2.status_code == 200, "login behind a TLS-terminating proxy still succeeds")
    check("Secure" in cookie2,
          "X-Forwarded-Proto: https -> the cookie carries Secure", cookie2)
    check("HttpOnly" in cookie2 and "SameSite=strict" in cookie2,
          "the other attributes are unchanged", cookie2)

    # A direct HTTPS connection to the app (no proxy in front at all).
    direct_https = TestClient(web.app, base_url="https://testserver")
    r3 = direct_https.post("/api/login", data={"password": pw})
    cookie3 = r3.headers.get("set-cookie", "")
    check(r3.status_code == 200, "login over a direct HTTPS connection succeeds")
    check("Secure" in cookie3,
          "a direct HTTPS request -> the cookie carries Secure", cookie3)

    # An attacker-controlled header must not be trusted from an insecure
    # connection to *remove* protections - this only ever ADDS Secure, never
    # removes HttpOnly/SameSite, so there is nothing to downgrade.
    check("HttpOnly" in cookie and "SameSite=strict" in cookie
          and "HttpOnly" in cookie2 and "SameSite=strict" in cookie2
          and "HttpOnly" in cookie3 and "SameSite=strict" in cookie3,
          "HttpOnly and SameSite hold in every scenario above")


# ======================================================================
# 3. MINIMAL REQUIREMENTS.TXT
# ======================================================================
EXPECTED_PACKAGES = {
    "fastapi", "uvicorn", "python-multipart", "pandas", "openpyxl", "pyyaml",
}


def test_requirements_txt():
    section("3. requirements.txt - EXACTLY THE SIX VERIFIED DEPENDENCIES")

    path = ROOT / "requirements.txt"
    check(path.exists(), "requirements.txt exists at the repository root")
    if not path.exists():
        return
    text = path.read_text()

    pinned = re.findall(r"^([A-Za-z0-9_.\-]+)==([0-9][A-Za-z0-9_.\-]*)\s*$",
                        text, re.MULTILINE)
    names = {name.lower() for name, _ in pinned}

    check(names == EXPECTED_PACKAGES,
          "the pinned package set is exactly the six verified dependencies "
          "- no more, no fewer", sorted(names))
    check(all(version for _, version in pinned),
          "every package is pinned to a concrete version, not a range")

    for imp, pkg in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                     ("pandas", "pandas"), ("openpyxl", "openpyxl"),
                     ("yaml", "pyyaml")):
        check(any(name == pkg for name in names),
              f"{imp} (imported by src/) has a pinned entry")
    check("python-multipart" in names,
          "python-multipart has a pinned entry (required transitively by "
          "FastAPI's Form()/File() parameters, imported by name nowhere)")

    check("poppler-utils" in text,
          "the OS-level pdftotext/poppler-utils dependency is documented "
          "in the same file, clearly marked as not a pip package")
    check("pip install" not in text.lower(),
          "requirements.txt contains no pip-install instructions itself "
          "(that belongs in the README, not the manifest)")

    # Must not silently balloon into "everything installed in dev" - no
    # test-only or dev-only tooling belongs in a PRODUCTION manifest.
    dev_only = {"pytest", "black", "flake8", "mypy", "ipython", "jupyter"}
    check(not (names & dev_only),
          "no dev/test-only tooling leaked into the production manifest")


# ======================================================================
# 4. RESTRICTIVE UMASK ON ACTUAL SERVER STARTUP
# ======================================================================
def test_umask_hardening():
    section("4. main() RESTRICTS THE PROCESS UMASK BEFORE ANYTHING ELSE")

    calls = []
    original_umask = os.umask
    prev_mask = original_umask(0o022)      # os.umask() only ever reports the
    original_umask(prev_mask)              # OLD value by setting a new one - read, then restore

    def fake_umask(mask):
        calls.append(mask)
        return original_umask(mask)               # keep it a real, working umask() call

    argv = sys.argv
    try:
        sys.argv = ["src.web", "--host", "127.0.0.1", "--port", "0"]
        with mock.patch("os.umask", side_effect=fake_umask) as m_umask, \
             mock.patch("uvicorn.run") as m_run:
            web.main()
        check(m_umask.called, "main() calls os.umask()")
        check(0o077 in calls,
              "main() restricts the umask to 0o077 (owner-only default "
              "for every subsequent file/directory it creates)", calls)
        check(m_run.called, "main() still goes on to start uvicorn as before")
    finally:
        sys.argv = argv
        original_umask(prev_mask)          # leave this process's umask exactly as found


def main() -> int:
    test_openapi_disabled()
    test_secure_cookie_behaviour()
    test_requirements_txt()
    test_umask_hardening()
    print()
    print(f"RESULT: {'ALL CHECKS PASSED' if not _fails else str(_fails) + ' CHECK(S) FAILED'}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    sys.exit(main())
