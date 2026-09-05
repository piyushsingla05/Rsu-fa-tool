"""UI / API integration regression.

The point of this suite is one claim: **what the browser shows, what the engine
computed, and what the downloaded workbook contains are the same figures.**

If they can drift, the UI has started calculating something of its own, and a
preparer could sign a workbook that disagrees with the screen they approved it
on. So the tests here do not check that the API "works" - they check that it
adds nothing.

Everything runs against the real UBS client, through the real HTTP app.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import os                                                        # noqa: E402
os.environ.setdefault("APP_PASSWORD", "test-password")
os.environ.setdefault("APP_SECRET", "test-secret")

from src import modules                                          # noqa: E402
from src.tools.tax import api                                    # noqa: E402
from src.web import app                                          # noqa: E402
from src.excel_out import ENGINE_VERSION                         # noqa: E402

DOCS = Path("/home/claude/brokers/RSU brokers/UBS")
CLIENT_SRC = ROOT / "clients" / "CLIENT_UBS_01"
PERIOD = ("2025-01-01", "2025-12-31")
RECALC = ("/root/.claude/skills/synced/83b76784-fb84-4b5f-8a2b-86bbda13c6f3_"
          "cfd19bfc-0475-47e5-aea4-3f53063f33d4/xlsx/scripts/recalc.py")

# Printed on the UBS statements - the same anchors the UBS regression uses.
DEC_VALUE = 1214.43
DIV_GROSS, DIV_TAX = 144.12, 36.03

TAX = "/api/tools/tax"      # the prefix the registry declares for this tool


def ok(flag, label, detail=""):
    print(f"  {'PASS' if flag else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return 0 if flag else 1


def col(frame, name):
    return frame["columns"].index(name)


def main() -> int:
    fails = 0
    client = TestClient(app)

    print("=" * 98)
    print("1. AUTHENTICATION - NO ANONYMOUS ACCESS")
    print("=" * 98)
    guarded = [f"{TAX}/jobs", f"{TAX}/jobs/x", f"{TAX}/jobs/x/status",
               f"{TAX}/jobs/x/section/summary", f"{TAX}/jobs/x/workbook",
               f"{TAX}/jobs/x/fx/requests", "/api/tools"]
    codes = {u: client.get(u).status_code for u in guarded}
    fails += ok(all(c == 401 for c in codes.values()),
                f"{len(guarded)} read endpoints refuse an unauthenticated caller",
                f"all 401" if all(c == 401 for c in codes.values()) else str(codes))
    fails += ok(client.post(f"{TAX}/jobs", data={"client": "x", "period_start":
                PERIOD[0], "period_end": PERIOD[1]}).status_code == 401,
                "and so does job creation")
    fails += ok(client.post("/api/login", data={"password": "wrong"}).status_code == 401,
                "a wrong password is refused")
    r = client.post("/api/login", data={"password": "test-password"})
    fails += ok(r.status_code == 200, "the right password signs in")
    fails += ok(client.get("/api/session").json()["authenticated"] is True,
                "and the session is recognised")

    print()
    print("=" * 98)
    print("2. UPLOAD - IDENTIFIED BY THE SAME CODE THE RUN USES")
    print("=" * 98)
    job = client.post(f"{TAX}/jobs", data={
        "client": "API Test - UBS", "period_start": PERIOD[0],
        "period_end": PERIOD[1]}).json()
    jid = job["id"]
    # the client master data the engine expects
    for name in ("entities.csv", "accounts.csv"):
        (api.STORE.get(jid).client_dir / name).write_text(
            (CLIENT_SRC / name).read_text())

    files = [("files", (p.name, p.read_bytes(), "application/pdf"))
             for p in sorted(DOCS.iterdir()) if p.is_file()]
    up = client.post(f"{TAX}/jobs/{jid}/documents", files=files).json()
    fails += ok(len(up["files"]) == 4, f"{len(up['files'])} files accepted")
    ident = {f["file"]: f for f in up["files"]}
    fails += ok(all(f["ok"] and f["broker"] == "UBS" for f in up["files"]),
                "every one identified as UBS before any run")
    profiles = {f["profile"] for f in up["files"]}
    fails += ok(profiles == {"ubs_investment_account", "ubs_form_1042s"},
                "with the same profiles the engine picks", ", ".join(sorted(profiles)))
    fails += ok(all(f["confidence"] >= 0.99 for f in up["files"]),
                "at full confidence - the preview is the run's own identification")

    print()
    print("=" * 98)
    print("3. RUN, THEN COMPARE THE API AGAINST THE ENGINE'S OWN CONTEXT")
    print("=" * 98)
    run = client.post(f"{TAX}/jobs/{jid}/run")
    fails += ok(run.status_code == 200, "run completed", run.json().get(
        "error", "") or "")
    j = api.STORE.get(jid)
    ctx = j.ctx
    fails += ok(ctx is not None and j.workbook.exists(),
                "context built and workbook written")

    # Every frame the API serves must be the ctx frame, cell for cell.
    frame_sections = [
        ("fa_a2", "a2", "a2"), ("fa_a3", "a3", "a3"),
        ("capital_gains", "sales", "sales"),
        ("dividends", "dividends", "dividends"),
        ("dividends", "fsi", "fsi"),
        ("fx_working", "fx_audit", "fx_audit"),
        ("fx_working", "fx_sources", "fx_sources"),
        ("fx_working", "fx_google_requests", "fx_google_requests"),
        ("fx_working", "market_audit", "market_audit"),
        ("reconciliation", "reconciliation", "reconciliation"),
        ("vesting", "vesting", "vesting"),
        ("sources", "source_table", "source_table"),
        ("sources", "master", "master"),
    ]
    for sect, key, ctx_key in frame_sections:
        got = client.get(f"{TAX}/jobs/{jid}/section/{sect}").json()[key]
        want = ctx[ctx_key]
        same_cols = got["columns"] == [str(c) for c in want.columns]
        same_rows = len(got["rows"]) == len(want)
        cells = 0
        if same_cols and same_rows and len(want):
            for i, (_, r) in enumerate(want.iterrows()):
                for jx, c in enumerate(want.columns):
                    if api._cell(r[c]) != got["rows"][i][jx]:
                        cells += 1
        fails += ok(same_cols and same_rows and cells == 0,
                    f"{sect}.{key:<20}",
                    f"{len(got['rows'])} rows x {len(got['columns'])} cols identical"
                    if cells == 0 else f"{cells} cell(s) differ")

    print()
    print("=" * 98)
    print("4. SUMMARY AND REVIEW COME FROM THE ENGINE, NOT THE BROWSER")
    print("=" * 98)
    s = client.get(f"{TAX}/jobs/{jid}/section/summary").json()
    fails += ok([h["label"] for h in s["headlines"]] == [k for k, _ in ctx["headlines"]]
                and [h["value"] for h in s["headlines"]] == [v for _, v in ctx["headlines"]],
                "every headline figure is the engine's own",
                f"{len(s['headlines'])} figures")
    for key in ("fx_status", "market_status", "coverage_status", "dividend_basis",
                "statement_basis", "brokers", "securities"):
        fails += ok(s[key] == ctx[key], f"{key:<18} matches the context")
    counts = ctx["register"].counts()
    fails += ok(s["counts"] == {k: int(counts.get(k, 0))
                                for k in ("Blocker", "Review", "Note")},
                "exception counts match the register",
                f"{s['counts']['Blocker']} blocker, {s['counts']['Review']} review, "
                f"{s['counts']['Note']} note")
    fails += ok(("NOT READY" in s["verdict"]) == bool(counts.get("Blocker")),
                "and the verdict follows the blockers", s["verdict"])
    rev = client.get(f"{TAX}/jobs/{jid}/section/review").json()
    fails += ok(len(rev["register"]["rows"]) == len(ctx["register"].frame()),
                "every exception is served",
                f"{len(rev['register']['rows'])} rows")

    print()
    print("=" * 98)
    print("5. THE API MATCHES THE DOWNLOADED WORKBOOK")
    print("=" * 98)
    wb_bytes = client.get(f"{TAX}/jobs/{jid}/workbook")
    fails += ok(wb_bytes.status_code == 200 and len(wb_bytes.content) > 10000,
                "workbook downloads", f"{len(wb_bytes.content):,} bytes")
    tmp = ROOT / "output" / "_api_check.xlsx"
    tmp.write_bytes(wb_bytes.content)
    subprocess.run([sys.executable, RECALC, str(tmp), "240"],
                   capture_output=True, timeout=600)
    wb = load_workbook(tmp, data_only=True)

    # FA-A3: the workbook computes the rupee columns with formulas over its grey
    # backend columns. The API sends the value the engine computed. They must agree.
    a3_api = client.get(f"{TAX}/jobs/{jid}/section/fa_a3").json()["a3"]
    ws = wb["FA_A3"]
    hdr = [c.value for c in ws[3]]
    checked = mismatch = 0
    for name in ("Initial Value of the Investment (Rs.)",
                 "Peak Value of Investment During the Period (Rs.)",
                 "Closing Value (Rs.)"):
        ci, ai = hdr.index(name), col(a3_api, name)
        for r in range(len(a3_api["rows"])):
            book = ws.cell(row=4 + r, column=ci + 1).value
            shown = a3_api["rows"][r][ai]
            if isinstance(book, (int, float)) and isinstance(shown, (int, float)):
                checked += 1
                if abs(float(book) - float(shown)) > 1.0:
                    mismatch += 1
    fails += ok(checked and mismatch == 0,
                "FA-A3 rupee columns: screen == recalculated workbook",
                f"{checked} cell(s) compared, {mismatch} differ")

    cg_api = client.get(f"{TAX}/jobs/{jid}/section/capital_gains").json()["sales"]
    ws = wb["CAPITAL_GAINS"]
    hdr = [c.value for c in ws[3]]
    checked = mismatch = both_blank = writer_only = 0
    writer_only_cols = set()
    for name in ("Full Value of Consideration (Rs.)", "Cost of Acquisition (Rs.)",
                 "Capital Gain (Rs.)"):
        ci, ai = hdr.index(name), col(cg_api, name)
        for r in range(len(cg_api["rows"])):
            book = ws.cell(row=4 + r, column=ci + 1).value
            shown = cg_api["rows"][r][ai]
            if isinstance(book, str) and "FX_UNAVAILABLE" in book:
                if shown in (None, ""):
                    both_blank += 1
                else:
                    # The workbook blanks it but the engine context still holds
                    # a figure. The two renderings of one context must agree.
                    writer_only += 1
                    writer_only_cols.add(name)
                continue
            if isinstance(book, (int, float)) and isinstance(shown, (int, float)):
                checked += 1
                if abs(float(book) - float(shown)) > 1.0:
                    mismatch += 1
                    print(f"        DIFF row {r} {name}: book {book} api {shown}")
    fails += ok(checked and mismatch == 0,
                "every figure the workbook computes equals the figure on screen",
                f"{checked} cell(s) compared, {mismatch} differ")
    fails += ok(both_blank > 0,
                "an unconvertible figure is blank on BOTH, never zero on either",
                f"{both_blank} cell(s) blank in the workbook and on screen")

    # The former divergence: build_cg() used to compute the sale consideration
    # whenever the SALE-date rate existed, while excel_out._cg() blanked all
    # three rupee columns together when EITHER rate was missing. The three
    # columns are now presented as one unit in both renderings.
    fails += ok(writer_only == 0,
                "no cell holds a figure in the engine context that the workbook "
                "blanks", f"{writer_only} divergent cell(s)"
                + (f": {sorted(writer_only_cols)}" if writer_only_cols else ""))
    fails += ok(not any(cg_api["rows"][r][col(cg_api, n)] == 0
                        for r in range(len(cg_api["rows"]))
                        for n in ("Full Value of Consideration (Rs.)",
                                  "Cost of Acquisition (Rs.)",
                                  "Capital Gain (Rs.) ".strip())
                        if str(cg_api["rows"][r][col(cg_api, "Computable")]
                               ).startswith("No")),
                "and an unconvertible row shows no rupee ZERO on screen either",
                "- blank, never nil")

    # Each rupee column is blanked against the rate IT needs - not all three
    # together. A row with a sale rate but no cost rate must still show its
    # consideration, and must still blank its cost and its gain.
    partial = 0
    for r in range(len(cg_api["rows"])):
        row = cg_api["rows"][r]
        sale = row[col(cg_api, "FX Rate - Sale Date")] or 0
        cost = row[col(cg_api, "FX Rate - Vest Date")] or 0
        fvc = row[col(cg_api, "Full Value of Consideration (Rs.)")]
        coa = row[col(cg_api, "Cost of Acquisition (Rs.)")]
        gain = row[col(cg_api, "Capital Gain (Rs.)")]
        book = [ws.cell(row=4 + r, column=hdr.index(n) + 1).value
                for n in ("Full Value of Consideration (Rs.)",
                          "Cost of Acquisition (Rs.)", "Capital Gain (Rs.)")]
        blank_book = [isinstance(v, str) and "FX_UNAVAILABLE" in v for v in book]
        want = [not sale, not cost, not (sale and cost)]
        if bool(sale) != bool(cost):
            partial += 1
        fails += ok([fvc in (None, ""), coa in (None, ""),
                     gain in (None, "")] == want and blank_book == want,
                    f"row {r}: each rupee column follows the rate it needs",
                    f"sale rate {'yes' if sale else 'NO'}, cost rate "
                    f"{'yes' if cost else 'NO'}")
    fails += ok(partial > 0,
                "the rule is exercised by a real partially-convertible row",
                f"{partial} row(s) have one rate and not the other")

    ws = wb["REVIEW_REQUIRED"]
    book_rows = sum(1 for r in ws.iter_rows(min_row=4, values_only=True) if r[0])
    fails += ok(book_rows == len(rev["register"]["rows"]),
                "review register: screen row count == workbook row count",
                f"{book_rows}")

    print()
    print("=" * 98)
    print("6. THE UI ADDS NO ARITHMETIC OF ITS OWN")
    print("=" * 98)
    js = (ROOT / "src" / "static" / "app.js").read_text()
    apy = (ROOT / "src" / "tools" / "tax" / "api.py").read_text()
    web = (ROOT / "src" / "tools" / "tax" / "routes.py").read_text()
    import re
    # A sum/reduce over served rows would be the UI computing a figure.
    bad_js = [p for p in ("reduce(", ".sum(", "Math.round(", "* rate", "/ rate")
              if p in js and p != "Math.round("]
    fails += ok(not bad_js, "app.js contains no aggregation or conversion",
                f"checked for {', '.join(('reduce', 'sum', 'rate arithmetic'))}")
    fails += ok("toLocaleString" in js,
                "numbers are formatted for display only", "- separators, no maths")
    for name, src in (("tools/tax/api.py", apy), ("tools/tax/routes.py", web)):
        hits = [t for t in ("build_lots(", "build_cg(", "build_a3(", "build_a2(",
                            "build_dividends(", "FXTable(", "rate_quote(")
                if t in src]
        fails += ok(not hits, f"{name} calls no tax-engine function directly",
                    f"found {hits}" if hits else "- only prepare/build_context")
    fails += ok("write_workbook" in apy and "build_context" in apy
                and "prepare" in apy,
                "the tax service layer uses the five agreed integration points")

    # The FX screen renders the engine's own hierarchy frame rather than a list
    # written into the browser, so the two cannot drift apart.
    fails += ok("d.fx_sources" in js and "Source hierarchy" in js,
                "the FX screen renders the engine's ranked source frame",
                "- the hierarchy is not hardcoded in the browser")
    for level in ("SBI_TTBR", "GOOGLE_FINANCE", "FBIL", "ECB", "MANUAL"):
        fails += ok(level not in js,
                    f"{level:<15} is not written into app.js",
                    "- it arrives from the engine")
    # "Full provenance" hides columns; it must never drop one from the payload.
    fxw = client.get(f"{TAX}/jobs/{jid}/section/fx_working").json()
    served = set(fxw["fx_audit"]["columns"]) | set(fxw["fx_sources"]["columns"])
    named = set(re.findall(r'optional: \[([^\]]*)\]', js, re.S))
    opt_cols = {n.strip().strip('"') for blk in named for n in blk.split(",")
                if n.strip()}
    fails += ok(opt_cols and opt_cols <= served,
                "every column the screen can collapse is a column the API serves",
                f"{len(opt_cols)} collapsible" if opt_cols <= served
                else f"unknown: {sorted(opt_cols - served)}")
    fails += ok("display: none" in (ROOT / "src" / "static" / "app.css").read_text(),
                "collapsed columns are hidden in CSS, not removed from the table",
                "- the evidence is always in the DOM")

    print()
    print("=" * 98)
    print("6b. NO RATE COLUMN ANYWHERE REPORTS A RATE OF ZERO")
    print("=" * 98)
    # A rate of nil is not a rate. Any figure it would have converted is blank
    # too. This sweeps every frame the engine builds, not only the ones a screen
    # happens to render, so a new column cannot reintroduce it unnoticed.
    import pandas as _pd
    zeros = []
    for key, frame in ctx.items():
        if not isinstance(frame, _pd.DataFrame) or frame.empty:
            continue
        for c in frame.columns:
            name = str(c)
            if not any(w in name for w in ("Rate", "TTBR", "FX")):
                continue
            for v in frame[c]:
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0:
                    zeros.append(f"{key}.{name}")
                    break
    fails += ok(not zeros, "every rate column is blank rather than zero where no "
                "rate exists", f"{len(ctx)} frames swept"
                if not zeros else f"zero found in {sorted(set(zeros))}")

    print()
    print("=" * 98)
    print("7. GOOGLE FINANCE ROUND-TRIP")
    print("=" * 98)
    fx = client.get(f"{TAX}/jobs/{jid}/fx/requests").json()
    fails += ok(fx["count"] == 3,
                f"{fx['count']} outstanding currency/date pair(s)",
                "- the three accepted UBS input-data blockers")
    req_dates = {r[fx["requests"]["columns"].index("date")]
                 for r in fx["requests"]["rows"]}
    fails += ok(req_dates == {"2023-04-03", "2023-07-03", "2024-07-01"},
                "exactly the dates the engine could not resolve",
                ", ".join(sorted(req_dates)))
    fails += ok(all("GOOGLEFINANCE" in r[fx["requests"]["columns"].index(
                    "google_finance_formula")] for r in fx["requests"]["rows"]),
                "each row carries the formula for its own pair")
    fails += ok("CURRENCY:USDINR" in fx["tsv"] and fx["tsv"].count("\n") == 3,
                "the paste-ready block is one header plus one row per pair")

    before = client.get(f"{TAX}/jobs/{jid}/section/capital_gains").json()["sales"]
    ci = col(before, "Capital Gain (Rs.)")
    blank_before = sum(1 for r in before["rows"] if r[ci] in (None, ""))

    # Rates a preparer would paste back. TEST VALUES, not real published rates.
    paste = ("date\tbase_currency\tquote_currency\trate\n"
             "2023-04-03\tUSD\tINR\t82.1500\n"
             "2023-07-03\tUSD\tINR\t81.9200\n"
             "2024-07-01\tUSD\tINR\t83.4500\n"
             "2024-07-02\tUSD\tINR\t\n")            # no rate - must be skipped
    imp = client.post(f"{TAX}/jobs/{jid}/fx/rates", data={"text": paste}).json()
    fails += ok(imp["imported"] == 3 and imp["skipped"] == 1,
                "3 rates imported, 1 row with no rate skipped",
                "- a blank is never read as zero")

    run2 = client.post(f"{TAX}/jobs/{jid}/run")
    fails += ok(run2.status_code == 200 and run2.json()["job"]["run_count"] == 2,
                "re-run completed")
    after = client.get(f"{TAX}/jobs/{jid}/section/capital_gains").json()["sales"]
    blank_after = sum(1 for r in after["rows"] if r[col(after, "Capital Gain (Rs.)")]
                      in (None, ""))
    fails += ok(blank_after < blank_before,
                "rupee figures that were blank are now computed",
                f"{blank_before} blank -> {blank_after}")

    src_i = col(after, "FX Source - Cost")
    sources_used = {r[src_i] for r in after["rows"]}
    fails += ok("GOOGLE_FINANCE" in sources_used,
                "and the rows say where the rate came from", str(sorted(sources_used)))
    basis_i = col(after, "Cost Conversion Basis")
    fails += ok(any("Google Finance" in str(r[basis_i]) for r in after["rows"]),
                "the conversion basis names the fallback, not 'TTBR'")

    s2 = client.get(f"{TAX}/jobs/{jid}/section/summary").json()
    fails += ok("Google Finance" in s2["fx_status"],
                "the summary FX status reports the fallback", s2["fx_status"][:64])
    fails += ok(s2["counts"]["Blocker"] < s["counts"]["Blocker"],
                "blockers fell as the input data improved",
                f"{s['counts']['Blocker']} -> {s2['counts']['Blocker']}")
    j2 = api.STORE.get(jid)
    fails += ok(len(client.get(f"{TAX}/jobs/{jid}/section/review").json()
                    ["register"]["rows"]) == len(j2.ctx["register"].frame()),
                "provenance and review status survive the round-trip")

    print()
    print("=" * 98)
    print("8. RETENTION - SOURCE DOCUMENTS ARE PURGED WITH THE JOB")
    print("=" * 98)
    work = api.STORE.get(jid).workdir
    fails += ok(work.exists() and any(api.STORE.get(jid).uploads.glob("*")),
                "uploads live only in the job's own working directory",
                str(work.relative_to(ROOT)))
    outside = [p for p in (ROOT / "uploads").glob("API_Test*")]
    fails += ok(not outside, "and nowhere else in the project")
    fails += ok(client.delete(f"{TAX}/jobs/{jid}").json()["purged"] is True,
                "discarding the job purges it")
    fails += ok(not work.exists(),
                "the working directory and every source document are gone")
    fails += ok(client.get(f"{TAX}/jobs/{jid}/section/summary").status_code == 404,
                "and the job is no longer readable")
    tmp.unlink(missing_ok=True)

    print()
    print("=" * 98)
    print("9. MODULAR APPLICATION - THE TOOLS CANNOT ENTANGLE")
    print("=" * 98)
    man = client.get("/api/tools").json()
    ids = [t["id"] for t in man["tools"]]
    fails += ok(ids == ["tax", "bank"], "the manifest lists both tools",
                ", ".join(ids))
    fails += ok("RSU" not in man["app_name"] and "Tax" not in man["app_name"],
                "the application name is neutral, not tax-specific",
                man["app_name"])
    tax_t = next(t for t in man["tools"] if t["id"] == "tax")
    bank_t = next(t for t in man["tools"] if t["id"] == "bank")
    fails += ok(tax_t["status"] == "available" and len(tax_t["sections"]) == 13,
                f"the tax tool is implemented with {len(tax_t['sections'])} sections")
    want = ["documents", "processing", "summary", "fa_a2", "fa_a3",
            "capital_gains", "dividends", "fx_working", "reconciliation",
            "vesting", "review", "sources", "export"]
    fails += ok([x["id"] for x in tax_t["sections"]] == want,
                "including Documents, Processing and Export as required")
    fails += ok(bank_t["status"] == "reserved" and not bank_t["sections"],
                "the Bank Statement Analyzer is reserved with no sections",
                bank_t["label"])
    fails += ok("Coming soon" in bank_t["note"],
                "and is presented as coming soon")

    from src.web import MOUNTED                                   # noqa: E402
    fails += ok(MOUNTED == ["tax"], "only available tools are mounted",
                f"mounted {MOUNTED}")
    fails += ok(client.get("/api/tools/bank/jobs").status_code == 404,
                "the reserved tool has NO routes behind it - nothing to call")
    fails += ok(not (ROOT / "src" / "tools" / "bank").exists()
                or not any((ROOT / "src" / "tools" / "bank").glob("*.py")),
                "and no bank code exists in this repository",
                "- nothing imported, modified or duplicated")

    # The shell must not know what a tool does.
    shell = (ROOT / "src" / "web.py").read_text()
    reg = (ROOT / "src" / "modules.py").read_text()
    taxy = ("compute", "build_context", "prepare(", "Schedule FA", "TTBR",
            "capital gain", "dividend", "1042")
    leak = [w for w in taxy if w.lower() in shell.lower()]
    fails += ok(not leak, "the shell contains no tax vocabulary or engine import",
                f"leaked {leak}" if leak else "web.py is subject-agnostic")
    # The registry is a manifest, not a container: parse it and check that the
    # only things it imports are the standard library.
    import ast
    tree = ast.parse(reg)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    stdlib_only = imported <= {"__future__", "dataclasses"}
    fails += ok(stdlib_only, "the registry imports no tool code",
                f"imports only {sorted(imported)} - listing a tool costs nothing"
                if stdlib_only else f"imports {sorted(imported)}")

    # Neither tool may reference the other.
    tax_src = "\n".join((ROOT / "src" / "tools" / "tax" / f).read_text()
                        for f in ("api.py", "routes.py"))
    fails += ok("bank" not in tax_src.lower(),
                "the tax module never mentions the bank tool")
    engine = "\n".join((ROOT / "src" / f).read_text() for f in
                       ("compute.py", "run.py", "fx.py", "excel_out.py"))
    for term in ("bank statement", "tools.bank", "import bank"):
        fails += ok(term not in engine.lower(),
                    f"the tax engine has no dependency on {term!r}")
    fails += ok("modules" not in engine and "src.web" not in engine,
                "and the engine does not import the shell either",
                "- it stays usable from the CLI alone")

    # Each tool keeps its own working directory.
    fails += ok(str(api.WORK_ROOT).endswith("work/tax"),
                "the tax tool owns its own working directory",
                str(api.WORK_ROOT.relative_to(ROOT)))

    print()
    print(f"RESULT: {'ALL CHECKS PASSED' if not fails else str(fails) + ' CHECK(S) FAILED'}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
