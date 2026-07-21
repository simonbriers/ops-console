// ===========================================================================
// Config tab — per-client config manager (docs/TOKEN_ECONOMY_PLAN.md
// Phase 7). Fourth file of the app.js split; loads AFTER app.js, reuses its
// $ / escapeHtml globals. Provides refreshConfigPage() for switchPage().
//
// Renders the field catalog from /api/config-catalog (one source of truth,
// mirrored from the product's cfg-* tabs; fields tagged business/managed),
// loads live values from /api/clients/{name}/config (API-first, SSH
// fallback = read-only raw YAML), saves ONLY changed fields per group
// through the instance's validated PUT /admin/config, runs the three-way
// drift check, and drives the enable/disable-managed-mode action.
// ===========================================================================

let cfgCatalog = null;      // [{key,title,fields:[...]}] from /api/config-catalog
let cfgCurrent = null;      // last GET /api/clients/{name}/config payload
let cfgOriginal = {};       // field name -> value as loaded (change tracking)
let cfgModelCatalog = null; // {catalog:[...], roles:[...], rates:[...]} from /api/model-catalog

// Fallback model ids for a type:"model" field when /api/model-catalog is
// unreachable — the field stays free-text either way, so you can always test
// an unlisted model; the catalog just makes valid ones discoverable + priced.
const CFG_MODEL_FALLBACK = ["mistral-small-2506", "mistral-large-2512"];

async function refreshConfigPage() {
  if (!cfgCatalog) {
    try {
      cfgCatalog = await (await fetch("/api/config-catalog")).json();
    } catch {
      $("cfgStatus").textContent = "couldn't load the field catalog";
      return;
    }
  }
  if (!cfgModelCatalog) {
    try {
      cfgModelCatalog = await (await fetch("/api/model-catalog")).json();
    } catch {
      cfgModelCatalog = { catalog: [], roles: [], rates: [] };  // fall back to free text
    }
  }
  await populateCfgClientSelect();
}

// Catalog entries offered for a type:"model" field: filtered by the field's
// role and, when the sibling provider field's value is known and matches at
// least one entry, by that provider too.
function cfgModelsForField(f) {
  const all = (cfgModelCatalog && cfgModelCatalog.catalog) || [];
  let subset = f.role ? all.filter((m) => m.role === f.role) : all.slice();
  const prov = f.provider_field ? cfgOriginal[f.provider_field] : "";
  if (prov) {
    const byProv = subset.filter((m) => String(m.provider) === String(prov));
    if (byProv.length) subset = byProv;
  }
  return subset;
}

// One-line price label in the model's native unit (D1: LLM per 1M tokens,
// STT per audio-minute, TTS per character).
function cfgModelPriceLabel(m) {
  if (m.role === "llm") {
    return `€${m.buy_in_per_m}/${m.buy_out_per_m} per 1M in/out · cached €${m.buy_cached_per_m}`;
  }
  if (m.unit === "audio_minute") return `€${m.buy_per_unit}/min`;
  if (m.unit === "character") return `€${m.buy_per_unit}/char`;
  return "";
}

async function populateCfgClientSelect() {
  const sel = $("cfgClientSelect");
  if (!sel) return;
  const prev = sel.value;
  let names = [];
  try {
    names = await (await fetch("/api/client-names")).json();
  } catch { /* leave empty */ }
  sel.innerHTML = (names || []).map((n) =>
    `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
  if (prev && names.includes(prev)) sel.value = prev;
}

async function cfgLoad() {
  const name = $("cfgClientSelect").value;
  if (!name) return;
  $("cfgStatus").textContent = "loading…";
  $("cfgDriftPanel").innerHTML = "";
  let data;
  try {
    const r = await fetch(`/api/clients/${encodeURIComponent(name)}/config`);
    data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.status);
  } catch (e) {
    $("cfgStatus").textContent = "load failed: " + e.message;
    return;
  }
  cfgCurrent = data;
  renderCfgManagedBar();
  renderCfgLlmSource();
  if (data.source === "api") {
    $("cfgStatus").textContent = `loaded via admin API${data.frozen ? " · FROZEN (writes blocked)" : ""}`;
    renderCfgGroups(data.config || {});
  } else if (data.source === "ssh") {
    $("cfgStatus").textContent = "admin API unreachable — on-disk file, read-only";
    $("cfgGroups").innerHTML =
      `<p class="muted">${escapeHtml(data.error || "")}</p>` +
      `<pre class="cfg-raw">${escapeHtml(data.raw_yaml || "")}</pre>`;
  } else {
    $("cfgStatus").textContent = "unreachable";
    $("cfgGroups").innerHTML = `<p class="empty">${escapeHtml(data.error || "instance unreachable")}</p>`;
  }
}

function renderCfgManagedBar() {
  const d = cfgCurrent;
  const el = $("cfgManagedBar");
  if (!d) { el.innerHTML = ""; return; }
  const state = d.managed
    ? `<span class="cfg-badge cfg-badge-managed">MANAGED MODE ON</span>`
    : `<span class="cfg-badge">managed mode off</span>`;
  const opTok = d.has_operator_token
    ? `operator token on file`
    : `no operator token recorded <button type="button" onclick="cfgFetchOperatorToken()">Fetch from .env</button>`;
  const next = d.managed ? "disable" : "enable";
  el.innerHTML = `
    <div class="ob-form cfg-managed-bar">
      ${state}
      <span class="muted">${opTok}</span>
      <label>Type the client name to ${next} managed mode
        <input id="cfgManagedConfirm" placeholder="${escapeHtml(d.client)}" autocomplete="off" />
      </label>
      <button type="button" id="cfgManagedBtn" onclick="cfgToggleManaged(${d.managed ? "false" : "true"})">
        ${d.managed ? "Disable" : "Enable"} managed mode</button>
      <span class="muted">edits .env + site.managed over SSH, then RECREATES the app container</span>
    </div>
    <div id="cfgManagedSteps"></div>`;
}

function renderCfgLlmSource() {
  const el = $("cfgLlmSource");
  const src = cfgCurrent && cfgCurrent.llm_source;
  if (!src) {
    el.innerHTML = `<p class="muted">No vault LLM assignment recorded for this client —
      the model picker below has no source facts to show (Credentials → Reconcile fixes that).</p>`;
    return;
  }
  el.innerHTML = `
    <div class="cfg-llm-source">
      <strong>LLM source:</strong> ${escapeHtml(src.set_name || "?")}
      <span class="cfg-badge">${escapeHtml(src.provider || "?")} · ${escapeHtml(src.tier || "?")}${src.owner === "client" ? " · BYOK" : ""}</span>
      ${src.notes ? `<pre class="cfg-source-notes">${escapeHtml(src.notes)}</pre>`
                  : `<span class="muted">no notes on this source — add rate-limit facts in the vault</span>`}
    </div>`;
}

function renderCfgGroups(values) {
  cfgOriginal = {};
  const managedInstance = !!(cfgCurrent && cfgCurrent.managed);
  const html = cfgCatalog.map((group) => {
    const rows = group.fields.map((f) => {
      const val = values[f.name];
      cfgOriginal[f.name] = normalizeCfgValue(f, val);
      return cfgFieldRow(f, val, managedInstance);
    }).join("");
    return `
      <fieldset class="cfg-group" data-group="${escapeHtml(group.key)}">
        <legend>${escapeHtml(group.title)}</legend>
        ${rows}
        <div class="ob-form">
          <button type="button" class="primary" onclick="cfgSaveGroup('${escapeHtml(group.key)}')">Save changed fields</button>
          <span class="muted cfg-group-status" id="cfgGroupStatus-${escapeHtml(group.key)}"></span>
        </div>
      </fieldset>`;
  }).join("");
  $("cfgGroups").innerHTML = html;
}

function cfgFieldRow(f, val, managedInstance) {
  const id = `cfgField-${f.name}`;
  const badge = f.managed
    ? `<span class="cfg-badge cfg-badge-managed" title="console-only on managed instances${managedInstance ? "" : " (instance not managed yet — clinic can still edit this)"}">managed</span>`
    : `<span class="cfg-badge cfg-badge-business" title="the clinic can also edit this in its own panel">business</span>`;
  let input;
  const v = val === null || val === undefined ? "" : val;
  if (f.type === "checkbox") {
    input = `<input type="checkbox" id="${id}" ${v ? "checked" : ""} />`;
  } else if (f.type === "select") {
    input = `<select id="${id}">` + (f.options || []).map((o) =>
      `<option value="${escapeHtml(o)}" ${String(v) === o ? "selected" : ""}>${escapeHtml(o)}</option>`
    ).join("") + `</select>`;
  } else if (f.type === "textarea") {
    input = `<textarea id="${id}" rows="2">${escapeHtml(String(v))}</textarea>`;
  } else if (f.type === "number") {
    input = `<input type="number" step="any" id="${id}" value="${escapeHtml(String(v))}" />`;
  } else if (f.type === "model") {
    const listId = `${id}-models`;
    const models = cfgModelsForField(f);
    const ids = models.length ? models.map((m) => m.id) : CFG_MODEL_FALLBACK;
    input = `<input id="${id}" value="${escapeHtml(String(v))}" list="${listId}" />` +
      `<datalist id="${listId}">` +
      ids.map((m) => `<option value="${escapeHtml(m)}"></option>`).join("") +
      `</datalist>`;
    if (models.length) {
      input += `<div class="cfg-model-prices">` + models.map((m) =>
        `<div><code>${escapeHtml(m.id)}</code> <span class="muted">${escapeHtml(cfgModelPriceLabel(m))}${m.default ? " · default" : ""}</span></div>`
      ).join("") + `</div>`;
    }
  } else {  // text / password
    input = `<input type="${f.type === "password" ? "password" : "text"}" id="${id}" value="${escapeHtml(String(v))}" />`;
  }
  return `
    <div class="cfg-field-row">
      <label for="${id}">${escapeHtml(f.label)} ${badge}</label>
      ${input}
      ${f.hint ? `<span class="muted cfg-hint">${escapeHtml(f.hint)}</span>` : ""}
    </div>`;
}

function normalizeCfgValue(f, val) {
  if (f.type === "checkbox") return !!val;
  if (val === null || val === undefined) return "";
  return String(val);
}

function readCfgField(f) {
  const el = document.getElementById(`cfgField-${f.name}`);
  if (!el) return undefined;
  if (f.type === "checkbox") return el.checked;
  if (f.type === "number") {
    const raw = el.value.trim();
    if (raw === "") return null;  // "unset" — will be skipped as unchanged unless it changed
    const n = Number(raw);
    return Number.isNaN(n) ? null : n;
  }
  return el.value;
}

async function cfgSaveGroup(groupKey) {
  if (!cfgCurrent || cfgCurrent.source !== "api") return;
  const group = cfgCatalog.find((g) => g.key === groupKey);
  const status = $(`cfgGroupStatus-${groupKey}`);
  const fields = {};
  for (const f of group.fields) {
    const now = readCfgField(f);
    if (now === undefined || now === null) continue;
    const nowNorm = f.type === "checkbox" ? !!now : String(now);
    if (nowNorm !== cfgOriginal[f.name]) fields[f.name] = now;
  }
  if (!Object.keys(fields).length) {
    status.textContent = "nothing changed";
    return;
  }
  status.textContent = "saving…";
  try {
    const r = await fetch(`/api/clients/${encodeURIComponent(cfgCurrent.client)}/config`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields }),
    });
    const data = await r.json();
    if (!r.ok) {
      const det = data.detail;
      throw new Error(typeof det === "string" ? det : JSON.stringify(det));
    }
    for (const k of Object.keys(fields)) {
      const f = group.fields.find((x) => x.name === k);
      cfgOriginal[k] = normalizeCfgValue(f, fields[k]);
    }
    status.textContent = data.mismatches && data.mismatches.length
      ? `saved, but echo mismatch: ${data.mismatches.map((m) => m.field).join(", ")} — is the instance up to date?`
      : `saved: ${data.written.join(", ")}`;
  } catch (e) {
    status.textContent = "save failed: " + e.message;
  }
}

async function cfgDrift() {
  const name = $("cfgClientSelect").value;
  if (!name) return;
  const panel = $("cfgDriftPanel");
  panel.innerHTML = `<p class="muted">running drift check (two SSH reads)…</p>`;
  let d;
  try {
    const r = await fetch(`/api/clients/${encodeURIComponent(name)}/config/drift`);
    d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.status);
  } catch (e) {
    panel.innerHTML = `<p class="empty">drift check failed: ${escapeHtml(e.message)}</p>`;
    return;
  }
  if (!d.ok) {
    panel.innerHTML = `<p class="empty">${escapeHtml(d.error || "drift check failed")}</p>`;
    return;
  }
  const missing = d.missing_defaults || [];
  const oob = d.out_of_band || [];
  let html = `<div class="cfg-drift">`;
  html += missing.length
    ? `<h4>Stale config — ${missing.length} shipped default(s) missing from the live file
         <span class="muted">(the DEPLOYMENT.md §10 gap)</span></h4>
       <ul>${missing.map((k) => `<li><code>${escapeHtml(k)}</code></li>`).join("")}</ul>
       <p class="muted">Fix: set the field through this page (API write adds the key), or
       edit /data/site_config.yaml deliberately.</p>`
    : `<p class="ok">✓ No shipped defaults missing from the live file.</p>`;
  html += oob.length
    ? `<h4>Out-of-band changes — ${oob.length} field(s) differ from what this console last wrote</h4>
       <table><thead><tr><th>Field</th><th>Console wrote</th><th>Live value</th><th>Written at</th></tr></thead>
       <tbody>${oob.map((o) => `<tr><td><code>${escapeHtml(o.field)}</code></td>
         <td>${escapeHtml(JSON.stringify(o.console_wrote))}</td>
         <td>${escapeHtml(JSON.stringify(o.live_value))}</td>
         <td class="muted">${escapeHtml(o.written_at || "?")}</td></tr>`).join("")}</tbody></table>`
    : `<p class="ok">✓ Nothing this console wrote has been changed behind its back.</p>`;
  html += `<p class="muted">live site.managed = ${d.live_managed} · checked ${escapeHtml(d.checked)}</p></div>`;
  panel.innerHTML = html;
}

async function cfgToggleManaged(enable) {
  if (!cfgCurrent) return;
  const name = cfgCurrent.client;
  const confirmVal = ($("cfgManagedConfirm") || {}).value || "";
  const stepsEl = $("cfgManagedSteps");
  const btn = $("cfgManagedBtn");
  if (confirmVal !== name) {
    stepsEl.innerHTML = `<p class="empty">Type the exact client name to confirm — this recreates the live container.</p>`;
    return;
  }
  btn.disabled = true;
  stepsEl.innerHTML = `<p class="muted">${enable ? "enabling" : "disabling"} managed mode — .env, site.managed, recreate, verify…</p>`;
  try {
    const r = await fetch(`/api/clients/${encodeURIComponent(name)}/managed-mode`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: enable, confirm_name: confirmVal }),
    });
    const data = await r.json();
    const steps = (data.steps || (data.detail || {}).steps || []);
    const stepHtml = steps.map((s) =>
      `<li class="${s.ok ? "ok" : "empty"}">${s.ok ? "✓" : "✗"} ${escapeHtml(s.step)} — ${escapeHtml(s.detail || "")}</li>`).join("");
    if (!r.ok) {
      const det = data.detail || {};
      stepsEl.innerHTML = `<ul>${stepHtml}</ul><p class="empty">${escapeHtml(
        typeof det === "string" ? det : det.error || "failed")}</p>`;
    } else {
      stepsEl.innerHTML = `<ul>${stepHtml}</ul><p class="ok">✓ managed mode ${enable ? "enabled" : "disabled"} and verified.</p>`;
      await cfgLoad();  // re-render the bar with the new state
    }
  } catch (e) {
    stepsEl.innerHTML = `<p class="empty">request failed: ${escapeHtml(e.message)}</p>`;
  } finally {
    btn.disabled = false;
  }
}

async function cfgFetchOperatorToken() {
  if (!cfgCurrent) return;
  const name = cfgCurrent.client;
  $("cfgStatus").textContent = "fetching OPERATOR_TOKEN from .env…";
  try {
    const r = await fetch(`/api/clients/${encodeURIComponent(name)}/fetch-operator-token`, { method: "POST" });
    const data = await r.json();
    if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
    $("cfgStatus").textContent = "operator token recorded";
    await cfgLoad();
  } catch (e) {
    $("cfgStatus").textContent = "fetch failed: " + e.message;
  }
}

// -- wiring ------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  const load = $("cfgLoadBtn");
  if (load) load.addEventListener("click", cfgLoad);
  const drift = $("cfgDriftBtn");
  if (drift) drift.addEventListener("click", cfgDrift);
  const sel = $("cfgClientSelect");
  if (sel) sel.addEventListener("change", cfgLoad);
});
