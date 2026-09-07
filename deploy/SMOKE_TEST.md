# Production smoke-test checklist

Run every item below against the **real HTTPS URL** of the deployed server —
never localhost, never the reverse proxy's loopback address. Use a real
browser for the UI-driven items (E–T) so cookie/redirect/rendering behaviour
matches what an actual operator sees; curl is fine for the header/status
checks (A, B, D, X, Y).

Use the real UBS sample documents already in this repository's working tree
under `work/realdata/UBS/` for G, together with a provisioned
`clients/<CLIENT_NAME>/entities.csv`/`accounts.csv` (RUNBOOK §12) so FA-A2's
entity columns are populated rather than blank.

- [ ] **A.** `curl -I https://your-domain/` returns `200` over HTTPS (not a
      certificate warning, not a redirect loop).
- [ ] **B.** `curl -I https://your-domain/api/tools` (no cookie) returns
      `401`; the browser's login page renders for an unauthenticated visit
      to `/`.
- [ ] **C.** Login with the real `APP_PASSWORD` succeeds.
- [ ] **D.** Inspect the `Set-Cookie` header from the login response (or the
      browser's dev tools → Application → Cookies): `HttpOnly` ✓,
      `SameSite=Strict` ✓, **`Secure` ✓** — the last one only appears
      correctly if the reverse proxy's forwarded headers are configured
      right (RUNBOOK §9/§11).
- [ ] **E.** The Workbench shell loads (sidebar, both tools listed — Tax
      available, Bank Statement Analyzer reserved).
- [ ] **F.** The Tax / Foreign Assets Working Paper tool opens.
- [ ] **G.** The real UBS sample documents (`work/realdata/UBS/*.pdf`)
      upload successfully and are identified (broker UBS, correct profiles).
- [ ] **H.** Processing completes (state reaches `done`, not `failed`).
- [ ] **I.** Summary renders (verdict + headline figures).
- [ ] **J.** FA-A2 renders.
- [ ] **K.** FA-A3 renders.
- [ ] **L.** Capital Gains renders.
- [ ] **M.** Dividends / 1042-S renders.
- [ ] **N.** FX Working renders.
- [ ] **O.** Reconciliation renders.
- [ ] **P.** Vesting & Sales renders.
- [ ] **Q.** Review Required renders (blockers/review/note counts visible).
- [ ] **R.** Source Traceability renders.
- [ ] **S.** Export downloads a workbook; opening it shows all 11 tabs
      (SUMMARY, FA_A2, FA_A3, RSU_MASTER, VESTING_SALES, DIVIDENDS_1042,
      CAPITAL_GAINS, FX_WORKING, FSI_TR_WORKING, RECONCILIATION,
      REVIEW_REQUIRED).
- [ ] **T.** Logout succeeds.
- [ ] **U.** After logout, `/api/tools` and every tax route return `401`
      again — no lingering access via a stale cookie.
- [ ] **V.** Restart the service (`sudo systemctl restart workbench`) with a
      job still inside its retention window; confirm its uploaded documents
      and/or workbook are still present in `work/tax/<job-id>/` afterward
      (do not confirm this by re-running the job — confirm the same job/
      files are still there).
- [ ] **W.** Kill the process (`sudo kill -9 <pid>`) and confirm systemd
      brings it back automatically within a few seconds
      (`systemctl status workbench`); separately, reboot the server and
      confirm the service is already running afterward with no manual step.
- [ ] **X.** Deliberately trigger a few error paths (a bogus job id, a
      malformed upload, an empty-upload `POST /run`) and confirm every
      response is a clean, generic message — no stack trace, no absolute
      filesystem path, no Python exception text leaking to the client.
- [ ] **Y.** `curl -I https://your-domain/openapi.json` returns `404` (and
      so do `/docs` and `/redoc`).
