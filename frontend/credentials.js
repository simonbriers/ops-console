// ===========================================================================
// Credentials tab — vault v2 (docs/TOKEN_ECONOMY_PLAN.md Phase 1).
// First file of the app.js split: loaded AFTER app.js (see index.html),
// reuses its $ / show / hide / escapeHtml globals, and provides the
// globals app.js's switchPage() calls for this page:
//   refreshVault(), populateVaultClientSelect()
// Everything vault-specific lives here: the role-grouped set table
// (reveal / edit / delete / rotation re-apply), the client × role
// assignment matrix with per-row swap+apply, reconcile-from-servers,
// the add/edit form, and the bulk apply/import block the onboarding
// stepper also depends on (.vault-pick checkboxes).
// ===========================================================================

const VAULT_ROLES = ["llm", "stt", "tts", "email", "sms"];
const VAULT_ROLE_LABELS = { llm: "LLM", stt: "STT", tts: "TTS", email: "E-mail", sms: "SMS" };

const VAULT_FIELDS = {
  "mistral": ["MISTRAL_API_KEY"],
  "openrouter": ["OPENROUTER_API_KEY"],
  "nvidia": ["NVIDIA_API_KEY"],
  "smtp": ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_USE_TLS"],
  "twilio": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"],
  "file/google_tts": [],
};

let vaultSets = [];          // latest redacted /api/vault/sets payload
let vaultAssignments = [];   // latest /api/vault/assignments payload
let vaultClientNames = [];   // latest /api/client-names payload
let vaultActiveByClient = {}; // client -> {role: active provider} (from last reconcile)

// -- add/edit form -----------------------------------------------------------

function renderVaultFields() {
  const kind = $("vaultKindSelect").value;
  const el = $("vaultFields");
  if (kind === "file/google_tts") {
    el.innerHTML = `<label>google_tts.json file <input type="file" id="vaultFileInput" accept=".json" /></label>`;
    return;
  }
  el.innerHTML = (VAULT_FIELDS[kind] || []).map((k) =>
    `<label>${escapeHtml(k)} <input name="v_${escapeHtml(k)}"
       type="${/PASSWORD|KEY|TOKEN/.test(k) ? "password" : "text"}" /></label>`).join("");
}

function resetVaultForm() {
  const form = $("vaultForm");
  form.reset();
  form.querySelector('input[name="id"]').value = "";
  renderVaultFields();
  $("vaultFormStatus").textContent = "";
}

async function submitVaultForm(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  const kind = fd.get("kind");
  const status = $("vaultFormStatus");
  const editingId = fd.get("id") || null;
  const body = {
    name: fd.get("name"), kind, values: {}, id: editingId,
    owner: fd.get("owner") || null, tier: fd.get("tier") || null,
    notes: fd.get("notes") ?? null,
  };
  for (const [k, v] of fd.entries()) {
    if (k.startsWith("v_") && String(v).trim()) body.values[k.slice(2)] = v;
  }
  if (kind === "file/google_tts") {
    const file = ($("vaultFileInput") || {}).files?.[0];
    if (!file && !editingId) { status.textContent = "pick the google_tts.json file first"; return; }
    if (file) {
      body.content_b64 = btoa(String.fromCharCode(...new Uint8Array(await file.arrayBuffer())));
    }
  }
  status.textContent = "saving…";
  const r = await fetch("/api/vault/sets", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { status.textContent = data.detail || "failed"; return; }
  status.textContent = "saved";
  resetVaultForm();
  await refreshVault();
  // Rotation: an EDITED set that clients run on → offer one-click re-apply,
  // so "rotate a key" is edit + confirm instead of a hunt across clients.
  const assigned = data.assigned_to || [];
  if (editingId && assigned.length
      && confirm(`This set is applied to: ${assigned.join(", ")}.\n\n`
        + `Re-apply it now to all ${assigned.length} client(s)? `
        + `(merges .env + tests + RECREATES each container)`)) {
    for (const clientName of assigned) {
      status.textContent = `re-applying to ${clientName}…`;
      const rr = await fetch(`/api/clients/${encodeURIComponent(clientName)}/apply-credentials`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ set_ids: [editingId] }) });
      const rd = await rr.json().catch(() => ({}));
      if (!rd.ok) {
        status.textContent = `re-apply FAILED on ${clientName}: ${rd.error || rd.detail || "?"} — stopped.`;
        return;
      }
    }
    status.textContent = `re-applied to ${assigned.length} client(s) — containers recreated.`;
    await refreshVault();
  }
}

// -- vault table (role-grouped) ----------------------------------------------

function vaultSourceBadges(s) {
  const owner = s.owner === "client"
    ? `<span class="vault-badge vault-badge-client" title="client-owned key (BYOK)">BYOK</span>` : "";
  const tier = `<span class="vault-badge vault-badge-${escapeHtml(s.tier || "paid")}">${escapeHtml(s.tier || "paid")}</span>`;
  return tier + owner;
}

async function refreshVault() {
  const r = await fetch("/api/vault/sets");
  vaultSets = await r.json();
  const body = $("vaultTableBody");
  if (!vaultSets.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty">No sets yet — add one below or import from a client.</td></tr>`;
    await renderVaultMatrix();
    return;
  }
  const rows = [];
  for (const role of VAULT_ROLES) {
    const inRole = vaultSets.filter((s) => (s.role || "llm") === role);
    if (!inRole.length) continue;
    rows.push(`<tr class="vault-role-row"><td colspan="8">${escapeHtml(VAULT_ROLE_LABELS[role])}</td></tr>`);
    for (const s of inRole) {
      const keys = s.has_file ? "(file)" : escapeHtml(Object.keys(s.values || {}).join(", "));
      const pair = (s.key_count || 0) > 1
        ? ` <span class="vault-badge" title="comma-separated key pair — primary + 429-fallback (resolve_llm key_index 1/2); each key meters under its own alias">×${s.key_count} keys</span>`
        : "";
      const alias = (s.aliases || []).length
        ? (pair + " " + s.aliases.map((a) =>
            `<code class="vault-alias" title="api_key_alias in /admin/metrics by_key">${escapeHtml(a)}</code>`).join(" "))
        : "";
      const assigned = (s.assigned_to || []).length
        ? escapeHtml(s.assigned_to.join(", "))
        : `<span class="muted">—</span>`;
      const notes = s.notes ? `<div class="muted vault-notes">${escapeHtml(s.notes)}</div>` : "";
      rows.push(`<tr data-id="${escapeHtml(s.id)}">
        <td><input type="checkbox" class="vault-pick" value="${escapeHtml(s.id)}" /></td>
        <td>${escapeHtml(s.name)}${notes}</td>
        <td>${escapeHtml(s.provider || s.kind)}</td>
        <td>${vaultSourceBadges(s)}</td>
        <td class="muted vault-keys-cell">${keys}${alias}</td>
        <td class="muted">${assigned}</td>
        <td class="muted">${escapeHtml((s.updated || "").slice(0, 16))}</td>
        <td class="vault-actions">
          <button type="button" class="vault-reveal" data-id="${escapeHtml(s.id)}">Reveal</button>
          <button type="button" class="vault-edit" data-id="${escapeHtml(s.id)}">Edit</button>
          <button type="button" class="danger vault-del" data-id="${escapeHtml(s.id)}">Delete</button>
        </td>
      </tr>`);
    }
  }
  body.innerHTML = rows.join("");
  body.querySelectorAll(".vault-del").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this credential set? Clients keep whatever was already applied.")) return;
      await fetch(`/api/vault/sets/${btn.dataset.id}`, { method: "DELETE" });
      refreshVault();
    });
  });
  body.querySelectorAll(".vault-reveal").forEach((btn) => {
    btn.addEventListener("click", () => vaultToggleReveal(btn.dataset.id, btn));
  });
  body.querySelectorAll(".vault-edit").forEach((btn) => {
    btn.addEventListener("click", () => vaultEditSet(btn.dataset.id));
  });
  await renderVaultMatrix();
}

async function vaultToggleReveal(setId, btn) {
  const row = $("vaultTableBody").querySelector(`tr[data-id="${CSS.escape(setId)}"]`);
  if (!row) return;
  const cell = row.querySelector(".vault-keys-cell");
  if (btn.dataset.open === "1") {           // hide again
    btn.dataset.open = "";
    btn.textContent = "Reveal";
    await refreshVault();
    return;
  }
  const r = await fetch(`/api/vault/sets/${encodeURIComponent(setId)}/reveal`, { method: "POST" });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || !data.ok) { cell.innerHTML = `<span class="down">${escapeHtml(data.detail || "reveal failed")}</span>`; return; }
  const s = data.set;
  cell.innerHTML = Object.entries(s.values || {}).map(([k, v]) =>
    `<div class="vault-revealed"><span class="kv-key">${escapeHtml(k)}</span>
     <code>${escapeHtml(v)}</code></div>`).join("")
    + (s.has_file ? `<div class="muted">(+ google_tts.json stored server-side)</div>` : "");
  btn.dataset.open = "1";
  btn.textContent = "Hide";
}

async function vaultEditSet(setId) {
  const r = await fetch(`/api/vault/sets/${encodeURIComponent(setId)}/reveal`, { method: "POST" });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || !data.ok) return;
  const s = data.set;
  const form = $("vaultForm");
  form.querySelector('input[name="id"]').value = s.id;
  form.querySelector('input[name="name"]').value = s.name || "";
  $("vaultKindSelect").value = s.kind;
  $("vaultOwnerSelect").value = s.owner || "ours";
  $("vaultTierSelect").value = s.tier || "paid";
  form.querySelector('input[name="notes"]').value = s.notes || "";
  renderVaultFields();
  for (const [k, v] of Object.entries(s.values || {})) {
    const input = form.querySelector(`input[name="v_${CSS.escape(k)}"]`);
    if (input) input.value = v;
  }
  $("vaultFormStatus").textContent =
    `editing "${s.name}" — Save re-stores it${(s.assigned_to || []).length
      ? ` and offers re-apply to: ${s.assigned_to.join(", ")}` : ""}`;
  form.scrollIntoView({ behavior: "smooth", block: "start" });
}

// -- client × role matrix ----------------------------------------------------

function vaultCurrentAssignment(clientName, role) {
  return vaultAssignments.find((a) => a.client === clientName && a.role === role) || null;
}

async function renderVaultMatrix() {
  const body = $("vaultMatrixBody");
  if (!body) return;
  try {
    vaultClientNames = await (await fetch("/api/client-names")).json();
    vaultAssignments = await (await fetch("/api/vault/assignments")).json();
  } catch { body.innerHTML = `<tr><td colspan="7" class="empty">couldn't load assignments</td></tr>`; return; }
  if (!vaultClientNames.length) {
    body.innerHTML = `<tr><td colspan="7" class="empty">No clients registered yet.</td></tr>`;
    return;
  }
  body.innerHTML = vaultClientNames.map((name) => {
    const cells = VAULT_ROLES.map((role) => {
      const current = vaultCurrentAssignment(name, role);
      // a set is offered for every role its kind can serve (mistral: llm+stt+tts)
      const options = vaultSets.filter((s) => (s.roles || [s.role || "llm"]).includes(role));
      // active provider from the last reconcile (piper = local engine → no
      // credential will ever be pickable; google without its file set yet →
      // credential missing from the vault). Shown as a hint line whenever
      // it isn't already represented by the assigned set.
      const act = (vaultActiveByClient[name] || {})[role];
      const currentSet = current ? vaultSets.find((s) => s.id === current.set_id) : null;
      const actHint = act && (!currentSet || (currentSet.provider || "") !== act)
        ? `<div class="muted vault-cell-src" title="active provider (from /admin/config). 'piper' is a local engine — no credential exists to assign; anything else means its credential isn't in the vault yet or isn't assigned.">active: ${escapeHtml(act)}</div>`
        : "";
      if (!options.length && !current) {
        return `<td class="muted vault-cell-empty">${actHint || "—"}</td>`;
      }
      const opts = [`<option value="">—</option>`].concat(options.map((s) =>
        `<option value="${escapeHtml(s.id)}"${current && current.set_id === s.id ? " selected" : ""}>
           ${escapeHtml(s.name)}</option>`));
      const src = current
        ? `<div class="muted vault-cell-src" title="recorded by: ${escapeHtml(current.source || "?")}">
             ${escapeHtml(current.source || "")}${current.set_deleted ? " ⚠ set deleted" : ""}</div>` : "";
      return `<td><select class="vault-matrix-pick" data-client="${escapeHtml(name)}"
        data-role="${role}" data-current="${current ? escapeHtml(current.set_id) : ""}">
        ${opts.join("")}</select>${src}${actHint}</td>`;
    }).join("");
    return `<tr>
      <td><strong>${escapeHtml(name)}</strong></td>${cells}
      <td class="vault-actions">
        <button type="button" class="primary vault-matrix-apply" data-client="${escapeHtml(name)}">Apply</button>
        <button type="button" class="vault-matrix-record" data-client="${escapeHtml(name)}"
          title="Record the changed dropdowns as this client's assignment WITHOUT touching the server — no .env write, no container recreate. For file credentials (invisible to reconcile) and frozen clients.">Record only</button>
        <div class="muted vault-matrix-status" data-client="${escapeHtml(name)}"></div>
      </td>
    </tr>`;
  }).join("");
  body.querySelectorAll(".vault-matrix-apply").forEach((btn) => {
    btn.addEventListener("click", () => vaultMatrixApply(btn.dataset.client));
  });
  body.querySelectorAll(".vault-matrix-record").forEach((btn) => {
    btn.addEventListener("click", () => vaultMatrixRecord(btn.dataset.client));
  });
}

async function vaultMatrixRecord(clientName) {
  const selects = [...document.querySelectorAll(
    `.vault-matrix-pick[data-client="${CSS.escape(clientName)}"]`)];
  const changed = selects.filter((sel) => sel.value && sel.value !== sel.dataset.current);
  const statusEl = document.querySelector(
    `.vault-matrix-status[data-client="${CSS.escape(clientName)}"]`);
  if (!changed.length) { statusEl.textContent = "nothing changed"; return; }
  statusEl.textContent = "recording…";
  for (const sel of changed) {
    const r = await fetch("/api/vault/assignments", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_name: clientName, role: sel.dataset.role, set_id: sel.value }) });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      statusEl.textContent = `✘ ${data.detail || "record failed"}`;
      return;
    }
  }
  statusEl.textContent = `✔ recorded (server untouched)`;
  await refreshVault();
}

async function vaultMatrixApply(clientName) {
  const selects = [...document.querySelectorAll(
    `.vault-matrix-pick[data-client="${CSS.escape(clientName)}"]`)];
  const changed = selects.filter((sel) => sel.value && sel.value !== sel.dataset.current);
  const statusEl = document.querySelector(
    `.vault-matrix-status[data-client="${CSS.escape(clientName)}"]`);
  if (!changed.length) { statusEl.textContent = "nothing changed"; return; }
  const setNames = changed.map((sel) => sel.selectedOptions[0].textContent.trim());
  if (!confirm(`Apply to ${clientName}:\n\n${setNames.join("\n")}\n\n`
      + `This merges the client's .env, tests the credentials, and RECREATES its container.`)) return;
  statusEl.textContent = "applying + testing + recreating…";
  const r = await fetch(`/api/clients/${encodeURIComponent(clientName)}/apply-credentials`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ set_ids: changed.map((sel) => sel.value) }) });
  const data = await r.json().catch(() => ({}));
  const tests = (data.tests || []).map((t) => `${t.kind}:${t.ok ? "ok" : "FAIL"}`).join(" ");
  statusEl.textContent = data.ok
    ? `✔ applied — recreated. ${tests}`
    : `✘ ${data.error || data.detail || "failed"} ${tests}`;
  await refreshVault();   // re-renders the matrix with the new assignments
}

async function vaultReconcile() {
  const status = $("vaultReconcileStatus");
  status.textContent = "reading every client's .env over SSH…";
  const r = await fetch("/api/vault/reconcile", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { status.textContent = data.detail || "reconcile failed"; return; }
  vaultActiveByClient = {};
  const parts = (data.reports || []).map((rep) => {
    if (!rep.ok) return `${rep.client}: ✘ ${rep.error}`;
    vaultActiveByClient[rep.client] = rep.active || {};
    const m = rep.matched.length
      ? rep.matched.map((x) =>
          `${x.role}=${x.set_name}${x.active_provider ? " [active]" : ""}`).join(", ")
      : "no active matches";
    const sb = (rep.standby || []).length
      ? ` — standby: ${rep.standby.map((x) => `${x.role}:${x.set_name}`).join(", ")}`
      : "";
    const cl = (rep.cleared || []).length
      ? ` — cleared stale: ${rep.cleared.map((x) => `${x.role}:${x.set_name}`).join(", ")}`
      : "";
    const d = (rep.drift || []).length
      ? ` — drift: ${rep.drift.map((x) =>
          `${x.role}(${x.env_key}${x.alias ? " " + x.alias : ""}${x.active ? " ⚠ACTIVE" : ""})`).join(", ")}`
      : "";
    return `${rep.client}: ${m}${sb}${cl}${d}`;
  });
  status.innerHTML = parts.map((p) =>
    `<div class="${p.includes("drift") || p.includes("✘") ? "vault-drift" : ""}">${escapeHtml(p)}</div>`).join("");
  await refreshVault();
}

// -- bulk apply / import (also serves the onboarding stepper) ----------------

async function populateVaultClientSelect() {
  const sel = $("vaultClientSelect");
  if (!sel) return;
  const prev = sel.value;
  const names = await (await fetch("/api/client-names")).json().catch(() => []);
  sel.innerHTML = (names || []).map((n) =>
    `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
  if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

async function vaultImport() {
  const client = $("vaultClientSelect").value;
  const status = $("vaultApplyStatus");
  status.textContent = "importing…";
  const r = await fetch("/api/vault/import", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_name: client }) });
  const data = await r.json().catch(() => ({}));
  status.textContent = r.ok
    ? `imported ${data.created?.length ?? 0} new set(s)`
      + (data.matched?.length ? `, recognized ${data.matched.length} existing` : "")
      + ` from ${client}`
    : (data.detail || data.error || "import failed");
  refreshVault();
}

async function vaultApply() {
  const client = $("vaultClientSelect").value;
  const setIds = [...document.querySelectorAll(".vault-pick:checked")].map((i) => i.value);
  const status = $("vaultApplyStatus");
  if (!setIds.length) { status.textContent = "check at least one set in the table above"; return; }
  status.textContent = "applying + testing + recreating…";
  const r = await fetch(`/api/clients/${encodeURIComponent(client)}/apply-credentials`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ set_ids: setIds }) });
  const data = await r.json().catch(() => ({}));
  const tests = (data.tests || []).map((t) => `${t.kind}:${t.ok ? "ok" : "FAIL"}`).join(" ");
  status.textContent = data.ok
    ? `applied ${data.applied?.join(", ")} — container recreated. ${tests}`
    : `${data.error || data.detail || "failed"} ${tests}`;
  refreshVault();
}

// -- wiring ------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  $("vaultKindSelect").addEventListener("change", renderVaultFields);
  $("vaultForm").addEventListener("submit", submitVaultForm);
  $("vaultFormResetBtn").addEventListener("click", resetVaultForm);
  $("vaultImportBtn").addEventListener("click", vaultImport);
  $("vaultApplyBtn").addEventListener("click", vaultApply);
  $("vaultReconcileBtn").addEventListener("click", vaultReconcile);
  $("openLegacyCredsBtn").addEventListener("click", () => openCredsModal());
  renderVaultFields();
});
