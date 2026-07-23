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
- Tests and Backups are split (backups buried at the bottom of Tests)
- No quick way to see "what needs attention" across 500 clients

---

## Proposed structure: 6 focused tabs

```
Fleet  ·  Lifecycle  ·  Credentials  ·  Billing  ·  Configuration  ·  Infrastructure
```

### 1. Fleet — the command center (replaces Dashboard + Updates + Tests)

**The primary view.** Everything an operator needs to scan 500 clients at a
glance and take immediate action.

#### Fleet summary bar (top)
- Total clients / Up / Down / Warning
- Pending updates (count + quick-deploy button)
- Over quota (count)
- Tests failing (count)
- Total est. cost this month / total minutes saved

#### Quick filters (always visible)
`All` | `Down` | `Needs updates` | `Over quota` | `Tests failing` | `Frozen` | `Unreachable`

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
  with the batch-deploy toolbar)

#### Search + sort
- Global search (name, hostname, display name)
- Sort by any column
- Keyboard shortcut `?` for help

#### Client detail — dedicated page (not a modal)
Clicking a client name navigates to `/client/<name>` — a full page with:
- KPI strip (health, version, uptime, tokens, cost, saved)
- Alert banners (down, over-quota, infra-risk)
- Tabs for sub-sections: **Overview** | **Resources** | **Version & Deploy** | **Usage** | **Interactions** | **Uptime** | **Reseed**
- URL is bookmarkable; browser back button works
- "Run tests…" button jumps to the Tests filter with this client pre-selected

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
- **Deploy updates** — the batch deploy table (from Updates tab)
  - Shows every client behind origin/master
  - Select + "Update selected" with typed confirmation
  - Live streaming console
  - Per-row deploy button for one-offs
- **Deploy history** — audit log of all deploy/reseed actions
  - Filter by client, date range, success/failure
  - Shows commit, stage, error output

### 3. Credentials — vault & providers (replaces Credentials + Catalog)

All credential and provider management in one place.

#### Sub-navigation
- **Vault** — credential sets (the current vault table)
  - Role-grouped: LLM | STT | TTS | E-mail | SMS
  - Reveal / edit / delete / test per set
  - Assignment matrix: which clients use which set
  - Rotation: edit a set → "re-apply to N clients" prompt
  - Import from existing client (one-time migration)
- **Providers** — provider keys and model/voice allow-lists (from Catalog tab)
  - Provider key management (paste, test, replace)
  - Per-role model allow-list (approved models)
  - TTS voice allow-list (approved voices, EU filtering)
  - Live browse for Mistral voices
- **Assignments** — the client × role matrix
  - Per-client dropdown to swap credentials
  - Apply (writes .env + recreates) or Record-only (for frozen clients)
  - Reconcile from servers (reads every .env, matches against vault)

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
  - Which clients drew through which key
  - Buy rates (€/1k), monthly caps
  - Free-tier vs paid vs local sources
- **Flow** — the tanks-and-pipes visualization
  - Source tanks (left) → client tanks (right)
  - Animated pipes (width = share, speed = burn)
  - Click to drill down

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

### 6. Infrastructure — VPS & host monitoring (replaces Host/VPS)

VPS-level monitoring and site discovery.

- Host gauges (disk, memory, load)
- Caddyfile sites (all sites, matched vs unmanaged)
- Disk breakdown (what's using space)
- Docker's own accounting (images/containers/volumes/build cache)

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

### The "New Client" vs "Add client" confusion is resolved
- "Provision" (wizard) and "Register existing" (add client) are both
  under the Lifecycle tab, clearly labeled
- "Provision" = creates a new instance from scratch
- "Register existing" = adds an already-running instance to monitoring

### Settings moves to a slide-out panel
- Poll interval and other settings accessible from a gear icon in the
  header, opens a slide-out panel (not a modal)

---

## Migration path

### Phase 1: Fleet table as the centerpiece
- Make the fleet table the default Dashboard view
- Add inline deploy/test buttons to table rows
- Add quick filters
- Move the Updates table and Tests table into filter views within this page

### Phase 2: Client detail as a dedicated page
- Convert the detail modal to a `/client/<name>` route
- Add sub-navigation tabs within the detail page
- Keep the modal as a fallback during transition

### Phase 3: Consolidate navigation
- Merge Onboarding + New Client into "Lifecycle"
- Merge Credentials + Catalog into "Credentials"
- Merge Tokens + Flow into "Billing"
- Merge Config + Pipeline into "Configuration"
- Rename "Host/VPS" to "Infrastructure"

### Phase 4: Polish
- Add column customization to the fleet table
- Add global search
- Add keyboard shortcuts
- Ensure all pages are bookmarkable with proper URLs
