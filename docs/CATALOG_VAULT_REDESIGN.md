# Catalog / Vault entity model — design decision (2026-07-21)

**Status: PARKED design, nothing built.** This captures the decision reached
in the 2026-07-21 discussion so a later session can implement it without
re-reading the vault/ledger/catalog code. Read this alongside
`TOKEN_ECONOMY_PLAN.md` (Part 2 D-decisions, Part 6 item #9). When you build,
follow the phase order at the bottom. Companion source-of-truth pointers are in
the "Where everything lives" table so you can jump straight to the code.

---

## 1. What triggered this

While adding the model catalog + picker (2026-07-21, see `TOKEN_ECONOMY_PLAN.md`
day-3 status) the owner asked the modelling questions that this doc answers:

- There are *general* models everyone draws from through one shared
  "same-for-everyone" key (the fleet bulk), AND a client might need its own
  model that isn't in the shared catalog, AND a client might bring its own key
  (BYOK). Do all of these go in **one** catalog?
- Does the **catalog hold keys**? Are keys stored in the vault as a
  `model + client + key` combination when client-specific, or just `model + key`
  when bulk?
- Does the vault need a rewrite?

The short answers, argued below: **one catalog, scoped**; **no, the catalog
never holds keys**; keys stay `provider + secret` in the vault and the
`client × key` link is the existing *assignment*; and **the vault needs almost
no change** — the real gap is that the catalog isn't yet a first-class,
editable, scoped entity.

---

## 2. The core principle — three entities, joined, never nested

The confusion comes from treating "model", "key", and "who-uses-what" as one
record. They are three separate nouns with different lifecycles. Keep them
separate and join them; do **not** nest keys inside the catalog or duplicate
model definitions per client. This is the same pipes / meters / tanks split the
plan already made (Part 3) — just applied to models.

1. **Model** — a *definition*: `id`, `provider`, `role` (llm/stt/tts),
   native `unit` (tokens / audio-minute / character), buy price, and — the new
   part — a **scope** (global, or belonging to one client). A model's price is
   a property of the *model*, because the provider bills per model regardless
   of which key you authenticate with. → the **catalog**.

2. **Credential (key)** — a *secret*: a key, its `provider`, its `owner`
   (`ours` | `client` = BYOK), its `tier` (paid/free/local). A key
   authenticates to a **provider** and can run **any** model that provider
   offers (one Mistral key already serves LLM + Voxtral STT + TTS). A key
   therefore belongs to a provider, **never to a single model**. → the **vault**.

3. **Assignment** — the *join*: "client X, for role `llm`, authenticates via
   set Z." This already exists (`vault.json.assignments`). It is a
   credential binding; it does **not** carry a model (see §5 for why).

Which model a given instance actually *runs* is a fourth, separate thing that
already has a home: the instance's `site_config.yaml` (`llm.model`,
`voice.llm.model`, `voice.stt.model`), edited from the console **Config tab**.
That is the *selection*; the catalog is the *menu + prices*; the ledger prices
the *observed* model from metering. Keeping selection in config (not on the
assignment) means there is exactly one source of truth for "what model is this
instance on."

### Why keys must never live in the catalog

- **One key → many models.** Put keys in the catalog per-model and you
  duplicate the same secret across every model that key can run (Mistral small,
  large, Voxtral). Rotation — the thing the vault restructure exists to make
  one-click — becomes a hunt again.
- **One model → many clients/keys.** Put model definitions per-client and you
  duplicate the price/unit everywhere; a price change means editing N rows.
- Normalisation (separate entities + join) is exactly what the plan's
  vault/meter/ledger boundary already buys us. Merging re-tangles it.

### How others do this (grounding)

The pattern is near-universal: **model definitions + prices**, **consumer
credentials**, and **the consumer↔model relationship** are three separate
things. LiteLLM (an actual LLM gateway) splits a *model list*
(alias → provider-model + which credential + params), *virtual keys* (per
consumer, with budgets), and *provider credentials*; multiple credentials can
back one model. OpenRouter: a global model catalog with per-model prices, and
one credit balance behind the account key — prices on models, credit on the
key. Stripe: products/prices vs customers vs subscriptions — nobody puts a
customer's card in the product. Secrets managers (AWS/Vault): the secret is
stored once by its own id and *referenced*, so rotation is one edit. Telco (the
plan's own metaphor): SIM (credential) vs plan (tariff) vs subscriber. In every
one: prices attach to models, secrets attach to providers and carry ownership,
and "who uses what" is its own record.

---

## 3. Where everything lives (locator table)

`<config-dir>` = the console's persistent config dir (same volume as
`clients.json`); `DEFAULT_CONFIG_PATH` in `backend/config.py` resolves it.

| Concern | Today — file / store / symbol | After — what changes |
|---|---|---|
| Model definitions + prices (shipped defaults) | `backend/model_catalog.py` → `MODEL_CATALOG`, helpers `get_model` / `models_for` / `default_model` / `llm_buy_rates_per_1k` / `llm_models_per_1k` | **stays** as the factory-default *seed* only |
| Model definitions + prices (live, editable) | *does not exist* — only `model_rates` (€/1k, LLM only) in `ledger.sqlite` | **new `models` table** in `ledger.sqlite` (superset of `model_rates`), seeded from `model_catalog.py`; gains `scope` |
| Per-model €/1k buy rate | `ledger.sqlite.model_rates` (`set_model_rate` / `get_model_rates` / `_model_rate_map`, seeded in `_db()`); route `POST /api/ledger/model-rates` | folded into the `models` table; keep the route working (back-compat alias) |
| Per-source blended buy rate + cap | `ledger.sqlite.source_rates` (`set_source_rates` / `_source_rate_map`); route `POST /api/ledger/source-rates` | unchanged (fallback pricing + free-tier €0) |
| Credentials (keys) | `<config-dir>/vault.json` → `sets[]`; `backend/vault.py` (`KIND_META`, `upsert_set`, `list_sets`, `reveal_set`, `apply_sets`); routes `GET/POST /api/vault/sets`, `POST /api/vault/sets/{id}/reveal`, `POST /api/vault/import`, `POST /api/clients/{name}/apply-credentials` | **unchanged** (BYOK already = `owner:"client"`) |
| Assignment (client × role → key) | `vault.json → assignments[]` `{client, role, set_id, applied, source}`; `record_assignment` / `list_assignments` / `reconcile_client` / `import_from_client`; routes `GET/POST /api/vault/assignments`, `POST /api/vault/reconcile` | **unchanged** (recommended — see §5) |
| Which model an instance runs (selection) | instance `site_config.yaml` (`llm.model`, `voice.llm.model`, `voice.stt.model`); console `backend/config_manager.py` `FIELD_GROUPS` fields `llm_model`/`voice_llm_model`/`voice_stt_model` (type `"model"`, carry `role`+`provider_field`); frontend `frontend/config.js` (`cfgFieldRow` model branch, `cfgModelsForField`); write path `PUT /admin/config` | picker filters by **scope** (global + this client) |
| Usage metering (attribution) | `ledger.sqlite` `snap_key` (client×alias), `snap_model` (client×model) from `GET /admin/metrics`; alias→set join via `vault.alias_for` hash (`ledger._alias_map`) | unchanged; per-model pricing reads the new `models` table |
| Sell-side plan/tariff | `ledger.sqlite.plans` (per client) | unchanged (sell stays per-client, not per-model) |
| Catalog editor UI | *does not exist* | **new** panel + `frontend/catalog.js` (or fold into `ledger.js`); routes below |

---

## 4. The four scenarios, mapped onto the model

- **General bulk ("same-for-everyone key").** One vault set (`owner:"ours"`,
  fleet key) assigned to many clients for role `llm`; each of those clients'
  `site_config.llm.model` selects the **same global catalog model**
  (`mistral-small-2506`). The "bulk storage" isn't physical — it's a shared
  pay-as-you-go pipe; each client's *allowance* is a `plans` row (their tank),
  exactly D2. Metering already splits usage per client (`snap_model` is
  client×model) even though they share one key alias.

- **Client needs a model not in the shared catalog.** Add a catalog row with
  `scope = <client>` (a client-scoped model). Their `site_config` selects it;
  the picker shows it only for that client. Still one catalog, just filtered by
  scope. No vault change.

- **BYOK.** A vault set with `owner:"client"` assigned to that client for the
  role. Purely a *credential* property — orthogonal to the model, which can be
  a global catalog model or a client-scoped one. The ledger prices BYOK flow at
  €0 to us (D5). No catalog or assignment special-casing.

- **Client-specific key that is still ours (dedicated paid key).** Same as
  bulk but the assigned set is a dedicated `owner:"ours"` key for that client;
  metering separates it automatically by alias. No new concept.

Note every scenario is expressed by combining the three existing axes
(catalog scope, credential owner, assignment) — none needs a new "mega-record"
tying model+client+key together.

---

## 5. The one real decision: model on the assignment? — NO (recommended)

Tempting to make an assignment `{client, role, set_id, model_id}` so one record
fully says "client → key → model." **Recommended against**, because:

- The *selected* model already lives in `site_config.yaml` (Config tab writes
  it, drift-check watches it). Putting it on the assignment too creates two
  sources of truth that can disagree.
- The ledger doesn't need it to price: `snap_model` records the model the
  instance *actually ran*, priced against the `models` table. Observed reality
  beats provisioning intent for billing.
- "Which models may this client use" is answered by catalog **scope** (global +
  client-scoped); "which one it uses" by config selection. Those are genuinely
  different questions and deserve different homes.

So the assignment stays a **credential binding only**. Model = catalog (menu +
price + scope) ⊕ config (selection) ⊕ metering (observed). Revisit only if we
ever want a provisioning ledger independent of live config (not now).

---

## 6. What to add — implementation checklist (when unparked)

Phased so each step is usable alone and nothing touches frozen instances.

**Phase A — catalog becomes a live, scoped, editable store (`ledger.py`).**
- New `models` table in `ledger.sqlite`: `id TEXT PK, provider, role, unit,
  scope TEXT DEFAULT 'global'` (`'global'` or a client name), `label`,
  `is_default INT`, `buy_in REAL, buy_cached REAL, buy_out REAL` (€/1k, token
  models), `buy_per_unit REAL` (voice per-unit models), `active INT DEFAULT 1`,
  `notes`, `updated`. Seed from `model_catalog.py` via `INSERT OR IGNORE` in
  `_db()` (same non-clobber pattern as `model_rates` today), all `scope='global'`.
- Fold `model_rates` into this table (keep `get_model_rates` /
  `POST /api/ledger/model-rates` working as thin aliases so nothing breaks).
- CRUD: `list_models(client=None, role=None, provider=None)` (returns global +
  that client's scoped rows), `upsert_model(...)`, `delete_model(id)` (guard:
  refuse to delete a `model_catalog.py`-seeded global row; only operator-added
  or client-scoped rows are deletable).
- `model_catalog.py` stays the shipped factory default (the seed); the table is
  the editable live catalog.

**Phase B — routes (`routes.py`).**
- `GET /api/models?client=&role=&provider=` → `list_models(...)`.
- `POST /api/models` (upsert; body: id, provider, role, unit, scope, prices,
  active, notes).
- `DELETE /api/models/{id}` (honours the delete guard).
- Point `GET /api/model-catalog` at `list_models()` (live table) instead of the
  static list, or add `?client=` to it — pick one and delete the other path.

**Phase C — catalog editor UI (frontend).**
- New `frontend/catalog.js` (app.js-split rule: don't grow `app.js`) or fold
  into `frontend/ledger.js` if it lands on the Tokens tab. **Open decision:**
  which tab — Credentials (keys+models together) vs Tokens (prices together) vs
  a new "Catalog" tab. Leaning Tokens, since it's price-adjacent and the ledger
  owns the store.
- Panel: table of models (global + selected client's scoped), add/edit form
  (id, provider, role, unit, scope=global|client, prices in native unit,
  active, notes), delete (guarded).

**Phase D — Config-tab picker filters by scope (`config.js`).**
- `cfgModelsForField` already filters by role + provider; add a **scope**
  filter using the currently-selected client (global + `scope==client`). Source
  from `GET /api/models?client=<name>` instead of the static catalog. Keep the
  free-text "Custom / other…" escape hatch (an unregistered model still works;
  the editor is how you formalise + price it later).

**Phase E — authoritative per-model buy pricing (`ledger._client_economics`).**
- This is the deferred item #9 flip. Price `by_model` usage from the `models`
  table, **tier-gated**: if the client's assigned `llm` source is `tier:free`
  or `owner:client`, buy = €0 regardless of model; if `paid`, buy = per-model
  rate. Falls back to `source_rates` when a model is unpriced. Today every
  source is free-tier so buy is €0 either way — which is why flipping this is
  safe whenever, and why the current `buy_by_model` is deliberately additive
  (informational) until this lands.

**Vault: no schema change.** Optionally surface `owner:"client"` (BYOK) and
per-role assignment more clearly in the Credentials UI, but the data model is
already correct.

---

## 7. Migration notes

- Seeding the `models` table is non-destructive (`INSERT OR IGNORE`) and
  already the pattern `model_rates` uses — no data loss, operator edits win.
- No `vault.json` migration: sets/assignments unchanged. BYOK is already
  `owner:"client"`.
- Instances whose `site_config.llm.model` names a model not in the catalog are
  "unregistered": they keep working (picker free-text), and the editor offers
  "register + price this model" (adds a row). Consider a one-off scan that
  lists every distinct model seen in `snap_model` that has no `models` row, so
  the operator can price stragglers.
- `clients.json` is untouched (legacy `monthly_token_quota`/`cost_per_1k_*`
  migration to `plans` is a separate, already-tracked item).

---

## 8. Open decisions to settle at build time

1. **Catalog UI location** — Tokens vs Credentials vs new tab (leaning Tokens).
2. **`scope` encoding** — a `scope` string (`'global'` / client name) vs a
   nullable `client` column (NULL = global). Either works; pick one.
3. **Buy price granularity** — per `(model)` (recommended: a provider's price
   is the same across its keys; tier gating handles free/paid) vs per
   `(source, model)` (only needed if two *paid* keys of one provider carry
   different negotiated prices — defer until real).
4. **Sell-side per-model?** Out of scope now — sell stays per-client
   (`plans.sell_*`). Revisit only if a client is sold different margins per
   model.
5. Confirm §5 (model NOT on the assignment) still holds when a provisioning-
   intent view is actually wanted.
