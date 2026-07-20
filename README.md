# ops-console

A status/usage dashboard for every deployed client instance running this
product — clinics, law firms, whatever vertical, all the same underlying
codebase. Shows up/down, deployed version (with the actual commit
subjects behind `origin/master`, not just a count), this month's
chat/token usage against a quota you set, an interaction funnel and
"minutes saved" estimate, real uptime/latency history, and — at the VPS
level — overall disk/memory usage, per-client container CPU/mem + disk,
and every site actually being served from that box, discovered straight
from its Caddyfile (so a non-clinic site sharing the same VPS, like a
marketing site, shows up too, not just whatever's been manually added
here). Almost all of this is read-only; the one exception (a guarded,
single-client "deploy what's already on origin/master" button) is
documented below.

Built the same way the product itself is: a `Dockerfile` +
`docker-compose.yml`/`docker-compose.local.yml` split, or a plain venv for
local dev — same shape as `dental-clinic-agent`'s own setup.

Almost everything here is read-only — nearly every check is `GET
/health`/`GET /admin/metrics`/`git fetch` + read-only git/docker
inspection over SSH. The one deliberate exception is the **Deploy**
button (see below): a guarded, single-client "pull what's already on
`origin/master` and restart" action. It does **not** replace
`deploy.ps1` — that's still how you commit and push your own local
changes. Deploy here only syncs a client to commits that are already on
the remote.

Deliberately **not** part of the `dental-clinic-agent` repo: this tool
holds per-client admin passwords and SSH targets in `clients.json`, and
that repo's `deploy.ps1` stages everything (`git add .`) on every deploy —
a separate, never-pushed project avoids that risk entirely.

## Run it — plain venv (simplest; no Docker, no SSH-permission gotchas)

```powershell
cd ops-console
python -m venv .venv
.venv\Scripts\activate
pip install -r backend\requirements.txt
copy clients.example.json clients.json
```

Edit `clients.json` (or use the in-app "Fetch admin token via SSH" button
once it's running) — real secrets, don't email or paste it anywhere.

Sanity-check headlessly first:

```powershell
python -m backend.core --check
```

Then run the dashboard:

```powershell
uvicorn backend.main:app --reload --port 5050
```

Open http://127.0.0.1:5050 in a browser. (Not port 8000 — the product's
own containers default to `127.0.0.1:8000` too, since that's the exact
port the VPS's Caddyfile proxies to, so a local copy of the chatbot
running in Docker will grab it first. 8100 can also collide, depending
on what else is running on your machine. If 5050 is ever taken too, run
`netstat -ano | findstr LISTENING` to see what's actually in use and pick
any number missing from that list — and if a port still fails to bind
even though it's absent from that list, Windows may have it reserved via
Hyper-V/WSL2's dynamic port-exclusion ranges; check with `netsh interface
ipv4 show excludedportrange protocol=tcp`.)

## Run it — Docker

```powershell
cd ops-console
.\local-deploy.ps1
```

`local-deploy.ps1` is the redeploy one-liner for this path: stop → rebuild
the image from the working tree → start → wait for `/health` and print the
URL. Run it after any code change — the Docker path bakes code into the
image at build time, so edits are invisible until a rebuild. Data
(`/data`: clients.json, history, deploy log, vault) lives in the
`app_data_local` named volume and survives every rebuild. The equivalent
manual command is `docker compose -f docker-compose.local.yml up -d
--build`.

Open http://127.0.0.1:8101 (moved off 8100, which collides with
primeconnect1's local preview — see the comment in
`docker-compose.local.yml`). It starts with **zero configured clients** —
add them from the UI ("Add Client", then "Fetch admin token via SSH" once
`ssh_target`/`remote_dir` are filled in). The Docker path's `clients.json`
lives *inside* the `app_data_local` named volume, at `/data/clients.json`
(see "Running in Docker vs. venv: two separate config stores" below) — a
`copy clients.example.json clients.json` on the host does **not** reach
it, so use the UI rather than that command for this path.

**If you ever manually seed/overwrite the container's `/data/clients.json`
from the host anyway** (e.g. `docker compose -f docker-compose.local.yml cp
clients.json app:/data/clients.json`) — don't. `docker cp` always writes as
root, but the app runs as the non-root `appuser`, so the very next save
from the UI will fail with `PermissionError: [Errno 13] Permission denied:
'/data/clients.json'`. If it's already happened, fix ownership with:
```powershell
docker compose -f docker-compose.local.yml exec -u root app chown -R appuser:appuser /data
```
(This bit us for real once, hence the ownership fix in `backend/core.py`
that additionally treats a still-placeholder `admin_token` as unset — see
"What it checks, and how" — so a half-seeded config can't also trip the
monitored instance's own brute-force lockout by polling it with the
literal example token.)

**SSH version-check in Docker**: uncomment the `.ssh-src` volume line in
`docker-compose.local.yml` to let the container reach your VPS the same
way `deploy.ps1` does. Note it mounts to `.ssh-src`, not `.ssh` directly —
`entrypoint.sh` copies it into a container-owned `~/.ssh` with correct
permissions at startup, because bind-mounting a Windows folder straight
into `~/.ssh` carries ACLs OpenSSH's client will refuse ("UNPROTECTED
PRIVATE KEY FILE"). If you'd rather avoid touching your SSH key from
Docker at all, the plain-venv path above uses Windows' own `ssh.exe`
directly — identical to how `deploy.ps1` already works, no extra
permission handling needed.

## What it checks, and how

Per client:

| Check | Source | Auth |
|---|---|---|
| Up/down, voice-active-sessions | `GET {base_url}/health` | none (public) |
| Deployed commit, commits behind `origin/master`, container state | `git fetch` + `git rev-parse`/`rev-list` + `docker compose ps`, over SSH | your SSH key |
| Chats + token usage this month | `GET {base_url}/admin/metrics` | `X-Admin-Token` header |
| Per-container CPU%/mem%, `/data` disk usage | `docker stats` + `docker compose exec app du -sh /data`, scoped to that client's own `remote_dir`, over SSH | your SSH key |
| Bookings/reschedules/cancellations/callbacks/registrations this month, "minutes saved" | `GET {base_url}/admin/audit` (best-effort parsing) | `X-Admin-Token` header |
| Estimated $ cost this month | computed locally from token usage × your configured rate | none (no extra call) |
| Real uptime %/latency (24h, 7d) | ops-console's own local `history.jsonl`, appended every poll | none (local state) |

Per host (the `hosts` array in `clients.json` — one entry seeded for the
shared VPS):

| Check | Source | Auth |
|---|---|---|
| Overall disk %, memory %, load average | `df` + `free` + `/proc/loadavg`, over SSH | your SSH key |
| Every site actually served from this box | `cat` the Caddyfile + resolve any `{$VARNAME}` hostname from `.env`, over SSH | your SSH key |

Each discovered site is cross-referenced against your configured clients
by hostname — matched ones show the client's name, unmatched ones (like a
non-clinic marketing site sharing the VPS) show as "unmanaged": ops-console
found them but has no `remote_dir` for them, so only the hostname and proxy
target are shown, no resource stats. Add it as a client with a `remote_dir`
to unlock full stats for it too. (A hostname can itself be a comma/space-
joined list — e.g. one `DOMAIN=` env var covering two hostnames for the
same site — matching splits it and checks each part individually.)

## Business impact: interactions, "minutes saved", cost, uptime history

Beyond up/down + tokens, ops-console now reads three more things per
client, all built entirely from data the product's own backend already
exposes (no redeployment needed):

- **Interaction funnel** (`GET {base_url}/admin/audit`): counts this
  month's bookings, reschedules, cancellations, human-handoff callbacks,
  and registrations. This is genuinely best-effort — it's just consuming
  whatever `/admin/audit` returns, tolerant of a couple of reasonable
  field-name variants, and reports an explicit error rather than fake
  zeros if the row shape doesn't match at all.
- **"Minutes saved"**: the interaction counts above, multiplied by a
  configurable assumed handling-time-per-task (defaults: 6 min/booking,
  4 min/reschedule, 3 min/cancellation, 5 min/callback — override per
  client via `minutes_per_booking`/`_reschedule`/`_cancellation`/
  `_callback` in the Add/Edit form's "Advanced" section or directly in
  `clients.json`). A rough but real number for the invoicing/sales pitch:
  "~14 hours of receptionist time saved this month."
- **Estimated cost**: token usage (already tracked) × a $-per-1,000-tokens
  rate you configure per client (`cost_per_1k_input_tokens`/
  `_cached_tokens`/`_output_tokens`) — ops-console has no way to know a
  clinic's actual LLM provider pricing on its own, so the cost estimate is
  simply **hidden** until you set a rate, never shown as a misleading
  $0.00.
- **Uptime & latency history**: every dashboard poll now appends one line
  (name, timestamp, up/down, latency) to a local `history.jsonl` (path
  overridable via `HISTORY_FILE` — points at the Docker image's persistent
  `/data` volume there) — purely ops-console's own state, nothing to do
  with the monitored product. Real uptime % and latency p50/p95 over the
  last 24h/7d are computed from that log and shown in the client table and
  detail modal, with the sample count alongside so "100% uptime" off 2
  samples right after a restart reads differently than off 1,000. The log
  self-prunes entries older than 30 days on a small random chance per poll
  rather than a separate scheduled job.

A "This month, across every client" summary panel above the client table
totals minutes saved, bookings, reschedules+cancellations, callbacks, and
(if any client has cost rates configured) estimated cost, across your
whole fleet.

## Deploy — the one mutating action, and why it's safe to have

The Version section of a client's detail modal shows not just "N commits
behind" but the actual commit subjects, so you can judge whether the gap
matters instead of trusting a bare count. When a client is behind, a
"Deploy latest" button appears. Clicking it:

1. Requires typing the exact client name to confirm — checked both in the
   browser and again server-side, so a stray click or an automated call
   without a matching `confirm_name` is rejected outright (`400`), nothing
   touched.
2. Runs `git fetch` + `git pull --ff-only origin master` — `--ff-only`
   means a diverged or dirty tree fails loudly instead of silently
   discarding anything. Nothing is ever force-reset.
3. Only if the pull succeeded: `docker compose build`.
4. Only if the build succeeded: `docker compose up -d`. A failed build
   never triggers a restart — the old, working container just keeps
   running.
5. Everything is scoped to exactly that client's own
   `remote_dir/docker-compose.yml` — the same scoping
   `check_client_resources` already uses for `docker stats`. Caddy, the
   other clinic, and everything else on the VPS are never touched.
6. Every attempt (success or failure) is appended to a local
   `deploy_log.jsonl` audit trail — timestamp, which stage it reached,
   the resulting commit, and any error — via `GET
   /api/clients/{name}/deploy-log`.

This does **not** add a login/password in front of the dashboard itself —
by design decision, since it's local-only on your own machine today. If
that ever stops being true (dashboard reachable beyond `127.0.0.1`), add
auth in front of it before that point, not after — a deploy-capable
dashboard with no auth is a materially bigger risk than a read-only one
once it's reachable from anywhere else.

### Two more guardrails, added after a real incident

A live deploy once hit a container-name collision: a compose file change
declared an explicit `container_name` that was already in use by a
*different* client's compose project (a shared Caddy container). Docker
correctly refused rather than tearing anything down, but the failure was
only discovered at `up` time with a raw Docker error. Two things now catch
this earlier:

- **Pre-flight collision check.** Between a successful build and `up -d`,
  deploy_client checks whether any container name the target compose file
  declares is already owned by a *different* compose project on the host
  — if so, it refuses cleanly (stage `"precheck"`, naming the exact
  container and owning project) instead of attempting `up` at all.
- **Infra-risk flagging.** The "what's not deployed yet" commit list now
  also flags — with a visible warning, both in the Version section and
  again in the deploy confirmation dialog — when the behind range touches
  `docker-compose*.yml`, a `Dockerfile`, or anything under `deploy/`. The
  precheck above only catches container-*name* collisions specifically;
  an infra-file change can still be worth a real look before deploying
  even when the precheck comes back clean.

Neither of these replaces judgment — they catch a specific, previously-hit
failure mode automatically, and flag a category of change that deserves
more scrutiny than routine application code.

### The actual root cause, and the real fix: `deploy_client` is override-aware

The paragraph that used to sit here speculated that the product's Caddy
architecture was ambiguous or mid-migration. That was wrong, and worth
correcting rather than quietly deleting: reading the real files on the
client's own machine (`docker-compose.override.yml` and
`CLINICA_VALOR_RUNBOOK.md` for the satellite clinic that hit this) showed
the product already has a single, deliberate, documented pattern for a
second clinic sharing a VPS with a primary one — a local-only,
never-committed `docker-compose.override.yml` that renames the `app`
service's container, moves it off host networking onto a published
loopback port, and *deliberately never starts its own `caddy` service*,
relying on the primary's shared Caddy (wired via a site block in the
primary's `deploy/Caddyfile`) instead. The runbook says explicitly:
"name the service explicitly — never a bare `docker compose up -d`, or
you'll try to start this checkout's own `caddy` service and it'll fail to
bind 80/443, already owned by the primary's Caddy." That's exactly the
collision this tool hit — because `deploy_client` ran an unscoped `docker
compose up -d`, ignoring both the override file and the documented
procedure. The bug was in ops-console, not the product.

`deploy_client` now checks for `{remote_dir}/docker-compose.override.yml`
before building/upping anything:

- **If an override file exists** (a satellite clinic), both compose files
  are loaded (`-f docker-compose.yml -f docker-compose.override.yml`) and
  both `build` and `up -d` are explicitly scoped to `app` only — matching
  the documented `docker compose up -d --build app` procedure exactly.
  This client's own `caddy` service is never even considered, so it can
  never again collide with a shared Caddy container.
- **If no override file exists** (a primary clinic, the sole owner of its
  own Caddy), behavior is unchanged: full-stack `build`/`up -d`, no
  service scoping.

The container-name precheck also had to change to match: when an override
exists, it reads the declared `container_name` from the override file
directly (since only `app` will ever start for that project), rather than
from a full `docker compose config` merge that would still include the
never-started `caddy` service's name.

This is general, not Clínica-Valor-specific — any future satellite clinic
onboarded the same documented way (an override file, no own Caddy) is
covered automatically, with no clinic-specific code anywhere in
`deploy_client`. Verified by extracting the actual SSH command
`deploy_client` constructs and executing it for real against fake `git`/
`docker` executables (not just mocking `run_ssh` and checking the parsed
result) across three cases: a satellite client succeeding (confirms `-f`
scoping and `app`-only build/up), a primary client succeeding unchanged,
and a simulated collision on the satellite path (confirms the precheck
still blocks `up` even with the override file in play) — plus a
regression check that the pull-failure short-circuit still skips
build/precheck/up correctly with the new override-detection step spliced
into the command chain.

### A second gap the same incident exposed: no way to force a rebuild once git reads "up to date"

`deploy_client` on the backend never actually required "commits behind >
0" — `pull`/`build`/`precheck`/`up` always run unconditionally; a
`--ff-only` pull with nothing new to pull is just a harmless no-op. The
gate was only in the frontend: the **Deploy latest** button was hidden
entirely once `git` reported 0 commits behind. That's exactly what made
the Clínica Valor incident hide for hours instead of surfacing
immediately — the original (pre-override-fix) deploy attempt pulled and
built successfully, then failed at `up` on the collision. That left the
checkout genuinely "up to date" while the *container* was still running
the pre-pull build, and with the button gone, there was no way through
the dashboard to retrigger a rebuild — only a manual SSH command could
have forced it.

The button now stays available even when up to date, relabeled **"Rebuild
& restart (up to date, but confirm the container matches)"**, with a note
in the confirm dialog explaining why: git reporting current doesn't by
itself prove the running container matches it. Same confirmation flow,
same pre-flight collision check, same audit log entry — this is not a new
code path, just no longer hidden behind a commit count that can lag
reality after a partial failure.

`/admin/metrics` and the per-conversation token columns it reads already
ship in `backend/admin.py` / `backend/db/models.py` of the product itself
— ops-console adds no backend code and needs no redeployment to any
client to work. The host-level checks are plain read-only shell commands
(`df`, `free`, `docker stats`, `cat`) — nothing product-specific, works
against any Linux box your SSH key reaches.

**Why per-container stats are scoped by `remote_dir` instead of matching
Caddy's proxy port directly to a container**: the product's
`docker-compose.yml` uses `network_mode: host`, so Docker itself has no
port-publishing metadata to map a `reverse_proxy 127.0.0.1:PORT` line back
to "which container" — that would need fragile `ss -ltnp` /
`/proc/<pid>/cgroup` inference. Using the `remote_dir` you already gave
each client (`docker compose -f {remote_dir}/docker-compose.yml ps -q`) is
exact and simple instead.

## Batch updates — the Updates tab (one run instead of N modals)

Updating the fleet used to mean opening each client's detail modal, typing
its name, clicking Deploy, waiting out the build, and moving to the next
one — fine for 3 clients, a day's work for 500. The **Updates** tab is the
same deploy, fleet-wide:

- **One table of every client that's behind `origin/master`** — running
  commit, behind count, the actual pending commit subjects (expandable per
  row), and an `⚠ infra` badge when the range touches
  `docker-compose*.yml` / `Dockerfile*` / `deploy/**`. A count badge on
  the tab itself shows how many need updates from anywhere in the app.
  Clients already up to date (or unreachable) are summarized below the
  table, not listed. Clicking a client's name still opens its detail
  modal.
- **Select all / deselect all / any subset**, then **Update selected**.
  One typed confirmation for the whole batch — `update <N>` — instead of
  typing each client's name. The count is checked server-side too
  (`POST /api/deploy-batch` rejects a mismatched count or an unknown name
  before touching anything, so a bad request deploys nothing rather than
  half the list). A per-row Update button covers the one-off case through
  the same flow (`update 1`).
- **A live status column + console** during the run — each client moves
  through queued → updating → ✔ updated @commit / ✘ failed (stage), with
  the console showing per-client results as they land and the tail of the
  failing stage's output on error, same information the individual deploy
  result shows.
- **Same guarded pipeline per client, not a new deploy path.** Every
  client in the batch goes through the exact `core.deploy_client()` the
  individual button uses — `git pull --ff-only` → build → container-name
  collision precheck → `up -d`, each stage gating the next, override-aware
  `-p`-pinned scoping and all. Each outcome is appended to the same
  `deploy_log.jsonl` (with `"batch": true`), so per-client deploy history
  stays in one place. One client failing never stops the rest.
- **Concurrency that respects the boxes:** parallel across VPSes (capped
  at `MAX_PARALLEL_DEPLOY_HOSTS`, 3), strictly **sequential per VPS** —
  a `docker compose build` is heavy for a small VPS, and the clients most
  likely to be batch-updated together are exactly the ones sharing a box;
  several simultaneous builds on one host is how a routine update becomes
  an outage.

The stream speaks the same NDJSON dialect as `/new-client/stream`:
`{"type": "start", "name"}` → `{"type": "result", "name", ok, stage,
commit, error, output}` per client → one final `{"type": "done",
"ok_count", "fail_count"}`.

## Fleet tests — the Tests tab

Same shape as the Updates tab, for checking instead of deploying: every
client in one table, select some or all, one click, a live per-client
status column + streaming console. Each selected client runs the full
check suite — the 9-check smoke suite (loopback + public health, `.env`
sanity, TLS certificate near-expiry, `/config` sanity, admin API with the
stored token, embed CSP, a **real chat round-trip** that proves the LLM
key end to end, backup timer) plus live `site_config.yaml` validation, with
expandable per-check results right in the table. This replaces the old
"Run smoke suite" / "Validate live config" buttons that were buried at
the bottom of the client detail modal (the modal's "Run tests…" button
now jumps here with that client preselected).

Verdict mirrors the single smoke endpoint's bar: every check except
`backup_timer` must pass AND the config must have no validation errors;
backup-timer failures and config warnings are reported as warnings, never
as failure. No typed confirmation — everything is read-only against the
monitored instances (the chat round-trip does burn a few LLM tokens per
client, the cost of actually proving the key works). `POST
/api/test-batch` streams the same NDJSON dialect as `/deploy-batch`, and
both now run through one generic `core.batch_stream(clients, job)` —
parallel across hosts, strictly sequential per host.

The `env_file` check (added 2026-07-20, same incident chain as the backups
work below) audits each instance's `.env` **structurally, over SSH, without
any secret value ever appearing in a result**: every line must be
`KEY=value` (a wrapped value is how the primary's backups broke), multi-key
vars (`NVIDIA_API_KEY`, `MISTRAL_API_KEY`, …) must not have a space after
the comma (keys stay comma-separated — rotation is unaffected), a
`BACKUP_PASSPHRASE` must exist, and `ADMIN_PASSWORD` must not be the
default — that one is checked unconditionally because `ENV=dev` (a
deliberate tradeoff on voice-enabled non-medical instances) silently
disables the product's own boot guard that would otherwise refuse the
default password. `ENV != prod` itself is reported as a note, not a
failure. `DOMAIN`'s comma+space is deliberately NOT flagged (Caddy
address-list syntax).

### Backups panel (same page)

Below the tests table: **Set up backups on selected** installs (or repairs)
the nightly encrypted-backup systemd timer on each selected client's VPS —
unit files named `<checkout-dir>-backup.*` with paths derived from that
client's real `remote_dir` (the naming the smoke suite's `backup_timer`
check expects) — then **proves it** by running a real backup immediately
via `systemctl start` and verifying the archive count in
`{remote_dir}/backups` grew. Born from a real incident (2026-07-20): the
primary's timer had fired nightly for 17 days producing nothing (unit
pointed at a nonexistent `/opt` path → `status=203/EXEC`, no journal
output), which is why "timer active" alone is never trusted — only a fresh
archive is. Stages gate each other (passphrase present → passwordless sudo
→ script present → install/enable → test run → verify), each failure comes
back with an actionable per-client error, and everything is idempotent.
Typed confirmation `backups <N>`, checked server-side (`POST
/api/backup-batch`, same NDJSON dialect); every result lands in
`deploy_log.jsonl` as action `"backup-setup"`. Requires the SSH user to
have passwordless sudo (the product's `setup-server.sh` grants it).

The client detail modal itself was rebuilt in the same pass: a wide
two-column card layout with an at-a-glance KPI strip (health, version,
uptime, tokens/quota, chats/time-saved, est. cost), alert banners for
down/over-quota/infra-risk, and the deploy area in its own card — in
place of the old single narrow scrolling column. The same pass fixed a
long-standing CSS gap: there was no generic `.hidden` rule (only
`.modal.hidden`/`.page.hidden`), so several "hidden" elements — the
onboarding console among them — were actually always visible.

## Running in Docker vs. venv: two separate config stores, and a real SSH limit

The venv path (`clients.json` next to the project root) and the Docker path
(`/data/clients.json` inside the `app_data_local` named volume) are **two
independent config stores** — adding/editing a client in one does not
appear in the other. Switching between them mid-project (as opposed to
picking one and sticking with it) means re-adding clients, re-fetching
tokens, and hand-copying the `hosts` array (see below) into whichever
store the other one is missing. There's no sync between them by design —
this tool has no database, just the one JSON file per running instance.

**Docker's SSH access has a real limitation venv doesn't hit**: `docker
compose up --build` bakes the code into the image (no live-reload — a code
change always needs a rebuild to take effect, unlike venv+`uvicorn
--reload`), and the optional SSH key mount
(`${USERPROFILE}/.ssh:/home/appuser/.ssh-src:ro` in
`docker-compose.local.yml`, commented out by default) only carries the key
*file* into the container, not a Windows SSH agent session. If your
private key has a passphrase and Windows' ssh-agent (or Pageant) normally
supplies it, the container has no access to that agent — `ssh
-o BatchMode=yes` inside the container will reject the key outright with
`Permission denied (publickey)` even though the file is right there,
because it can't prompt for the passphrase and has nothing cached.
Genuinely fixable (Docker Desktop can forward the Windows OpenSSH agent
socket, but that's more setup than this project currently does), but until
that's wired up, **venv/uvicorn is the reliable path for anything
SSH-dependent** (version checks, resource stats, deploy, fetch-token) —
it uses Windows' own `ssh.exe` and whatever agent you already have working
for `deploy.ps1`, no extra plumbing needed.

Two SSH errors worth recognizing immediately if they show up again:
- `Host key verification failed` — the container/user's `~/.ssh/known_hosts`
  doesn't have this host trusted yet (a fresh container home, or a person
  who never manually accepted the host key on that specific machine/user).
  `run_ssh()` now passes `-o StrictHostKeyChecking=accept-new`, so this
  should self-heal on the next connection attempt — if it doesn't, SSH
  itself (not ops-console) is the thing to debug.
- `Permission denied (publickey)` *after* host-key trust succeeds — the
  key material itself isn't usable from wherever ops-console is running;
  see the agent-forwarding limitation above.

**The `hosts` array (VPS-level disk/mem/Caddyfile panel) has no Add/Edit UI
at all** — it's config-file-only, by design (see the Design section of the
original plan). Whichever store is currently active (venv's project-root
file, or Docker's volume) needs it added by hand if it's missing:
```json
"hosts": [
  {"name": "Hetzner VPS", "ssh_target": "deploy@chat.my-ai-receptionist.com",
   "caddyfile_path": "~/dental-clinic-agent/deploy/Caddyfile",
   "env_path": "~/dental-clinic-agent/.env"}
]
```

## SSH-fetched admin metrics for tunnel-only instances (2026-07-19)

An instance that sets `ADMIN_TUNNEL_ONLY` in its own `.env` (currently:
PrimeConnect AI) hides its ENTIRE admin surface from the public internet —
`/admin/metrics` and `/admin/audit` return 404 through the reverse proxy,
so the plain-HTTPS usage/interactions checks would show "unknown" forever.
Two per-client fields in clients.json handle this:

```json
{"admin_via_ssh": true, "admin_local_port": 8002}
```

With `admin_via_ssh` set, `core._admin_get_json()` fetches those two
endpoints by running `curl http://127.0.0.1:<port>/admin/...` ON the VPS
via the same `run_ssh()` (mounted key) every other SSH check uses — the
admin token still goes in the `X-Admin-Token` header but never crosses the
public internet. Loopback ports on the shared VPS: 8000 primary demo,
8001 Clínica Valor, 8002 PrimeConnect AI. Instances with public admin can
keep `admin_via_ssh: false` (plain HTTPS, as before). The UI's client
editor round-trips both fields (`ClientIn` in routes.py — if you add more
client fields, add them there too or a UI edit silently strips them).

**Docker config store is authoritative and survives rebuilds.** Reminder
with teeth (see the section above): the Docker path reads
`/data/clients.json` from the `app_data_local` VOLUME — `docker compose up
--build --force-recreate` rebuilds CODE but never touches that volume, so
editing the repo's `clients.json` does nothing until you push it in:

```powershell
docker cp clients.json ops-console-local:/data/clients.json
```

No restart needed — config is re-read from disk on every poll. Also note
`ssh_target` everywhere is `deploy@chat.my-ai-receptionist.com` now;
`chat.briers.eu` is retired (no TLS answer) and must not be used as a
chatbot OR tooling address anymore.

## A path bug worth understanding if it ever looks like it's back

A `remote_dir` written with a leading `~` (e.g. `~/dental-clinic-agent` —
the natural way to type it) broke `deploy_client` once already:
`docker compose $compose_files build` failed with `open
/home/deploy/~/dental-clinic-agent/docker-compose.yml: no such file or
directory` — Docker itself, given a literal, un-expanded `~`, resolved it
relative to the SSH session's cwd (its home dir) instead of expanding it.

The cause is a specific bash rule, not a docker-compose quirk: bash only
tilde-expands a literal `~` written directly in the command text, at the
start of a word — **never** the result of a variable/parameter expansion.
`deploy_client`'s override-detection logic assigns compose file paths into
a shell variable (`compose_files`) before using them, so a `~` that
survived intact into that variable's value stayed literal forever after,
no matter how it was quoted downstream. `check_version`/
`check_client_resources` never had this problem because they splice
`remote_dir` directly into the command text, not through a variable — but
that made the bug easy to miss in one place while "working" in another.

Fixed at the one shared source every remote_dir-using function already
goes through: `core._shell_remote_dir()` converts a leading `~` to
`$HOME` (which *does* expand correctly through a variable, since it's
ordinary parameter expansion, not tilde expansion) before any command
string gets built. Any new function that splices `remote_dir` into an SSH
command needs to call this first — that's the actual lesson, not just the
patch.

## The New Client wizard: provisioning a client end to end

Standing up a new client used to mean a human working through a 10+ step
runbook over SSH by hand every time (Clínica Valor's and PrimeConnect AI's
first onboardings both were) — slow, and every manual step is a place to
typo a hostname or forget a flag. `POST /api/new-client` (and its streaming
sibling below) automates everything mechanical: clone, pick a free port,
template the override file / starter `site_config.yaml` / a bootable
placeholder `.env`, first build/boot, swap the real starter config into
`/data`, wire the new hostname into the shared Caddy, and register it in
`clients.json`. It deliberately leaves the one genuinely judgment-requiring
step — which real secrets go in `.env`, and where they come from — to the
Credentials tool above, rather than guessing at it.

**Crash-hardened**: an earlier version of this route had no top-level
exception handling, so any unhandled error (a `PermissionError` on a
half-provisioned `/data` volume was the one that actually happened) just
500'd with no usable detail. It now always returns `{"ok": false, "phase":
"crash", ...}` instead of raising, same defensive posture as every other
mutating route in this codebase.

**Streaming console**: a `docker compose build` here genuinely takes
minutes, and staring at a blank spinner that whole time was the direct
complaint that led to `POST /api/new-client/stream` — an
`application/x-ndjson` response, one JSON object per line
(`{"type": "phase"/"log"/"result", ...}`), consumed frontend-side via
`resp.body.getReader()` (not `EventSource`, which only supports GET). The
New Client modal's console renders these live as they arrive, the same
give-me-real-progress reasoning as the Version/Deploy sections above. The
non-streaming route stays as a thin wrapper that drains the same generator
and returns only the final result — written this way (one real
implementation, one wrapper) specifically so the two paths can never drift
apart.

### Compose project-name pinning — the incident that made every `docker compose` call `-p`-scoped

A live New Client wizard run once took **a different, already-running
client** down. Root cause: `docker compose` infers its project name from
the current directory when none is given explicitly — but every SSH
command this tool builds runs as a single non-interactive command string
with no `cd` into `remote_dir` first (each SSH round trip is its own
process; a `cd` in one command doesn't persist to the next). Two different
clients' checkouts sitting under similarly-shaped directory names left
enough ambiguity that a restart issued for one client's project ended up
recreating/removing the *other* client's container on the same box.

Fixed by scoping literally every `docker compose` invocation in this
codebase with an explicit `-p <project>` — never relying on inferred
context. `core._project_name(remote_dir)` derives the project name the
same simple way every client's own directory is already named
(`remote_dir.rstrip("/").rsplit("/", 1)[-1]`), and it's now threaded
through `check_version`, `deploy_client`, `restart_container`,
`check_client_resources`, and the New Client wizard's own combined
clone/build/up/swap command. Verified with a dedicated test that extracts
the actual SSH command each function builds and asserts `-p <project>` is
in it, rather than just trusting the parsed result.

**The actual lesson, for any future code here that invokes `docker
compose` over SSH**: never assume the remote shell's inferred project name
is the one you mean. Pass `-p` explicitly, always, even when it looks
redundant — "it worked in testing" doesn't rule out a second client's
checkout existing on the same box in production, which is exactly the gap
that caused this.

## Copying credentials between deploys (.env manager)

The **Credentials** button in the header opens a tool for exactly the thing
that used to mean SSHing in twice and hand copy-pasting through vim: getting
a working set of API keys from an existing deploy into a brand-new one.

- **Source / Destination** each resolve to an `ssh_target` + `remote_dir` —
  either pick an already-configured client from the dropdown, or type them
  directly (a brand-new deploy doesn't need to be added as a client first,
  same "works before it's saved" pattern the Add-client form's "Fetch token
  via SSH" button already uses).
- **Load .env** on the source `cat`s its `.env` over SSH (read-only) and
  shows every key. **Load existing .env** on the destination does the same,
  merged into the table rather than replacing it — so loading source, then
  destination, then editing a couple of values, never silently drops a key
  either side already had. A missing `.env` (the normal state for a deploy
  that hasn't been configured yet) is reported as "nothing to load", not an
  error.
- The table always includes the product's known env-var names (from
  `dental-clinic-agent`'s own `.env.example`/`env.clinica-valor`) even
  before anything is loaded, so a brand-new deploy's checklist is visible
  up front. **+ Add key** adds anything not on that list (a future env var,
  or something client-specific).
- **Copy** puts the source value on the clipboard; **&rarr; Use** copies it
  straight into the destination column — either way, no value is ever
  hand-typed through a terminal.
- **Test**, next to whichever keys it applies to, makes one real, minimal,
  read-only API call with the value(s) currently in the destination column
  — a models-list call for Mistral/NVIDIA/OpenRouter, an account fetch for
  Twilio (needs both `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` filled
  in), an SMTP connect+login, or `GET {base_url}/admin/metrics` with
  `X-Admin-Token` for the admin password (needs a Base URL — pulled
  automatically if the destination is a known client, or type one in the
  custom fields for a deploy that isn't a saved client yet but still
  resolves DNS/has a reachable URL). Never guesses — a blank/wrong value
  just reports back what the API said.
- **Write .env to destination** sends the whole table as the new file,
  after typing a confirmation. The existing `.env` on the destination (if
  any) is backed up first as `.env.bak-<timestamp>` in the same directory,
  automatically, every time — so an overwrite is always recoverable
  directly on the VPS, no different from the habit of `cp .env .env.bak`
  before hand-editing except you no longer have to remember to do it. This
  is a genuine "replace the whole file" write, not a partial patch — that's
  why the table is always pre-seeded with the union of source + existing
  destination + known keys before you write, so "replace" doesn't mean
  "lose something".
- Secret values transit as plain JSON between the browser and this local
  FastAPI backend, same trust boundary as the existing `admin_token`/
  `fetch-token` handling — fine while ops-console is local-only on your own
  machine (see the Deploy section above for why that stops being fine the
  moment this is reachable from anywhere else).
- Content is written to the remote `.env` via base64 over the SSH command,
  not spliced in as raw text — a key containing a `$`, backtick, or quote
  can't be misread as a shell command on the way in.

**Round-robin keys are one env var with commas, not two env vars.**
`MISTRAL_API_KEY`/`NVIDIA_API_KEY` can each hold several comma-separated
keys in a single line (`key1, key2`) for the app's own internal rotation —
this is deliberate app-level semantics, not something `env_tool.py`'s
`parse_env()` should ever split on. Two real bugs came from getting this
wrong: `test_mistral` didn't strip to the first key before testing (fixed
to match `test_nvidia`'s existing `key.split(",")[0].strip()`), and an
earlier version of this UI had invented fake `MISTRAL_API_KEY2`/
`NVIDIA_API_KEY2` rows based on a wrong assumption that rotation meant two
separate env vars — removed from `KNOWN_ENV_KEYS`/`CRED_TEST_KIND` in
`app.js`. If a client's own values are ever "our test keys" shared across
every deploy as alternates (confirmed as the intended pattern, not a
one-off), the comma-joined single-line form above is the correct way to
enter them here — not a second row.

## Voice calls on a client

Covered in full in `docs/VOICE_NETWORKING.md` (and as a runnable
`enable-client-voice` Claude Code skill under `.claude/skills/`) — the
short version, since it cost real time to work out and is worth knowing
before it comes up again: a client's voice call can fail in three
different, easy-to-conflate ways — (1) it "rings but never answers"
because plain bridge-mode Docker networking has no path for WebRTC's real
audio (UDP, negotiated separately from the HTTPS signaling) to reach the
container — fixed by every satellite client now running `network_mode:
host` with its own pinned port, same as `_generate_override_yaml()`
generates for every new client automatically; (2) the container
crash-loops on boot with `RuntimeError: Refusing to start in production
with voice.enabled and a non-EU voice provider` whenever a client's `.env`
has `ENV=prod` and `tts.provider` isn't EU-owned (Google TTS is the only
TTS actually wired up in the product today, and it isn't EU-owned) — a
real compliance guard, not a bug, and bypassing it (`ENV=dev`) is a
business decision to make per client, never a default; (3) a
`PermissionError` writing `/data/site_config.yaml` from ownership drift on
the volume, fixed with `chown -R appuser:appuser /data` as root inside the
container. New clients from the wizard ship with a full, ready-to-enable
`voice:` block and voice-capable networking by default — turning it on for
any client (new or old) is the skill's job, not a from-scratch
investigation.

## Known gaps (not built yet)

- **Voice minutes** aren't tracked anywhere in the product's backend yet,
  so usage here is text-chat tokens only. Adding it means a schema change
  + instrumentation in `backend/voice/pipeline.py` on the product side,
  then redeploying every voice-enabled client — real backend work,
  deliberately out of scope for this read-only tool.
- **Version check relies on SSH** rather than a baked-in version endpoint
  — works today since SSH access already exists for `deploy.ps1`, but a
  `GET /admin/version` returning the build-time git SHA would be cleaner
  long-term, and would let ops-console drop the SSH dependency entirely.
- **No alerting yet.** `python -m backend.core --check` is a headless
  entry point specifically so a scheduled/unattended mode (Windows Task
  Scheduler, notify on down or over-quota) is a small addition later
  rather than a rewrite.
- **Cost estimate is a rough $/1K-token calculation you configure per
  client, not a real invoice.** No invoice export (PDF, line items) yet —
  turning the raw numbers into an actual sendable invoice is a deliberate
  next step, not done here.
- **The interaction funnel's action-name matching is best-effort.** It
  buckets `/admin/audit` rows by keyword (book/reschedule/cancel/
  callback/register) rather than an agreed, versioned contract with the
  product's exact action-string names — solid enough to be useful, but
  worth a real second look against the product's actual `AuditLog.action`
  values if the counts ever look off.
- **Uptime/latency history only covers what's been logged since this
  feature shipped** — there's no way to backfill history from before
  `history.jsonl` existed, so uptime % will look sparse (low sample count)
  for the first day or so after upgrading.
- **Local-only.** Not deployed anywhere public yet — no Caddy site, no
  auth in front of the dashboard itself. Fine while it's just you running
  it on your own machine; would need both before it's reachable from
  anywhere but localhost.
