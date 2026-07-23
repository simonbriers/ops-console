// ===========================================================================
// Catalog tab — the operator allow-list, organized ROLE-FIRST exactly like the
// pipeline lanes. Per role (LLM / STT / TTS): which providers are valid and,
// per provider, which models are allowed. The Pipeline selects from exactly this.
//
// Provider KEYS are NOT entered here — all credential paste/replace/test lives
// on the Credentials tab (the vault). This tab only reads the vault indirectly:
// its "browse live models" calls /api/provider-models, which authenticates with
// whatever key is already in the vault. (The per-provider key boxes that used to
// sit at the top of this tab were removed 2026-07-23 as a duplicate of
// Credentials — one home for a provider's key.)
//
// Voice-only providers (google/piper TTS) have no model — you approve the
// PROVIDER for the role and the voice is the real pick in the pipeline.
// Reuses existing endpoints: /api/models/registry(+/remove),
// /api/provider-models, /api/voices/*. Loads after app.js; provides
// refreshCatalogPage() for switchPage().
// ===========================================================================

// per role: which providers can be browsed for models, and which are voice-only.
// browse/voiceOnly are filled at load from GET /api/providers (the single
// source, backend/providers.py); the literals here are only a fallback. Titles
// stay local. Do NOT hand-edit provider membership here — edit providers.py.
let CAT_ROLES = [
  { role: "llm", title: "LLM — text & voice",
    browse: ["mistral", "zenmux", "openrouter", "nvidia", "ollama"], voiceOnly: [] },
  { role: "stt", title: "STT",
    browse: ["mistral"], voiceOnly: [] },
  { role: "tts", title: "TTS",
    browse: ["mistral", "zenmux", "nvidia"], voiceOnly: ["google", "piper"] },
];

let catRegistry = [];
const catBrowseCache = {};   // `${role}:${provider}` -> live models

// TTS voice allow-list (the curation layer between provider-approval and the
// Pipeline's per-clinic voice selection). Providers that carry voices.
// Filled at load from /api/providers (the TTS providers); literal is a fallback.
let CAT_VOICE_PROVIDERS = ["google", "mistral", "nvidia", "piper", "zenmux"];
let catVoiceProvider = "google";
let catVoiceEuOnly = true;     // safe default: browse EU-resident voices first
let catVoiceFilter = "";
let catVoiceCache = [];
let catVoiceMsg = "";     // error/status line for the voice browse (e.g. live Mistral fetch)
// Providers whose voices are account-specific and browsed LIVE from the
// provider API (like models) rather than the static snapshot catalog.
const CAT_VOICE_LIVE = new Set(["mistral"]);

async function refreshCatalogPage() {
  await Promise.all([catLoadProviders(), catLoadRegistry()]);
  catRenderRoles();
}
async function catLoadRegistry() { try { catRegistry = await (await fetch("/api/models/registry")).json(); } catch { catRegistry = []; } }
// Fill each role's browse/voiceOnly (and the TTS voice-provider list) from the
// canonical /api/providers source; keep the fallback literals on failure.
async function catLoadProviders() {
  try {
    const byRole = await (await fetch("/api/providers")).json();
    for (const r of CAT_ROLES) {
      const list = byRole[r.role] || [];
      if (list.length) {
        r.browse = list.filter((p) => !p.voice_only).map((p) => p.name);
        r.voiceOnly = list.filter((p) => p.voice_only).map((p) => p.name);
      }
    }
    const tts = byRole.tts || [];
    if (tts.length) CAT_VOICE_PROVIDERS = tts.map((p) => p.name);
  } catch { /* keep the fallback literals */ }
}

// ── roles → providers → allowed models ──────────────────────────────────────
function catRenderRoles() {
  const host = $("catRolesBody");
  if (!host) return;
  host.innerHTML = CAT_ROLES.map((r) => {
    const approved = catRegistry.filter((m) => m.role === r.role);
    const byProv = {};
    approved.forEach((m) => { (byProv[m.provider] = byProv[m.provider] || []).push(m); });
    const provBlocks = Object.keys(byProv).sort().map((prov) => {
      const items = byProv[prov].map((m) => m.id
        ? `<li><code>${escapeHtml(m.id)}</code>${m.label && m.label !== m.id ? ` <span class="muted">${escapeHtml(m.label)}</span>` : ""}
             <button type="button" class="cat-rm" data-id="${escapeHtml(m.id)}" data-provider="${escapeHtml(prov)}">×</button></li>`
        : `<li><span class="muted">(voice-only — no model)</span>
             <button type="button" class="cat-rm" data-id="" data-provider="${escapeHtml(prov)}">×</button></li>`);
      return `<div style="margin:.2rem 0"><strong>${escapeHtml(prov)}</strong>
        <ul style="margin:.1rem 0 .3rem 1rem;padding:0">${items.join("")}</ul></div>`;
    }).join("") || `<p class="muted">no providers approved for ${escapeHtml(r.role)} yet</p>`;

    const browseOpts = r.browse.map((p) => `<option value="${p}">${p}</option>`).join("");
    const voiceOnlyBtns = (r.voiceOnly || []).map((p) =>
      `<button type="button" class="cat-approve-voiceonly" data-role="${r.role}" data-provider="${p}">approve ${p} (voice-only)</button>`).join(" ");
    return `<section class="ob-panel">
      <h3>${escapeHtml(r.title)}</h3>
      <div style="max-height:220px;overflow:auto;border:1px solid var(--border,#ddd);border-radius:6px;padding:.3rem .6rem">${provBlocks}</div>
      <div class="ob-form" style="margin-top:.4rem">
        <label>Provider <select class="cat-add-prov" data-role="${r.role}">${browseOpts}</select></label>
        <button type="button" class="cat-browse" data-role="${r.role}">Browse live models…</button>
        ${voiceOnlyBtns}
        <span class="muted cat-add-status" data-role="${r.role}"></span>
      </div>
      <div class="cat-browse-box" data-role="${r.role}"></div>
      ${r.role === "tts" ? `<div style="margin-top:.6rem;border-top:1px solid var(--border,#ddd);padding-top:.5rem">
        <h4 style="margin:.2rem 0">TTS voice allow-list <span class="muted" style="font-weight:normal">— the Pipeline may only pick these</span></h4>
        <div id="catVoicesBox"></div></div>` : ""}
    </section>`;
  }).join("");
  host.querySelectorAll(".cat-rm").forEach((b) => b.addEventListener("click", () => catRemove(b.dataset.id, b.dataset.provider)));
  host.querySelectorAll(".cat-browse").forEach((b) => b.addEventListener("click", () => catBrowse(b.dataset.role)));
  host.querySelectorAll(".cat-approve-voiceonly").forEach((b) =>
    b.addEventListener("click", () => catApprove(b.dataset.role, b.dataset.provider, "", b.dataset.provider)));
  catRenderVoices();
}

// ── TTS voice allow-list: browse the catalog → approve into the registry ─────
function catRenderVoices() {
  const box = document.getElementById("catVoicesBox");
  if (!box) return;
  box.innerHTML = `<div class="ob-form" style="margin:.2rem 0;display:flex;gap:.6rem;flex-wrap:wrap;align-items:center">
      <label>Provider <select id="catVoiceProv">${CAT_VOICE_PROVIDERS.map((p) =>
        `<option value="${p}"${p === catVoiceProvider ? " selected" : ""}>${escapeHtml(p)}</option>`).join("")}</select></label>
      <label style="display:inline-flex;align-items:center;gap:.3rem"><input type="checkbox" id="catVoiceEu"${catVoiceEuOnly ? " checked" : ""}/> EU-resident only</label>
      <input id="catVoiceFilter" placeholder="filter…" value="${escapeHtml(catVoiceFilter)}" style="min-width:12rem" />
    </div>
    <ul id="catVoiceList" style="list-style:none;margin:0;padding:0;max-height:240px;overflow:auto;border:1px solid var(--border,#ccc);border-radius:4px"></ul>`;
  document.getElementById("catVoiceProv").addEventListener("change", (e) => { catVoiceProvider = e.target.value; catLoadVoiceList(); });
  document.getElementById("catVoiceEu").addEventListener("change", (e) => { catVoiceEuOnly = e.target.checked; catLoadVoiceList(); });
  document.getElementById("catVoiceFilter").addEventListener("input", (e) => { catVoiceFilter = e.target.value; catRenderVoiceList(); });
  catLoadVoiceList();
}

async function catLoadVoiceList() {
  const ul = document.getElementById("catVoiceList");
  if (ul) ul.innerHTML = `<li class="muted" style="padding:.4rem">loading…</li>`;
  catVoiceMsg = "";
  try {
    if (CAT_VOICE_LIVE.has(catVoiceProvider)) {
      const r = await (await fetch(`/api/provider-voices?provider=${encodeURIComponent(catVoiceProvider)}`)).json();
      catVoiceCache = (r && r.voices) || [];
      if (r && r.ok === false) catVoiceMsg = r.message || "live voice list failed";
    } else {
      catVoiceCache = await (await fetch(
        `/api/voices/catalog?provider=${encodeURIComponent(catVoiceProvider)}&eu_only=${catVoiceEuOnly ? "true" : "false"}`)).json();
    }
  } catch (e) { catVoiceCache = []; catVoiceMsg = String(e); }
  catRenderVoiceList();
}

function catRenderVoiceList() {
  const ul = document.getElementById("catVoiceList");
  if (!ul) return;
  if (catVoiceMsg) {
    ul.innerHTML = `<li class="down" style="padding:.4rem">✘ ${escapeHtml(catVoiceMsg)}</li>`;
    return;
  }
  const q = (catVoiceFilter || "").trim().toLowerCase();
  const shown = q ? catVoiceCache.filter((v) => (v.id + " " + (v.label || "")).toLowerCase().includes(q)) : catVoiceCache;
  ul.innerHTML = shown.slice(0, 500).map((v) => {
    const eu = v.eu_resident ? "" : ` <span style="color:#d98a00" title="not EU-resident">· non-EU</span>`;
    return `<li style="padding:.2rem .4rem;border-bottom:1px solid var(--border,#eee);display:flex;justify-content:space-between;gap:.5rem;align-items:center">
      <span><code>${escapeHtml(v.id)}</code> <span class="muted">${escapeHtml(v.label || "")}</span>${eu}</span>
      ${v.approved
        ? `<span><span class="ok">approved ✓</span> <button type="button" class="cat-voice-rm" data-id="${escapeHtml(v.id)}" data-prov="${escapeHtml(v.provider)}">×</button></span>`
        : `<button type="button" class="cat-voice-approve" data-id="${escapeHtml(v.id)}" data-prov="${escapeHtml(v.provider)}">Approve</button>`}</li>`;
  }).join("") || `<li class="muted" style="padding:.4rem">no voices</li>`;
  ul.querySelectorAll(".cat-voice-approve").forEach((b) => b.addEventListener("click", () => catApproveVoice(b.dataset.id, b.dataset.prov)));
  ul.querySelectorAll(".cat-voice-rm").forEach((b) => b.addEventListener("click", () => catRemoveVoice(b.dataset.id, b.dataset.prov)));
}

async function catApproveVoice(id, provider) {
  const v = catVoiceCache.find((x) => x.id === id) || { id, provider };
  await fetch("/api/voices/registry", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, provider, lang: v.lang, gender: v.gender, tier: v.tier,
                           label: v.label, eu_resident: v.eu_resident }) });
  await catLoadVoiceList();
}
async function catRemoveVoice(id, provider) {
  if (!confirm(`Remove ${id} from the approved voices? Clinics can no longer be set to it.`)) return;
  await fetch("/api/voices/registry/remove", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, provider }) });
  await catLoadVoiceList();
}

async function catRemove(id, provider) {
  if (!confirm(`Remove ${id || `(${provider} voice-only)`} from the approved list?`)) return;
  await fetch("/api/models/registry/remove", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: id || "", provider }) });
  await refreshCatalogPage();
}

async function catBrowse(role) {
  const provider = document.querySelector(`.cat-add-prov[data-role="${CSS.escape(role)}"]`).value;
  const box = document.querySelector(`.cat-browse-box[data-role="${CSS.escape(role)}"]`);
  box.innerHTML = `<p class="muted">loading ${escapeHtml(provider)} models…</p>`;
  let d;
  try { d = await (await fetch(`/api/provider-models?provider=${encodeURIComponent(provider)}`)).json(); }
  catch (e) { box.innerHTML = `<p class="empty">${escapeHtml(String(e))}</p>`; return; }
  if (!d.ok) { box.innerHTML = `<p class="empty">${escapeHtml(d.message || "failed")}</p>`; return; }
  catBrowseCache[`${role}:${provider}`] = d.models || [];
  box.innerHTML = `<input class="cat-filter" data-role="${role}" data-provider="${provider}" placeholder="filter ${(d.models || []).length}…"
      style="width:100%;box-sizing:border-box;margin:.3rem 0" oninput="catRenderBrowse('${role}','${provider}')" />
    <ul class="cat-list" data-role="${role}" data-provider="${provider}" style="list-style:none;margin:0;padding:0;max-height:200px;overflow:auto;border:1px solid var(--border,#ccc);border-radius:4px"></ul>`;
  catRenderBrowse(role, provider);
}

function catRenderBrowse(role, provider) {
  const ul = document.querySelector(`.cat-list[data-role="${CSS.escape(role)}"][data-provider="${CSS.escape(provider)}"]`);
  if (!ul) return;
  const filterEl = document.querySelector(`.cat-filter[data-role="${CSS.escape(role)}"][data-provider="${CSS.escape(provider)}"]`);
  const q = (filterEl ? filterEl.value : "").trim().toLowerCase();
  let all = catBrowseCache[`${role}:${provider}`] || [];
  if (role === "llm") { const wm = all.filter((m) => (m.output_modalities || []).length); if (wm.length) all = all.filter((m) => (m.output_modalities || []).includes("text")); }
  const approved = new Set(catRegistry.filter((m) => m.role === role && m.provider === provider).map((m) => m.id));
  const shown = q ? all.filter((m) => (m.id + " " + (m.label || "")).toLowerCase().includes(q)) : all;
  ul.innerHTML = shown.slice(0, 400).map((m) => {
    const has = approved.has(m.id);
    const ctx = m.context_length ? ` · ${Number(m.context_length).toLocaleString()} ctx` : "";
    return `<li style="padding:.2rem .4rem;border-bottom:1px solid var(--border,#eee);display:flex;justify-content:space-between;gap:.5rem;align-items:center">
      <span><code>${escapeHtml(m.id)}</code><span class="muted">${escapeHtml((m.label && m.label !== m.id ? m.label : "") + ctx)}</span></span>
      ${has ? `<span class="muted">approved ✓</span>`
            : `<button type="button" class="cat-approve-one" data-role="${role}" data-provider="${provider}" data-id="${encodeURIComponent(m.id)}" data-label="${escapeHtml(m.label || m.id)}">Approve</button>`}</li>`;
  }).join("") || `<li class="muted" style="padding:.4rem">no match</li>`;
  ul.querySelectorAll(".cat-approve-one").forEach((b) =>
    b.addEventListener("click", () => catApprove(b.dataset.role, b.dataset.provider, decodeURIComponent(b.dataset.id), b.dataset.label)));
}

async function catApprove(role, provider, id, label) {
  const status = document.querySelector(`.cat-add-status[data-role="${CSS.escape(role)}"]`);
  const r = await fetch("/api/models/registry", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: id || "", provider, role, label: label || id || provider }) });
  const d = await r.json().catch(() => ({}));
  if (status) status.textContent = r.ok ? `✔ approved ${id || `${provider} (voice-only)`}` : `✘ ${d.detail || "failed"}`;
  await catLoadRegistry();
  catRenderRoles();
}
