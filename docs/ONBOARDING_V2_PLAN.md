# Onboarding v2 — analysis & implementation plan (2026-07-19)

Goal: a new client instance should cost **≤ 10 minutes of human attention**,
most of it filling in a form — down from the ~60 minutes that Clínica Valor
and PrimeConnect AI each took. This doc first accounts for where the hour
actually goes, then lays out a phased plan with concrete code changes.

Sources: `backend/new_client.py`, `backend/env_tool.py`, README ("New
Client wizard"), dental repo's `deploy/MULTI_CLINIC_ONBOARDING.md`,
`deploy/DEPLOYMENT.md §11`, both per-client runbooks, and the 2026-07-19
session (PrimeConnect hardening) — including the ~15 ad-hoc `pc_*.sh`
scripts that one instance accumulated, which are themselves evidence of
what the wizard doesn't cover.

---

## Part 1 — Analysis

### 1.1 What the wizard already automates (and does well)

`POST /api/new-client(/stream)` handles the purely mechanical middle:
name validation → free-port scan → clone → generated
`docker-compose.override.yml` (host networking + port marker) → starter
`site_config.yaml` → bootable placeholder `.env` (random
`ADMIN_PASSWORD`/`BACKUP_PASSPHRASE`, `COMPOSE_PROJECT_NAME` pinned) →
build → up → config swap into `/data` → reseed → restart → Caddy wiring
(`add_clinic_site.sh`, validated + backed up) → `clients.json`
registration. Stage-gated, streamed live, every compose call `-p`-pinned.
This is solid and stays; nothing below replaces it.

### 1.2 Where the hour actually goes — the nine gaps

Ranked roughly by minutes lost per onboarding:

1. **Real `site_config.yaml` content (≈15–25 min).** The starter config is
   deliberately placeholder. Real hours, services (Valor: 62 of them),
   consultants, phone/email, brand color all get hand-authored in YAML —
   with two known landmines (unquoted `10:00` parsing as sexagesimal 600;
   non-EU voice provider crashing prod boot even with voice disabled).
   `validate_site_config.py` catches both but lives in the OTHER repo and
   is run by hand, sometimes after the crash instead of before.
2. **Secrets (≈10 min).** The placeholder `.env` leaves LLM/SMTP/Twilio
   blank by design. Filling them means the Credentials tool key-by-key,
   plus a separate manual `docker cp` for `google_tts.json`, plus knowing
   the recreate-vs-restart rule (`docker restart` does NOT re-read `.env`
   — bit us live on 2026-07-19; `env_tool.write_remote_env`'s restart path
   needs auditing for the same bug).
3. **DNS (≈5–10 min + retry loops).** A/AAAA records are created by hand at
   the registrar, and nothing checks propagation — if Caddy wiring runs too
   early, cert issuance fails in ways that read like script bugs
   (MULTI_CLINIC #6). Humans poll by feel.
4. **Persona / SOUL.md (0 for a default clinic, ≈20+ min when it matters).**
   The wizard doesn't touch SOUL.md at all. PrimeConnect needed a fully
   custom sales persona; any non-dental client needs at least name/framing
   edits. Today that's hand-editing a 12KB prompt file with no template
   support, then knowing the copy-to-checkout-root + rebuild deploy dance.
5. **Post-provision verification (≈10 min, or a bug found days later).**
   No automated smoke check. The embed/CORS/frame-ancestors mistake has
   bitten BOTH sites; admin login, a real booking round-trip, cert
   validity, widget-on-customer-site are all eyeballed or skipped.
6. **Failed runs aren't resumable (≈10 min when it happens).** The wizard
   refuses to touch an existing directory, so any mid-run failure (build
   flake, SSH drop) means SSH in, `rm -rf` the half-provisioned dir and
   dangling containers/volumes by hand, and start over.
7. **Backups (≈5 min, often deferred).** Each instance needs its systemd
   `*.service`/`*.timer` pair installed and enabled — pure boilerplate,
   done by hand from the runbook, easy to forget entirely.
8. **Caddyfile git hygiene (deferred cost, large).** `add_clinic_site.sh`
   appends on the VPS and *asks* the operator to commit — which didn't
   happen for PrimeConnect, producing two days of repo/server divergence
   and the 2026-07-19 stale-inode wild-goose chase. The commit-back must
   stop being optional-and-forgotten.
9. **Embed handoff (≈5 min).** `allowed_embed_domains` and the customer's
   `widget.js` snippet are assembled by hand; a wrong origin surfaces as a
   blank iframe on the customer's site later.

### 1.3 UX findings (feedback 2026-07-19, confirmed in frontend/index.html)

- **Navigation is a flat row of five terse header buttons** (Refresh /
  New Client / Credentials / Settings / Add client) over one dashboard,
  with all real functionality buried in stacked modals. "New Client"
  (provisions a whole instance) sits next to "Add client" (merely
  registers an already-existing instance in clients.json) — near-identical
  labels for wildly different actions.
- **Onboarding is a modal, not a process.** One dialog fires the whole
  pipeline; there is no visible sequence of steps, no way to see where an
  in-flight onboarding stands, and closing the browser loses the context.
- **Credential copying is structurally awkward**: values live only inside
  each client's remote `.env`; getting them into a new client means
  picking a "template client" and copying key-by-key over SSH round
  trips. There is no single place where "our Mistral key" exists — it's
  implicitly "whatever client X happens to have", and rotating a key
  means visiting every client by hand.

### 1.3b Structural observations

- The wizard automates the middle of the funnel; the hour lives at the
  **edges** — content in, verification out.
- Everything judgment-shaped (services list, secrets, persona) is punted to
  "afterwards, by hand" with no structure. Right instinct (don't guess),
  wrong mechanics (no intake, no templates, no checklist).
- Knowledge is scattered across five documents in two repos; the operator
  is the integration point. That's what "an hour of work" feels like.
- The `pc_*.sh` pile shows post-onboarding fixes are ad-hoc SSH scripts —
  i.e., the wizard has no "repair/rerun stage N" concept.

---

## Part 2 — Implementation plan

Three phases. Each is independently shippable and independently valuable;
each has acceptance criteria. Effort in "sessions" ≈ one focused
evening.

### Phase 1 — Stop the bleeding (2 sessions) → target ≈ 20 min/client

**1A. DNS precheck stage** (`new_client.py` + wizard UI).
New pre-clone stage: resolve the hostname *from the VPS*
(`run_ssh(target, "getent hosts <hostname>")`), compare against the VPS's
own public IP (`curl -4s ifconfig.me` once, cached in `hosts` entry).
UI shows a "DNS ✓/✗ (expected X, got Y)" line with a Re-check button;
Caddy wiring stays disabled until green. Kills the cert-issuance retry
loop for good.

**1B. Integrate the config validator.** Port
`deploy/validate_site_config.py`'s checks (EU voice provider, quoted
hours, plus the rest) into `backend/` as a pure function; run it (a) on
the generated starter config — belt and braces — and (b) as a
**"Validate config"** button/endpoint that pulls `/data/site_config.yaml`
from any live client and reports. Single source of truth: keep the
dental-repo script as a thin wrapper importing the same rules, or
vendor the rules with a header comment pointing both ways.

**1C. One-click "Copy credentials from <template client>".**
Extends `env_tool.py`: copy a chosen subset (LLM keys, SMTP block, Twilio)
from an existing client's `.env` in one action, run the existing per-kind
credential tests on what landed, and — critically — finish with
`docker compose -p <name> up -d app` (recreate), **never** `docker
restart` (the `.env` re-read gotcha, 2026-07-19). Audit
`write_remote_env`'s current restart path for that same bug while in
there. Include `google_tts.json` copy (`scp` VPS-side `cp` from template
checkout) as an optional checkbox with the EU-provider warning inline.

**1D. Post-provision smoke suite** (`new_client.py` new final stage +
standalone `POST /api/clients/{name}/smoke`). Checks, each a named
pass/fail line in the streaming console:
`/health` 200 via loopback AND via public hostname; TLS cert hostname +
expiry; `/config` parses and `site.name` ≠ starter placeholder;
`X-Admin-Token` accepted (`env_tool.test_admin_token`); CSP
`frame-ancestors` contains the customer's site origin; `check_slots`
round-trip via `/chat` API with a scripted message; optional voice:
`/voice/offer` reachable. Rerunnable any time — this doubles as the
"is this client healthy after ANY change" button.

**1E. Resume & teardown.** Replace the "directory exists → refuse" rule:
detect which stages already completed (dir exists? files written?
container built/running? config swapped?) and offer **Resume from failed
stage** or **Teardown** (scoped: `docker compose -p <name> down -v`,
`rm -rf ~/<name>`, remove Caddy block via a new
`remove_clinic_site.sh` mirroring add's backup/validate pattern, drop the
clients.json entry). Every stage becomes idempotent (they nearly are —
the file writes and `docker cp` already are; clone gains
`git -C dir pull || git clone`).

**1F. Caddyfile commit-back becomes a gate, not a suggestion.**
After `wire_caddy`, run `git -C ~/dental-clinic-agent status --porcelain
deploy/Caddyfile`; if dirty, attempt commit+push with the VPS deploy key;
if the key can't push (likely — verify once), surface a RED persistent
checklist item on the client card until a human confirms the commit
landed (`git log origin/master -- deploy/Caddyfile` contains the block).
Never let it silently drift again.

*Acceptance:* a fresh test client (throwaway hostname) provisions
end-to-end from the UI with zero SSH terminal use except DNS record
creation; a deliberately killed build resumes without cleanup; smoke
suite green; Caddyfile clean in git.

### Phase 2 — The intake bundle (2–3 sessions) → target ≤ 10 min/client

**2A. Client Intake form** (new wizard step 0, replacing the 4-field
modal). Collects, with validation as you type: display name, deploy name
(auto-suggested), hostname (auto-suggested `<name>.my-ai-receptionist.com`
— the standing naming rule; briers.eu is retired), business type,
language(s), timezone, hours (structured widget — the generator quotes
them, humans never touch YAML), brand color, customer website origin(s)
(→ `allowed_embed_domains` + CORS), phone/email, voice on/off,
consultants (name/specialty rows), and **services via CSV/XLSX paste or
upload** (name, duration, price, description — Valor's 62 services become
a 30-second paste). Persisted as a `bundle.json` per client (in the
ops-console data volume) so onboarding is re-runnable and auditable.

**2B. Generators consume the bundle.** `_generate_starter_site_config`
becomes `generate_site_config(bundle)` — real content from day one,
validated by 1B before anything ships. `.env` generation fills
`CORS_ORIGINS` from the bundle's origins. The embed snippet
(`<script src="https://<hostname>/widget.js" ...>`) is rendered on the
success screen ready to email to the customer.

**2C. SOUL template library.** A `soul_templates/` directory in the
dental repo (`{business_type}.md` with `{{display_name}}`-style
placeholders; the shipped dental SOUL and the PrimeConnect sales SOUL
become the first two templates). The wizard picks by business type,
fills placeholders, writes it to the checkout root before the build
stage (so it's baked correctly), and stores the rendered copy under
`deploy/<client>/SOUL.md` for git. Custom personas stay a human job —
but they start from a template and a defined deploy path instead of a
blank editor.

**2D. Backup timer stage.** Template
`<client>-backup.service`/`.timer` (the existing per-client units are
already near-identical), `scp` + `systemctl enable --now` via a
sudo-scoped rule (verify the deploy user's sudoers once; else emit the
two commands as a copy-ready checklist item). Smoke suite gains a
"backup timer active" check (`systemctl is-active <client>-backup.timer`).

**2E. Checklist UI.** The client card gets an Onboarding tab rendering
every stage 1A–2D as ✓/✗/pending with timestamps — the five scattered
runbook documents collapse into one live checklist. "Done" is defined as
all green, not operator memory.

*Acceptance:* onboard a fictional clinic start-to-finish; human time
(form + DNS record + credential selection) ≤ 10 minutes measured; the
resulting instance passes smoke; services/hours in the live admin match
the pasted CSV exactly.

### Phase 3 — Scale-out (optional; decide when lead volume is real)

Only worth building when onboarding frequency > ~1/week:
batch mode (multiple bundles queued), a second VPS in `hosts` with
placement choice (port scan + Caddy are already per-host-parameterized),
and/or the CI/CD route from MULTI_CLINIC_ONBOARDING.md (GitHub Actions +
deploy-key secret running validate → clone → wire on a per-client config
push). Note the trust-boundary decision it requires (a runner holding
push-capable SSH credentials) — deliberately not designed here.

### Phase-cutting change (2026-07-19 feedback): UX redesign is not garnish

The three items below are requirements, not polish, and reshape the
phases: navigation lands as a Phase-1 quick win, the credentials vault
REPLACES plan item 1C, and the stepper is the spine of Phase 2's intake
work rather than an extra.

**UX-1. Navigation: labeled sections instead of button soup (Phase 1,
cheap).** Replace the header-button row with 4–5 labeled tabs:
**Dashboard** (today's status board + infra/impact panels) ·
**Onboarding** (the stepper below + a list of in-flight onboardings with
their current step) · **Clients** (list → detail page; "Register existing
instance" lives here, renamed from "Add client") · **Credentials** (the
vault, UX-3) · **Host** (VPS gauges/Caddyfile sites, moved off the
dashboard if it crowds). Immediate quick win even before the tabs: rename
"New Client" → "Onboard new client…" and "Add client" → "Register
existing…" so the two can never be confused again.

**UX-2. Onboarding as a persistent stepper (Phase 2 spine).** A
first-class page, not a modal, with numbered gated steps:

1. **Intake** — the bundle form (2A); saved as draft from the first
   keystroke.
2. **DNS** — shows the exact record to create (`A <hostname> → <VPS IP>`),
   polls the precheck (1A), gates Next until green.
3. **Provision** — the existing streamed clone/build/boot pipeline with
   per-stage ticks; failures offer Resume/Teardown (1E) right here.
4. **Credentials** — assign sets from the vault (UX-3), tests run inline,
   finishes with a container recreate.
5. **Config & persona** — generated site_config applied + validated (1B),
   SOUL template chosen/rendered (2C), services CSV imported.
6. **Verify** — the smoke suite (1D) as a live checklist; Caddyfile
   commit-back gate (1F) and backup timer (2D) surface here.
7. **Done** — handoff sheet: admin URL + generated password, embed
   snippet, ops-console entry confirmed, printable/emailable.

Stepper state persists per client (bundle.json + per-stage status in the
ops-console data volume): close the browser mid-onboarding, come back
tomorrow, the Onboarding tab shows "acme-dental — step 4/7, waiting on
credentials". This *is* the checklist UI (2E); they are one feature.

**UX-3. Credentials vault (Phase 1 — replaces plan item 1C).** Stop
copying between clients; store credentials ONCE, assign them many times:

- Named **credential sets** in ops-console's own store, typed by kind:
  `llm/mistral` ("Mistral main key"), `smtp` ("PrimeConnect mailbox"),
  `twilio`, `file/google_tts` (file-type credential holding the JSON).
  Stored in the data volume alongside clients.json (same trust level as
  the admin tokens already there; documented as such).
- A client's Credentials step = pick one set per slot → ops-console
  merges them into the remote `.env` (via the existing
  `write_remote_env`), uploads file credentials, runs the existing
  per-kind tests, and finishes with `docker compose -p <name> up -d app`
  — a RECREATE, never `docker restart` (the .env re-read gotcha; audit
  the current restart path in the same change).
- Each set records which clients use it → **rotation becomes one edit +
  "re-apply to N clients"** instead of N manual visits.
- Migration path: an "Import from existing client" button reads a chosen
  client's current `.env` once and seeds the vault — the troublesome
  copy flow runs exactly one final time, then never again.

### UX-4 (2026-07-19, after live testing): the one-button standard

Live testing of the stepper produced the decisive feedback: "unclear what
to do, steps not explained, not connected, no flow — at 100 deploys this
is still an ordeal." The corrected north star, now partially shipped and
binding for all future work on this screen:

- **The operator's entire job is: three fields → Deploy → create the DNS
  record it asks for. Nothing else.** Saving the form starts the full run
  (shipped). The credentials step self-serves by importing the template
  client's working credentials automatically (shipped). Every remaining
  pause that isn't DNS is a defect to be engineered away, not documented.
- **No internal vocabulary on the main path.** "Vault", "sets", "template
  client", "site_config" may appear on the Credentials tab for power use
  (rotation), never as something the deploy flow requires the operator to
  learn. Plain language in every step title and failure message.
- **At 100 deploys**: a queue — paste rows of (business name, subdomain),
  the console works through them with the same engine, one progress line
  each, pausing the whole queue only on real failures. This is the true
  Phase-3 item, ahead of CI/CD.
- A failure must always render as: what went wrong in one plain sentence,
  and ONE button that retries from the right place.

### Explicitly out of scope

- Automating DNS record creation (registrar API — different trust
  boundary, low frequency; the 1A precheck removes the pain).
- Auto-generating personas/marketing copy (human judgment; 2C gives
  structure, not automation).
- Multi-tenant single instance (product model is per-client instances —
  dental CLAUDE.md convention #1).

### Sequencing & first session

Phase 1 order: 1E (resume/teardown) first — it de-risks testing all the
rest, since every other item needs repeated throwaway provisions — then
the UX-1 rename quick-win, 1A, 1B, 1D, UX-3 (vault, superseding 1C), 1F. First session deliverable: 1E + 1A working against a
throwaway `test-onboarding` client, torn down afterwards by its own new
teardown button.
