# Token economy & managed-service overhaul — design plan (2026-07-20)

Goal: turn the Credentials tab from a write-only .env copy tool into the
commercial core of the platform — a **credential vault** (keys as pipes),
a **metering ledger** (usage per client per key, priced twice), a
**tanks-and-pipes visual** of token flow, a client-facing **allowance
gauge with 80%/100% warnings**, and a **managed mode** that moves
infrastructure config out of the clinics' hands and into a console-side
**config manager**. This doc is the outcome of the 2026-07-20 brainstorm
session; every decision reached there is logged in Part 2 so a fresh
session can build without re-deriving the reasoning.

Sources: `backend/vault.py`, `backend/core.py` (`check_usage`,
`compute_cost_estimate`, `_admin_get_json`), `backend/routes.py`,
`clients.json` fields, and on the product side
`dental-clinic-agent/backend/admin.py` (`get_metrics`),
`backend/providers/llm.py` (`get_llm_info`, the key_index fallback),
`backend/admin.py`'s `SiteConfigPatch`.

Status: **DESIGN ONLY — nothing built yet.** Read Part 5 (rollout
safety) before touching any live instance.

---

## Part 1 — Analysis: what exists today

### 1.1 The vault is a copy tool that forgets

`backend/vault.py` stores "sets" keyed by **provider** (`mistral`,
`smtp`, `twilio`, `file/google_tts`), lists them redacted, and
`apply_sets()` merges them into a client's remote `.env` + recreates the
container — then records nothing. There is no assignment history (which
client runs on which key), no way to view a stored value, no link to
usage, no notion of cost. That is why the tab "brings no value": it is a
one-way copy mechanism, not a system of record.

### 1.2 Metering already exists — and the join key is already there

The critical discovery of the brainstorm: the product already meters
LLM usage **per key**. Every `Conversation` row records `model`,
`endpoint` (provider), token counts, and `api_key_alias` — which
`providers/llm.py:get_llm_info()` computes as
`"key_" + sha256(api_key)[:6]` (or `"local"` for Ollama). The product's
`GET /admin/metrics` aggregates these into `overall`, `by_key`, and
`by_model`.

Since the vault holds the **raw** keys, the console can compute the same
hash and join: *"client X consumed N tokens through vault credential Y
on model Z"* — **with zero product-side changes**. Today
`core.py:check_usage()` fetches `/admin/metrics` but keeps only
`overall`, throwing `by_key`/`by_model` away. The pipeline from source
tank → client → consumption already exists in the data; the console just
never persists or draws it.

### 1.3 Embryos already in the codebase

- `clients.json` per client: `monthly_token_quota` (the future pushed
  allowance denominator) and `cost_per_1k_input/cached/output_tokens`
  (the future sell-side tariff).
- `core.py:compute_cost_estimate()` + the `over_quota` flag in
  `check_client()` — the seed of the economic layer, currently scattered.
- `providers/llm.py:chat()` already falls back from `key_index=1` to
  `key_index=2` on a 429 — the overflow pipe, in miniature, product-side.
- `_admin_get_json()` reaches every instance's admin API two ways: plain
  HTTPS with `X-Admin-Token`, or curl-over-SSH-loopback for
  `ADMIN_TUNNEL_ONLY` instances. Both transports the config manager will
  need already exist.
- `env_tool` already does backed-up SSH read-modify-write of remote
  `.env` files — the break-glass write path.

### 1.4 The gaps

1. No assignment record — the console cannot say which key a client is on.
2. No time series — `/admin/metrics` is month-to-date aggregate; burn
   rate, projections, and the flow animation need periodic snapshots.
3. Metering covers **LLM tokens only**. Voice STT/TTS minutes, SMS, and
   emails are not counted anywhere yet.
4. The clinic's Configuración page exposes **every** setting — LLM
   provider/model, voice providers, SMTP, security parameters,
   `demo_mode`. A clinic admin can break the bot, trip the EU prod boot
   guard, burn our key on an untested model, or silently disable the
   registration gate. And if they can swap keys underneath us, the
   alias-hash usage attribution breaks — the config problem and the
   token problem are the same problem.
5. `chat.my-ai-receptionist.com` is simultaneously the website's public
   demo (Clínica Sonrisa) and the main development instance — there is
   no safe place to build any of this.

---

## Part 2 — The model (decisions settled in the brainstorm)

The core conceptual move: keep three layers separate, the way every
utility does.

1. **Physical layer — pipes.** Credentials. Who authenticates to which
   provider. The provider doesn't know which of our clients drank
   through a key.
2. **Measurement layer — meters.** Usage records: client × credential ×
   model × units × time. Largely exists (LLM).
3. **Economic layer — tanks and tariffs.** Quotas, refills, prices,
   overage. Pure accounting we define. A client's tank is a ledger
   entry, not a physical reservoir — with one exception: providers that
   sell real prepaid credits (OpenRouter, NVIDIA NIM) have tanks with a
   true level that can be reconciled against the provider's balance.

### Decision log

- **D1 — Units & currency.** The ledger stores **native units** (tokens,
  TTS minutes/characters, STT minutes, SMS sends, emails). The internal
  accounting currency is **eurocents**: every metered unit is priced
  twice — a buy-rate (what the provider charges us, per credential) and
  a sell-rate (what the client's plan charges them). No invented
  "credits" currency; the conversion table is simply a price list.
  Because the ledger is native-units underneath, either side can be
  re-priced at any time without touching history.
- **D2 — Business model.** Profit = **breakage** (paid-for-but-unused
  allowance) + **overage** (billed use past the allowance) +
  margin-per-unit. Key insight: pay-as-you-go provider keys (Mistral,
  NIM PAYG) are open pipes, not packages — we pay only for actual flow,
  so unused client allowance is pure margin automatically, with zero
  pooling risk. A real "common pool" with exhaustion risk only exists
  for genuinely prepaid sources or future committed-volume deals; for
  those, the **oversubscription ratio** (sum of sold allowances vs.
  provisioned capacity) is a first-class dashboard number.
- **D3 — Overflow behavior.** Default is **accounting overage**: service
  never interrupts; usage past the allowance is re-rated at the overage
  price on the statement (telco model — brownout, never blackout).
  Physical key swaps are reserved for a real prepaid source running dry
  or a BYOK key failing. Hard cutoff exists only as a per-client policy
  flag (trials, non-payers).
- **D4 — Client-facing framing.** The clinic buys an **entitlement**
  ("your plan includes up to X per month"), never a stored money
  balance — this maximizes breakage, avoids stored-value
  accounting/consumer-protection baggage, and is plain SaaS billing.
  The only number a clinic ever sees is **percent of allowance
  remaining** (never euros — euros invite dividing by Mistral's public
  price list). Allowance resets on the client's billing anchor day; no
  rollover by default (rollover is a retention perk to grant
  deliberately, later). Warnings at **80% and 100%**, each fired **once
  per billing cycle**, from the instance itself (it watches usage live
  and has SMTP), each logged to `AuditLog` so overage invoices are
  indisputable. The console watches the same thresholds independently
  for our own alerting.
- **D5 — BYOK is a plan type, not an architecture fork.** A credential
  gains `owner: ours | client`. Client-owned key: no cost accrues to
  us, no allowance tank, no overage — flat subscription only. We trade
  breakage profit for zero billing administration and zero
  provider-cost risk. The meter still meters (support + fair-use), the
  vault still records the assignment, the ledger prices the flow at
  €0/€0. Caveats: their key still physically sits in the instance's
  `.env` on our VPS (we remain its custodian), and when *their* key
  dies the bot goes down on our watch — BYOK clients need their own
  monitor alert type ("client-owned key failing") and arguably a higher
  base fee.
- **D6 — Architecture: direct + polling.** Instances keep talking
  straight to providers with their applied key; the console meters by
  polling `/admin/metrics`. No central gateway (LiteLLM-style proxy)
  for now — it would be a single point of failure and route all client
  traffic through one box. Revisit only at real scale; the ledger
  design below doesn't preclude it.
- **D7 — Audience.** The tanks-and-pipes visual, prices, margins, and
  breakage numbers are **internal-only** (ops-console). The clinic's
  own admin panel gets exactly one thing: the allowance gauge (%) plus
  the warning banners/emails.
- **D8 — Managed mode.** Infrastructure config moves out of the
  clinic's panel and into the console, behind a `managed: true` flag
  (details in Part 4, Phase 6/7). Unmanaged installs keep the full
  panel — the product stays sellable as a standalone template.

### Prior art the design borrows from

Telco data bundles (entitlement + overage + threshold notifications —
the EU mandates 80/100% warnings for mobile data precisely to prevent
bill shock; we adopt it voluntarily because it makes overage invoices
stick), prepaid electricity meters (hard cutoff as a policy, not a
default), LLM gateways (LiteLLM virtual keys/budgets, OpenRouter
prepaid credits — studied, not adopted as architecture), and metered
billing infra (Stripe metered billing / OpenMeter / Lago — deferred
until statements become real invoices).

---

## Part 3 — Architecture: four concerns, two codebases

```
PRODUCT INSTANCE (each clinic)             OPS-CONSOLE (operator only)
┌────────────────────────────┐            ┌───────────────────────────────┐
│ 1. METER                   │── poll ──▶ │ 3. LEDGER   backend/ledger.py │
│ usage per key/model        │            │    ledger.sqlite (own store)  │
│ (Conversation rows — DONE  │            │  snapshots × assignments ×    │
│  for LLM; voice/SMS later) │            │  price tables → balances,     │
│                            │            │  breakage, margin, thresholds │
│ 5. PLAN GAUGE + WARNINGS   │◀─ plan ──┐ └──────┬───────────▲────────────┘
│ % left · 80/100% emails    │   push   │        │           │
│                            │          │  4. VISUAL     2. VAULT
│ 6. MANAGED MODE            │◀─ config │  new tab,      keys · roles ·
│ infra fields hidden +      │   writes │  own JS file   assignments
│ API-rejected               │          │                (secret)
└────────────────────────────┘          └── 7. CONFIG MANAGER
                                            API-first read/write,
                                            SSH fallback, drift detection
```

Boundary rules that prevent the "overloaded console" failure mode:

- **The meter is dumb.** The instance counts; it never knows prices,
  pools, or key ownership. (Clinic staff can see everything on their
  own box — so nothing commercial may live there.)
- **The ledger knows no machines.** `backend/ledger.py` has **zero**
  knowledge of SSH, Docker, or deployments. It consumes three inputs
  (usage snapshots, vault assignments, price tables) and emits numbers.
  Its own SQLite store, its own `/api/ledger/*` routes. If the platform
  ever outgrows one console, the ledger lifts out into a service
  cleanly because its storage and API were separate from day one.
- **The visual is a reader.** A new console tab with its **own JS
  file** (this is where the `app.js` split starts — it is already
  ~100KB; the new tab must not be appended to it), rendering purely
  from ledger endpoints. Small node count (a handful of credentials ×
  clients) → plain SVG + `requestAnimationFrame`, no chart library.
- **`clients.json` stops growing.** Plans/tariffs live in the ledger's
  store; the existing `monthly_token_quota` / `cost_per_1k_*` fields
  migrate there and are then retired from `clients.json`.

---

## Part 4 — Phased plan

Phases are ordered so that each ships something usable on its own and
none touches the frozen instances (Part 5) until cutover.

### Phase 0 — a safe place to build: the `acme` dev instance

**Status 2026-07-20: largely DONE.** `acme` exists on the VPS
(`~/acme`, loopback port 8003, `acme.my-ai-receptionist.com`),
onboarded via the New Client wizard; its Caddy block is committed in
the dental repo's `deploy/Caddyfile` and documented in
`deploy/acme/RUNBOOK.md`. **All development and integration testing
happens against acme.** The website's demo keeps pointing at
`chat.my-ai-receptionist.com` untouched. Product-side code work
continues locally as always (`chatbot.bat`); acme is for testing
console↔instance integration on a real VPS deployment without risking
the demo. Remaining Phase-0 items: verify acme's registration in the
console's live `clients.json` (config volume) + fetch its admin token;
add the `frozen` markers for chat.* and primeconnectai.* once the flag
exists (Phase 1); clean any leftover `~/acme-dental` checkouts from
the failed 07-19 wizard test runs.

### Phase 1 — vault restructure (console-side only, read-only toward instances*)

- Sets gain a **role**: `llm | stt | tts | email | sms` (function-first;
  provider becomes an attribute of the set, not its identity). The
  current provider-keyed kinds map onto roles (`mistral`→llm,
  `smtp`→email, `twilio`→sms, `google_tts`→tts) and new provider kinds
  slot in per role.
- Sets gain `owner: ours | client` (D5) and optional buy-side metadata:
  purchase price per unit, and for prepaid sources a real
  balance/refill amount.
- **Assignment records**: `apply_sets()` writes an assignment
  (client × role × set id × applied-at) instead of forgetting.
  Reconciliation job: read each client's remote `.env` (existing
  `env_tool`), hash the key, match against the vault → detects drift
  and backfills assignments for the current fleet without guessing.
- **Key rotation, first-class.** The assignment records make rotation a
  one-click operation instead of a hunt: replacing a set's value offers
  "re-apply to the N clients using this set" (today's UI *says* rotation
  works this way but has no record of who uses what). Beyond
  single-key replacement, sets of the same role+provider can form a
  **rotation pool** the console spreads clients across or cycles
  through — the dental repo's red-team harness already does multi-key
  rotation with rate-limit throttling (`redteam` `_bot_chat`/
  `ratelimit.py`), so the pattern exists in-house; product-side, the
  existing `key_index` 1/2 slots are the natural landing points for a
  primary + rotation-partner pair.
- **Source-agnostic by design.** The vault does not care where a key
  comes from — free tier, paid, or no provider at all. Every source is
  just: a credential (or a local endpoint), a buy price (€0 is a
  perfectly valid price), and optional caps (requests/day,
  tokens/month, reset cadence) that give its tank a real capacity so
  the visual shows it running low and the monitor can warn or rotate
  before it hard-fails. A **local/VPS-hosted LLM** (Ollama) slots in
  identically — a source with €0 buy price and whatever capacity the
  hardware allows; the metering side already handles it
  (`get_llm_info()` reports alias `"local"` for keyless providers).
  Free and paid sources mix freely in the same pool/fallback chains;
  which source fuels which client (demo bots and free trials on free
  tiers, for instance) is **operator policy set case by case**, never
  hardcoded. The console's only opinion is a warning when a
  configuration looks likely to provoke provider push-back (e.g.
  several free-tier keys from one provider in a rotation pool) — it
  warns, it never blocks.
- Vault UI rework: per-role columns; per-client "current provider" view
  with a swap dropdown (pick another credential of the same role →
  apply); a reveal button on stored values (it is our own trust
  boundary — same file class as `clients.json` admin tokens).
- *The reconciliation read and any `apply` to a frozen instance is
  operator-triggered only; nothing automatic touches frozen clients.

### Phase 2 — metering ledger

- New `backend/ledger.py` + `ledger.sqlite`.
- The existing poll loop (60s) additionally fetches `/admin/metrics`
  `by_key`/`by_model` per client and appends **snapshots** (deltas) to
  the ledger → time series for burn rate, projections, animation speed.
  Month-to-date totals remain reconcilable against `/admin/metrics`
  directly (the instance stays the source of truth; the ledger is the
  history).
- Join usage → credential via the alias hash (`sha256(key)[:6]`,
  matching `get_llm_info()`).
- Plans/tariffs store: per client — allowance (in €), billing anchor
  day, sell-rates per unit, overage rates, policy flags (overage /
  alert-only / hard-cutoff; BYOK). Plan types include **trial**
  (time-limited, a week or a month, with an expiry date and an
  expiry alert; can be pointed at any source — a free-tier key or a
  local model makes a trial cost nothing) and **demo** (our own
  instances — never billed). Migrate `monthly_token_quota` and
  `cost_per_1k_*` out of `clients.json`.

### Phase 3 — €-accounting & reporting

- Price every snapshot twice (buy via credential, sell via plan).
- Ledger outputs: per-client balance (% of allowance left), breakage €,
  overage €, gross margin per client, per-credential spend, and the
  oversubscription ratio for prepaid sources.
- Monthly statement export per client (the artifact behind the invoice:
  included allowance, usage, overage lines, "typical interaction cost"
  — computable today from chats count ÷ total tokens).
- Console-side threshold alerts (80/100% and prepaid-source-low).

### Phase 4 — the flow visual

New console tab (own JS file): source tanks left (fill = real prepaid
balance, or budget-remaining for PAYG pipes shown with ∞), client tanks
right (fill = allowance remaining this cycle), pipes with animated
particles whose speed tracks recent burn rate, colored per role. Client
tank past empty → overflow pipe glows amber (overage flowing). Click
any tank → drill-down: by-model breakdown, daily burn sparkline,
projected empty date, statement-to-date.

### Phase 5 — product-side allowance gauge + warnings

- New `plan` block in `site_config.yaml` (pushed by the console, D4):
  included allowance, billing anchor day, overage rate. **The console
  owns the plan; the instance owns the display** — it computes % used
  from its own `Conversation` rows, live, no dependency on the console
  being up.
- Dashboard tab: sixth KPI card — tank/gauge, "Included usage — 62%
  remaining · resets on the 1st". Amber card + banner at 80%; red
  persistent banner at 100% ("further use this month is billed at €X
  per …").
- Threshold emails to the clinic owner at 80/100%, once per cycle
  (dedupe flag resetting on the anchor day), each `AuditLog`-logged.
- First version is LLM-tokens-only end to end (counters exist). The
  gauge only ever shows %, so voice/SMS join later invisibly.
- Follows the established config patterns: `SiteConfigPatch`
  optional-field + `is not None` guard; schema-affecting changes need
  `python -m backend.db.seed --reset` noted to the user.

### Phase 6 — managed mode (product-side)

- `managed: true` in `site_config.yaml` (default **false** — unmanaged
  installs keep today's full panel; the product remains a standalone
  sellable template).
- Field partition by one question — *business decision or
  infrastructure decision?*
  - **Clinic keeps**: hours, services & prices, consultants, staff
    accounts, welcome copy, greetings, language, brand color,
    `disclose_prices`, booking rules (horizon, lead time).
  - **Console takes**: LLM provider/model/temperature, all voice
    STT/TTS/LLM provider+model+voice settings, SMTP, security
    parameters, `demo_mode`, embed domains, backup settings.
  - **Judgment calls to settle at build time**: `voice.enabled` (a
    business wish with cost/guard consequences — proposal: console owns
    the switch, clinic gets a request path), `scheduling_granularity`.
    Walk all six cfg-tabs field-by-field against the rule.
- Enforcement is server-side, not just UI: infra-group fields in
  `SiteConfigPatch` are **rejected** unless the request carries the
  operator credential — a new `OPERATOR_TOKEN` env var known only to
  the console (same header pattern as `X-Admin-Token`). Tab-hiding in
  the clinic panel reuses the existing role-gating mechanism
  (`updateAdminUIForRole` precedent).
- Escape hatch by construction: the config file on disk via SSH —
  managed mode can never lock the operator out.

### Phase 7 — console config manager

- New console module + per-client Config page mirroring the product's
  six cfg-tabs, each field tagged *business* (clinic can also edit) or
  *managed* (console-only). Both panels are windows onto the same live
  config — no second copy.
- **API-first**: read via `GET /admin/config`; write via
  `PUT /admin/config` with `OPERATOR_TOKEN`. This gets validation (EU
  voice guard, brand_color, field checks), hot reload, and the
  instance's own audit trail for free.
- **SSH as fallback tier**: file read for down instances and for
  periodic **drift detection** (diff live file vs. last-written state
  vs. shipped defaults — this finally closes the tracked "stale
  config" gap, dental repo `deploy/DEPLOYMENT.md §10`, the
  purple-chatbot incident). File **write** only as break-glass (app
  won't boot) or for fields the API never exposes (the `managed` flag
  itself, `.env` values) — and every raw write runs
  `deploy/validate_site_config.py` first (sexagesimal-hours and
  EU-provider landmines).
- Two-writers safety: field-level partial updates via the existing
  all-optional `SiteConfigPatch` mean console and clinic writes can't
  clobber each other; after Phase 6 they no longer share any fields at
  all.

### Phase 8 — meter the remaining roles (product-side)

Voice minutes (session durations already exist in voice sessions),
TTS characters/minutes, SMS sends and emails (countable from the audit
ledger, which already records them). Each lands in the same snapshot →
ledger → gauge chain; the clinic-visible % silently starts covering
them (D1: native units under one € allowance).

### Deferred / explicitly not now

Central LLM gateway (D6), client-facing tank visual beyond the % gauge,
rollover-as-perk, real invoicing integration (Stripe/Lago/OpenMeter),
committed-volume provider deals + oversubscription management, a
client-facing "request a change" flow for managed fields.

---

## Part 5 — Rollout safety (read before touching anything live)

Constraints set by the owner on 2026-07-20:

- **`chat.my-ai-receptionist.com` (Clínica Sonrisa) is the website's
  public demo** — it must stay online and unchanged throughout the
  overhaul.
- **`primeconnectai.my-ai-receptionist.com` (PrimeConnect AI's own
  assistant) — same: do not touch** until everything is ready to
  convert.
- The development role moves **off** the demo instance onto the new
  `acme` instance (Phase 0). The website keeps pointing where it
  points today; no website or DNS change is needed for development to
  proceed — which is exactly why this ordering is safe.

Freeze mechanics:

- Add a per-client `frozen: true` marker in the console (clients.json
  or the plan store). Frozen clients: polling/metering/read-only
  reconciliation **allowed** (harmless GETs), but `apply_sets`, plan
  push, config writes, and managed-mode enablement are **blocked in
  code**, not just by operator discipline.
- Everything product-side ships **default-off**: `managed` defaults
  false; no `plan` block ⇒ no gauge, no warnings; instances not
  upgraded behave exactly as today. Console features degrade
  gracefully against un-upgraded instances (LLM metering already ships
  in them).
- Cutover per frozen instance, later, is then a small checklist:
  upgrade the checkout, push the plan block, flip `managed`, verify
  gauge + a config round-trip via the console, unfreeze. Revert =
  don't flip the flags; nothing else changed underneath.

Also inherited from the dental repo's working rules, applying to all
phases here: file tools over bash for repo edits, never run `pytest`
in-sandbox (hand off to the user's 3.12 machine), never drive the
running app in a browser, `docker compose up -d app` (recreate) for
any `.env` change, schema changes need a reseed.

---

## Part 6 — Open decisions (not settled; decide at build time)

1. Concrete sell-rates and plan tiers (the price list itself), and the
   overage rate relative to in-bundle rate (premium or same).
2. `voice.enabled` and `scheduling_granularity` ownership (Part 4
   Phase 6 judgment calls).
3. acme instance naming/persona details (any placeholder business is
   fine — it's a guinea pig, seeded `--demo`).
4. Whether the clinic gauge also shows "typical interaction ≈ €x" or
   strictly % (statement-only vs. dashboard).
5. Statement format & delivery (PDF by email? downloadable from the
   console only?).
6. Whether BYOK is offered commercially at all, and its base-fee
   premium.
7. When Phase 8 lands, the exact native-unit list for voice (minutes
   vs. characters differs per TTS provider — mirror whatever the
   provider bills in, per D1).
8. Sourcing policy per client is the operator's case-by-case call —
   which sources (free-tier, paid, local) fuel which clients, and how
   far to stack free-tier keys per provider. The console's role is
   limited to surfacing caps and warning when a setup risks provider
   push-back (some providers' terms forbid free-tier stacking); it
   never blocks a configuration.
