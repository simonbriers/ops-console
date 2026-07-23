# ops-console — duplication & inconsistency audit (2026-07-23)

A full-console sweep for the disease we already hit once with provider keys:
a field/list/mapping/number that originated in one place, then a later tab or
module grew its own copy, so the two can silently disagree. Four parallel
passes covered (1) providers/models/voices, (2) per-client config fields,
(3) client status/health/deploy, (4) billing/token economics. The already-fixed
case (provider→env-key mapping, now behind `GET /api/vault/kinds`) is excluded.

Findings are grouped by *how they bite*, worst first. Items marked **[VERIFIED]**
were confirmed by reading the source during this audit; the rest are reported
with file:line to check when picking them up.

---

## Status (updated 2026-07-23 — all changes committed & tested)

**Resolved:**
- **1.1 EU list drift** — canonical `GET /api/eu-voice-providers` (mirrors the
  boot guard); `pipeline.js` builds `PL_EU` from it, `whisper` removed.
- **1.2 off-policy wizard default** — starter config now takes the catalog
  default (`mistral-small`), not a hardcoded `mistral-large-latest`.
- **1.3 leaking config field** — Config hides model fields by attribute, so
  `voice_tts_model` no longer leaks.
- **2.1 model prices** — `model_catalog` is the sole owner; ledger `model_rates`
  refresh from it (operator overrides preserved via `operator_set`); dead
  registry price copy removed and stripped on load.
- **3.1 provider lists** — canonical `backend/providers.py` + `GET /api/providers`;
  `catalog.js`/`pipeline.js`/`setup.js`/`config_manager` all derive from it.
- **3.2 slot→field→path** — `model_registry.SLOTS`/`_SLOT_YAML` derived from
  `config_manager.FIELD_GROUPS`; write field and read path can't drift.
- **3.4 (part)** — `model_registry.ROLES` reuses `model_catalog.ROLES`; dead
  `voice_catalog.PROVIDER_LABELS` deleted.
- **4.1 buy-cost basis** — resolved *by existing design*: per-source `buy_eur`
  is authoritative (drives margin), per-model is explicitly informational. No
  change needed; documented in `ledger._client_economics`.
- **4.2 assignment authority** — behavior was already correct (the ledger bills
  off the metered draw, not the assignment). Corrected the `model_registry`
  docstring that overclaimed its assignment as "the billing join."
- **4.3 alias parsing** — `vault.set_aliases` / `vault.alias_to_set_map` are the
  one place the id-key split lives; `list_sets` and `ledger._alias_map` call
  them instead of reaching into `vault._ID_KEY`.
- **5.1 status disagreement** — one uptime threshold (`history.uptime_band`)
  folded into overall status (`core.apply_uptime_to_status`); dashboard dot and
  detail modal now agree.
- **2.2 cost currency/source** — the estimate is EUR (`estimated_eur`) and reads
  the ledger plan's sell rates (single source), falling back to the legacy
  clients.json fields only when a client has no plan.
- **2.3 stale voice metadata** — `voice_registry.list_registry` resolves
  lang/gender/tier/label/`eu_resident` from `voice_catalog` at read time, so a
  catalog correction reaches already-approved voices; non-catalog voices keep
  their stored metadata.
- **3.3 second model source** — deleted the dead model-picker path in
  `config.js` (the `/api/model-catalog` fetch + helpers); those fields are
  hidden here and set on the Model Setup tab.
- **5.2 backup_timer severity** — `smoke._row` carries a `severity`
  ("critical"|"warn"); the five verdict/render spots read it instead of
  string-matching the check name.
- **Phase 0 (pre-audit)** — provider-key mapping unified behind `/api/vault/kinds`.

**Remaining (deliberately not yet done — next tranche):**
- **5.3 / 5.4** per-client formatters, deploy request/result, infra-warning, and
  the NDJSON reader duplicated across `app.js` — a larger frontend-helper
  refactor with regression risk; best done alongside the restructure plan's
  Fleet-table / `/client/<name>` work where these get one home anyway.
- **Cross-repo sync-by-comment** — `config_manager`/`validator` mirror the
  product repo's rules by comment; fix by having the instance echo them, or add
  contract tests.

---

## Tier 1 — Already inconsistent in the current code (live bugs, fix now)

### 1.1 EU-residency provider list has drifted — a whisper lane reads "EU-safe" but the boot guard blocks it **[VERIFIED]**
`frontend/pipeline.js:26` `PL_EU = {"mistral","gladia","piper","local","whisper"}`
vs `backend/validator.py:29` `EU_VOICE_PROVIDERS = {"gladia","local","mistral","piper"}`.
`whisper` is in the frontend set only. The Pipeline paints a whisper STT lane
green ("EU — safe"), while `validator.py` (which mirrors the product's `ENV=prod`
boot guard) would refuse to boot. Same class of drift also shows up as prose in
`backend/new_client.py:204`. This is the one duplicated datum with a GDPR/safety
consequence, so it leads the list.

Compounding it: `google` is non-EU in both provider sets, so `pipeline.js` paints
Google TTS lanes red, but `backend/voice_catalog.py` marks Google Neural2/Studio
voices `eu_resident: True` and `voice_registry` therefore auto-approves them as
"EU-safe." The same Google voice is simultaneously EU-approved (catalog/registry)
and non-EU-blocked (pipeline).

**Fix:** one server-owned EU fact. Serve the canonical provider set (and rely on
per-voice `eu_resident` where residency is genuinely per-voice, as with Google),
expose it via an endpoint, delete `PL_EU`, and reconcile the `whisper`/`google`
contradictions deliberately.

### 1.2 New-client wizard provisions the off-policy (expensive) LLM by default **[VERIFIED]**
`backend/new_client.py:224` writes `llm.model: mistral-large-latest`, but the
declared fleet policy — `backend/model_catalog.py` (`mistral-small-2506`,
`"default": True`) and `config_manager.py:107-109` ("small by default, large only
as a named exception") — is the opposite. Every new instance boots on the pricey
model until someone reassigns it. `mistral-large-latest` isn't even a catalog id
(the catalog has `mistral-large-2512`), so it's also an unpriced string.

**Fix:** generate the starter `llm.model` from the catalog's `default: true` LLM
entry instead of a literal.

### 1.3 `voice_tts_model` leaks onto the Config tab while its siblings are hidden **[VERIFIED]**
`frontend/config.js:21-25` `CFG_MODEL_FIELDS_HIDDEN` lists `voice_tts_provider`
but not `voice_tts_model`, so that single model field still renders on the Config
tab while every other provider/model field is hidden (they're meant to live on the
Model Setup/Pipeline tab). Traceable directly to a hand-maintained name list.

**Fix:** hide by attribute, not by name — `if (f.type === "model" || f.provider_field) return ""` — since `config_manager.FIELD_GROUPS` already tags these.

---

## Tier 2 — "Correct at first boot, wrong after the first edit" (seed-and-freeze copies)

### 2.1 Per-model BUY price exists in three places; only one is live **[VERIFIED: ledger never reads the registry copy]**
- Origin: `backend/model_catalog.py:39-155` (declares itself the source of truth).
- Copy A: `backend/ledger.py:109-117` seeds `model_rates` from the catalog with
  `INSERT OR IGNORE` — so a later catalog price change never reaches an
  already-seeded row; the DB copy is authoritative forever after first seed.
- Copy B: `backend/model_registry.py:75,86-100` copies all price fields onto each
  registry entry in `models.json` at first seed. `ledger.py` never imports
  `model_registry`, so this copy is dead weight that still feeds some UI price
  labels (`models.js`, `pipeline.js`) — a third number that can disagree with both
  the catalog and `model_rates`.

**Fix:** make `model_catalog` the only price store; compute €/1k on read. Drop the
price fields from registry entries (reference a catalog id instead). Replace the
ledger's seed-and-forget with a refresh-catalog-rows-but-keep-operator-overrides
upsert, or a catalog lookup at read time.

### 2.2 One persisted rate field is read as USD cost in one place and EUR sell-rate in another
`clients.json` `cost_per_1k_input/cached/output` (`routes.py:55-57`) is consumed by
`core.py:1371-1382` as a **USD** cost estimate, and the *same three numbers* seed
the ledger plan's `sell_*` as **EUR** sell rates (`ledger.py:149-156`). After the
plan seeds once, editing the client's cost field still moves the core USD estimate
but no longer moves billing. Currency conflation plus decoupling-after-seed.

**Fix:** once the ledger owns pricing, retire `cost_per_1k_*` from the core path
(the comments already call them "legacy/dormant") or have `compute_cost_estimate`
read `ledger.get_plan().sell_*`; pin one currency.

### 2.3 Approved-voice metadata is snapshotted into voices.json and goes stale
`backend/voice_registry.py:32,43-53,101-108` copies `eu_resident`/`label`/`tier`
from the catalog into `voices.json` at approval time. A later correction to a
voice's `eu_resident` in `voice_catalog.py` never reaches already-approved voices —
and that stored copy is exactly what feeds the Pipeline's EU filter. Same disease
as 2.1, on the safety-relevant flag from 1.1.

**Fix:** store only `{id, provider, source, added}`; resolve display/EU metadata
from `voice_catalog.VOICE_BY_ID` at read time (keep a fallback only for
live-browsed voices absent from the catalog).

---

## Tier 3 — The same enumeration hardcoded in N places (drift-prone)

### 3.1 "Which providers are valid for a role" — 4–5 independent lists
`frontend/catalog.js:21-28` (`CAT_ROLES`), `frontend/pipeline.js:28-49`
(`PL_LANES[].providers`), `backend/config_manager.py:104` (`llm_provider` options),
`frontend/setup.js:23-33` (`SU_PROVIDERS`/`SU_BROWSE_PROVIDERS`), plus
`catalog.js:35` (`CAT_VOICE_PROVIDERS`). Same five LLM providers written in three
different orders; TTS represented incompatibly (catalog splits browse/voiceOnly,
pipeline flattens). Adding/removing a provider means editing 4–5 places.

**Fix:** derive per-role provider lists from `/api/vault/kinds` (each kind carries
`roles`+`provider`), with a small voice-only flag; every tab builds from that.
Same shape as the key-mapping fix already shipped.

### 3.2 Slot → config-field → YAML-path mapping defined in four places
`backend/model_registry.py:57-61` (`SLOTS`) + `:65-69` (`_SLOT_YAML`),
`backend/config_manager.py:101-192` (`FIELD_GROUPS` with `path`/`role`/
`provider_field`), and `frontend/pipeline.js:28-49` (`PL_LANES` field-name strings).
A typo between `SLOTS` (write path) and `_SLOT_YAML` (read path) makes `assign_model`
write one place and `reconcile_models` read another, invisibly, until a reconcile
mislabels a live model "un-approved." Adding the anticipated `voice_tts` slot means
editing all four.

**Fix:** make `config_manager.FIELD_GROUPS` the sole source; derive `SLOTS`/
`_SLOT_YAML` from the fields' dotted `path` (`.split(".")` → the yaml tuple); have
`pipeline.js` read field names from `/api/config-catalog` / `/api/models/slots`.

### 3.3 Two backend sources for "which models are selectable"
`/api/model-catalog` (static `MODEL_CATALOG`) feeds the Config tab's model picker
(`config.js:43-63`); `/api/models/registry` (operator allow-list) feeds Catalog,
Pipeline, Models (`catalog.js`, `pipeline.js`, `models.js`). Approve a model in
Catalog and it never appears in Config; the static catalog lists models the
registry dropped. Partly masked today only because the Config model fields are
hidden (see 1.3), leaving dead dropdown-building code ready to mislead.

**Fix:** point the Config picker at `/api/models/registry` like every other tab;
reserve `/api/model-catalog` for pricing, or delete the hidden model-field path.

### 3.4 Smaller enumerations restated
- `ROLES` tuple defined in `vault.py:68`, `model_catalog.py:159`,
  `model_registry.py:71` (the last two identical; `model_registry` already imports
  `model_catalog` but redefines it, and validates `add_model` against its own copy).
- Slot order/labels: `frontend/models.js:20-21` and `frontend/setup.js:32-33`
  re-list what `model_registry.SLOTS` owns and `/api/models/slots` already serves.
- Dead provider labels: `backend/voice_catalog.py:20-26 PROVIDER_LABELS` is never
  referenced anywhere and gives different strings than `vault.KIND_META` labels.

---

## Tier 4 — Overlapping authority (structural, decide-then-dedup)

### 4.1 Two non-reconciling buy-cost bases for the same tokens
`ledger.py:402-413` bills per-source (alias→vault-set→`source_rates`); `:420-427`
computes per-model (`model_rates`). Both price the same month; the code comment
admits they can differ. Statements print both; margin uses only the per-source one.

### 4.2 Three overlapping "client ↔ source" records, two each claiming the billing join
`vault.py:388-414` `(client, role)→set_id` (intent), `model_registry.py:196-224`
`(client, slot)→model_id` (calls itself "the authoritative billing join"), and
`ledger.py:508-538` the actual metered draw (computed, never stored, and what the €
math actually uses). Vault/registry assignments can lie relative to what's metered.

**Fix (4.1+4.2):** designate the ledger's computed alias/model draw as
truth-for-billing; treat vault + registry assignments explicitly as intent/
governance and surface intent-vs-observed mismatches, rather than three modules
each asserting authority.

### 4.3 api-key alias parsing re-implemented reaching into vault internals
`vault.alias_for()` (`vault.py:154-165`) is the one correct hash, but the
"split the id-key on commas, alias each part" logic is rewritten in `vault.list_sets`
(`:222-227`), `ledger._alias_map` (`ledger.py:332-343`, which imports the *private*
`vault_mod._ID_KEY`), and `vault.reconcile_client`. A vault refactor of the
multi-key convention breaks billing with no import error.

**Fix:** one public `vault.aliases_of_set()` / `alias_to_set_map()` helper both
callers use; stop reaching into `_ID_KEY` from the ledger.

---

## Tier 5 — Frontend rendering duplication (per-client views)

### 5.1 "Down vs warning vs ok" split between backend and JS on different inputs
`core.py:1413-1420` derives `overall_status` from health/quota/version/usage and
**never** looks at uptime; `app.js:210` and `app.js:408` independently color uptime
at 99%/95% thresholds. A 92%-uptime client shows a red uptime tile in the detail
modal while the Dashboard status dot stays green. The 99/95 threshold itself is
duplicated between the two JS spots.

**Fix:** fold uptime into the backend status (or expose `status_reasons[]`); every
surface renders that, no client-side re-thresholding.

### 5.2 "backup_timer is the only warn-level check" hardcoded in five places
`routes.py:1020-1023`, `:1096`, `:1162-1166` and `app.js:2220-2221`, `:2316` each
special-case the check *name* because `smoke.py` rows carry no severity. Add a
second warn-level check or rename it, and backend and UI disagree on pass/fail.

**Fix:** give `smoke.py` rows a `severity` field; everything reads that.

### 5.3 Same per-client fields formatted by 3–5 parallel formatters
health (`app.js:185-190` / `:392-393` / `:2251-2253`), version/"N behind"
(`:194` / `:398-401` / `:479` / `:621-622` / `:1968-1982`), cost (shown `toFixed(2)`
at `:432` but `toFixed(4)` at `:513` — same number, same modal, two precisions),
quota % (`:202` / `:418` / `:523`). **Fix:** one `fmt.health/version/cost/quotaPct`
module used everywhere.

### 5.4 Deploy/reseed request + result handled twice with divergent contracts
Detail-modal deploy posts `{confirm_name}` (`app.js:698-702`); batch posts
`{confirm}` (`:2116-2119`); reseed uses yet another confirm field (`:807`). The
result shape (`stage`/`commit`/`error`/`output`) is rendered by two independent
functions (`:670-683`, `:2087-2096`). Also: infra-risk warning worded four
different ways (`:449-450`, `:632-635`, `:1971-1972`, `:2044-2048`), and the NDJSON
stream-reader loop copy-pasted six times (`:1371, 1656, 1814, 2128, 2349, 2442`).

**Fix:** shared request builder + one `deployResultView(res)`, one
`renderInfraWarning(version)`, one `readNdjson(resp)` generator.

---

## Cross-repo sync-by-comment (lower priority, but real)
`config_manager.py:49-53` (managed-vs-business field flags mirror the product's
`MANAGED_CONFIG_FIELDS`), `config_manager.py:392` (`_DRIFT_IGNORE_PATHS` "keep in
sync with config_merge.py"), `validator.py:8-11` (mirrors the product's validate/
boot rules). Each is a "single source of truth" enforced only by a comment; when the
product side changes, ops-console diverges silently (false drift noise, or a config
that passes here but crash-loops the instance). **Fix where feasible:** have the
instance echo these facts (it already knows them); otherwise add contract tests.

---

## Suggested sequencing
1. **Tier 1** now — three verified live inconsistencies, each a small, contained fix.
2. **One canonical providers endpoint** (extends the `/api/vault/kinds` work) — kills
   3.1, the EU set in 1.1, and most of `setup.js`/`catalog.js`/`pipeline.js` provider
   arrays at once.
3. **One price owner** (2.1) and **one field/slot/path catalog** (3.2/3.3) — collapses
   the seed-and-freeze copies and the config-field spread.
4. **Billing authority** (Tier 4) — a design decision, best made alongside the
   Billing-tab consolidation in the restructure plan.
5. **Frontend formatters/helpers** (Tier 5) — naturally folds into building the Fleet
   table and the `/client/<name>` page (restructure plan Phases 1–2), where these
   values get one home anyway.
