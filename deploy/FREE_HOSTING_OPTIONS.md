# Free hosting options — evaluated against this app's actual requirements

This is a separate document from `deploy/RUNBOOK.md` on purpose: a free
option is judged here strictly against the hard requirements this
application has, not against "free" alone. Verified via live web search on
2026-09-07 — free-tier terms change without notice, so re-verify the
provider's own current terms before committing to one, especially the
region/capacity caveats noted below.

**Decision made:** Oracle Cloud Always Free was selected — the host is
provisioned and reachable as `rsu-tool` (Ubuntu 24.04 E2 Micro; see
`deploy/RUNBOOK.md` §0). This document is retained as the record of why,
not as an open question.

## The hard requirements (all must hold, not most)

1. **Persistent filesystem** — `work/tax/` and `clients/` must survive
   restarts and redeploys. Anything with an ephemeral/reset-on-deploy
   filesystem fails this outright, however good it looks otherwise.
2. **One long-running process** — this is a stateful, in-memory
   (`JobStore`), single-worker application, not a stateless request handler.
   Serverless/functions platforms (cold-start-per-request) are structurally
   incompatible, not just inconvenient.
3. **HTTPS** on a **stable URL**.
4. **Ability to install the required OS package** (`poppler-utils`) —
   platforms that only accept application code, with no OS package access
   and no custom container/Dockerfile, cannot satisfy this.
5. **No unacceptable sleep/lifecycle limits** — a preparer walking through
   a multi-step working paper should not hit a 30–60 second cold-start
   partway through, or lose an in-progress job because the instance was
   recycled.

## Platforms that fail structurally — ruled out without further evaluation

- **Vercel, Netlify, Cloudflare Pages/Workers** — built for static sites and
  short-lived serverless functions. No long-running process, no persistent
  filesystem. Fails (2) and (1) by design, not by tier.
- **PythonAnywhere (free tier)** — no custom OS package installation (no way
  to `apt-get install poppler-utils`), and free-tier CPU/daily runtime and
  outbound-network limits. Fails (4), likely (5).
- **Fly.io** — genuinely free hosting **no longer exists here**. Verified:
  Fly.io deprecated its free Hobby/Launch/Scale allowances in 2024; new
  organizations get a 2-hour trial only, after which even a single small
  always-on machine costs roughly $2–6/month plus separate per-GB volume
  charges. Not a free option as of this check.
- **Render (free web service tier)** — historically ephemeral disk on the
  free tier (a persistent disk is a paid add-on) and services spin down
  after inactivity, with a cold-start delay on the next request. Likely
  fails (1) and (5); re-verify current terms directly before relying on
  this, as tier details shift often.
- **Replit (free/Hacker tier)** — "Always On"/reserved-VM deployments are a
  paid feature; the free tier's own deployments are not guaranteed
  always-on. Likely fails (5).

## Platforms that can genuinely satisfy all five requirements

Both of these are real virtual machines with root access — meaning nothing
about this application needs to change to run on them; they are a "Linux
VM," exactly what Phase 4 asks for, that happens to cost nothing.

### Oracle Cloud Infrastructure — "Always Free" Ampere A1 (recommended)

- **Persistent, for the life of the account** — not a trial, not a credit
  that expires. Always Free tenancies get up to 4 OCPU / 24 GB RAM of
  Ampere A1 compute (a single Always Free VM.Standard.A1.Flex instance is
  commonly sized 2 OCPU / 12 GB RAM), plus persistent block storage.
- **Full root/systemd VM** — `deploy/workbench.service`,
  `deploy/Caddyfile.example`/`nginx.conf.example`, and `poppler-utils`
  install exactly as written in `deploy/RUNBOOK.md`, with no platform-specific
  changes.
- **Caveat — capacity, not cost**: Oracle's own community reporting (see
  sources) describes intermittent "out of host capacity" errors when
  provisioning A1 Flex instances in high-demand regions (notably some US
  regions); EU/APAC regions (Frankfurt, Singapore, Tokyo) reportedly
  provision more reliably. This is a provisioning-time inconvenience, not a
  running-cost or a "not really free" situation — once provisioned, the
  instance is real and yours.
- **Caveat — domain**: Oracle gives you a public IP, not a domain. Use a
  free dynamic-DNS subdomain (see below) or a domain you already own.

  Sources: [Always Free Resources — Oracle docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm), [OCI Always Free: Updated Ampere A1 Compute Allocation](https://community.oracle.com/customerconnect/discussion/970310/oci-always-free-updated-ampere-a1-compute-allocation)

### Google Cloud Platform — "Always Free" e2-micro

- **Persistent, permanent monthly quota**, not a trial — one e2-micro
  instance plus 30 GB persistent disk, free every month, indefinitely.
- **Region-locked**: only free in `us-west1`, `us-central1`, or `us-east1`.
  Launching anywhere else incurs normal charges — pick one of those three
  explicitly when creating the instance.
- **Small**: e2-micro is a burstable, low-resource shape. Fine for a
  single-preparer, single-worker app with the low/single concurrency this
  deployment already assumes; do not expect headroom for heavy PDF batches.
- **Caveat — domain**: same as Oracle — a public IP, not a domain, comes
  with it.

  Sources: [Google Cloud Free Tier services and products](https://cloud.google.com/free)

### Getting a free, stable domain to pair with either VM

Both options above need is a real, DNS-resolvable name for Caddy (or
certbot) to issue a publicly-trusted HTTPS certificate against — an IP
address alone cannot get one through the normal ACME flow. A free dynamic-DNS
subdomain (for example, a `*.duckdns.org`-style provider) that you point an
A record at your VM's IP satisfies this at zero cost: it is genuine public
DNS, which is all Caddy/certbot need — it does not need to be a domain you
purchased.

### Bottom line

**A fully free, genuinely persistent, HTTPS, single-worker deployment is
achievable today**: an Oracle Cloud Always Free A1 VM (or a GCP e2-micro in
one of the three free regions) + a free dynamic-DNS subdomain + Caddy for
automatic HTTPS + this repository's `deploy/workbench.service`. The
trade-offs are modest hardware and, for Oracle specifically, possible
provisioning delays in high-demand regions — not cost, and not a violation
of any of the five hard requirements above.
