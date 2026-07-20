# Onboarding v2 — implementation progress tracker

Companion to `ONBOARDING_V2_PLAN.md`. **Update the Status column every time
a step lands or fails** — this file is the resume point: if a session dies
mid-implementation, the next one reads this table, verifies the last "done"
step actually works, and continues from the first non-done row. Statuses:
`pending` / `in-progress` / `done (YYYY-MM-DD)` / `blocked: <reason>`.

| # | Step | Files | Status |
|---|------|-------|--------|
| 1 | Onboarding record store (per-client step state, survives restarts) | `backend/onboarding.py` | done (2026-07-19) |
| 2 | Config validator ported (EU voice, quoted hours, structure) | `backend/validator.py`, `requirements.txt` (+pyyaml) | done (2026-07-19) |
| 3 | DNS precheck (resolve from VPS, compare to VPS public IP) | `backend/core.py: check_dns` | done (2026-07-19) |
| 4 | Recreate helper (env changes need recreate, never restart) | `backend/core.py: recreate_app`; `/api/env/write` audited | done (2026-07-19) |
| 5 | Idempotent/resumable provisioning (skip done stages; keep existing .env; seed-once marker) | `backend/new_client.py` | done (2026-07-19) |
| 6 | Teardown (compose down -v, rm dir, Caddy block removal + validate + recreate, clients.json cleanup) | `backend/new_client.py: teardown_client_stream`, route | done (2026-07-19) |
| 7 | Smoke suite (health loopback+public, cert, /config, admin API, CSP, chat round-trip, backup timer) | `backend/smoke.py`, route | done (2026-07-19) |
| 8 | Credentials vault (named sets, apply-to-client + tests + recreate, import-from-client, file creds) | `backend/vault.py`, routes | done (2026-07-19) |
| 9 | Onboarding step-runner API (run/re-run any step, streamed, state recorded) | `backend/routes.py` (/api/onboardings…) | done (2026-07-19) |
| 10 | Frontend: labeled tab navigation (Dashboard / Onboarding / Credentials / Host) + button renames | `frontend/index.html`, `app.js`, `style.css` | done (2026-07-19) |
| 11 | Frontend: onboarding stepper page (list in-flight, per-step run/status, streamed console, resume) | same | done (2026-07-19) |
| 12 | Frontend: vault UI (sets CRUD, import from client, apply to client) | same | done (2026-07-19) |
| 13 | Frontend: smoke checklist in client detail + validate-config button | same | done (2026-07-19) |
| 14 | Caddyfile commit-back gate after wiring | `backend/new_client.py`, surfaced in verify step | done (2026-07-19) |
| 15 | End-to-end test with a throwaway client (`acme`), then teardown via its own button | — human step — | done (2026-07-20) |
| 16 | Phase 2: intake bundle form fields beyond basics (services CSV import, hours widget, SOUL templates, backup timers) | future session | pending |

## Step 15 result (2026-07-20)

Real run on the live VPS with client `acme` (acme.my-ai-receptionist.com,
port 8003): three fields + medical checkbox → Deploy → all six steps green
hands-off (DNS auto-retry, credentials auto-imported from template, config
valid, verify 7/7 critical smoke checks PASS). Two housekeeping warnings by
design: backup timer not installed; Caddyfile auto-commit stays local (the
VPS deploy key is read-only, cannot push). Fixed along the way, both
verified in the fake-VPS harness and then live:

- verify readiness gate: the credentials step recreates the app container;
  verify now polls loopback /health (5s interval, 2 min cap, visible
  countdown) before running smoke — without it a healthy deploy showed 6
  false FAILs.
- teardown UI: was buffering the whole 1–2 min response before printing
  anything (looked dead) and swallowed errors; now streams line by line,
  reports failures as one sentence, leaves the Confirm button live to retry.

## Verification notes per landed step

- Steps 1–9: `python -m py_compile` on every touched module; validator +
  onboarding store unit-smoked in the sandbox (see below); SSH-dependent
  paths (5,6,7 DNS/teardown/smoke) are compile-checked and code-reviewed
  but NOT run against the real VPS from the sandbox — step 15 is where
  they get exercised for real.
- Step 10–13: static HTML/JS; JS syntax-checked with `node --check`;
  renders verified only structurally (no browser run against a live
  backend from the sandbox) — step 15 covers the click-through.

## How to resume after an interruption

1. Read this table top to bottom; find the first row not `done`.
2. Re-verify the row above it actually works (its Files compile; for
   backend steps, hit its endpoint against a local `uvicorn backend.main`).
3. Continue implementing from the first non-done row; keep this file
   updated as you go.
4. The rebuilt console ships only when `docker compose -f
   docker-compose.local.yml up -d --build --force-recreate` has been run
   locally — code changes are baked at build time (no live reload in
   Docker; see README).
