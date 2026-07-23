// ===========================================================================
// Models tab — operator-controlled model registry + per-client model
// assignments (the model-side twin of credentials.js's vault + matrix).
// Loads AFTER app.js; reuses its $ / escapeHtml globals. Provides
// refreshModelsPage() for switchPage().
//
// Governing principle: operators own the allow-list. Only models in the
// REGISTRY can be assigned to a client; assign refuses anything else. The
// assignment (client -> slot -> model) is the per-client tracking the ledger's
// usage/overage billing joins against. Reconcile reads each instance's live
// config and flags any model NOT in the registry.
// ===========================================================================

let mrSlots = {};         // {slot: {field, role, provider_field}} from /api/models/slots
let mrRegistry = [];      // approved models (allow-list)
let mrAssignments = [];   // [{client, slot, model_id, provider, in_registry, source}]
let mrClientNames = [];
const mrBrowseCache = {}; // provider -> live /models list

const MR_SLOT_ORDER = ["llm", "voice_llm", "voice_stt"];
const MR_SLOT_LABEL = { llm: "LLM (text)", voice_llm: "Voice LLM", voice_stt: "Voice STT" };

async function refreshModelsPage() {
  if (!Object.keys(mrSlots).length) {
    try { mrSlots = await (await fetch("/api/models/slots")).json(); }
    catch { mrSlots = { llm: { role: "llm" }, voice_llm: { role: "llm" }, voice_stt: { role: "stt" } }; }
  }
  await Promise.all([loadModelRegistry(), loadModelAssignments()]);
  renderModelRegistry();
  await renderModelMatrix();
}

async function loadModelRegistry() {
  try { mrRegistry = await (await fetch("/api/models/registry")).json(); }
  catch { mrRegistry = []; }
}

async function loadModelAssignments() {
  try { mrAssignments = await (await fetch("/api/models/assignments")).json(); }
  catch { mrAssignments = []; }
}

function mrPriceLabel(m) {
  if (m.role === "llm" && m.buy_in_per_m != null) return `€${m.buy_in_per_m}/${m.buy_out_per_m} per 1M`;
  if (m.buy_per_unit != null) return `€${m.buy_per_unit}/${m.unit || "unit"}`;
  return "";
}

// -- registry (the allow-list) ----------------------------------------------

function renderModelRegistry() {
  const body = $("mrRegistryBody");
  if (!body) return;
  if (!mrRegistry.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty">No approved models yet — approve some from a provider below.</td></tr>`;
    return;
  }
  body.innerHTML = mrRegistry.map((m) => `
    <tr>
      <td><code>${escapeHtml(m.id)}</code></td>
      <td>${escapeHtml(m.provider || "")}</td>
      <td>${escapeHtml(m.role || "")}</td>
      <td class="muted">${escapeHtml(m.label && m.label !== m.id ? m.label : "")}</td>
      <td class="muted">${escapeHtml(mrPriceLabel(m))}</td>
      <td><button type="button" class="danger mr-remove" data-id="${escapeHtml(m.id)}"
            data-provider="${escapeHtml(m.provider || "")}">Remove</button></td>
    </tr>`).join("");
  body.querySelectorAll(".mr-remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm(`Remove ${btn.dataset.id} from the approved registry?\n\n`
        + `Clients already assigned it keep running it until reassigned; reconcile will then flag it as un-approved.`)) return;
      await fetch("/api/models/registry/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: btn.dataset.id, provider: btn.dataset.provider || null }) });
      await refreshModelsPage();
    });
  });
}

// -- approve a model (browse a provider's live /models -> add to registry) --

async function mrBrowse() {
  const provider = $("mrAddProvider").value;
  const box = $("mrBrowseBox");
  if (!box) return;
  if (!provider) { box.innerHTML = `<p class="empty">pick a provider first</p>`; return; }
  box.innerHTML = `<p class="muted">loading ${escapeHtml(provider)} models…</p>`;
  let data;
  try { data = await (await fetch(`/api/provider-models?provider=${encodeURIComponent(provider)}`)).json(); }
  catch (e) { box.innerHTML = `<p class="empty">${escapeHtml(String(e))}</p>`; return; }
  if (!data.ok) { box.innerHTML = `<p class="empty">${escapeHtml(data.message || "failed to list models")}</p>`; return; }
  mrBrowseCache[provider] = data.models || [];
  box.innerHTML =
    `<input id="mrBrowseFilter" placeholder="filter ${(data.models || []).length} models…" oninput="mrRenderBrowse()" `
    + `style="width:100%;box-sizing:border-box;margin:.3rem 0" />`
    + `<ul id="mrBrowseList" style="list-style:none;margin:0;padding:0;max-height:240px;overflow:auto;`
    + `border:1px solid var(--border,#ccc);border-radius:4px"></ul>`;
  mrRenderBrowse();
}

function mrRenderBrowse() {
  const ul = $("mrBrowseList");
  if (!ul) return;
  const provider = $("mrAddProvider").value;
  const role = $("mrAddRole").value;
  const q = (($("mrBrowseFilter") || {}).value || "").trim().toLowerCase();
  let all = mrBrowseCache[provider] || [];
  // best-effort role filter for llm: text-output models when modality is known
  if (role === "llm") {
    const withMod = all.filter((m) => (m.output_modalities || []).length);
    if (withMod.length) all = all.filter((m) => (m.output_modalities || []).includes("text"));
  }
  const approved = new Set(mrRegistry.filter((m) => m.provider === provider && m.role === role).map((m) => m.id));
  const shown = q ? all.filter((m) => (m.id + " " + (m.label || "")).toLowerCase().includes(q)) : all;
  ul.innerHTML = shown.slice(0, 500).map((m) => {
    const ctx = m.context_length ? ` · ${Number(m.context_length).toLocaleString()} ctx` : "";
    const sub = (m.label && m.label !== m.id ? m.label : "") + ctx;
    const btn = approved.has(m.id)
      ? `<span class="muted">approved ✓</span>`
      : `<button type="button" class="mr-approve-one" data-id="${encodeURIComponent(m.id)}" `
        + `data-label="${escapeHtml(m.label || m.id)}">Approve</button>`;
    return `<li style="padding:.25rem .4rem;border-bottom:1px solid var(--border,#eee);display:flex;`
      + `justify-content:space-between;align-items:center;gap:.5rem">`
      + `<span><code>${escapeHtml(m.id)}</code>${sub ? ` <span class="muted">${escapeHtml(sub)}</span>` : ""}</span>${btn}</li>`;
  }).join("") || `<li class="muted" style="padding:.4rem">no match</li>`;
  ul.querySelectorAll(".mr-approve-one").forEach((btn) => {
    btn.addEventListener("click", () => mrApprove(decodeURIComponent(btn.dataset.id), btn.dataset.label));
  });
}

async function mrApprove(id, label) {
  const provider = $("mrAddProvider").value;
  const role = $("mrAddRole").value;
  const status = $("mrAddStatus");
  const r = await fetch("/api/models/registry", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, provider, role, label: label || id }) });
  const data = await r.json().catch(() => ({}));
  if (status) status.textContent = r.ok ? `✔ approved ${id}` : `✘ ${data.detail || "failed"}`;
  await loadModelRegistry();
  renderModelRegistry();
  mrRenderBrowse();
  await renderModelMatrix();
}

async function mrApproveManual() {
  const id = ($("mrManualId").value || "").trim();
  const provider = $("mrAddProvider").value;
  const role = $("mrAddRole").value;
  if (!id) { $("mrAddStatus").textContent = "type a model id first"; return; }
  await mrApprove(id, id);
  $("mrManualId").value = "";
}

// -- assignment matrix (client x slot) --------------------------------------

function mrCurrentAssignment(client, slot) {
  return mrAssignments.find((a) => a.client === client && a.slot === slot) || null;
}

async function renderModelMatrix() {
  const body = $("mrMatrixBody");
  if (!body) return;
  const span = MR_SLOT_ORDER.length + 2;
  try { mrClientNames = await (await fetch("/api/client-names")).json(); }
  catch { body.innerHTML = `<tr><td colspan="${span}" class="empty">couldn't load clients</td></tr>`; return; }
  if (!mrClientNames.length) {
    body.innerHTML = `<tr><td colspan="${span}" class="empty">No clients registered yet.</td></tr>`;
    return;
  }
  body.innerHTML = mrClientNames.map((name) => {
    const cells = MR_SLOT_ORDER.map((slot) => {
      const role = (mrSlots[slot] || {}).role || "llm";
      const current = mrCurrentAssignment(name, slot);
      const opts = mrRegistry.filter((m) => m.role === role);
      const optsHtml = [`<option value="">—</option>`].concat(opts.map((m) =>
        `<option value="${escapeHtml(m.id)}"${current && current.model_id === m.id ? " selected" : ""}>${escapeHtml(m.id)}</option>`));
      let note = "";
      if (current && !current.in_registry) {
        note = `<div class="muted" style="color:#b00" title="running a model that is NOT in the registry">⚠ ${escapeHtml(current.model_id)} (un-approved)</div>`;
      } else if (current) {
        note = `<div class="muted" title="recorded by: ${escapeHtml(current.source || "?")}">${escapeHtml(current.source || "")}</div>`;
      }
      return `<td><select class="mr-pick" data-client="${escapeHtml(name)}" data-slot="${slot}"
        data-current="${current ? escapeHtml(current.model_id) : ""}">${optsHtml.join("")}</select>${note}</td>`;
    }).join("");
    return `<tr>
      <td><strong>${escapeHtml(name)}</strong></td>${cells}
      <td><button type="button" class="primary mr-apply" data-client="${escapeHtml(name)}">Apply</button>
        <div class="muted mr-status" data-client="${escapeHtml(name)}"></div></td>
    </tr>`;
  }).join("");
  body.querySelectorAll(".mr-apply").forEach((btn) => {
    btn.addEventListener("click", () => mrMatrixApply(btn.dataset.client));
  });
}

async function mrMatrixApply(client) {
  const selects = [...document.querySelectorAll(`.mr-pick[data-client="${CSS.escape(client)}"]`)];
  const changed = selects.filter((s) => s.value && s.value !== s.dataset.current);
  const statusEl = document.querySelector(`.mr-status[data-client="${CSS.escape(client)}"]`);
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
  await loadModelAssignments();
  await renderModelMatrix();
}

async function mrReconcile() {
  const panel = $("mrReconcilePanel");
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
    html += `<h4 style="color:#b00">⚠ ${unapproved.length} un-approved model(s) running (not in the registry)</h4><ul>`
      + unapproved.map((u) => `<li><strong>${escapeHtml(u.client)}</strong> · ${escapeHtml(u.slot)} → `
        + `<code>${escapeHtml(u.model_id)}</code> (${escapeHtml(u.provider || "?")})</li>`).join("") + `</ul>`;
  } else {
    html += `<p class="ok">✓ Every running model is in the registry.</p>`;
  }
  if (failed.length) {
    html += `<p class="muted">couldn't read: ${failed.map((r) => escapeHtml(r.client)).join(", ")}</p>`;
  }
  panel.innerHTML = html;
  await loadModelAssignments();
  await renderModelMatrix();
}

// -- wiring ------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  const b = $("mrAddBrowseBtn");
  if (b) b.addEventListener("click", mrBrowse);
  const man = $("mrAddManualBtn");
  if (man) man.addEventListener("click", mrApproveManual);
  const rec = $("mrReconcileBtn");
  if (rec) rec.addEventListener("click", mrReconcile);
});
