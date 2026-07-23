// ===========================================================================
// Model Setup tab — ONE guided page that replaces the scattered provider/model
// controls (the old Credentials matrix, the Config-tab provider/model fields,
// and the standalone Models tab). Three stacked, independently-scrollable
// stages that read top-to-bottom as a pipeline:
//
//   1. Providers & keys   — which providers are valid + their API key (Test)
//   2. Allowed models     — per provider, browse live /models and APPROVE the
//                           ones clinics may use (the operator allow-list)
//   3. Clinic assignments — assign an approved model to each clinic's role;
//                           Apply writes it to the instance; Reconcile flags
//                           anything running off the allow-list.
//
// Reuses existing endpoints only (no backend change): /api/vault/sets(+/test),
// /api/provider-models, /api/models/registry(+/remove), /api/models/slots,
// /api/models/assignments, /api/models/assign, /api/models/reconcile.
// Loads AFTER app.js; reuses its $ / escapeHtml globals. Provides
// refreshSetupPage() for switchPage().
// ===========================================================================

// LLM/STT/TTS providers that use a single API key (google-TTS file creds and
// twilio/smtp comms creds stay in the Credentials tab — different concern).
// Filled at load from GET /api/vault/kinds (the credential-bearing kinds);
// literal is only a fallback. Do NOT hand-edit — add a KIND_META row instead.
let SU_PROVIDERS = [
  { kind: "zenmux",     label: "ZenMux (aggregator)",  envKey: "ZENMUX_API_KEY" },
  { kind: "openrouter", label: "OpenRouter",           envKey: "OPENROUTER_API_KEY" },
  { kind: "mistral",    label: "Mistral",              envKey: "MISTRAL_API_KEY" },
  { kind: "nvidia",     label: "NVIDIA",               envKey: "NVIDIA_API_KEY" },
];
// providers browsable for models (adds ollama = local, no key). Filled at load
// from GET /api/providers (backend/providers.py, llm role); literal is fallback.
let SU_BROWSE_PROVIDERS = ["zenmux", "openrouter", "mistral", "nvidia", "ollama"];

const SU_SLOT_ORDER = ["llm", "voice_llm", "voice_stt"];
const SU_SLOT_LABEL = { llm: "LLM (text)", voice_llm: "Voice LLM", voice_stt: "Voice STT" };

let suSets = [];         // /api/vault/sets (redacted)
let suRegistry = [];     // /api/models/registry (allow-list)
let suSlots = {};        // /api/models/slots
let suAssignments = [];  // /api/models/assignments
let suClients = [];      // /api/client-names
const suBrowseCache = {}; // provider -> live /models list

async function refreshSetupPage() {
  if (!Object.keys(suSlots).length) {
    try { suSlots = await (await fetch("/api/models/slots")).json(); }
    catch { suSlots = { llm: { role: "llm" }, voice_llm: { role: "llm" }, voice_stt: { role: "stt" } }; }
  }
  await Promise.all([suLoadKinds(), suLoadProviders(), suLoadSets(), suLoadRegistry(),
                     suLoadAssignments(), suLoadClients()]);
  suRenderProviders();
  suRenderRegistry();
  await suRenderMatrix();
}
// SU_PROVIDERS (credential-bearing key boxes) from /api/vault/kinds, and
// SU_BROWSE_PROVIDERS (llm model-browse list) from /api/providers — the two
// canonical sources. Both keep their fallback literal if the fetch fails.
async function suLoadKinds() {
  try {
    const kinds = await (await fetch("/api/vault/kinds")).json();
    const provs = (kinds || []).filter((k) => k.id_key)
      .map((k) => ({ kind: k.kind, label: k.label || k.kind, envKey: k.id_key }));
    if (provs.length) SU_PROVIDERS = provs;
  } catch { /* keep the fallback literal */ }
}
async function suLoadProviders() {
  try {
    const byRole = await (await fetch("/api/providers")).json();
    const names = (byRole.llm || []).map((p) => p.name);
    if (names.length) SU_BROWSE_PROVIDERS = names;
  } catch { /* keep the fallback literal */ }
}

async function suLoadSets() {
  try { suSets = await (await fetch("/api/vault/sets")).json(); } catch { suSets = []; }
}
async function suLoadRegistry() {
  try { suRegistry = await (await fetch("/api/models/registry")).json(); } catch { suRegistry = []; }
}
async function suLoadAssignments() {
  try { suAssignments = await (await fetch("/api/models/assignments")).json(); } catch { suAssignments = []; }
}
async function suLoadClients() {
  try { suClients = await (await fetch("/api/client-names")).json(); } catch { suClients = []; }
}

// ── Stage 1: providers & keys ──────────────────────────────────────────────

function suSetForKind(kind) {
  return suSets.find((s) => s.kind === kind) || null;
}

function suRenderProviders() {
  const body = $("suProvidersBody");
  if (!body) return;
  body.innerHTML = SU_PROVIDERS.map((p) => {
    const set = suSetForKind(p.kind);
    const keyState = set
      ? `<span class="ok">key on file</span>${set.alias ? ` <code>${escapeHtml(set.alias)}</code>` : ""}`
      : `<span class="muted">no key yet</span>`;
    const testBtn = set ? `<button type="button" class="su-key-test" data-id="${escapeHtml(set.id)}">Test</button>` : "";
    return `
      <div class="su-provider-row" style="padding:.4rem 0;border-bottom:1px solid var(--border,#eee)">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem;flex-wrap:wrap">
          <strong>${escapeHtml(p.label)}</strong>
          <span>${keyState}</span>
        </div>
        <div class="ob-form" style="margin-top:.25rem">
          <input type="password" class="su-key-input" data-kind="${p.kind}" data-envkey="${p.envKey}"
            data-setid="${set ? escapeHtml(set.id) : ""}" placeholder="${set ? "replace key…" : `paste ${p.envKey}…`}"
            style="min-width:22rem" />
          <button type="button" class="su-key-save" data-kind="${p.kind}">${set ? "Replace" : "Save key"}</button>
          ${testBtn}
          <span class="muted su-key-status" data-kind="${p.kind}"></span>
        </div>
      </div>`;
  }).join("");
  body.querySelectorAll(".su-key-save").forEach((btn) =>
    btn.addEventListener("click", () => suSaveKey(btn.dataset.kind)));
  body.querySelectorAll(".su-key-test").forEach((btn) =>
    btn.addEventListener("click", () => suTestKey(btn.dataset.id)));
}

async function suSaveKey(kind) {
  const input = document.querySelector(`.su-key-input[data-kind="${CSS.escape(kind)}"]`);
  const status = document.querySelector(`.su-key-status[data-kind="${CSS.escape(kind)}"]`);
  const val = (input.value || "").trim();
  if (!val) { status.textContent = "paste a key first"; return; }
  const p = SU_PROVIDERS.find((x) => x.kind === kind);
  const existingId = input.dataset.setid || null;
  status.textContent = "saving…";
  const r = await fetch("/api/vault/sets", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: `${p.label} key`, kind, values: { [p.envKey]: val }, id: existingId }) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { status.textContent = `✘ ${data.detail || "save failed"}`; return; }
  status.textContent = "✔ saved — testing…";
  await suLoadSets();
  const set = suSetForKind(kind);
  if (set) { const t = await suTestKeyQuiet(set.id); status.textContent = t; }
  suRenderProviders();
}

async function suTestKeyQuiet(setId) {
  try {
    const r = await fetch(`/api/vault/sets/${encodeURIComponent(setId)}/test`, { method: "POST" });
    const data = await r.json().catch(() => ({}));
    const t = data.test || {};
    return `${t.ok ? "✔" : "✘"} ${t.message || (t.ok ? "OK" : "failed")}`;
  } catch (e) { return `✘ ${e}`; }
}

async function suTestKey(setId) {
  const set = suSets.find((s) => s.id === setId);
  const status = set ? document.querySelector(`.su-key-status[data-kind="${CSS.escape(set.kind)}"]`) : null;
  if (status) status.textContent = "testing…";
  const msg = await suTestKeyQuiet(setId);
  if (status) status.textContent = msg;
}

// ── Stage 2: allowed models (registry) ─────────────────────────────────────

function suPriceLabel(m) {
  if (m.role === "llm" && m.buy_in_per_m != null) return `€${m.buy_in_per_m}/${m.buy_out_per_m} per 1M`;
  if (m.buy_per_unit != null) return `€${m.buy_per_unit}/${m.unit || "unit"}`;
  return "";
}

function suRenderRegistry() {
  const body = $("suRegistryBody");
  if (!body) return;
  if (!suRegistry.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty">No approved models yet — approve some from a provider below.</td></tr>`;
    return;
  }
  body.innerHTML = suRegistry.map((m) => `
    <tr>
      <td><code>${escapeHtml(m.id)}</code></td>
      <td>${escapeHtml(m.provider || "")}</td>
      <td>${escapeHtml(m.role || "")}</td>
      <td class="muted">${escapeHtml(m.label && m.label !== m.id ? m.label : "")}</td>
      <td class="muted">${escapeHtml(suPriceLabel(m))}</td>
      <td><button type="button" class="danger su-model-remove" data-id="${escapeHtml(m.id)}"
            data-provider="${escapeHtml(m.provider || "")}">Remove</button></td>
    </tr>`).join("");
  body.querySelectorAll(".su-model-remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm(`Remove ${btn.dataset.id} from the approved list?\n\nClients already assigned it keep running it until reassigned; Reconcile then flags it.`)) return;
      await fetch("/api/models/registry/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: btn.dataset.id, provider: btn.dataset.provider || null }) });
      await refreshSetupPage();
    });
  });
}

async function suBrowse() {
  const provider = $("suAddProvider").value;
  const box = $("suBrowseBox");
  if (!box) return;
  if (!provider) { box.innerHTML = `<p class="empty">pick a provider first</p>`; return; }
  box.innerHTML = `<p class="muted">loading ${escapeHtml(provider)} models…</p>`;
  let data;
  try { data = await (await fetch(`/api/provider-models?provider=${encodeURIComponent(provider)}`)).json(); }
  catch (e) { box.innerHTML = `<p class="empty">${escapeHtml(String(e))}</p>`; return; }
  if (!data.ok) { box.innerHTML = `<p class="empty">${escapeHtml(data.message || "failed to list models")}</p>`; return; }
  suBrowseCache[provider] = data.models || [];
  box.innerHTML =
    `<input id="suBrowseFilter" placeholder="filter ${(data.models || []).length} models…" oninput="suRenderBrowse()" `
    + `style="width:100%;box-sizing:border-box;margin:.3rem 0" />`
    + `<ul id="suBrowseList" style="list-style:none;margin:0;padding:0;max-height:200px;overflow:auto;`
    + `border:1px solid var(--border,#ccc);border-radius:4px"></ul>`;
  suRenderBrowse();
}

function suRenderBrowse() {
  const ul = $("suBrowseList");
  if (!ul) return;
  const provider = $("suAddProvider").value;
  const role = $("suAddRole").value;
  const q = (($("suBrowseFilter") || {}).value || "").trim().toLowerCase();
  let all = suBrowseCache[provider] || [];
  if (role === "llm") {
    const withMod = all.filter((m) => (m.output_modalities || []).length);
    if (withMod.length) all = all.filter((m) => (m.output_modalities || []).includes("text"));
  }
  const approved = new Set(suRegistry.filter((m) => m.provider === provider && m.role === role).map((m) => m.id));
  const shown = q ? all.filter((m) => (m.id + " " + (m.label || "")).toLowerCase().includes(q)) : all;
  ul.innerHTML = shown.slice(0, 500).map((m) => {
    const ctx = m.context_length ? ` · ${Number(m.context_length).toLocaleString()} ctx` : "";
    const sub = (m.label && m.label !== m.id ? m.label : "") + ctx;
    const btn = approved.has(m.id)
      ? `<span class="muted">approved ✓</span>`
      : `<button type="button" class="su-approve-one" data-id="${encodeURIComponent(m.id)}" data-label="${escapeHtml(m.label || m.id)}">Approve</button>`;
    return `<li style="padding:.25rem .4rem;border-bottom:1px solid var(--border,#eee);display:flex;justify-content:space-between;align-items:center;gap:.5rem">`
      + `<span><code>${escapeHtml(m.id)}</code>${sub ? ` <span class="muted">${escapeHtml(sub)}</span>` : ""}</span>${btn}</li>`;
  }).join("") || `<li class="muted" style="padding:.4rem">no match</li>`;
  ul.querySelectorAll(".su-approve-one").forEach((btn) => {
    btn.addEventListener("click", () => suApprove(decodeURIComponent(btn.dataset.id), btn.dataset.label));
  });
}

async function suApprove(id, label) {
  const provider = $("suAddProvider").value;
  const role = $("suAddRole").value;
  const status = $("suAddStatus");
  const r = await fetch("/api/models/registry", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, provider, role, label: label || id }) });
  const data = await r.json().catch(() => ({}));
  if (status) status.textContent = r.ok ? `✔ approved ${id}` : `✘ ${data.detail || "failed"}`;
  await suLoadRegistry();
  suRenderRegistry();
  suRenderBrowse();
  await suRenderMatrix();
}

async function suApproveManual() {
  const id = ($("suManualId").value || "").trim();
  if (!id) { $("suAddStatus").textContent = "type a model id first"; return; }
  await suApprove(id, id);
  $("suManualId").value = "";
}

// ── Stage 3: clinic assignments ────────────────────────────────────────────

function suCurrentAssignment(client, slot) {
  return suAssignments.find((a) => a.client === client && a.slot === slot) || null;
}

async function suRenderMatrix() {
  const body = $("suMatrixBody");
  if (!body) return;
  const span = SU_SLOT_ORDER.length + 2;
  if (!suClients.length) {
    body.innerHTML = `<tr><td colspan="${span}" class="empty">No clients registered yet.</td></tr>`;
    return;
  }
  body.innerHTML = suClients.map((name) => {
    const cells = SU_SLOT_ORDER.map((slot) => {
      const role = (suSlots[slot] || {}).role || "llm";
      const current = suCurrentAssignment(name, slot);
      const opts = suRegistry.filter((m) => m.role === role);
      const optsHtml = [`<option value="">—</option>`].concat(opts.map((m) =>
        `<option value="${escapeHtml(m.id)}"${current && current.model_id === m.id ? " selected" : ""}>${escapeHtml(m.id)}</option>`));
      let note = "";
      if (current && !current.in_registry) {
        note = `<div class="muted" style="color:#b00" title="running a model that is NOT approved">⚠ ${escapeHtml(current.model_id)} (un-approved)</div>`;
      } else if (current) {
        note = `<div class="muted">${escapeHtml(current.source || "")}</div>`;
      }
      return `<td><select class="su-pick" data-client="${escapeHtml(name)}" data-slot="${slot}"
        data-current="${current ? escapeHtml(current.model_id) : ""}">${optsHtml.join("")}</select>${note}</td>`;
    }).join("");
    return `<tr>
      <td><strong>${escapeHtml(name)}</strong></td>${cells}
      <td><button type="button" class="primary su-apply" data-client="${escapeHtml(name)}">Apply</button>
        <div class="muted su-status" data-client="${escapeHtml(name)}"></div></td>
    </tr>`;
  }).join("");
  body.querySelectorAll(".su-apply").forEach((btn) =>
    btn.addEventListener("click", () => suMatrixApply(btn.dataset.client)));
}

async function suMatrixApply(client) {
  const selects = [...document.querySelectorAll(`.su-pick[data-client="${CSS.escape(client)}"]`)];
  const changed = selects.filter((s) => s.value && s.value !== s.dataset.current);
  const statusEl = document.querySelector(`.su-status[data-client="${CSS.escape(client)}"]`);
  if (!changed.length) { statusEl.textContent = "nothing changed"; return; }
  if (!confirm(`Assign to ${client}:\n\n${changed.map((s) => `${s.dataset.slot} → ${s.value}`).join("\n")}\n\n`
      + `This writes the model + its provider to the instance and RECREATES its container.`)) return;
  statusEl.textContent = "assigning + recreating…";
  for (const s of changed) {
    const r = await fetch("/api/models/assign", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_name: client, slot: s.dataset.slot, model_id: s.value }) });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      statusEl.textContent = `✘ ${typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail)}`;
      return;
    }
  }
  statusEl.textContent = "✔ assigned";
  await suLoadAssignments();
  await suRenderMatrix();
}

async function suReconcile() {
  const panel = $("suReconcilePanel");
  if (!panel) return;
  panel.innerHTML = `<p class="muted">reading every client's live config…</p>`;
  let data;
  try {
    data = await (await fetch("/api/models/reconcile", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) })).json();
  } catch (e) { panel.innerHTML = `<p class="empty">${escapeHtml(String(e))}</p>`; return; }
  const reports = data.reports || [];
  const unapproved = reports.flatMap((r) => (r.unapproved || []).map((u) => ({ client: r.client, ...u })));
  const failed = reports.filter((r) => !r.ok);
  let html = `<p class="ok">Reconciled ${reports.length} client(s).</p>`;
  if (unapproved.length) {
    html += `<h4 style="color:#b00">⚠ ${unapproved.length} un-approved model(s) running (not in the allow-list)</h4><ul>`
      + unapproved.map((u) => `<li><strong>${escapeHtml(u.client)}</strong> · ${escapeHtml(u.slot)} → `
        + `<code>${escapeHtml(u.model_id)}</code> (${escapeHtml(u.provider || "?")})</li>`).join("") + `</ul>`;
  } else {
    html += `<p class="ok">✓ Every running model is approved.</p>`;
  }
  if (failed.length) html += `<p class="muted">couldn't read: ${failed.map((r) => escapeHtml(r.client)).join(", ")}</p>`;
  panel.innerHTML = html;
  await suLoadAssignments();
  await suRenderMatrix();
}

// ── wiring ──────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  const br = $("suAddBrowseBtn"); if (br) br.addEventListener("click", suBrowse);
  const man = $("suAddManualBtn"); if (man) man.addEventListener("click", suApproveManual);
  const rec = $("suReconcileBtn"); if (rec) rec.addEventListener("click", suReconcile);
});
