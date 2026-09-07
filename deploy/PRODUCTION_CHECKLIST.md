# Production checklist — Phase 4 exit criteria

Unchecked until executed against a real host. Nothing here has been marked
done by this deployment kit itself — it is the checklist to work through
once hosting exists, not a record of having done so. This session has no
SSH access to the target host, so nothing below could be verified live from
here either — see `deploy/RUNBOOK.md` §0 for what the operator has reported
as already provisioned on the current target (`rsu-tool`, Oracle Ubuntu
24.04 E2 Micro) and still needs a live check, vs. what is genuinely
outstanding.

- [x] Persistent Linux VM/server provisioned — `rsu-tool` (Oracle Cloud
      Always Free Ubuntu 24.04 E2 Micro). See `deploy/RUNBOOK.md` §1–2.
- [ ] Python 3.12 + `.venv` confirmed to hold exactly `requirements.txt`
      (reported already in place on `rsu-tool` — verify, don't recreate;
      RUNBOOK §0/§3).
- [ ] `poppler-utils` installed at the OS level, `pdftotext -v` verified
      (reported already in place on `rsu-tool` — verify; RUNBOOK §0/§4).
- [ ] Dedicated low-privilege service account created; application
      directory and `clients/` owned by it (RUNBOOK §5).
- [ ] `work/tax/` confirmed to sit on durable disk, survives a restart
      (reported already in place on `rsu-tool` — verify the restart
      survives live; RUNBOOK §0/§6; validated live in smoke test item V).
- [ ] Secured `.env` confirmed to hold real **pinned** `APP_PASSWORD` and
      `APP_SECRET` (never left to auto-generate), mode 600, and
      `workbench.service`'s `EnvironmentFile=` pointed at its real path
      (reported already in place on `rsu-tool` — verify location and
      contents, wire up the unit; RUNBOOK §0/§7).
- [ ] systemd unit installed and enabled (`workbench.service`), confirmed
      **exactly one** Uvicorn worker, no `--workers` flag anywhere
      (RUNBOOK §8).
- [ ] Reverse proxy (Caddy or nginx) installed, bound to the app's
      `127.0.0.1:8000`, forwarding correct headers (RUNBOOK §9).
- [ ] DNS configured, resolving to the server's public IP (RUNBOOK §10).
- [ ] HTTPS certificate issued and auto-renewing (RUNBOOK §11).
- [ ] Client master data (`entities.csv`/`accounts.csv`) provisioned
      server-side for each real client (RUNBOOK §12).
- [ ] Full smoke test (`deploy/SMOKE_TEST.md`, items A–Y) run against the
      **real HTTPS URL**, not localhost — all passed.
- [ ] Supervisor restart-on-crash verified live (kill the process, confirm
      it comes back) — RUNBOOK §14, smoke test item W.
- [ ] Supervisor restart-on-reboot verified live (`sudo reboot`, confirm it
      comes back without manual intervention) — smoke test item W.
- [ ] Backup approach for `clients/` and the `workbench.env` secret in
      place (RUNBOOK §15).
- [ ] Rollback procedure understood and, ideally, rehearsed once on a
      non-critical change (RUNBOOK §16).
- [ ] `git status` clean on the server's checkout, `HEAD` matches
      `origin/project-completion`.

Phase 4 is complete only when every box above is checked against a real
server, not simulated.
