# Restructuring Plan — ops-console for 500 chatbots

## Current state: 11 tabs + scattered modals

The console has grown organically into 11 top-level tabs with features
buried in modals:

```
Dashboard  Updates  Tests  Onboarding  Credentials  Tokens  Flow  Config  Catalog  Pipeline  Host/VPS
```

Plus modals: client detail, add/edit client, new-client wizard, credentials
copy tool, settings.

**Problems at 500 clients:**
- 11 tabs is overwhelming — operators lose context
- Client detail is a modal — can't bookmark, can't have its own URL
- Deploy is hidden in the detail modal; batch deploy is a separate tab
- "New Client" (provisions) vs "Add client" (registers existing) — near-identical labels
- Credentials vault and legacy copy tool are on the same tab
- Model allow-list (Catalog) and per-client model wiring (Pipeline) are separate tabs
- **Provider API keys are entered in TWO tabs** — Credentials (the vault form)
  and Catalog (its own per-provider "paste key" box) both write the same
  `/api/vault/sets`, and the env-key↔provider mapping is hardcoded in three
  places (see Phase 0 below)
- Tests and Backups are split (backups buried at the bottom of Tests)
- No quick way to see "what needs attention" across 500 clients

---

## Proposed structure: 6 focused tabs

```
Fleet  ·  Lifecycle  ·  Credentials  ·  Billing  ·  Configuration  ·  Infrastructure
```

A cross-cutting principle for the whole redesign: **each feature has exactly
one home.** Where the fleet view and a tab seem to both "own" an action
(deploy, tests, credential assignment), the rule is *Fleet is where you
trigger; the owning tab is where you review/configure.* This is what keeps
consolidation from just re-scattering the same buttons.

---

## Phase 0 — deduplicate provider-key handling (do this FIRST)

Before any navigation is moved, resolve the Credentials/Catalog overlap. It's
low-risk (the backend is already unified), it removes a real
add-a-provider-in-4-places footgun, and it de-risks the Credentials + Catalog
merge that Phase 3 depends on.

**What's actually duplicated (audited in code):**

- **The backend is clean — keep it.** Provider keys live in exactly one store:
  `vault.py` (`vault.json`, single writer of `/api/vault/sets`). `model_catalog`/
  `model_registry` hold models with *no keys*; `voice_catalog`/`voice_registry`
  hold voices with *no keys*. There is no backend key duplication to fix.
- **The frontend duplicates the key-entry UI.** `credentials.js` has the full
  vault add/edit form (create/edit/reveal/test/delete + assignment matrix +
  reconcile + import). `catalog.js` *reimplements* a stripped-down "paste LLM/
  voice provider key → Save → Test" box (`CAT_PROVIDERS`, `catRenderProviders`,
  `catSaveKey`, `catTestKey`) that POSTs the **same** `/api/vault/sets` and
  `/api/vault/sets/{id}/test`. Two surfaces, one endpoint.
- **The env-key↔provider mapping is hardcoded three times:** `vault.KIND_META`
  (backend, the real source of truth), `credentials.js:VAULT_FIELDS`, and
  `catalog.js:CAT_PROVIDERS`. Adding one provider today means editing all three
  (plus `CAT_ROLES.browse`). Miss one and the two tabs silently disagree about
  what a provider's key is called.

**The cleanup:**

1. Expose the mapping once. Add a tiny read endpoint (e.g. `GET /api/vault/kinds`)
   that returns `KIND_META` (kind → roles, provider, env keys, label). The
   frontend stops hardcoding `VAULT_FIELDS` and `CAT_PROVIDERS` and renders key
   fields from that payload. One place to add a provider from then on.
2. Pick a single key-entry surface. The Catalog's per-provider box is the nicer
   UX (one row per provider, "key on file / no key yet", inline Test); keep that
   pattern and drop the generic kind-picker form's *duplicate* role for LLM/voice
   keys — or vice versa, but not both. SMTP/Twilio/file credentials keep the
   fuller form (multi-field), so the vault form still exists for those.
3. Merge the tabs (this is the Phase 3 "Credentials + Catalog → Credentials"
   step, brought forward conceptually): once key entry, model approval, and
   voice approval all sit under one **Providers** sub-tab, the duplicate box
   simply disappears rather than being maintained in parallel.

**Why first:** it's a self-contained refactor with an already-shared backend,
it deletes code rather than adds it, and it makes "a provider" a single
first-class object instead of a concept smeared across two tabs — which is the
foundation the Credentials tab below is built on.

---

### 1. Fleet — the command center (replaces Dashboard + Updates + Tests)

**The primary view.** Everything an operator needs to scan 500 clients at a
glance and take immediate action.

#### Attention queue (the true home)
Above the table, a prioritized, clearable list of *things that need an operator
today* — client down, deploy failed, over quota, tests failing, cert expiring,
credential/model drift detected, backup timer inactive. At 500 clients nobody
reads a table top to bottom; the system should surface the 8 things that matter
and let the operator work them down. The fleet table is for *browsing*; this
queue is for *working*. Each item deep-links to the relevant client/action.

#### Fleet summary bar (top)
- Total clients / Up / Down / Warning
- Pending updates (count + quick-deploy button)
- Over quota (count)
- Tests failing (count)
- Total est. cost this month / total minutes saved

#### Composite health score
Every client carries a single rolled-up health score (or R/A/G) derived from
uptime + tests + quota + drift + infra headroom. It makes "sort worst-first"
trivial, makes the table scannable at a glance, and gives the attention queue
its ranking. "Health" stops being a binary up/down dot and becomes the column
you sort the fleet by.

#### Quick filters + saved views
`All` | `Down` | `Needs updates` | `Over quota` | `Tests failing` | `Frozen` | `Unreachable`

Beyond the fixed set, operators can **save arbitrary filter + column + sort
combos** as named views ("EU voice clients", "trials near expiry", "still on
the old model"). Saved views are shareable by URL and can drive bulk actions.
This scales as operational patterns grow, where a hardcoded filter row doesn't.

#### Fleet table (the centerpiece)
Columns: **Health** | **Name** | **Version** | **Usage** | **Uptime** | **Cost** | **Saved** | **Last checked**

Key changes from today:
- **Inline actions** per row — no modal needed for common actions:
  - Deploy button (shows "Deploy N commits" or "Rebuild & restart")
  - Test button (runs smoke suite on just this client)
  - Quick status indicators with hover tooltips
- **Expandable rows** — click to expand inline for resource usage,
  interactions, uptime details (replaces the detail modal for quick scans)
- **Column customization** — operators can show/hide columns (voice
  sessions, burn rate, plan type, etc.)
- **Bulk actions toolbar** — when rows are selected: Deploy selected,
  Test selected, Freeze selected, Export CSV
- The Updates table and Tests table from today become **filter views**
  within this page (e.g., "Needs updates" filter shows the same table
  with the batch-deploy toolbar). "Deploy updates" is therefore a Fleet
  saved view, not a second home for deploy — Lifecycle keeps only the
  *history/audit* of deploys (see the one-home rule above).

#### Safer bulk operations
Typed confirmation is necessary but not sufficient at this scale:
- **Dry-run / preview** before committing — "40 will rebuild, 3 are frozen and
  will be skipped, 1 is unreachable" — so the operator sees the blast radius.
- **Staged / canary rollout** — deploy to N, watch health, then release the
  rest, with auto-halt if failures spike. A single bad fleet-wide deploy is the
  worst-case incident; the UI should make the cautious path the default.

#### Search + command palette
- Global search (name, hostname, display name)
- **Command palette (`Cmd/Ctrl-K`)** — type a client name to jump to its page;
  type "deploy", "over quota", "tests" to run an action or open a view. At 500
  clients this collapses the entire navigation for power users; it's the single
  highest-leverage speed feature, not a "polish" afterthought.
- Sort by any column; keyboard shortcut `?` for help

#### Client detail — dedicated page (not a modal)
Clicking a client name navigates to `/client/<name>` — a full page with:
- KPI strip (health, version, uptime, tokens, cost, saved)
- Alert banners (down, over-quota, infra-risk)
- Tabs for sub-sections: **Overview** | **Resources** | **Version & Deploy** | **Usage** | **Interactions** | **Uptime** | **Reseed**
- URL is bookmarkable; browser back button works
- "Run tests…" button jumps to the Tests filter with this client pre-selected
- Rule of thumb to avoid re-scatter: the global tabs show *fleet-wide
  aggregates + bulk*; the client page shows the *single-client deep dive* of
  the same data. Same feature, two zoom levels — never two separate
  implementations.

### 2. Lifecycle — provisioning & maintenance (replaces Onboarding + New Client wizard + Deploy history)

Everything about bringing clients up, keeping them current, and tearing them down.

#### Sub-navigation
- **Provision** — the new-client wizard, now a proper page (not a modal)
  - Form: deploy name, hostname, display name, same VPS as
  - Live streaming console below the form
  - "Register existing instance" button (the old "Add client" flow)
- **Active onboardings** — list of in-flight provisions with stepper
  - Each shows current step, status, progress
  - Click to open the stepper for that onboarding
  - Resume / teardown buttons
- **Deploy history** — audit log of all deploy/reseed actions
  - Filter by client, date range, success/failure
  - Shows commit, stage, error output
  - (Triggering a deploy lives in Fleet; this is the review/audit side only.)

### 3. Credentials — vault & providers (replaces Credentials + Catalog)

All credential and provider management in one place. Phase 0 collapses the
duplicate key-entry here first.

#### Sub-navigation
- **Vault** — credential sets (the current vault table)
  - Role-grouped: LLM | STT | TTS | E-mail | SMS
  - Reveal / edit / delete / test per set
  - Rotation: edit a set → "re-apply to N clients" prompt
  - Import from existing client (one-time migration)
- **Providers** — the single home for a provider (from the Catalog tab)
  - Provider key management (paste, test, replace) — **the only key-entry
    surface after Phase 0**, rendered from `GET /api/vault/kinds`
  - Per-role model allow-list (approved models)
  - TTS voice allow-list (approved voices, EU filtering)
  - Live browse for models and Mistral voices
- **Assignments** — the client × role matrix
  - Per-client dropdown to swap credentials
  - Apply (writes .env + recreates) or Record-only (for frozen clients)
  - Reconcile from servers (reads every .env, matches against vault)
  - *Note:* "which client draws through which key" also appears in Billing →
    Sources. Keep the **assignment** (operator intent) here and the **usage**
    (metered reality) there, and cross-link the two rather than duplicating.

### 4. Billing — usage, plans & cost (replaces Tokens + Flow)

Everything about metering, pricing, and token economics.

#### Sub-navigation
- **Overview** — fleet-wide economics
  - Total tokens, sold allowance, consumed, breakage, overage, margin
  - Alerts (80%/100% threshold crossings)
- **Clients** — per-client plans and balances
  - Allowance, used, left, burn rate, projected empty
  - Edit plan (type, fees, allowance, rates, frozen flag)
  - Push plan to instance (enables clinic-side gauge)
  - Statement export
- **Sources** — per-credential usage and buy rates
  - Which clients drew through which key (usage/metered side; the *assignment*
    side lives in Credentials → Assignments — cross-link, don't duplicate)
  - Buy rates (€/1k), monthly caps
  - Free-tier vs paid vs local sources
- **Flow** — the tanks-and-pipes visualization
  - Source tanks (left) → client tanks (right)
  - Animated pipes (width = share, speed = burn)
  - Click to drill down
  - *Reality check:* 500 client tanks won't render as a readable diagram. Treat
    Flow as an exec/"wow" view and aggregate the client side (by plan, or
    top-N burners) rather than one tank per client, or it becomes noise
    operators skip.

### 5. Configuration — per-client settings (replaces Config + Pipeline)

All per-client configuration management.

#### Sub-navigation
- **Config manager** — site_config.yaml editor
  - Per-client selector
  - Field groups (business vs managed tagged)
  - Changed-fields-only saves
  - Drift detection (live file vs console-written vs shipped defaults)
  - Managed mode toggle (enable/disable with name confirmation)
- **Pipeline** — per-client model/voice wiring
  - Per-clinic node graph: provider → model → voice → endpoint
  - Per-lane: LLM text | LLM voice | STT | TTS | E-mail | SMS
  - Test buttons per lane (LLM reply, TTS audio preview, STT record)
  - Apply changes (hot reload, no recreate)
  - *Governance link:* the Pipeline may only select providers/models/voices
    approved in Credentials → Providers — the approval lives there, the
    per-client pick lives here.

### 6. Infrastructure — VPS & host monitoring (replaces Host/VPS)

VPS-level monitoring and site discovery.

- Host gauges (disk, memory, load)
- Caddyfile sites (all sites, matched vs unmanaged)
- Disk breakdown (what's using space)
- Docker's own accounting (images/containers/volumes/build cache)
- Backup timer health per instance (surfaces the "silently dead for 17 days"
  failure mode as an attention-queue item, not something you find by luck)

---

## Cross-cutting concerns (apply to every tab)

### Global audit log
Every operator action across the fleet — credential rotations, plan edits,
freezes, config saves, deploys — in one filterable place, not buried under
Deploy history. When something goes wrong at 500 clients, "who changed what,
when" is the first question. Deploy history becomes a filtered view of this.

### Deep-linkable state
Everything is a URL: `/client/<name>`, filter + selection + active sub-tab all
encoded in the query string. An operator can paste "here are the 12 failing
clients" into chat and a colleague opens the identical view. Shared operational
state is a force multiplier and is nearly free once routing exists.

### On-call / mobile slice
A phone-friendly view for the 2am "X is down, restart it" moment — essentially
the attention queue plus per-client restart, not the full console.

### Performance at 500 rows
Server-side filtering/sorting/pagination and row virtualization from day one.
A "command center" with inline actions, expandable rows, and live status will
feel sluggish exactly when it matters unless the table isn't rendering 500 live
rows at once.

---

## Key interaction changes

### Inline actions instead of modals
The detail modal is replaced by:
- Expandable rows in the fleet table for quick scans
- A dedicated `/client/<name>` page for deep dives

### Batch operations are first-class
- Select checkboxes in the fleet table
- Bulk action toolbar appears: Deploy | Test | Freeze | Export
- No need to navigate to a separate "Updates" tab
- Bulk deploys are preview-first and can be staged/canaried (see Fleet)

### The "New Client" vs "Add client" confusion is resolved
- "Provision" (wizard) and "Register existing" (add client) are both
  under the Lifecycle tab, clearly labeled
- "Provision" = creates a new instance from scratch
- "Register existing" = adds an already-running instance to monitoring

### One key-entry surface (Phase 0)
- Provider keys are pasted/tested in exactly one place (Credentials →
  Providers), rendered from the backend `KIND_META` — never re-typed in a
  second tab, never re-hardcoded in a second file.

### Settings moves to a slide-out panel
- Poll interval and other settings accessible from a gear icon in the
  header, opens a slide-out panel (not a modal)

---

## Migration path

### Phase 0: Deduplicate provider-key handling
- Add `GET /api/vault/kinds` exposing `KIND_META`
- Render Credentials/Catalog key fields from it (drop `VAULT_FIELDS` and
  `CAT_PROVIDERS` hardcoding)
- Collapse to a single LLM/voice key-entry surface; keep the multi-field form
  only for SMTP/Twilio/file
- Self-contained, backend already shared — do this before touching navigation

### Phase 1: Fleet table as the centerpiece
- Make the fleet table the default Dashboard view
- Add the attention queue and composite health score
- Add inline deploy/test buttons to table rows
- Add quick filters (and the saved-views mechanism)
- Move the Updates table and Tests table into filter views within this page

### Phase 2: Client detail as a dedicated page
- Convert the detail modal to a `/client/<name>` route
- Add sub-navigation tabs within the detail page
- Keep the modal as a fallback during transition

### Phase 3: Consolidate navigation
- Merge Onboarding + New Client into "Lifecycle"
- Merge Credentials + Catalog into "Credentials" (Phase 0 already removed the
  duplication, so this is now mostly moving panels, not reconciling two UIs)
- Merge Tokens + Flow into "Billing"
- Merge Config + Pipeline into "Configuration"
- Rename "Host/VPS" to "Infrastructure"

### Phase 4: Polish
- Command palette (`Cmd/Ctrl-K`)
- Column customization on the fleet table
- Global audit log + deep-linkable state everywhere
- On-call/mobile slice
- Keyboard shortcuts; ensure all pages are bookmarkable with proper URLs
