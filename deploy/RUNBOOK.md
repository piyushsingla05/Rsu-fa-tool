# Deployment runbook — Tax / Foreign Assets Working Paper

This is the step-by-step procedure for Phase 4 (production deployment) once
a real, persistent Linux host is available. Nothing in this document has
been executed against a live server — no hosting has been provisioned, no
domain chosen, no certificate issued. This is preparation only, so Phase 4
can be carried out immediately once that access exists.

**Architecture preserved throughout:** reverse proxy (HTTPS) → exactly one
Uvicorn worker → FastAPI → the tax engine. `JobStore`
(`src/tools/tax/api.py`) is a single in-memory store — running more than one
application process (multiple systemd instances, multiple Uvicorn workers,
or a load balancer fanning out to more than one copy) would give each
process its own disconnected set of jobs. Do not do that.

---

## 1. Supported Linux environment

Any current Debian/Ubuntu LTS is assumed below (the `apt-get` commands and
`poppler-utils` package name match those distributions). RHEL/Fedora
equivalents are noted inline where they differ.

## 2. Application directory

Clone the repository to a fixed path, e.g.:

```bash
sudo mkdir -p /opt/rsu-fa-tool
sudo chown "$USER:$USER" /opt/rsu-fa-tool
git clone <your-repo-url> /opt/rsu-fa-tool
cd /opt/rsu-fa-tool
git checkout project-completion
```

Substitute this path everywhere `/opt/rsu-fa-tool` appears below and in
`deploy/workbench.service`.

## 3. Python environment

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip
cd /opt/rsu-fa-tool
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

`requirements.txt` pins exactly the six packages the application's own
import graph requires (fastapi, uvicorn, python-multipart, pandas,
openpyxl, PyYAML) — nothing from a wider development environment.

## 4. OS dependency: poppler-utils

PDF broker statements are read via the external `pdftotext` binary
(`src/ingest/tabular.py`), not a Python library. The Python process starts
fine without it; every PDF upload then fails.

```bash
sudo apt-get install -y poppler-utils      # Debian/Ubuntu
# sudo dnf install -y poppler-utils        # RHEL/Fedora
```

Verify: `pdftotext -v` should print a version string.

## 5. Service account and filesystem permissions

Run the application under its own dedicated, low-privilege account — not
root, not a shared shell login:

```bash
sudo useradd --system --home /opt/rsu-fa-tool --shell /usr/sbin/nologin workbench
sudo chown -R workbench:workbench /opt/rsu-fa-tool
```

The application itself sets a restrictive process umask (`0o077`, in
`main()`) the moment it actually starts serving, so every job/upload/
workbook file and directory it creates from then on defaults to owner-only
(`0700`/`0600`) regardless of the host's own umask — this is already
covered by Phase 3B and needs no further action here.

What still needs deliberate handling on the host:

- **`/etc/workbench/workbench.env`** (the real, filled-in environment file —
  see §7): create it `chmod 600`, owned by `workbench:workbench`. It holds
  `APP_PASSWORD`/`APP_SECRET` in plain text; treat it like any other
  credential file.
- **`clients/<CLIENT_NAME>/`** (client master data — see §11): same
  ownership, and consider `chmod 700` on the directory tree since these are
  real client entity/account details, not job-scoped transient uploads.
- **`work/tax/`** (see §6): created automatically by the application under
  the repository root; no manual step needed beyond making sure the
  `workbench` user owns the repository directory (done above).

## 6. Persistent storage: `work/tax`

`WORK_ROOT` (`src/tools/tax/api.py`) is a fixed path,
`<repository-root>/work/tax` — not an environment variable. It holds every
job's uploaded documents and generated workbook for the life of that job
(purged automatically 8 hours after creation, or immediately on manual
close — this retention logic is unchanged from Phase 2 and needs no
reconfiguration).

For this to actually be "persistent" in the production sense:

- It must live on the VM's real, durable disk — not a container overlay
  that gets discarded on redeploy, not a tmpfs mount.
- A deploy step must never `git clean`/`rm -rf` the repository directory in
  a way that removes `work/`. A plain `git pull` in place is safe (`work/`
  is not tracked by git — check `.gitignore`); rebuilding the deployment
  from a fresh clone is not, unless `work/` is preserved/copied across
  first.
- On process restart (crash, redeploy, reboot), `JobStore.reconcile_orphans()`
  runs automatically at startup and reconciles any job directories left on
  disk against the (now-empty) in-memory registry, purging only those past
  the existing `JOB_TTL` (8 hours) — this is existing Phase 2 behaviour,
  unchanged, and is exactly what makes a restart safe for `work/tax`.

## 7. Environment variables

Copy `.env.example` to the real environment file **outside the git
repository**:

```bash
sudo mkdir -p /etc/workbench
sudo cp .env.example /etc/workbench/workbench.env
sudo chmod 600 /etc/workbench/workbench.env
sudo chown workbench:workbench /etc/workbench/workbench.env
sudo nano /etc/workbench/workbench.env    # fill in real APP_PASSWORD / APP_SECRET
```

Both `APP_PASSWORD` and `APP_SECRET` **must** be set to real, persistent
values here — never left unset in production:

- Unset `APP_PASSWORD` auto-generates one and prints it once to stdout,
  which nobody will see under a supervisor running headless.
- Unset `APP_SECRET` regenerates in the process's own memory on every
  start, which force-logs-out every session on every restart (deploy,
  crash, reboot).

Generate strong values, e.g. `openssl rand -base64 32` for `APP_SECRET`, and
a password manager-generated passphrase for `APP_PASSWORD`. Never commit
the filled-in file; `.env.example` (committed) carries variable names only.

## 8. systemd service

```bash
sudo cp deploy/workbench.service /etc/systemd/system/workbench.service
# edit the placeholder paths/user inside it first if you deviated from
# /opt/rsu-fa-tool or the "workbench" account above
sudo systemctl daemon-reload
sudo systemctl enable --now workbench
sudo systemctl status workbench
```

This unit runs **exactly one** Uvicorn worker (`python3 -m src.web --host
127.0.0.1 --port 8000`, no `--workers`), restarts on failure
(`Restart=on-failure`), and starts on boot (`enable` + `After=network.target`).

## 9. Reverse proxy configuration

Pick one:

- **Caddy** (recommended — automatic HTTPS with no separate certbot step):
  see `deploy/Caddyfile.example`.
- **nginx** (if you already standardise on it): see
  `deploy/nginx.conf.example`, paired with certbot for the certificate.

Either way, the proxy binds `:80`/`:443` publicly and forwards to
`127.0.0.1:8000`, where the systemd unit above binds the app. The app never
listens on a public interface directly.

## 10. DNS configuration

Point an A record (and AAAA, if the host has IPv6) for your chosen
domain/subdomain at the server's public IP address. Propagation is usually
minutes; allow up to 24–48 hours worst case before assuming failure. If you
don't have a domain, see `deploy/FREE_HOSTING_OPTIONS.md` for a genuinely
free way to get a stable, resolvable subdomain.

## 11. HTTPS certificate process

- **Caddy**: nothing manual — it requests a Let's Encrypt certificate for
  the domain in `Caddyfile.example` automatically the first time it starts
  with DNS already pointing at it, and renews it automatically thereafter.
- **nginx**: run `sudo certbot --nginx -d your-domain` once DNS is live;
  certbot edits the nginx config to add the `:443` block and installs its
  own renewal timer/cron job.

Confirm forwarded headers are correct after this step — with either proxy,
`curl -I https://your-domain/` followed by inspecting a login response's
`Set-Cookie` header should show `Secure` present (see §14 and the smoke
test).

## 12. Client master data

The engine reads `clients/<CLIENT_NAME>/entities.csv` and `accounts.csv` as
pre-provisioned, per-client reference data (Schedule FA entity/account
disclosure fields) — this is a filesystem convention, not something the web
UI can upload or edit (confirmed in the Phase 3B audit; not redesigned in
this phase). For each real client you intend to run:

```bash
sudo -u workbench mkdir -p /opt/rsu-fa-tool/clients/<CLIENT_NAME>
# then place entities.csv and accounts.csv there, matching the column
# headers in src/models.py (ENTITIES_COLUMNS / ACCOUNTS_COLUMNS)
sudo chmod 700 /opt/rsu-fa-tool/clients/<CLIENT_NAME>
```

A job created through the web UI does not automatically pick this up today
(its own `client_dir` starts empty) — see the Phase 3B audit for why, and
treat wiring the two together as a distinct, future change, not part of
this deployment.

`/clients/` is deliberately excluded from git (see `.gitignore` — real
account numbers, addresses and entity names must never reach the
repository, private or not). Cloning the repository onto the server brings
**none** of this: every client's `entities.csv`/`accounts.csv` must be
placed on each server by hand, out of band from any deploy step, every
time.

## 13. Verifying the service

```bash
sudo systemctl status workbench          # Active: active (running)
sudo journalctl -u workbench -f          # tail logs
curl -I http://127.0.0.1:8000/           # 200, direct to the app
curl -I https://your-domain/             # 200, through the proxy + TLS
```

Then work through `deploy/SMOKE_TEST.md` (items A–Y) against the real
HTTPS URL — not localhost.

## 14. Restart / recovery commands

```bash
sudo systemctl restart workbench   # after a config or code change
sudo systemctl stop workbench
sudo systemctl start workbench
sudo systemctl status workbench    # confirm it came back
```

To verify the supervisor genuinely restarts on crash (Phase 4 item W):

```bash
sudo systemctl status workbench --no-pager | grep 'Main PID'
sudo kill -9 <that PID>
sleep 5
sudo systemctl status workbench    # should show Active: active (running) again, new PID
```

To verify it survives a reboot: `sudo reboot`, then once back,
`systemctl status workbench` should already show it running without manual
intervention.

## 15. Backup considerations

- **`clients/<CLIENT_NAME>/`** — real, hand-maintained reference data with
  no other copy. Back this up like any other source-of-record data (a
  simple periodic `tar`/rsync to another location is sufficient given its
  size).
- **`/etc/workbench/workbench.env`** — back up the *fact that it exists and
  its values*, via your secrets manager or password manager, not as a plain
  file copy sitting next to backups of everything else.
- **`work/tax/`** — deliberately transient by design (8-hour retention);
  not a backup target. If a specific client's workbook needs to be kept
  long-term, download it via Export and store it wherever finished
  deliverables are normally kept — that is the durable copy, not the job
  directory.
- **The git repository itself** is the backup for all application code and
  configuration; nothing here should be edited only on the server without
  also being committed.

## 16. Rollback procedure

```bash
cd /opt/rsu-fa-tool
git fetch origin
git log --oneline -5                    # identify the last known-good commit
git checkout <known-good-sha>           # or: git reset --hard origin/project-completion
sudo systemctl restart workbench
sudo systemctl status workbench
```

`work/tax/` and `clients/` are untouched by any of this (neither is tracked
by git — confirm with `git status` before a `reset --hard` regardless, per
standard practice). If the rollback is due to a bad requirements.txt change,
also re-run `./venv/bin/pip install -r requirements.txt` before restarting.
