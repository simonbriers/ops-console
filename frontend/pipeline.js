// ===========================================================================
// Pipeline tab — a ComfyUI-style, per-clinic view of the whole model/comms
// stream. Pick a clinic; six lanes flow top-to-bottom on a FIXED GRID so every
// level lines up and all chatbot endpoints sit on the bottom row:
//
//   row: title · provider · model · voice/lang · endpoint     (pipes between)
//   lanes: LLM·text  LLM·voice  STT  TTS  E-mail  SMS
//
// Providers + approved models come from the vault (operator-global). The wiring
// is PER CLINIC, written to that instance's live config via PUT
// /api/clients/{name}/config (hot reload, no recreate). Keys stay in the vault.
//
// Each lane has a TEST button that runs the actual combination and shows what
// comes back — reply text for LLM, a playable clip per language for TTS.
// Node borders: grey unset · green set · amber needs-a-pick · red non-EU (the
// prod boot guard blocks it on a voice lane). Nodes are fixed, not draggable.
//
// TTS model sub-node is disabled until the agent gains a voice.tts.model field
// (+ deploy). Voice fields are catalog dropdowns (grouped by tier, from
// /api/voices) with a custom-id escape hatch and a "▶ all" audition panel that
// plays a sample of each voice via /api/pipeline/test; providers with no
// console catalog yet fall back to a free-text id box. Loads AFTER app.js;
// reuses $ / escapeHtml.
// ===========================================================================

// Canonical EU voice-provider set. Loaded at refresh from
// GET /api/eu-voice-providers, which mirrors the product's prod boot guard
// (validator.EU_VOICE_PROVIDERS); this literal is only a fallback if that fetch
// fails. Do NOT hand-edit the membership here (it had drifted to include a
// "whisper" the boot guard never allowed) — change validator.EU_VOICE_PROVIDERS.
let PL_EU = new Set(["gladia", "local", "mistral", "piper"]);

const PL_LANES = [
  { key: "llm_text",  title: "LLM · text",  kind: "model", role: "llm",
    providerField: "llm_provider",       modelField: "llm_model",
    providers: ["mistral", "nvidia", "openrouter", "zenmux", "ollama"] },
  { key: "llm_voice", title: "LLM · voice", kind: "model", role: "llm",
    providerField: "voice_llm_provider", modelField: "voice_llm_model",
    providers: ["mistral", "nvidia", "openrouter", "zenmux", "ollama"] },
  { key: "stt",       title: "STT",         kind: "model", role: "stt",
    providerField: "voice_stt_provider", modelField: "voice_stt_model",
    // Only Mistral (Voxtral) STT is wired in the agent + credentialed today.
    // Add gladia/whisper/google/nvidia here once make_stt() and a vault kind exist.
    providers: ["mistral"] },
  { key: "tts",       title: "TTS",         kind: "tts",   role: "tts",
    providerField: "voice_tts_provider", modelField: "voice_tts_model",
    voiceEs: "voice_tts_voice_es", voiceEn: "voice_tts_voice_en",
    providerEs: "voice_tts_provider_es", providerEn: "voice_tts_provider_en",
    providers: ["mistral", "google", "piper", "nvidia", "zenmux"] },
  { key: "email",     title: "E-mail",      kind: "comms",
    enabledField: "email_enabled", providerLabel: "SMTP" },
  { key: "sms",       title: "SMS",         kind: "comms",
    providerField: "sms_provider", enabledField: "sms_enabled", providerLabel: "Twilio" },
];

let plClients = [];
let plClient = "";
let plConfig = {};
let plDraft = {};
let plRegistry = [];
let plVoices = [];                        // catalog of selectable TTS voices (/api/voices)
let plVoiceCustom = { es: false, en: false };  // per-language: typing a custom id instead of picking
let plEuOnly = false;                     // "EU-resident only" filter for the voice picker
let plVoiceMem = {};                      // { provider: {es, en} } — remembers voices per TTS provider
let plSource = null;

// Sample lines spoken by the audition/preview (per language).
const PL_TTS_SAMPLE = {
  es: "Hola, le atiende la clínica dental. ¿En qué puedo ayudarle?",
  en: "Hello, you've reached the dental clinic. How can I help you today?",
};
// Short language code -> Google locale used by the voice catalog.
const PL_LOCALE = { es: "es-ES", en: "en-US" };

async function refreshPipelinePage() {
  // Canonical EU provider set from the backend (mirrors the boot guard); keeps
  // the fallback literal if the fetch fails. Replaces the old hardcoded PL_EU.
  try {
    const eu = await (await fetch("/api/eu-voice-providers")).json();
    if (eu && Array.isArray(eu.providers) && eu.providers.length) PL_EU = new Set(eu.providers);
  } catch { /* keep the fallback set */ }
  // Canonical per-role provider lists (backend/providers.py via /api/providers)
  // fill each lane's provider dropdown; keep the PL_LANES fallback on failure.
  try {
    const byRole = await (await fetch("/api/providers")).json();
    for (const lane of PL_LANES) {
      const names = lane.role && byRole[lane.role] ? byRole[lane.role].map((p) => p.name) : [];
      if (names.length) lane.providers = names;
    }
  } catch { /* keep the fallback provider lists */ }
  try { plClients = await (await fetch("/api/client-names")).json(); } catch { plClients = []; }
  try { plRegistry = await (await fetch("/api/models/registry")).json(); } catch { plRegistry = []; }
  // Approved voices — the operator allow-list curated in the Catalog tab
  // (already carries provider/lang/gender/tier/label/eu_resident). The Pipeline
  // only SELECTS from these, so a non-approved (e.g. non-EU) voice can't be set.
  try { plVoices = await (await fetch("/api/voices/registry")).json(); } catch { plVoices = []; }
  const sel = $("plClientSelect");
  if (sel) {
    const prev = sel.value || plClient;
    sel.innerHTML = plClients.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
    if (prev && plClients.includes(prev)) sel.value = prev;
    plClient = sel.value;
  }
  if (plClient) await plLoad(); else plRenderGraph();
}

async function plLoad() {
  plClient = $("plClientSelect").value;
  const status = $("plStatus");
  if (!plClient) return;
  status.textContent = "loading…";
  let data;
  try {
    const r = await fetch(`/api/clients/${encodeURIComponent(plClient)}/config`);
    data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.status);
  } catch (e) { status.textContent = "load failed: " + e.message; return; }
  plSource = data.source;
  if (data.source === "api") {
    plConfig = data.config || {};
    plDraft = { ...plConfig };
    status.textContent = `loaded${data.frozen ? " · FROZEN" : ""}`;
  } else {
    plConfig = {}; plDraft = {};
    status.textContent = data.source === "ssh"
      ? "admin API unreachable — can't wire this clinic (read-only file)."
      : (data.error || "instance unreachable");
  }
  plRenderGraph();
}

function plBorder(state) {
  return { unset: "#c2c2c2", set: "#22a06b", warn: "#d98a00", bad: "#dd3333" }[state] || "#c2c2c2";
}
function plNode(inner, state, extra) {
  return `<div style="border:2px solid ${plBorder(state)};border-radius:8px;padding:.4rem .5rem;`
    + `background:var(--card,#fff);height:100%;box-sizing:border-box;${extra || ""}">${inner}</div>`;
}
function plPipe() {
  return `<div style="height:100%;min-height:16px;display:flex;justify-content:center">`
    + `<div style="width:2px;background:#c2c2c2"></div></div>`;
}
function plModelsFor(role, provider) {
  return plRegistry.filter((m) => m.role === role && (!provider || m.provider === provider));
}

// Providers valid for a role = the distinct providers the operator approved for
// that role in the Catalog (registry), plus whatever is currently live on the
// clinic (so an in-use provider always shows even if not yet approved).
function plProvidersFor(role, current) {
  const s = new Set(plRegistry.filter((m) => m.role === role).map((m) => m.provider).filter(Boolean));
  if (current) s.add(current);
  return Array.from(s).sort();
}

// Returns {provider, model, voice, endpoint} html for one lane.
function plLaneNodes(lane) {
  const v = (f) => (plDraft[f] === undefined || plDraft[f] === null ? "" : String(plDraft[f]));
  const isVoiceLane = lane.key === "stt" || lane.key === "tts";
  let provider, model, voice;

  if (lane.kind === "comms") {
    const on = !!plDraft[lane.enabledField];
    provider = plNode(`<div class="muted" style="font-size:.85em">provider</div><strong>${escapeHtml(lane.providerLabel)}</strong>`
      + `<div class="muted" style="font-size:.8em">${on ? "enabled ✓" : "disabled"}</div>`, on ? "set" : "unset");
    model = plPipe();
    voice = plPipe();
  } else if (lane.kind === "tts") {
    // TTS is configured PER LANGUAGE: each of ES / EN has its own provider AND
    // its own voice (voice.tts.provider_es/_en + voice_es/_en). Exactly one
    // voice per language, so there's never ambiguity about "which voice wins".
    const provs = lane.providers || [];
    const provSel = (field, curP) => {
      const o = [`<option value="">—</option>`].concat(provs.map((p) =>
        `<option value="${escapeHtml(p)}"${curP === p ? " selected" : ""}>${escapeHtml(p)}</option>`));
      const nonEu = curP && !PL_EU.has(curP);
      return `<select class="pl-field" data-field="${field}" data-lane="tts" style="width:100%">${o.join("")}</select>`
        + (nonEu ? `<div style="color:#dd3333;font-size:.7em">not EU</div>` : "");
    };
    const provEs = v("voice_tts_provider_es"), provEn = v("voice_tts_provider_en");
    const anyNonEu = (provEs && !PL_EU.has(provEs)) || (provEn && !PL_EU.has(provEn));
    provider = plNode(`<div class="muted" style="font-size:.85em">provider per language</div>`
      + `<div style="font-size:.76em;margin-top:.15rem"><strong>ES</strong> ${provSel("voice_tts_provider_es", provEs)}</div>`
      + `<div style="font-size:.76em;margin-top:.25rem"><strong>EN</strong> ${provSel("voice_tts_provider_en", provEn)}</div>`,
      (provEs || provEn) ? (anyNonEu ? "bad" : "set") : "unset");
    model = plNode(`<div class="muted" style="font-size:.85em">model</div>`
      + `<div class="muted" style="font-size:.78em">voice-only — the voice is the pick</div>`, "set");
    const esV = v("voice_tts_voice_es"), enV = v("voice_tts_voice_en");
    voice = plNode(`<div class="muted" style="font-size:.85em">voice per language</div>`
      + plVoiceLangRow("es", provEs, esV)
      + plVoiceLangRow("en", provEn, enV),
      (esV || enV) ? "set" : "warn");
  } else {
    const prov = v(lane.providerField);
    const provList = plProvidersFor(lane.role, prov);
    const nonEu = isVoiceLane && prov && !PL_EU.has(prov);
    const pstate = !prov ? "unset" : (nonEu ? "bad" : "set");
    const opts = [`<option value="">—</option>`].concat(provList.map((p) =>
      `<option value="${escapeHtml(p)}"${prov === p ? " selected" : ""}>${escapeHtml(p)}</option>`));
    provider = plNode(`<div class="muted" style="font-size:.85em">provider</div>`
      + `<select class="pl-field" data-field="${lane.providerField}" data-lane="${lane.key}" style="width:100%">${opts.join("")}</select>`
      + (nonEu ? `<div style="color:#dd3333;font-size:.78em">not EU — prod boot guard blocks this</div>` : ""), pstate);
    const cur = v(lane.modelField);
    const models = plModelsFor(lane.role, prov).filter((m) => m.id);
    const inList = models.some((m) => m.id === cur);
    const mstate = !prov ? "unset" : (cur ? (inList ? "set" : "warn") : "warn");
    let inner = `<div class="muted" style="font-size:.85em">model</div>`;
    if (!prov) {
      inner += `<select disabled style="width:100%"><option>pick a provider first</option></select>`;
    } else if (!models.length) {
      inner += `<select disabled style="width:100%"><option>none approved — approve in Catalog</option></select>`;
      if (cur) inner += `<div style="color:#d98a00;font-size:.78em">running: ${escapeHtml(cur)}</div>`;
    } else {
      const opts2 = [`<option value="">—</option>`]
        .concat(models.map((m) => `<option value="${escapeHtml(m.id)}"${cur === m.id ? " selected" : ""}>${escapeHtml(m.id)}</option>`));
      if (cur && !inList) opts2.push(`<option value="${escapeHtml(cur)}" selected>${escapeHtml(cur)} (un-approved)</option>`);
      inner += `<select class="pl-field" data-field="${lane.modelField}" data-lane="${lane.key}" style="width:100%">${opts2.join("")}</select>`;
      if (cur && !inList) inner += `<div style="color:#d98a00;font-size:.78em">not in the approved list</div>`;
    }
    model = plNode(inner, mstate);
    voice = plPipe();
  }

  let controls;
  if (lane.role === "stt") {
    controls = `<div style="margin-top:.3rem;display:flex;flex-direction:column;gap:.2rem;align-items:center">`
      + `<button type="button" class="pl-stt-conn" data-lane="${lane.key}">Test connection</button>`
      + `<button type="button" class="pl-stt-rec" data-lane="${lane.key}">Record</button>`
      + `<div class="muted" style="font-size:.72em">read aloud: “Quiero reservar una cita.”</div>`
      + `</div>`;
  } else {
    controls = `<button type="button" class="pl-test" data-lane="${lane.key}" style="margin-top:.3rem">Test</button>`;
  }
  const endpoint = plNode(`<div style="text-align:center">`
    + `<strong>→ ${escapeHtml(lane.title)}</strong><div class="muted" style="font-size:.78em">chatbot</div>`
    + controls
    + `<div class="pl-test-result" data-lane="${lane.key}" style="margin-top:.3rem;font-size:.82em"></div>`
    + `</div>`, "set", "background:#f3f4f6");

  return { provider, model, voice, endpoint };
}

// Catalog voices for a provider + short language code (es/en), via /api/voices.
function plVoicesFor(provider, lang) {
  const loc = PL_LOCALE[lang] || lang;
  return plVoices.filter((v) => (!provider || v.provider === provider) && v.lang === loc
    && (!plEuOnly || v.eu_resident));
}

// Short language code (es/en) from a voice id or locale, for the preview sample.
function plShortLang(voiceOrLocale) {
  return String(voiceOrLocale || "").toLowerCase().startsWith("es") ? "es" : "en";
}

// One language's row in the TTS voice cell: the current voice for that language
// (flagged if it isn't an approved voice of that language's provider) + a
// Choose… button that opens the picker scoped to that provider + language.
function plVoiceLangRow(lang, provider, cur) {
  const label = lang.toUpperCase();
  const provTxt = provider ? escapeHtml(provider) : "—";
  let disp;
  if (!provider) disp = `<span class="muted">pick a provider ↑</span>`;
  else if (!cur) disp = `<span class="muted">—</span>`;
  else {
    const hit = plVoices.find((x) => x.id === cur && x.provider === provider);
    disp = escapeHtml(cur) + (hit ? "" : ` <span style="color:#d98a00" title="not an approved ${provTxt} voice">· not approved</span>`);
  }
  return `<div style="font-size:.76em;margin:.2rem 0"><strong>${label}</strong> <span class="muted">${provTxt}</span> ${disp}`
    + (provider ? ` <button type="button" class="pl-voice-choose" data-lang="${lang}" style="font-size:.68em">Choose…</button>` : "")
    + `</div>`;
}

// The TTS voice node: compact ES/EN summary + a "Choose voice…" button (opens
// the cascade). Providers with no catalog fall back to free-text id boxes.
function plVoiceNodeInner(prov, lane, es, en) {
  const provider = prov || "google";
  const catalog = plVoices.filter((v) => v.provider === provider);
  let html = `<div class="muted" style="font-size:.85em">voice per language</div>`;
  if (!catalog.length) {
    html += `<div style="font-size:.8em;margin-top:.15rem"><span class="muted">ES </span>`
      + `<input class="pl-field" data-field="${lane.voiceEs}" value="${escapeHtml(es)}" placeholder="voice id" style="width:100%"/></div>`
      + `<div style="font-size:.8em;margin-top:.15rem"><span class="muted">EN </span>`
      + `<input class="pl-field" data-field="${lane.voiceEn}" value="${escapeHtml(en)}" placeholder="voice id" style="width:100%"/></div>`;
    return html;
  }
  const label = (id) => {
    if (!id) return `<span class="muted">—</span>`;
    const hit = plVoices.find((v) => v.id === id);
    let tag = "";
    if (hit && hit.provider !== provider) {
      tag = ` <span style="color:#dd3333" title="a ${escapeHtml(hit.provider)} voice, not ${escapeHtml(provider)} — re-pick">· wrong provider</span>`;
    } else if (!hit) {
      tag = ` <span style="color:#d98a00" title="not an approved ${escapeHtml(provider)} voice">· not approved</span>`;
    }
    return escapeHtml(id) + tag;
  };
  html += `<div style="font-size:.78em;margin:.15rem 0"><strong>ES</strong> ${label(es)}</div>`
    + `<div style="font-size:.78em;margin:.15rem 0"><strong>EN</strong> ${label(en)}</div>`
    + `<button type="button" class="pl-voice-choose" style="font-size:.72em;margin-top:.25rem">▾ Choose voice…</button>`;
  return html;
}

// The voice picker for ONE language (lang = "es"/"en"), rendered into the TTS
// lane's result area. Pure SELECTION from the approved allow-list, scoped to
// THAT language's provider (voice.tts.provider_<lang>) and to voices that fit
// the language (matching locale, or multilingual "multi"). Apply sets that
// language's voice. Curation/EU-residency live in the Catalog.
async function plVoiceCascade(lang) {
  const out = document.querySelector('.pl-test-result[data-lane="tts"]');
  if (!out) return;
  try { plVoices = await (await fetch("/api/voices/registry")).json(); } catch (e) { /* keep cache */ }
  const provider = plDraft[`voice_tts_provider_${lang}`] || "";
  const langName = lang === "es" ? "Spanish" : "English";
  if (!provider) {
    out.innerHTML = `<div class="down" style="font-size:.8em">Pick a ${escapeHtml(langName)} provider first.</div>`;
    return;
  }
  // approved voices for THIS language's provider, appropriate for the language
  // (matching locale prefix, or multilingual "multi" voices like Mistral's).
  const pool = plVoices.filter((v) => v.provider === provider
    && (String(v.lang || "").toLowerCase().startsWith(lang) || v.lang === "multi"));
  const uniq = (a) => Array.from(new Set(a)).filter((x) => x !== undefined && x !== null && x !== "");
  const tiers = uniq(pool.map((v) => v.tier));
  const genders = uniq(pool.map((v) => v.gender));

  out.innerHTML =
    `<div style="text-align:left;border-top:1px solid #ddd;margin-top:.3rem;padding-top:.3rem">`
    + `<div style="display:flex;justify-content:space-between;align-items:center;gap:.4rem">`
    + `<strong style="font-size:.8em">${escapeHtml(langName)} voice · ${escapeHtml(provider)}</strong>`
    + `<button type="button" class="plc-refresh" title="Reload approved voices" style="font-size:.72em">↻</button></div>`
    + `<div style="display:flex;gap:.5rem;margin-top:.3rem;flex-wrap:wrap">`
    + `<label style="font-size:.72em">Quality <select class="plc-tier"><option value="">All</option>`
    +   tiers.map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("") + `</select></label>`
    + `<label style="font-size:.72em">Gender <select class="plc-gender"><option value="">All</option>`
    +   genders.map((g) => `<option value="${escapeHtml(g)}">${escapeHtml(g)}</option>`).join("") + `</select></label></div>`
    + `<div style="margin-top:.3rem"><select class="plc-voice" size="6" style="width:100%"></select></div>`
    + `<div style="display:flex;gap:.4rem;align-items:center;margin-top:.35rem">`
    + `<button type="button" class="plc-preview" style="font-size:.72em">▶ Preview</button>`
    + `<span class="plc-slot" style="flex:1"></span></div>`
    + `<button type="button" class="plc-apply" style="width:100%;font-size:.72em;margin-top:.35rem">Set as the ${escapeHtml(langName)} voice</button>`
    + `<div class="plc-empty" style="font-size:.72em;color:#d98a00;margin-top:.3rem"></div></div>`;

  const $q = (s) => out.querySelector(s);
  const tierSel = $q(".plc-tier"), genSel = $q(".plc-gender"), voiceSel = $q(".plc-voice");

  function fillVoices() {
    const t = tierSel.value, g = genSel.value;
    const list = pool.filter((v) => (!t || v.tier === t) && (!g || v.gender === g))
      .sort((a, b) => String(a.tier).localeCompare(b.tier) || String(a.id).localeCompare(b.id));
    const byTier = {};
    list.forEach((v) => { (byTier[v.tier] = byTier[v.tier] || []).push(v); });
    voiceSel.innerHTML = Object.keys(byTier).map((tier) =>
      `<optgroup label="${escapeHtml(tier)}">`
      + byTier[tier].map((v) => `<option value="${escapeHtml(v.id)}">${escapeHtml(v.lang)} · ${escapeHtml(v.label)}${v.default ? " ★" : ""}</option>`).join("")
      + `</optgroup>`).join("");
    $q(".plc-empty").textContent = pool.length
      ? (list.length ? "" : "No voices match these filters.")
      : `No approved ${provider} voices for ${langName} — approve some in Catalog → TTS voice allow-list.`;
  }
  tierSel.addEventListener("change", fillVoices);
  genSel.addEventListener("change", fillVoices);
  $q(".plc-refresh").addEventListener("click", () => plVoiceCascade(lang));

  $q(".plc-preview").addEventListener("click", async () => {
    const vid = voiceSel.value, slot = $q(".plc-slot"), btn = $q(".plc-preview");
    if (!vid) return;
    const oldt = btn.textContent; btn.disabled = true; btn.textContent = "…"; slot.textContent = "";
    try {
      const r = await fetch("/api/pipeline/test", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "tts", provider, model: plDraft[`voice_tts_model_${lang}`] || "", voice: vid, language: lang, text: PL_TTS_SAMPLE[lang] }) });
      const d = await r.json();
      slot.innerHTML = d.ok && d.audio
        ? `<audio controls autoplay src="${d.audio}" style="height:24px;vertical-align:middle"></audio>`
        : `<span class="down" style="font-size:.72em">✘ ${escapeHtml(d.message || d.detail || "failed")}</span>`;
    } catch (e) { slot.innerHTML = `<span class="down" style="font-size:.72em">✘ ${escapeHtml(String(e))}</span>`; }
    btn.disabled = false; btn.textContent = oldt;
  });
  $q(".plc-apply").addEventListener("click", () => {
    const vid = voiceSel.value;
    if (!vid) return;
    plDraft[lang === "es" ? "voice_tts_voice_es" : "voice_tts_voice_en"] = vid;
    plUpdatePending();
    plRenderGraph();
  });

  fillVoices();
}

// One language's voice control: a catalog <select> grouped by tier when the
// provider has voices, else (or on toggle) a free-text id box. ★ marks the
// fleet default; "✎ id" toggles custom entry; "▶ all" opens the audition list.
function plVoiceControl(provider, lang, field, cur) {
  const list = plVoicesFor(provider, lang);
  const label = lang.toUpperCase();
  const custom = plVoiceCustom[lang] || (!!provider && !list.length);
  const auditionBtn = list.length
    ? `<button type="button" class="pl-voice-audition" data-lang="${lang}" title="Hear every ${label} voice" style="font-size:.72em;padding:0 .35rem">▶ all (${list.length})</button>`
    : "";
  if (custom) {
    const back = list.length
      ? `<a href="#" class="pl-voice-custom" data-lang="${lang}" style="font-size:.72em">▤ list</a>` : "";
    return `<div style="font-size:.8em;margin-top:.2rem"><span class="muted">${label}</span>`
      + `<input class="pl-field" data-field="${field}" value="${escapeHtml(cur)}" placeholder="voice id" style="width:100%"/>`
      + `<div style="display:flex;gap:.5rem;align-items:center">${back}${auditionBtn}</div></div>`;
  }
  const tiers = [];
  list.forEach((v) => { if (!tiers.includes(v.tier)) tiers.push(v.tier); });
  const inList = list.some((v) => v.id === cur);
  let opts = `<option value="">—</option>`;
  tiers.forEach((t) => {
    opts += `<optgroup label="${escapeHtml(t)}">`;
    list.filter((v) => v.tier === t).forEach((v) => {
      opts += `<option value="${escapeHtml(v.id)}"${v.id === cur ? " selected" : ""}>${escapeHtml(v.label)}${v.default ? " ★" : ""}</option>`;
    });
    opts += `</optgroup>`;
  });
  if (cur && !inList) opts += `<option value="${escapeHtml(cur)}" selected>${escapeHtml(cur)} (custom)</option>`;
  return `<div style="font-size:.8em;margin-top:.2rem"><span class="muted">${label}</span>`
    + `<select class="pl-field" data-field="${field}" style="width:100%">${opts}</select>`
    + `<div style="display:flex;gap:.5rem;align-items:center">`
    + `<a href="#" class="pl-voice-custom" data-lang="${lang}" style="font-size:.72em">✎ id</a>${auditionBtn}</div></div>`;
}

function plRenderGraph() {
  const host = $("plGraph");
  if (!host) return;
  if (!plClient) { host.innerHTML = `<p class="empty">Pick a clinic.</p>`; return; }
  if (plSource !== "api") { host.innerHTML = `<p class="empty">Can't wire this clinic (instance unreachable / read-only).</p>`; return; }
  const cols = PL_LANES.length;
  const nodes = PL_LANES.map(plLaneNodes);
  const cell = (col, row, inner) => `<div style="grid-column:${col};grid-row:${row}">${inner}</div>`;
  let g = `<div style="display:grid;grid-template-columns:repeat(${cols},minmax(160px,1fr));column-gap:14px;align-items:stretch">`;
  // row 1 titles · 2 pipe · 3 provider · 4 pipe · 5 model · 6 pipe · 7 voice · 8 pipe · 9 endpoint
  PL_LANES.forEach((lane, i) => {
    g += cell(i + 1, 1, `<div style="text-align:center;font-weight:600;margin-bottom:.2rem">${escapeHtml(lane.title)}</div>`);
    g += cell(i + 1, 2, plPipe());
    g += cell(i + 1, 3, nodes[i].provider);
    g += cell(i + 1, 4, plPipe());
    g += cell(i + 1, 5, nodes[i].model);
    g += cell(i + 1, 6, plPipe());
    g += cell(i + 1, 7, nodes[i].voice);
    g += cell(i + 1, 8, plPipe());
    g += cell(i + 1, 9, nodes[i].endpoint);
  });
  g += `</div>`;
  host.innerHTML = g;

  host.querySelectorAll(".pl-field").forEach((el) => {
    const ev = el.tagName === "SELECT" ? "change" : "input";
    el.addEventListener(ev, () => {
      const field = el.dataset.field, val = el.value;
      plDraft[field] = val;
      // Per-language TTS provider changed: drop the voice set for that language
      // if it isn't an approved voice of the NEW provider, so a Google voice
      // never lingers under Mistral (one provider + one voice per language).
      if (field === "voice_tts_provider_es" || field === "voice_tts_provider_en") {
        const vf = field.endsWith("_es") ? "voice_tts_voice_es" : "voice_tts_voice_en";
        const curV = plDraft[vf];
        if (curV && !plVoices.find((x) => x.id === curV && x.provider === val)) plDraft[vf] = "";
      }
      if (el.dataset.lane) plRenderGraph();   // provider change re-filters models
      plUpdatePending();
    });
  });
  host.querySelectorAll(".pl-test").forEach((btn) =>
    btn.addEventListener("click", () => plTest(btn.dataset.lane)));
  host.querySelectorAll(".pl-voice-choose").forEach((btn) =>
    btn.addEventListener("click", () => plVoiceCascade(btn.dataset.lang)));
  host.querySelectorAll(".pl-stt-conn").forEach((btn) =>
    btn.addEventListener("click", () => plSttConn(btn.dataset.lane)));
  host.querySelectorAll(".pl-stt-rec").forEach((btn) =>
    btn.addEventListener("click", () => plSttRecord(btn.dataset.lane, btn)));
  plUpdatePending();
}

// -- STT: connection check + record-and-transcribe ---------------------------
let plRec = { mr: null, chunks: [], stream: null };

async function plSttConn(laneKey) {
  const lane = PL_LANES.find((l) => l.key === laneKey);
  const out = document.querySelector(`.pl-test-result[data-lane="${CSS.escape(laneKey)}"]`);
  const provider = plDraft[lane.providerField] || "";
  if (!provider) { out.textContent = "pick a provider first"; return; }
  out.textContent = "testing connection…";
  try {
    const r = await fetch("/api/pipeline/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "stt_conn", provider }) });
    const d = await r.json();
    out.innerHTML = d.ok
      ? `<span class="ok">✔ ${escapeHtml(d.message || "connected")}</span>`
      : `<span class="down">✘ ${escapeHtml(d.message || d.detail || "failed")}</span>`;
  } catch (e) { out.innerHTML = `<span class="down">✘ ${escapeHtml(String(e))}</span>`; }
}

async function plSttRecord(laneKey, btn) {
  const lane = PL_LANES.find((l) => l.key === laneKey);
  const out = document.querySelector(`.pl-test-result[data-lane="${CSS.escape(laneKey)}"]`);
  // stop if already recording
  if (plRec.mr && plRec.mr.state === "recording") { plRec.mr.stop(); return; }
  const provider = plDraft[lane.providerField] || "";
  if (!provider) { out.textContent = "pick a provider first"; return; }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch (e) { out.textContent = "mic unavailable: " + e; return; }
  plRec.stream = stream; plRec.chunks = [];
  const mr = new MediaRecorder(stream);
  plRec.mr = mr;
  mr.ondataavailable = (e) => { if (e.data && e.data.size) plRec.chunks.push(e.data); };
  mr.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    btn.textContent = "Record";
    out.textContent = "transcribing…";
    try {
      const blob = new Blob(plRec.chunks, { type: mr.mimeType || "audio/webm" });
      const wav = await plBlobToWavB64(blob);
      const r = await fetch("/api/pipeline/test", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "stt", provider, audio: wav }) });
      const d = await r.json();
      out.innerHTML = d.ok
        ? `<span class="ok">heard:</span> “${escapeHtml(d.transcript || "")}”${d.ms ? ` <span class="muted">(${d.ms} ms)</span>` : ""}`
        : `<span class="down">✘ ${escapeHtml(d.message || d.detail || "failed")}</span>`;
    } catch (e) { out.innerHTML = `<span class="down">✘ ${escapeHtml(String(e))}</span>`; }
  };
  mr.start();
  btn.textContent = "Stop";
  out.textContent = "recording… read the sentence, then Stop.";
}

// Decode the mic blob and re-encode as 16 kHz mono 16-bit WAV (a base64 data URL).
async function plBlobToWavB64(blob) {
  const buf = await blob.arrayBuffer();
  const AC = window.AudioContext || window.webkitAudioContext;
  const ctx = new AC();
  const audio = await ctx.decodeAudioData(buf);
  const src = audio.getChannelData(0);
  const target = 16000, ratio = audio.sampleRate / target;
  const n = Math.max(1, Math.floor(src.length / ratio));
  const pcm = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    let s = src[Math.floor(i * ratio)] || 0;
    s = Math.max(-1, Math.min(1, s));
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  try { ctx.close(); } catch (e) { /* ignore */ }
  const bytes = new Uint8Array(44 + pcm.length * 2);
  const dv = new DataView(bytes.buffer);
  const wr = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
  wr(0, "RIFF"); dv.setUint32(4, 36 + pcm.length * 2, true); wr(8, "WAVE"); wr(12, "fmt ");
  dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, target, true); dv.setUint32(28, target * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  wr(36, "data"); dv.setUint32(40, pcm.length * 2, true);
  new Int16Array(bytes.buffer, 44).set(pcm);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return "data:audio/wav;base64," + btoa(bin);
}

// -- test a lane's current combination ---------------------------------------
async function plTest(laneKey) {
  const lane = PL_LANES.find((l) => l.key === laneKey);
  const out = document.querySelector(`.pl-test-result[data-lane="${CSS.escape(laneKey)}"]`);
  if (!lane || !out) return;
  const v = (f) => (plDraft[f] === undefined || plDraft[f] === null ? "" : String(plDraft[f]));

  if (lane.kind === "comms") { out.textContent = "email/SMS: test from the Credentials tab"; return; }
  const provider = v(lane.providerField);
  if (!provider && lane.role !== "tts") { out.textContent = "pick a provider first"; return; }

  if (lane.role === "llm") {
    const model = v(lane.modelField);
    if (!model) { out.textContent = "pick a model first"; return; }
    out.textContent = "testing…";
    try {
      const r = await fetch("/api/pipeline/test", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "llm", provider, model }) });
      const d = await r.json();
      out.innerHTML = d.ok
        ? `<span class="ok">✔ ${escapeHtml((d.reply || "").slice(0, 80))}</span>${d.ms ? ` <span class="muted">(${d.ms} ms)</span>` : ""}`
        : `<span class="down">✘ ${escapeHtml(d.message || d.detail || "failed")}</span>`;
    } catch (e) { out.innerHTML = `<span class="down">✘ ${escapeHtml(String(e))}</span>`; }
    return;
  }

  if (lane.role === "stt") {
    out.textContent = "STT test needs an audio sample — coming with the on-instance test (deploy).";
    return;
  }

  if (lane.role === "tts") {
    // Each language is tested with ITS OWN provider + model (multi-model).
    const langs = [
      { code: "es", voice: v("voice_tts_voice_es"), provider: v("voice_tts_provider_es"), model: v("voice_tts_model_es"), text: "Hola, le atiende la clínica dental." },
      { code: "en", voice: v("voice_tts_voice_en"), provider: v("voice_tts_provider_en"), model: v("voice_tts_model_en"), text: "Hello, you've reached the dental clinic." },
    ];
    out.innerHTML = "synthesizing…";
    const parts = [];
    for (const L of langs) {
      if (!L.provider) { parts.push(`<div class="muted">${L.code.toUpperCase()}: no provider set</div>`); continue; }
      if (!L.voice) { parts.push(`<div class="muted">${L.code.toUpperCase()}: no voice set</div>`); continue; }
      const vhit = plVoices.find((x) => x.id === L.voice);
      if (vhit && vhit.provider !== L.provider) {
        parts.push(`<div class="down">${L.code.toUpperCase()}: ✘ “${escapeHtml(L.voice)}” is a ${escapeHtml(vhit.provider)} voice — choose a ${escapeHtml(L.provider)} voice</div>`);
        continue;
      }
      try {
        const r = await fetch("/api/pipeline/test", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ kind: "tts", provider: L.provider, model: L.model, voice: L.voice, language: L.code, text: L.text }) });
        const d = await r.json();
        parts.push(d.ok && d.audio
          ? `<div>${L.code.toUpperCase()}: <audio controls src="${d.audio}" style="height:26px;vertical-align:middle"></audio></div>`
          : `<div class="down">${L.code.toUpperCase()}: ✘ ${escapeHtml(d.message || d.detail || "failed")}</div>`);
      } catch (e) { parts.push(`<div class="down">${L.code.toUpperCase()}: ✘ ${escapeHtml(String(e))}</div>`); }
    }
    out.innerHTML = parts.join("");
    return;
  }
}

// -- audition: hear every catalog voice for one language, on demand ----------
// Renders a grouped list into the TTS lane's result area; each row has a ▶ to
// synthesize+play that single voice (via the same /api/pipeline/test route the
// Test button uses) and a "use" button to select it. Synthesis is per-click, so
// nothing is generated until you actually ask to hear it (saves TTS quota).
async function plAudition(lang) {
  const out = document.querySelector('.pl-test-result[data-lane="tts"]');
  if (!out) return;
  const provider = plDraft["voice_tts_provider"] || "google";  // catalog is Google-only today
  const list = plVoicesFor(provider, lang);
  if (!list.length) { out.textContent = "no catalog voices for this provider"; return; }
  const tiers = [];
  list.forEach((v) => { if (!tiers.includes(v.tier)) tiers.push(v.tier); });
  let html = `<div style="text-align:left;max-height:300px;overflow:auto;border-top:1px solid #ddd;margin-top:.3rem;padding-top:.3rem">`
    + `<div style="font-weight:600;font-size:.8em">${lang.toUpperCase()} — audition (${list.length}) · ★ = current fleet default</div>`;
  tiers.forEach((t) => {
    html += `<div class="muted" style="font-size:.72em;margin-top:.3rem">${escapeHtml(t)}</div>`;
    list.filter((v) => v.tier === t).forEach((v) => {
      html += `<div style="display:flex;align-items:center;gap:.35rem;margin:.12rem 0">`
        + `<button type="button" class="pl-aud-play" data-voice="${escapeHtml(v.id)}" data-lang="${lang}" style="font-size:.72em">▶</button>`
        + `<button type="button" class="pl-aud-pick" data-voice="${escapeHtml(v.id)}" data-lang="${lang}" title="Select this voice" style="font-size:.72em">use</button>`
        + `<span style="font-size:.78em">${escapeHtml(v.label)}${v.default ? " ★" : ""}</span>`
        + `<span class="pl-aud-slot" data-voice="${escapeHtml(v.id)}"></span></div>`;
    });
  });
  html += `</div>`;
  out.innerHTML = html;
  out.querySelectorAll(".pl-aud-play").forEach((b) =>
    b.addEventListener("click", () => plAuditionOne(b.dataset.voice, b.dataset.lang, b)));
  out.querySelectorAll(".pl-aud-pick").forEach((b) =>
    b.addEventListener("click", () => {
      plDraft[b.dataset.lang === "es" ? "voice_tts_voice_es" : "voice_tts_voice_en"] = b.dataset.voice;
      plVoiceCustom[b.dataset.lang] = false;
      plRenderGraph();
      plUpdatePending();
    }));
}

async function plAuditionOne(voice, lang, btn) {
  const provider = plDraft["voice_tts_provider"] || "google";
  const model = plDraft["voice_tts_model"] || "";
  const slot = document.querySelector(`.pl-aud-slot[data-voice="${CSS.escape(voice)}"]`);
  const old = btn.textContent;
  btn.textContent = "…"; btn.disabled = true;
  try {
    const r = await fetch("/api/pipeline/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "tts", provider, model, voice, language: lang, text: PL_TTS_SAMPLE[lang] }) });
    const d = await r.json();
    if (slot) {
      slot.innerHTML = d.ok && d.audio
        ? `<audio controls autoplay src="${d.audio}" style="height:24px;vertical-align:middle"></audio>`
        : `<span class="down" style="font-size:.72em">✘ ${escapeHtml(d.message || d.detail || "failed")}</span>`;
    }
  } catch (e) {
    if (slot) slot.innerHTML = `<span class="down" style="font-size:.72em">✘ ${escapeHtml(String(e))}</span>`;
  }
  btn.textContent = old; btn.disabled = false;
}

function plDiff() {
  const changed = {};
  const fields = new Set();
  PL_LANES.forEach((l) => [l.providerField, l.modelField, l.voiceEs, l.voiceEn, l.providerEs, l.providerEn].forEach((f) => { if (f) fields.add(f); }));
  fields.forEach((f) => {
    const now = plDraft[f] === undefined ? "" : String(plDraft[f]);
    const was = plConfig[f] === undefined || plConfig[f] === null ? "" : String(plConfig[f]);
    if (now !== was) changed[f] = plDraft[f];
  });
  return changed;
}

function plUpdatePending() {
  const changed = plDiff();
  const n = Object.keys(changed).length;
  const el = $("plPending");
  if (el) el.textContent = n ? `${n} pending change(s): ${Object.keys(changed).join(", ")}` : "no pending changes";
  const btn = $("plApplyBtn");
  if (btn) btn.disabled = !n;
}

async function plApply() {
  const changed = plDiff();
  if (!Object.keys(changed).length) return;
  const status = $("plApplyStatus");
  if (!confirm(`Apply to ${plClient}:\n\n${Object.entries(changed).map(([k, x]) => `${k} → ${x}`).join("\n")}\n\n`
      + `Writes to the instance's live config (hot reload). Make sure the provider's key is in the vault/assigned.`)) return;
  status.textContent = "applying…";
  try {
    const r = await fetch(`/api/clients/${encodeURIComponent(plClient)}/config`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ fields: changed }) });
    const data = await r.json();
    if (!r.ok) { const d = data.detail; throw new Error(typeof d === "string" ? d : JSON.stringify(d)); }
    status.textContent = data.mismatches && data.mismatches.length
      ? `saved, but echo mismatch: ${data.mismatches.map((m) => m.field).join(", ")}`
      : `✔ applied: ${(data.written || []).join(", ")}`;
    await plLoad();
  } catch (e) { status.textContent = "✘ " + e.message; }
}

document.addEventListener("DOMContentLoaded", () => {
  const sel = $("plClientSelect"); if (sel) sel.addEventListener("change", plLoad);
  const rl = $("plReloadBtn"); if (rl) rl.addEventListener("click", plLoad);
  const ap = $("plApplyBtn"); if (ap) ap.addEventListener("click", plApply);
});
