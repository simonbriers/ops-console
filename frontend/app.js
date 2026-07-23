// ops-console dashboard — vanilla JS, no build step. Polls GET /api/clients
// and GET /api/hosts on a timer (interval pulled from GET /api/settings) and
// renders a client table + an infrastructure panel (VPS disk/mem gauges +
// every site discovered from the Caddyfile); clicking a client row opens a
// detail modal with the same data plus per-container resource usage.
// Add/Edit uses a second modal that POSTs/PUTs to /api/clients, with a
// "Fetch via SSH" button that calls /api/fetch-token so a secret never has
// to be hand-typed/copy-pasted anywhere in this UI.

let latestResults = [];
let latestHosts = [];
let pollTimer = null;
let editingName = null; // null => add mode

// -- credentials (.env copy) modal state ------------------------------------
// The product's own known env-var names (dental-clinic-agent's .env.example
// / env.clinica-valor) — used to pre-seed the destination table with every
// key a new deploy is likely to need, even before a source is loaded, so
// nothing gets missed just because it wasn't already set on the source.
// NOTE: MISTRAL_API_KEY / NVIDIA_API_KEY can each hold several
// comma-separated keys in ONE line for round-robin (see env_tool.py's
// test_mistral/test_nvidia) — there is no separate "_2" env var; an
// earlier version of this list wrongly assumed there was, which just
// produced a permanently-empty ghost row in the table below.
const KNOWN_ENV_KEYS = [
  "OLLAMALOCAL_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "ZENMUX_API_KEY",
  "MISTRAL_API_KEY", "ADMIN_PASSWORD", "CORS_ORIGINS",
  "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_USE_TLS", "SMTP_OVERRIDE_RECIPIENT",
  "ENV", "DOMAIN", "ACME_EMAIL", "LOG_LEVEL", "BACKUP_PASSPHRASE",
  "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
  "APP_CONTAINER_NAME", "COMPOSE_PROJECT_NAME",
];
// Which key(s) a "Test" click actually needs — some tests need more than
// one key from the table at once (Twilio, SMTP), so the button only shows
// on one representative row per group and pulls the rest live by name.
const CRED_TEST_KIND = {
  MISTRAL_API_KEY: "mistral",
  NVIDIA_API_KEY: "nvidia",
  OPENROUTER_API_KEY: "openrouter",
  ZENMUX_API_KEY: "zenmux",
  TWILIO_ACCOUNT_SID: "twilio", TWILIO_AUTH_TOKEN: "twilio",
  SMTP_HOST: "smtp", SMTP_USERNAME: "smtp", SMTP_PASSWORD: "smtp",
  ADMIN_PASSWORD: "admin_token",
};
let credsSourceEnv = {}; // key -> value, from the loaded source .env
let credsDestRows = []; // [{key, value}], in display order

// Deploy UI state — the one mutating action in ops-console. Lives outside
// applyDetail's normal render-from-scratch flow because applyDetail also
// runs on every background poll of an open modal (to keep it live) and we
// must never clobber a half-typed confirmation or an in-flight deploy just
// because a 60s poll tick happened to land at the wrong moment.
let deployUiState = { mode: "idle", name: null, result: null }; // idle | confirm | deploying | result

// Reseed UI state — same rationale as deployUiState: a destructive action
// with a type-the-name confirmation that a background poll must never wipe
// mid-type. `choice` is which reseed the operator picked at the confirm step
// ("conversations" | "full").
let reseedUiState = { mode: "idle", name: null, choice: null, result: null }; // idle | confirm | reseeding | result

const $ = (id) => document.getElementById(id);

// -- SVG gauges --------------------------------------------------------------

function gaugeColor(pct) {
  if (pct >= 90) return "#c0392b";
  if (pct >= 70) return "#b58900";
  return "#1a7f37";
}

function renderGauge(label, pct, subLabel) {
  const r = 38;
  const circumference = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(100, pct));
  const offset = circumference * (1 - clamped / 100);
  const color = gaugeColor(clamped);
  return `
    <div class="gauge">
      <svg width="96" height="96" viewBox="0 0 96 96">
        <circle cx="48" cy="48" r="${r}" stroke="#eee" stroke-width="10" fill="none" />
        <circle cx="48" cy="48" r="${r}" stroke="${color}" stroke-width="10" fill="none"
          stroke-dasharray="${circumference.toFixed(1)}" stroke-dashoffset="${offset.toFixed(1)}"
          stroke-linecap="round" transform="rotate(-90 48 48)" />
        <text x="48" y="54" text-anchor="middle" font-size="20" font-weight="700" fill="#1a1a1a">${clamped.toFixed(0)}%</text>
      </svg>
      <div class="gauge-label">${escapeHtml(label)}</div>
      <div class="gauge-sub">${escapeHtml(subLabel || "")}</div>
    </div>`;
}

function fmtBytes(n) {
  if (n == null) return "?";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

async function refreshHosts() {
  try {
    const resp = await fetch("/api/hosts");
    latestHosts = await resp.json();
    renderInfraPanel();
  } catch (e) {
    $("infraGauges").innerHTML = `<p class="muted">Failed to reach API: ${escapeHtml(String(e))}</p>`;
  }
}

function renderInfraPanel() {
  const gaugesEl = $("infraGauges");
  if (latestHosts.length === 0) {
    gaugesEl.innerHTML = `<p class="muted">No hosts configured yet — add one to "hosts" in clients.json.</p>`;
  } else {
    gaugesEl.innerHTML = latestHosts
      .map((h) => {
        const res = h.resources || {};
        if (!res.ok) {
          return `<div class="gauge"><div class="gauge-label">${escapeHtml(h.name)}</div><div class="gauge-sub">unknown — ${escapeHtml(res.error || "check failed")}</div></div>`;
        }
        const d = res.disk, m = res.memory;
        const diskSub = `${fmtBytes(d.used)} / ${fmtBytes(d.total)}`;
        const memSub = `${fmtBytes(m.used)} / ${fmtBytes(m.total)}`;
        return renderGauge(`${h.name} — Disk`, d.pct, diskSub) + renderGauge(`${h.name} — Memory`, m.pct, memSub);
      })
      .join("");
  }

  const tbody = $("sitesTableBody");
  const allSites = latestHosts.flatMap((h) => h.sites || []);
  if (allSites.length === 0) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty">No sites discovered yet.</td></tr>`;
  } else {
    tbody.innerHTML = allSites
      .map((s) => {
        const target = s.type === "proxy" ? `127.0.0.1:${s.port}` : s.type === "redirect" ? `redirect → ${escapeHtml(s.target)}` : "(unrecognized)";
        const clientPill = s.matched_client
          ? `<span class="pill-managed">${escapeHtml(s.matched_client)}</span>`
          : `<span class="pill-unmanaged">unmanaged</span>`;
        return `<tr><td>${escapeHtml(s.hostname)}</td><td>${escapeHtml(s.type)}</td><td>${target}</td><td>${clientPill}</td></tr>`;
      })
      .join("");
  }

  renderDiskBreakdown();
}

function renderDiskBreakdown() {
  const el = $("infraDiskBreakdown");
  const withBreakdown = latestHosts.filter((h) => {
    const res = h.resources || {};
    return res.ok && ((res.disk_breakdown && res.disk_breakdown.length) || res.docker_df);
  });
  if (withBreakdown.length === 0) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = withBreakdown
    .map((h) => {
      const res = h.resources;
      const rows = (res.disk_breakdown || [])
        .map((d) => `<tr><td>${escapeHtml(d.size)}</td><td>${escapeHtml(d.path)}</td></tr>`)
        .join("");
      const dockerBlock = res.docker_df
        ? `<h4>Docker's own accounting (images/containers/volumes/build cache)</h4><pre class="docker-df">${escapeHtml(res.docker_df)}</pre>`
        : "";
      return `
        <details class="disk-breakdown-details">
          <summary>What's using disk space on ${escapeHtml(h.name)}?</summary>
          ${rows ? `<table class="breakdown-table"><thead><tr><th>Size</th><th>Path</th></tr></thead><tbody>${rows}</tbody></table>` : ""}
          ${dockerBlock}
        </details>`;
    })
    .join("");
}

function statusDot(status) {
  const glyphs = { ok: "●", warning: "●", down: "●", unknown: "○" };
  return `<span class="dot ${status}">${glyphs[status] || "?"}</span>`;
}

function fmtHealth(h) {
  if (!h.up) return `DOWN${h.error ? " — " + escapeHtml(h.error) : ""}`;
  let s = `UP (${h.latency_ms}ms)`;
  if (h.voice_enabled) s += `, voice active: ${h.voice_active_sessions}`;
  return s;
}

function fmtVersion(v) {
  if (!v.ok) return `unknown${v.error ? " — " + escapeHtml(v.error) : ""}`;
  const behind = v.behind ? `, ${v.behind} behind origin/master` : ", up to date";
  return `${v.commit}${behind}`;
}

function fmtUsage(u, quota) {
  if (!u.ok) return `unknown${u.error ? " — " + escapeHtml(u.error) : ""}`;
  let s = `${u.chats} chats, ${u.total_tokens.toLocaleString()} tok`;
  if (quota) {
    const pct = Math.round((u.total_tokens / quota) * 100);
    s += ` (${pct}% of quota)`;
  }
  return s;
}

function fmtUptime(up) {
  if (!up || up.uptime_7d_pct == null) return `<span class="muted">no data yet</span>`;
  // band comes from the backend (history.uptime_band) — one threshold, shared
  // with the status dot and the detail modal, no re-thresholding here.
  const cls = { ok: "ok", warn: "warning", down: "down" }[up.uptime_band] || "muted";
  return `<span class="dot ${cls}">●</span> ${up.uptime_7d_pct}%`;
}

function fmtMinutesSaved(i) {
  if (!i || !i.ok) return `<span class="muted">—</span>`;
  const hours = (i.minutes_saved / 60).toFixed(1);
  return `${i.minutes_saved} min <span class="muted">(${hours}h)</span>`;
}

function fmtHM(minutes) {
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function renderTable() {
  const tbody = $("clientTableBody");
  if (latestResults.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">No clients configured yet — click "Add client" to start monitoring one.</td></tr>`;
    renderUpdatesPage();
    renderTestsPage();
    return;
  }
  tbody.innerHTML = latestResults
    .map((r) => {
      const time = (r.checked_at || "").split("T")[1] || r.checked_at;
      return `<tr data-name="${escapeHtml(r.name)}">
        <td>${statusDot(r.status)}</td>
        <td>${escapeHtml(r.name)}</td>
        <td>${fmtHealth(r.health)}</td>
        <td>${fmtVersion(r.version)}</td>
        <td>${fmtUsage(r.usage, r.quota)}</td>
        <td>${fmtUptime(r.uptime)}</td>
        <td>${fmtMinutesSaved(r.interactions)}</td>
        <td>${time}</td>
      </tr>`;
    })
    .join("");
  [...tbody.querySelectorAll("tr[data-name]")].forEach((row) => {
    row.addEventListener("click", () => openDetail(row.dataset.name));
  });
  renderImpactPanel();
  renderUpdatesPage();
  renderTestsPage();
}

function renderImpactPanel() {
  const el = $("impactCards");
  if (latestResults.length === 0) {
    el.innerHTML = `<p class="muted">No clients configured yet.</p>`;
    return;
  }
  let totalMinutes = 0, bookings = 0, reschedules = 0, cancellations = 0, callbacks = 0;
  let anyInteractionsOk = false;
  let totalCost = 0, anyCostConfigured = false, allCostOk = true;
  latestResults.forEach((r) => {
    const i = r.interactions;
    if (i && i.ok) {
      anyInteractionsOk = true;
      totalMinutes += i.minutes_saved || 0;
      bookings += i.bookings || 0;
      reschedules += i.reschedules || 0;
      cancellations += i.cancellations || 0;
      callbacks += i.callbacks || 0;
    }
    const c = r.cost;
    if (c && c.ok && c.configured) {
      anyCostConfigured = true;
      totalCost += c.estimated_eur || 0;
    } else if (c && !c.ok) {
      allCostOk = false;
    }
  });

  const cards = [];
  cards.push(`
    <div class="impact-card">
      <div class="impact-value">${anyInteractionsOk ? fmtHM(totalMinutes) : "—"}</div>
      <div class="impact-label">Est. receptionist time saved</div>
    </div>`);
  cards.push(`
    <div class="impact-card">
      <div class="impact-value">${bookings}</div>
      <div class="impact-label">Bookings</div>
    </div>`);
  cards.push(`
    <div class="impact-card">
      <div class="impact-value">${reschedules + cancellations}</div>
      <div class="impact-label">Reschedules + cancellations</div>
    </div>`);
  cards.push(`
    <div class="impact-card">
      <div class="impact-value">${callbacks}</div>
      <div class="impact-label">Human handoffs (callbacks)</div>
    </div>`);
  if (anyCostConfigured) {
    cards.push(`
      <div class="impact-card">
        <div class="impact-value">€${totalCost.toFixed(2)}</div>
        <div class="impact-label">Est. LLM cost${allCostOk ? "" : " (partial)"}</div>
      </div>`);
  }
  el.innerHTML = cards.join("");
}

function updateStatusBar() {
  const down = latestResults.filter((r) => r.status === "down").length;
  const warn = latestResults.filter((r) => r.status === "warning").length;
  $("statusBar").textContent = `${latestResults.length} client(s) — ${down} down, ${warn} warning`;
}

async function refreshClients() {
  $("statusBar").textContent = "Refreshing…";
  try {
    const resp = await fetch("/api/clients");
    latestResults = await resp.json();
    renderTable();
    updateStatusBar();
    // Keep an open detail modal in sync with the new data.
    if (!$("detailModal").classList.contains("hidden")) {
      const name = $("detailModal").dataset.name;
      const r = latestResults.find((x) => x.name === name);
      if (r) applyDetail(r);
    }
  } catch (e) {
    $("statusBar").textContent = "Failed to reach API: " + e;
  }
}

async function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  let interval = 60;
  try {
    const resp = await fetch("/api/settings");
    interval = (await resp.json()).poll_interval_seconds || 60;
  } catch (e) {
    /* fall back to default */
  }
  await Promise.all([refreshClients(), refreshHosts()]);
  pollTimer = setInterval(() => {
    refreshClients();
    refreshHosts();
  }, interval * 1000);
}

// -- detail modal ----------------------------------------------------------

function fmtCompact(n) {
  if (n == null) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "K";
  return String(n);
}

// One stat tile for the at-a-glance strip. Status is never color alone:
// the value carries a glyph (● / ○ / ⚠) alongside the tint.
function glanceTile(label, value, cls, sub) {
  return `<div class="glance-tile">
    <div class="glance-value ${cls || ""}">${value}</div>
    <div class="glance-label">${escapeHtml(label)}</div>
    <div class="glance-sub">${sub || ""}</div>
  </div>`;
}

function renderDetailGlance(r) {
  const h = r.health || {};
  const v = r.version || {};
  const u = r.usage || {};
  const i = r.interactions || {};
  const c = r.cost || {};
  const up = r.uptime || {};
  const tiles = [];

  // Health
  tiles.push(h.up
    ? glanceTile("Health", `<span class="ok">●</span> UP`, "",
        `${h.latency_ms}ms${h.voice_enabled ? ` · ${h.voice_active_sessions} voice` : ""}`)
    : glanceTile("Health", `<span class="down">●</span> DOWN`, "down", "not answering"));

  // Version
  if (v.ok) {
    const behind = v.behind || 0;
    tiles.push(glanceTile("Version", `<code>${escapeHtml(v.commit || "?")}</code>`,
      behind ? "warn" : "",
      behind ? `⚠ ${behind} commit${behind === 1 ? "" : "s"} behind` : "up to date"));
  } else {
    tiles.push(glanceTile("Version", "?", "muted", "no SSH check"));
  }

  // Uptime 7d
  if (up.uptime_7d_pct != null) {
    const cls = { ok: "", warn: "warn", down: "down" }[up.uptime_band] || "muted";
    tiles.push(glanceTile("Uptime 7d", `${up.uptime_7d_pct}%`, cls,
      up.latency_p95_ms != null ? `p95 ${up.latency_p95_ms}ms` : `${up.samples_7d} samples`));
  } else {
    tiles.push(glanceTile("Uptime 7d", "—", "muted", "no history yet"));
  }

  // Tokens this month
  if (u.ok) {
    const quotaSub = r.quota
      ? `${Math.round((u.total_tokens / r.quota) * 100)}% of quota`
      : "no quota set";
    tiles.push(glanceTile("Tokens (mo)", fmtCompact(u.total_tokens),
      r.over_quota ? "down" : "", r.over_quota ? "⚠ over quota" : quotaSub));
  } else {
    tiles.push(glanceTile("Tokens (mo)", "—", "muted", "unavailable"));
  }

  // Chats + time saved
  tiles.push(glanceTile("Chats (mo)", u.ok ? String(u.chats) : "—", u.ok ? "" : "muted",
    i.ok ? `${fmtHM(i.minutes_saved)} saved` : ""));

  // Cost (only when configured)
  if (c.ok && c.configured) {
    tiles.push(glanceTile("Est. cost (mo)", `€${c.estimated_eur.toFixed(2)}`, "", "from configured rates"));
  }

  $("detailGlance").innerHTML = tiles.join("");
}

function renderDetailAlert(r) {
  const el = $("detailAlert");
  const alerts = [];
  const h = r.health || {};
  if (!h.up) {
    alerts.push(`<div class="detail-alert down">● DOWN — ${escapeHtml(h.error || "health check failed")}</div>`);
  }
  if (r.over_quota) {
    alerts.push(`<div class="detail-alert down">⚠ Over the monthly token quota — billable overage.</div>`);
  }
  if ((r.version || {}).ok && r.version.infra_risk) {
    alerts.push(`<div class="detail-alert warn">⚠ Pending commits touch infrastructure files
      (${(r.version.infra_files || []).map(escapeHtml).join(", ")}) — review before deploying.</div>`);
  }
  el.innerHTML = alerts.join("");
}

function applyDetail(r) {
  $("detailModal").dataset.name = r.name;
  $("detailName").textContent = r.name;
  const base = ((r.client || {}).base_url || "").replace(/\/+$/, "");
  const urlEl = $("detailUrl");
  if (base) {
    urlEl.textContent = base.replace(/^https?:\/\//, "");
    urlEl.href = base;
    urlEl.classList.remove("hidden");
  } else {
    urlEl.textContent = "";
    urlEl.classList.add("hidden");
  }
  const pill = $("detailStatus");
  pill.textContent = r.status.toUpperCase();
  pill.className = `status-pill ${r.status}`;
  $("detailChecked").textContent = "Last checked " + (r.checked_at || "—").replace("T", " ");

  renderDetailAlert(r);
  renderDetailGlance(r);

  const v = r.version;
  let versionHtml;
  if (v.ok) {
    versionHtml = `<div class="kv-row"><span class="kv-key">Running</span><span><code>${escapeHtml(v.commit || "?")}</code>${v.behind ? ` — ${v.behind} commit${v.behind === 1 ? "" : "s"} behind origin/master` : " — up to date"}</span></div>`;
  } else {
    versionHtml = `<div class="kv-row"><span class="kv-key">Running</span><span class="muted">unknown — ${escapeHtml(v.error || "check failed")}</span></div>`;
  }
  if (v.ok && v.behind_commits && v.behind_commits.length) {
    const shown = v.behind_commits.map((line) => `<li>${escapeHtml(line)}</li>`).join("");
    const truncatedNote = v.behind > v.behind_commits.length
      ? `<p class="muted">…and ${v.behind - v.behind_commits.length} more not shown.</p>` : "";
    versionHtml += `<details class="upd-commits"${v.behind <= 5 ? " open" : ""}><summary>What's not deployed yet</summary>
      <ul class="behind-commits">${shown}</ul>${truncatedNote}</details>`;
  }
  if (v.ok && v.containers && v.containers.length) {
    versionHtml += v.containers.map((c) => {
      const running = (c.state || "").toLowerCase() === "running";
      return `<div class="kv-row"><span class="kv-key">${escapeHtml(c.name)}</span>
        <span><span class="${running ? "ok" : "down"}">●</span> ${escapeHtml(c.state)} ${escapeHtml(c.health || "")}</span></div>`;
    }).join("");
  }
  $("detailVersion").innerHTML = versionHtml;
  renderDeployArea(r);
  renderReseedArea(r);

  const u = r.usage;
  let usageHtml;
  if (u.ok) {
    usageHtml =
      `<div class="kv-row"><span class="kv-key">Chats</span><span>${u.chats}</span></div>` +
      `<div class="kv-row"><span class="kv-key">Input</span><span>${u.input_tokens.toLocaleString()} tok</span></div>` +
      `<div class="kv-row"><span class="kv-key">Cached</span><span>${u.cached_tokens.toLocaleString()} tok</span></div>` +
      `<div class="kv-row"><span class="kv-key">Output</span><span>${u.output_tokens.toLocaleString()} tok</span></div>` +
      `<div class="kv-row"><span class="kv-key">Total</span><span><strong>${u.total_tokens.toLocaleString()} tok</strong></span></div>` +
      `<div class="kv-row"><span class="kv-key">Quota</span><span>${r.quota ? r.quota.toLocaleString() + " tok/mo" + (r.over_quota ? ' — <strong class="down">over quota</strong>' : "") : '<span class="muted">none configured</span>'}</span></div>`;
    const c = r.cost || {};
    if (c.ok && c.configured) {
      usageHtml += `<div class="kv-row"><span class="kv-key">Est. cost</span><span>€${c.estimated_eur.toFixed(2)}</span></div>`;
    } else if (c.ok) {
      usageHtml += `<div class="kv-row"><span class="kv-key">Est. cost</span><span class="muted">no rate configured</span></div>`;
    }
  } else {
    usageHtml = `<span class="muted">unknown — ${escapeHtml(u.error || "check failed")}</span>`;
  }
  $("detailUsage").innerHTML = usageHtml;

  const bar = $("detailQuotaBar");
  const pct = r.quota && u.ok ? Math.min(100, (u.total_tokens / r.quota) * 100) : 0;
  bar.style.width = pct + "%";
  bar.className = "progress-fill" + (r.over_quota ? " over" : "");
  bar.parentElement.style.display = r.quota ? "" : "none";

  const res = r.resources || {};
  let resourcesHtml;
  if (res.ok) {
    resourcesHtml = (res.containers || []).map((c) =>
      `<div class="kv-row"><span class="kv-key">${escapeHtml(c.name)}</span>
       <span>cpu ${c.cpu_pct != null ? c.cpu_pct + "%" : "?"} · mem ${c.mem_pct != null ? c.mem_pct + "%" : "?"} <span class="muted">(${escapeHtml(c.mem_usage || "")})</span></span></div>`
    ).join("") || `<span class="muted">No containers found.</span>`;
    if (res.data_disk_usage) {
      resourcesHtml += `<div class="kv-row"><span class="kv-key">/data</span><span>${escapeHtml(res.data_disk_usage)}</span></div>`;
    }
  } else {
    resourcesHtml = `<span class="muted">unknown — ${escapeHtml(res.error || "check failed")}</span>`;
  }
  $("detailResources").innerHTML = resourcesHtml;

  const i = r.interactions || {};
  let interactionsHtml;
  if (i.ok) {
    interactionsHtml =
      `<div class="kv-row"><span class="kv-key">Bookings</span><span>${i.bookings}</span></div>` +
      `<div class="kv-row"><span class="kv-key">Reschedules</span><span>${i.reschedules}</span></div>` +
      `<div class="kv-row"><span class="kv-key">Cancellations</span><span>${i.cancellations}</span></div>` +
      `<div class="kv-row"><span class="kv-key">Callbacks</span><span>${i.callbacks}</span></div>` +
      `<div class="kv-row"><span class="kv-key">Registrations</span><span>${i.registrations}</span></div>` +
      (i.other ? `<div class="kv-row"><span class="kv-key">Other</span><span>${i.other}</span></div>` : "") +
      `<div class="kv-row"><span class="kv-key">Time saved</span><span><strong>${fmtHM(i.minutes_saved)}</strong></span></div>`;
  } else {
    interactionsHtml = `<span class="muted">unknown — ${escapeHtml(i.error || "check failed")}</span>`;
  }
  $("detailInteractions").innerHTML = interactionsHtml;

  const up = r.uptime || {};
  let uptimeHtml;
  if (up.uptime_7d_pct == null) {
    uptimeHtml = `<span class="muted">Not enough history yet — a sample is recorded on every poll.</span>`;
  } else {
    uptimeHtml =
      `<div class="kv-row"><span class="kv-key">Last 24h</span><span>${up.uptime_24h_pct != null ? up.uptime_24h_pct + "%" : "—"} <span class="muted">(${up.samples_24h} samples)</span></span></div>` +
      `<div class="kv-row"><span class="kv-key">Last 7d</span><span>${up.uptime_7d_pct}% <span class="muted">(${up.samples_7d} samples)</span></span></div>`;
    if (up.latency_p50_ms != null) {
      uptimeHtml += `<div class="kv-row"><span class="kv-key">Latency</span><span>p50 ${up.latency_p50_ms}ms · p95 ${up.latency_p95_ms}ms</span></div>`;
    }
  }
  $("detailUptime").innerHTML = uptimeHtml;
}

function openDetail(name) {
  deployUiState = { mode: "idle", name, result: null };
  const r = latestResults.find((x) => x.name === name);
  if (!r) return;
  applyDetail(r);
  show("detailModal");
}

function renderDeployArea(r) {
  const el = $("deployArea");
  const v = r.version;
  if (deployUiState.name !== r.name) {
    // Detail modal now shows a different client than deployUiState
    // remembers (shouldn't normally happen — openDetail always resets
    // this — but guards against a stray applyDetail call after a client
    // was deleted/renamed).
    deployUiState = { mode: "idle", name: r.name, result: null };
  }
  // "deploying"/"result" must render regardless of the current behind
  // count — a successful deploy brings behind back to 0, which would
  // otherwise hit the early-return below and wipe the success message
  // out right when it should be shown.
  //
  // Deliberately does NOT gate on v.behind: the backend's deploy_client
  // never required "behind > 0" either (pull/build/up run unconditionally),
  // and gating the button on it here is what let a real incident go
  // undetected — a deploy that pulled+built successfully but failed at the
  // `up` stage left git reporting "up to date" while the running container
  // was still hours stale, with no way in the UI to force a rebuild. Only
  // a fully failed version check (v.ok false — no SSH/remote_dir at all)
  // hides this control now.
  if (deployUiState.mode !== "deploying" && deployUiState.mode !== "result" && !v.ok) {
    el.innerHTML = "";
    return;
  }

  if (deployUiState.mode === "confirm") {
    // A background poll refresh calls applyDetail while this modal is
    // open too — if the confirm form is already showing, leave it alone
    // rather than rebuilding it and wiping out whatever the user's mid-
    // typing into the confirmation box.
    if (el.dataset.deployRenderedFor === r.name && $("deployConfirmInput")) return;
  } else {
    delete el.dataset.deployRenderedFor;
  }

  if (deployUiState.mode === "idle") {
    const label = v.behind
      ? `Deploy latest (${v.behind} commit${v.behind === 1 ? "" : "s"} behind)`
      : "Rebuild & restart (up to date, but confirm the container matches)";
    const btnClass = v.behind ? "danger" : "";
    el.innerHTML = `<button id="deployStartBtn" type="button" class="${btnClass}">${label}</button>`;
    $("deployStartBtn").addEventListener("click", () => {
      deployUiState.mode = "confirm";
      applyDetail(r);
    });
  } else if (deployUiState.mode === "confirm") {
    el.dataset.deployRenderedFor = r.name;
    const infraNotice = v.infra_risk
      ? `<p class="infra-warning">⚠ This includes infrastructure file changes (${(v.infra_files || []).map(escapeHtml).join(", ")}) —
          a pre-flight check will refuse to restart if it would collide with another client's containers, but that
          only catches naming collisions specifically. Worth actually looking at this commit before proceeding.</p>`
      : "";
    const upToDateNotice = !v.behind
      ? `<p class="muted">Git already shows "up to date" — this reruns build + restart anyway, in case an
          earlier deploy pulled/built successfully but never actually restarted the container (git looking
          current does not by itself prove the running container matches it).</p>`
      : "";
    el.innerHTML = `
      <div class="deploy-confirm">
        <p class="muted">Runs <code>git pull --ff-only</code>, rebuilds, and restarts only
          <strong>${escapeHtml(r.name)}'s</strong> own containers on the VPS — Caddy, the other
          client, and everything else on the box are never touched. A pre-flight check also refuses
          to restart if it would collide with a container name another client's project already owns.</p>
        ${upToDateNotice}
        ${infraNotice}
        <label>Type "<strong>${escapeHtml(r.name)}</strong>" to confirm:
          <input id="deployConfirmInput" type="text" autocomplete="off" spellcheck="false" />
        </label>
        <div class="modal-actions">
          <button id="deployConfirmBtn" type="button" class="danger" disabled>Confirm deploy</button>
          <button id="deployCancelBtn" type="button">Cancel</button>
        </div>
      </div>`;
    const input = $("deployConfirmInput");
    const confirmBtn = $("deployConfirmBtn");
    input.addEventListener("input", () => {
      confirmBtn.disabled = input.value !== r.name;
    });
    confirmBtn.addEventListener("click", () => startDeploy(r.name));
    $("deployCancelBtn").addEventListener("click", () => {
      deployUiState.mode = "idle";
      applyDetail(r);
    });
  } else if (deployUiState.mode === "deploying") {
    el.innerHTML = `<p class="muted">Deploying — pulling, rebuilding, restarting. This can take a minute or two for the build; don't close this.</p>`;
  } else if (deployUiState.mode === "result") {
    const res = deployUiState.result || {};
    const cls = res.ok ? "ok" : "down";
    const outputBlock = res.output
      ? `<details class="deploy-output"><summary>Full output</summary><pre>${escapeHtml(res.output)}</pre></details>`
      : "";
    el.innerHTML = `
      <div class="deploy-result ${cls}">
        <p><strong>${res.ok ? "Deployed successfully." : "Deploy failed at the " + escapeHtml(res.stage || "?") + " stage."}</strong>
          ${res.commit ? " Now at " + escapeHtml(res.commit) + "." : ""}</p>
        ${res.error ? `<p class="muted">${escapeHtml(res.error)}</p>` : ""}
        ${outputBlock}
        <button id="deployDismissBtn" type="button">Dismiss</button>
      </div>`;
    $("deployDismissBtn").addEventListener("click", () => {
      deployUiState.mode = "idle";
      refreshOneDetail();
    });
  }
}

async function startDeploy(name) {
  deployUiState.mode = "deploying";
  const current = latestResults.find((x) => x.name === name);
  if (current) applyDetail(current);

  let result;
  try {
    const resp = await fetch(`/api/clients/${encodeURIComponent(name)}/deploy`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_name: name }),
    });
    const data = await resp.json();
    result = resp.ok ? data : { ok: false, stage: "request", error: data.detail || resp.statusText, output: "" };
  } catch (e) {
    result = { ok: false, stage: "request", error: String(e), output: "" };
  }

  deployUiState.mode = "result";
  deployUiState.result = result;
  // Pull fresh status right away so the Version section reflects the new
  // commit (or confirms the old one is still running after a failure)
  // instead of waiting for the next poll tick.
  refreshOneDetail();
}

// Nuke-and-reseed: wipes the instance's whole DB back to the starter demo
// (seed --reset). Destructive, so it goes through the same type-the-name
// confirmation the deploy area uses.
function renderReseedArea(r) {
  const el = $("reseedArea");
  if (!el) return;
  const v = r.version || {};
  if (reseedUiState.name !== r.name) {
    reseedUiState = { mode: "idle", name: r.name, choice: null, result: null };
  }
  // No ssh_target/remote_dir → the console can't reach the box to reseed;
  // say so rather than offer a button that only 502s. (deploy area gates on
  // the same v.ok signal.)
  if (reseedUiState.mode === "idle" && !v.ok) {
    el.innerHTML = `<p class="muted">Reseed needs SSH access (ssh_target + remote_dir) to this instance — not configured, so it's unavailable here.</p>`;
    return;
  }

  // Don't rebuild the confirm form under a half-typed name on a poll tick.
  if (reseedUiState.mode === "confirm") {
    if (el.dataset.reseedRenderedFor === r.name && $("reseedConfirmInput")) return;
  } else {
    delete el.dataset.reseedRenderedFor;
  }

  if (reseedUiState.mode === "idle") {
    el.innerHTML = `
      <p class="muted">Wipes <strong>${escapeHtml(r.name)}</strong>'s entire database — all chats, bookings, clients and callbacks — and rebuilds it as a full demo (starter consultants/services <em>plus</em> generated demo clients, chats and bookings), then restarts its <code>app</code> container. Other clients on the VPS are never touched.</p>
      <div class="modal-actions">
        <button id="reseedStartBtn" type="button" class="danger">Wipe &amp; reseed to demo</button>
      </div>`;
    $("reseedStartBtn").addEventListener("click", () => {
      reseedUiState.mode = "confirm";
      applyDetail(r);
    });
  } else if (reseedUiState.mode === "confirm") {
    el.dataset.reseedRenderedFor = r.name;
    el.innerHTML = `
      <div class="deploy-confirm">
        <p class="infra-warning"><strong>Wipes the entire database</strong> — conversations, appointments, clients and callbacks — then rebuilds it as a full demo with freshly generated demo clients, chats and bookings. This cannot be undone.</p>
        <label>Type "<strong>${escapeHtml(r.name)}</strong>" to confirm:
          <input id="reseedConfirmInput" type="text" autocomplete="off" spellcheck="false" />
        </label>
        <div class="modal-actions">
          <button id="reseedConfirmBtn" type="button" class="danger" disabled>Wipe &amp; reseed</button>
          <button id="reseedCancelBtn" type="button">Cancel</button>
        </div>
      </div>`;
    const input = $("reseedConfirmInput");
    const confirmBtn = $("reseedConfirmBtn");
    input.addEventListener("input", () => {
      confirmBtn.disabled = input.value !== r.name;
    });
    confirmBtn.addEventListener("click", () => startReseed(r.name));
    $("reseedCancelBtn").addEventListener("click", () => {
      reseedUiState.mode = "idle";
      applyDetail(r);
    });
  } else if (reseedUiState.mode === "reseeding") {
    el.innerHTML = `<p class="muted">Wiping and reseeding, then restarting the container — don't close this.</p>`;
  } else if (reseedUiState.mode === "result") {
    const res = reseedUiState.result || {};
    const cls = res.ok ? "ok" : "down";
    const outputBlock = res.output
      ? `<details class="deploy-output"><summary>Full output</summary><pre>${escapeHtml(res.output)}</pre></details>`
      : "";
    el.innerHTML = `
      <div class="deploy-result ${cls}">
        <p><strong>${res.ok ? "Wiped & reseeded to a full demo." : "Reseed failed."}</strong></p>
        ${res.error ? `<p class="muted">${escapeHtml(res.error)}</p>` : ""}
        ${outputBlock}
        <button id="reseedDismissBtn" type="button">Dismiss</button>
      </div>`;
    $("reseedDismissBtn").addEventListener("click", () => {
      reseedUiState.mode = "idle";
      refreshOneDetail();
    });
  }
}

async function startReseed(name) {
  reseedUiState.mode = "reseeding";
  const current = latestResults.find((x) => x.name === name);
  if (current) applyDetail(current);

  let result;
  try {
    const resp = await fetch(`/api/clients/${encodeURIComponent(name)}/reseed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: name }),
    });
    const data = await resp.json();
    result = resp.ok ? data : { ok: false, error: data.detail || resp.statusText, output: "" };
  } catch (e) {
    result = { ok: false, error: String(e), output: "" };
  }

  reseedUiState.mode = "result";
  reseedUiState.result = result;
}

async function refreshOneDetail() {
  const name = $("detailModal").dataset.name;
  if (!name) return;
  try {
    const resp = await fetch(`/api/clients/${encodeURIComponent(name)}/status`);
    const r = await resp.json();
    applyDetail(r);
    const idx = latestResults.findIndex((x) => x.name === name);
    if (idx >= 0) latestResults[idx] = r;
    renderTable();
  } catch (e) {
    alert("Refresh failed: " + e);
  }
}

async function deleteCurrentClient() {
  const name = $("detailModal").dataset.name;
  if (!name) return;
  if (!confirm(`Remove '${name}' from monitoring?`)) return;
  await fetch(`/api/clients/${encodeURIComponent(name)}`, { method: "DELETE" });
  hide("detailModal");
  refreshClients();
}

// -- add/edit modal ----------------------------------------------------------

function openAddModal() {
  editingName = null;
  $("editTitle").textContent = "Add client";
  $("editForm").reset();
  $("fetchTokenStatus").textContent = "";
  show("editModal");
}

const ADVANCED_NUMERIC_FIELDS = [
  "minutes_per_booking", "minutes_per_reschedule", "minutes_per_cancellation", "minutes_per_callback",
  "cost_per_1k_input_tokens", "cost_per_1k_cached_tokens", "cost_per_1k_output_tokens",
];

function openEditModal() {
  const name = $("detailModal").dataset.name;
  const r = latestResults.find((x) => x.name === name);
  if (!r) return;
  editingName = name;
  $("editTitle").textContent = "Edit client";
  const form = $("editForm");
  const c = r.client;
  form.name.value = c.name || "";
  form.base_url.value = c.base_url || "";
  form.ssh_target.value = c.ssh_target || "";
  form.remote_dir.value = c.remote_dir || "";
  form.monthly_token_quota.value = c.monthly_token_quota || 0;
  form.admin_token.value = c.admin_token || "";
  // These two used to be missing from the form entirely, so ANY edit wiped
  // them from the saved record (PUT replaces the whole client) — a real
  // incident on 2026-07-20: refreshing the primary's admin token silently
  // dropped its admin_local_port, breaking the loopback smoke check.
  form.admin_local_port.value = c.admin_local_port != null ? c.admin_local_port : "";
  form.admin_via_ssh.checked = !!c.admin_via_ssh;
  ADVANCED_NUMERIC_FIELDS.forEach((key) => {
    form[key].value = c[key] != null ? c[key] : "";
  });
  $("fetchTokenStatus").textContent = "";
  hide("detailModal");
  show("editModal");
}

async function submitEditForm(e) {
  e.preventDefault();
  const form = $("editForm");
  const body = {
    name: form.name.value.trim(),
    base_url: form.base_url.value.trim(),
    ssh_target: form.ssh_target.value.trim(),
    remote_dir: form.remote_dir.value.trim(),
    monthly_token_quota: parseInt(form.monthly_token_quota.value || "0", 10),
    admin_token: form.admin_token.value.trim(),
    admin_local_port: form.admin_local_port.value.trim() === ""
      ? null : parseInt(form.admin_local_port.value, 10),
    admin_via_ssh: form.admin_via_ssh.checked,
  };
  ADVANCED_NUMERIC_FIELDS.forEach((key) => {
    const raw = form[key].value.trim();
    body[key] = raw === "" ? null : parseFloat(raw);
  });
  const url = editingName ? `/api/clients/${encodeURIComponent(editingName)}` : "/api/clients";
  const method = editingName ? "PUT" : "POST";
  const resp = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const detail = await resp.json().catch(() => ({}));
    alert("Save failed: " + (detail.detail || resp.statusText));
    return;
  }
  hide("editModal");
  refreshClients();
}

async function fetchTokenViaSSH() {
  const form = $("editForm");
  const ssh_target = form.ssh_target.value.trim();
  const remote_dir = form.remote_dir.value.trim();
  if (!ssh_target || !remote_dir) {
    $("fetchTokenStatus").textContent = "Fill in SSH target + remote dir first.";
    return;
  }
  $("fetchTokenStatus").textContent = "Fetching…";
  try {
    const resp = await fetch("/api/fetch-token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssh_target, remote_dir }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    form.admin_token.value = data.admin_token;
    $("fetchTokenStatus").textContent = "Fetched.";
  } catch (e) {
    $("fetchTokenStatus").textContent = "Failed: " + e.message;
  }
}

// -- settings modal ----------------------------------------------------------

async function openSettingsModal() {
  const resp = await fetch("/api/settings");
  const data = await resp.json();
  $("pollIntervalInput").value = data.poll_interval_seconds;
  $("opKeyResults").innerHTML = "";
  show("settingsModal");
  refreshOpKeyStatus();
}

// -- operator key (fleet-wide managed-field token) ---------------------------

async function refreshOpKeyStatus() {
  const el = $("opKeyStatus");
  el.textContent = "Checking…";
  try {
    const resp = await fetch("/api/operator-key/status");
    const data = await resp.json();
    el.innerHTML = data.configured
      ? '<span class="ok">●</span> A key is set and stored. Rotate to replace it.'
      : '<span class="down">●</span> No key set yet — generate one to enable console edits of protected settings.';
  } catch (e) {
    el.innerHTML = '<span class="muted">Could not read status — ' + escapeHtml(String(e)) + "</span>";
  }
}

function renderOpKeyResults(res) {
  const box = $("opKeyResults");
  if (!res || !res.results) { box.innerHTML = ""; return; }
  const rows = res.results.map((r) => {
    const dot = r.ok ? '<span class="ok">●</span>' : '<span class="down">●</span>';
    const detail = r.ok ? "updated & restarted"
      : escapeHtml((r.stage ? r.stage + ": " : "") + (r.error || "failed"));
    return '<div class="kv-row"><span class="kv-key">' + dot + " " + escapeHtml(r.name) +
           '</span><span>' + detail + "</span></div>";
  }).join("");
  const head = res.total
    ? '<p class="' + (res.ok ? "ok" : "muted") + '" style="margin:0 0 6px;">Pushed to ' +
      res.pushed + " of " + res.total + " instances." +
      (res.ok ? "" : " Failed ones kept their old key — fix and use “Re-push to all”.") + "</p>"
    : '<p class="muted">No instances registered.</p>';
  box.innerHTML = head + rows;
}

async function rotateOperatorKey() {
  const typed = prompt(
    "Generate a NEW operator key and push it to EVERY instance?\n\n" +
    "This restarts each instance's app for a few seconds (one at a time).\n" +
    'Type "rotate" to confirm.');
  if (typed === null) return;
  const btn = $("rotateOpKeyBtn");
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = "Rotating…";
  $("opKeyResults").innerHTML = '<p class="muted">Generating key and pushing to all instances…</p>';
  try {
    const resp = await fetch("/api/operator-key/rotate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: typed }),
    });
    const data = await resp.json();
    if (!resp.ok) { $("opKeyResults").innerHTML = '<p class="down">' + escapeHtml(data.detail || "Rotate failed") + "</p>"; return; }
    renderOpKeyResults(data);
    refreshOpKeyStatus();
  } catch (e) {
    $("opKeyResults").innerHTML = '<p class="down">Rotate failed — ' + escapeHtml(String(e)) + "</p>";
  } finally {
    btn.disabled = false; btn.textContent = prev;
  }
}

async function pushOperatorKey() {
  const btn = $("pushOpKeyBtn");
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = "Pushing…";
  $("opKeyResults").innerHTML = '<p class="muted">Re-sending the stored key to all instances…</p>';
  try {
    const resp = await fetch("/api/operator-key/push", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) { $("opKeyResults").innerHTML = '<p class="down">' + escapeHtml(data.detail || "Push failed") + "</p>"; return; }
    renderOpKeyResults(data);
    refreshOpKeyStatus();
  } catch (e) {
    $("opKeyResults").innerHTML = '<p class="down">Push failed — ' + escapeHtml(String(e)) + "</p>";
  } finally {
    btn.disabled = false; btn.textContent = prev;
  }
}

async function saveSettings() {
  const value = parseInt($("pollIntervalInput").value, 10);
  const resp = await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ poll_interval_seconds: value }),
  });
  if (!resp.ok) {
    alert("Save failed");
    return;
  }
  hide("settingsModal");
  startPolling();
}

// -- credentials (.env copy) modal --------------------------------------------

function populateCredsClientSelect(selectEl) {
  const current = selectEl.value;
  selectEl.innerHTML = '<option value="">Custom (type below)…</option>' +
    latestResults.map((r) => `<option value="${escapeHtml(r.name)}">${escapeHtml(r.name)}</option>`).join("");
  selectEl.value = current;
}

function wireCredsSelect(prefix) {
  $(`${prefix}Select`).addEventListener("change", (e) => {
    if (e.target.value) hide(`${prefix}Custom`);
    else show(`${prefix}Custom`);
  });
}

// Resolves the current ssh_target/remote_dir/base_url for "source" or
// "dest", either from a picked existing client or from the custom fields.
function credsTarget(prefix) {
  const select = $(`${prefix}Select`);
  if (select.value) {
    const r = latestResults.find((x) => x.name === select.value);
    if (r) {
      return {
        ssh_target: r.client.ssh_target || "",
        remote_dir: r.client.remote_dir || "",
        base_url: r.client.base_url || "",
      };
    }
  }
  const baseUrlInput = $(`${prefix}BaseUrl`);
  return {
    ssh_target: ($(`${prefix}Ssh`).value || "").trim(),
    remote_dir: ($(`${prefix}Dir`).value || "").trim(),
    base_url: baseUrlInput ? (baseUrlInput.value || "").trim() : "",
  };
}

// presetDestName: when opening straight from a just-finished New Client
// provision, pre-selects the new client as the destination and loads its
// (freshly-written, placeholder) .env immediately, instead of making
// someone re-pick it from the dropdown right after watching it get created.
function openCredsModal(presetDestName) {
  populateCredsClientSelect($("credsSourceSelect"));
  populateCredsClientSelect($("credsDestSelect"));
  $("credsSourceSelect").value = "";
  ["credsSourceSsh", "credsSourceDir", "credsDestSsh", "credsDestDir", "credsDestBaseUrl"].forEach((id) => {
    $(id).value = "";
  });
  show("credsSourceCustom");
  credsSourceEnv = {};
  credsDestRows = [];
  $("credsRestartAfter").checked = false;
  $("credsSourceStatus").textContent = "";
  $("credsDestStatus").textContent = "";
  $("credsWriteStatus").textContent = "";
  show("credsModal");
  if (presetDestName && latestResults.some((r) => r.name === presetDestName)) {
    $("credsDestSelect").value = presetDestName;
    hide("credsDestCustom");
    renderCredsTable();
    loadCredsDest();
  } else {
    $("credsDestSelect").value = "";
    show("credsDestCustom");
    renderCredsTable();
  }
}

// Merges a fresh set of known keys into the existing table without
// clobbering values already typed in — used both when a source is loaded
// (so its keys appear even if the destination table didn't have them yet)
// and when a destination's existing .env is loaded (same merge, plus real
// values pulled from the destination itself).
function mergeCredsKeys(extraKeys, destValues) {
  const allKeys = new Set([...KNOWN_ENV_KEYS, ...credsDestRows.map((r) => r.key), ...extraKeys]);
  credsDestRows = [...allKeys].sort().map((key) => {
    const existing = credsDestRows.find((r) => r.key === key);
    const fromDest = destValues && destValues[key] != null ? destValues[key] : null;
    return { key, value: fromDest != null ? fromDest : (existing ? existing.value : "") };
  });
}

async function loadCredsSource() {
  const t = credsTarget("credsSource");
  if (!t.ssh_target || !t.remote_dir) {
    $("credsSourceStatus").textContent = "Fill in SSH target + remote dir (or pick a client) first.";
    return;
  }
  $("credsSourceStatus").textContent = "Loading…";
  try {
    const resp = await fetch("/api/env/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssh_target: t.ssh_target, remote_dir: t.remote_dir }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    credsSourceEnv = data.env;
    $("credsSourceStatus").textContent = data.exists
      ? `Loaded ${Object.keys(data.env).length} key(s).`
      : "No .env found at that path — nothing to load.";
    mergeCredsKeys(Object.keys(credsSourceEnv), null);
    renderCredsTable();
  } catch (e) {
    $("credsSourceStatus").textContent = "Failed: " + e.message;
  }
}

async function loadCredsDest() {
  const t = credsTarget("credsDest");
  if (!t.ssh_target || !t.remote_dir) {
    $("credsDestStatus").textContent = "Fill in SSH target + remote dir (or pick a client) first.";
    return;
  }
  $("credsDestStatus").textContent = "Loading…";
  try {
    const resp = await fetch("/api/env/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssh_target: t.ssh_target, remote_dir: t.remote_dir }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    $("credsDestStatus").textContent = data.exists
      ? `Loaded ${Object.keys(data.env).length} existing key(s) — merged into the table below.`
      : "No .env there yet — starting from source keys + known defaults.";
    mergeCredsKeys(Object.keys(data.env), data.env);
    renderCredsTable();
  } catch (e) {
    $("credsDestStatus").textContent = "Failed: " + e.message;
  }
}

function renderCredsTable() {
  const tbody = $("credsTableBody");
  if (!credsDestRows.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">Load a source .env, or click "+ Add key", to begin.</td></tr>';
    return;
  }
  tbody.innerHTML = credsDestRows.map((row, i) => {
    const sourceVal = credsSourceEnv[row.key];
    const kind = CRED_TEST_KIND[row.key];
    return `
      <tr>
        <td><code>${escapeHtml(row.key)}</code></td>
        <td class="creds-value-cell">${sourceVal != null ? escapeHtml(sourceVal) : '<span class="muted">—</span>'}</td>
        <td>${sourceVal != null ? `
          <button type="button" class="icon-btn creds-copy-btn" data-idx="${i}" title="Copy source value to clipboard">Copy</button>
          <button type="button" class="icon-btn creds-use-btn" data-idx="${i}" title="Use source value as destination value">&rarr; Use</button>
        ` : ""}</td>
        <td><input type="text" class="creds-dest-input" data-idx="${i}" value="${escapeHtml(row.value)}" autocomplete="off" spellcheck="false" /></td>
        <td>${kind ? `<button type="button" class="icon-btn creds-test-btn" data-idx="${i}" data-kind="${kind}">Test</button><span class="creds-test-result" data-idx="${i}"></span>` : ""}</td>
      </tr>`;
  }).join("");

  tbody.querySelectorAll(".creds-dest-input").forEach((input) => {
    input.addEventListener("input", (e) => {
      credsDestRows[+e.target.dataset.idx].value = e.target.value;
    });
  });
  tbody.querySelectorAll(".creds-copy-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = credsDestRows[+btn.dataset.idx];
      navigator.clipboard?.writeText(credsSourceEnv[row.key] || "");
    });
  });
  tbody.querySelectorAll(".creds-use-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = +btn.dataset.idx;
      credsDestRows[idx].value = credsSourceEnv[credsDestRows[idx].key] || "";
      renderCredsTable();
    });
  });
  tbody.querySelectorAll(".creds-test-btn").forEach((btn) => {
    btn.addEventListener("click", () => runCredsTest(+btn.dataset.idx, btn.dataset.kind));
  });
}

function addCredsRow() {
  const raw = prompt("New env var name (e.g. MY_NEW_KEY):");
  if (!raw) return;
  const key = raw.trim().toUpperCase().replace(/[^A-Z0-9_]/g, "_");
  if (!key || credsDestRows.some((r) => r.key === key)) return;
  credsDestRows.push({ key, value: "" });
  renderCredsTable();
}

async function runCredsTest(idx, kind) {
  const resultEl = document.querySelector(`.creds-test-result[data-idx="${idx}"]`);
  resultEl.textContent = " Testing…";
  resultEl.className = "creds-test-result";
  const valueOf = (key) => (credsDestRows.find((r) => r.key === key) || {}).value || "";
  let values = {};
  if (kind === "mistral" || kind === "nvidia" || kind === "openrouter") {
    values = { key: credsDestRows[idx].value };
  } else if (kind === "twilio") {
    values = { account_sid: valueOf("TWILIO_ACCOUNT_SID"), auth_token: valueOf("TWILIO_AUTH_TOKEN") };
  } else if (kind === "smtp") {
    values = {
      host: valueOf("SMTP_HOST"), port: valueOf("SMTP_PORT") || "587",
      username: valueOf("SMTP_USERNAME"), password: valueOf("SMTP_PASSWORD"),
      use_tls: valueOf("SMTP_USE_TLS") || "true",
    };
  } else if (kind === "admin_token") {
    const dest = credsTarget("credsDest");
    values = { base_url: dest.base_url, token: valueOf("ADMIN_PASSWORD") };
  }
  try {
    const resp = await fetch("/api/env/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, values }),
    });
    const data = await resp.json();
    resultEl.textContent = " " + data.message;
    resultEl.classList.add(data.ok ? "ok" : "down");
  } catch (e) {
    resultEl.textContent = " Failed: " + e.message;
    resultEl.classList.add("down");
  }
}

async function writeCredsEnv() {
  const t = credsTarget("credsDest");
  if (!t.ssh_target || !t.remote_dir) {
    $("credsWriteStatus").textContent = "Fill in destination SSH target + remote dir first.";
    return;
  }
  const envObj = {};
  credsDestRows.forEach((r) => {
    if (r.key) envObj[r.key] = r.value;
  });
  const count = Object.keys(envObj).length;
  const restartAfter = $("credsRestartAfter").checked;
  if (!confirm(`Write ${count} key(s) as the complete .env at ${t.remote_dir} on ${t.ssh_target}?\n\nAny existing .env there is backed up first (.env.bak-<timestamp>), but this replaces the whole file.${restartAfter ? "\n\nThe container will also be restarted afterward." : ""}`)) {
    return;
  }
  $("credsWriteStatus").textContent = "Writing…";
  try {
    const resp = await fetch("/api/env/write", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssh_target: t.ssh_target, remote_dir: t.remote_dir, env: envObj, restart_after: restartAfter }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    if (restartAfter) {
      $("credsWriteStatus").textContent = data.restarted
        ? "Written and restarted — previous .env backed up alongside it."
        : `Written, but restart failed: ${data.restart_error || "unknown error"}`;
    } else {
      $("credsWriteStatus").textContent = "Written — previous .env (if any) backed up alongside it.";
    }
  } catch (e) {
    $("credsWriteStatus").textContent = "Failed: " + e.message;
  }
}

// -- New client wizard ---------------------------------------------------

// Force-refreshes the client list before populating the template dropdown,
// rather than trusting whatever's already in latestResults — opening this
// modal before the page's first poll finishes (or right after a poll that
// failed) otherwise leaves latestResults empty, and the <select> silently
// renders with zero <option>s: it looks like a dropdown but there's
// nothing in it to show, which is exactly the "arrow does nothing" bug.
async function openNewClientModal() {
  const select = $("newClientTemplateSelect");
  select.innerHTML = '<option value="">Loading clients…</option>';
  $("newClientForm").reset();
  $("newClientResult").innerHTML = "";
  $("newClientConsole").textContent = "Console output will appear here once you click Provision — this streams live over SSH as the clone/build/boot/Caddy steps actually happen.";
  show("newClientModal");
  await refreshClients();
  if (!latestResults.length) {
    select.innerHTML = '<option value="">No clients configured yet — add one first, or check Refresh now</option>';
    return;
  }
  select.innerHTML = latestResults.map((r) => `<option value="${escapeHtml(r.name)}">${escapeHtml(r.name)}</option>`).join("");
}

async function submitNewClientForm(e) {
  e.preventDefault();
  const form = $("newClientForm");
  const submitBtn = form.querySelector('button[type="submit"]');
  const body = {
    deploy_name: form.deploy_name.value.trim(),
    hostname: form.hostname.value.trim(),
    display_name: form.display_name.value.trim(),
    template_client_name: form.template_client_name.value,
  };
  submitBtn.disabled = true;
  submitBtn.textContent = "Provisioning… (a docker build can take a couple of minutes)";
  $("newClientResult").innerHTML = "";

  const consoleEl = $("newClientConsole");
  consoleEl.textContent = "";
  const appendConsoleLine = (text) => {
    consoleEl.textContent += text + "\n";
    consoleEl.scrollTop = consoleEl.scrollHeight;
  };

  try {
    // Streams newline-delimited JSON events (see POST /api/new-client/stream's
    // docstring) so the console above fills in live — a docker build can
    // genuinely take minutes, and a blank spinner the whole time gives no
    // way to tell "still working" from "stuck".
    const resp = await fetch("/api/new-client/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      // A fast precondition failure (unknown template client, etc.) is
      // raised before streaming starts, so it comes back as a plain JSON
      // error body, not ndjson.
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || resp.statusText);
    }
    if (!resp.body) throw new Error("this browser doesn't support streaming responses");

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalResult = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newlineIdx;
      while ((newlineIdx = buffer.indexOf("\n")) !== -1) {
        const rawLine = buffer.slice(0, newlineIdx);
        buffer = buffer.slice(newlineIdx + 1);
        if (!rawLine.trim()) continue;
        let event;
        try {
          event = JSON.parse(rawLine);
        } catch {
          appendConsoleLine(rawLine); // never swallow output silently, even if malformed
          continue;
        }
        if (event.type === "phase") {
          appendConsoleLine(`\n=== ${event.label} ===`);
        } else if (event.type === "log") {
          appendConsoleLine(event.line);
        } else if (event.type === "result") {
          finalResult = event;
        }
      }
    }

    if (!finalResult) {
      throw new Error("connection closed before the result arrived — check the console output above for where it stopped");
    }
    renderNewClientResult(finalResult, body);
    if (finalResult.ok) {
      form.reset();
      refreshClients();
    }
  } catch (e) {
    renderNewClientResult({ ok: false, error: e.message, stage: "request" }, body);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = "Provision";
  }
}

function renderNewClientResult(data, requestBody) {
  const el = $("newClientResult");
  el.className = `newclient-result ${data.ok ? "ok" : "down"}`;
  if (!data.ok) {
    el.innerHTML = `
      <strong>Failed${data.phase ? " at " + escapeHtml(data.phase) : ""}${data.stage ? " (" + escapeHtml(data.stage) + ")" : ""}</strong>
      <p>${escapeHtml(data.error || "unknown error")}</p>
      ${data.output ? `<details class="newclient-output"><summary>Output</summary><pre>${escapeHtml(data.output)}</pre></details>` : ""}
      ${data.caddy_output ? `<details class="newclient-output"><summary>Caddy output</summary><pre>${escapeHtml(data.caddy_output)}</pre></details>` : ""}
    `;
    return;
  }
  el.innerHTML = `
    <strong>${escapeHtml(requestBody.display_name || requestBody.deploy_name)} is live at ${escapeHtml(requestBody.hostname)}</strong>
    <p class="muted">Port ${data.port}, ${escapeHtml(data.remote_dir)} on ${escapeHtml(requestBody.template_client_name)}'s server. Registered here with the admin token below already filled in.</p>
    <div class="secret-row">Admin password: ${escapeHtml(data.admin_password || "—")}</div>
    <div class="secret-row">Backup passphrase: ${escapeHtml(data.backup_passphrase || "—")}</div>
    <p class="muted">LLM/SMTP/Twilio keys are still blank — use Credentials to copy real values in, then restart.</p>
    <div class="modal-actions">
      <button type="button" id="newClientOpenCredsBtn">Open Credentials for this client</button>
    </div>
    ${data.output ? `<details class="newclient-output"><summary>Provisioning output</summary><pre>${escapeHtml(data.output)}</pre></details>` : ""}
    ${data.caddy_output ? `<details class="newclient-output"><summary>Caddy output</summary><pre>${escapeHtml(data.caddy_output)}</pre></details>` : ""}
  `;
  const credsBtn = $("newClientOpenCredsBtn");
  if (credsBtn) {
    credsBtn.addEventListener("click", () => {
      hide("newClientModal");
      openCredsModal(requestBody.display_name || requestBody.deploy_name);
    });
  }
}

// -- modal plumbing ----------------------------------------------------------

function show(id) { $(id).classList.remove("hidden"); }
function hide(id) { $(id).classList.add("hidden"); }

document.addEventListener("DOMContentLoaded", () => {
  $("refreshBtn").addEventListener("click", refreshClients);
  $("addClientBtn").addEventListener("click", openAddModal);
  $("settingsBtn").addEventListener("click", openSettingsModal);
  $("saveSettingsBtn").addEventListener("click", saveSettings);
  $("rotateOpKeyBtn").addEventListener("click", rotateOperatorKey);
  $("pushOpKeyBtn").addEventListener("click", pushOperatorKey);
  $("editForm").addEventListener("submit", submitEditForm);
  $("fetchTokenBtn").addEventListener("click", fetchTokenViaSSH);
  $("detailRefreshBtn").addEventListener("click", refreshOneDetail);
  $("detailEditBtn").addEventListener("click", openEditModal);
  $("detailDeleteBtn").addEventListener("click", deleteCurrentClient);

  $("credsLoadSourceBtn").addEventListener("click", loadCredsSource);
  $("credsLoadDestBtn").addEventListener("click", loadCredsDest);
  $("credsAddRowBtn").addEventListener("click", addCredsRow);
  $("credsWriteBtn").addEventListener("click", writeCredsEnv);
  wireCredsSelect("credsSource");
  wireCredsSelect("credsDest");

  $("newClientForm").addEventListener("submit", submitNewClientForm);
  [...document.querySelectorAll("[data-close]")].forEach((btn) => {
    btn.addEventListener("click", () => hide(btn.dataset.close));
  });
  document.querySelectorAll(".modal").forEach((modal) => {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) hide(modal.id);
    });
  });

  startPolling();
});
// ===========================================================================
// Onboarding v2 UI (docs/ONBOARDING_V2_PLAN.md): tab navigation, the
// resumable onboarding stepper, the credentials vault, and the smoke/
// validate checks in the client detail modal. Appended to app.js — reuses
// its $ / show / hide / escapeHtml helpers and latestResults state.
// ===========================================================================

// -- tab navigation ----------------------------------------------------------

const PAGES = ["dashboard", "updates", "tests", "onboarding", "credentials", "ledger", "flow", "config", "catalog", "pipeline", "host"];

function switchPage(page) {
  PAGES.forEach((p) => {
    const el = document.getElementById(`page-${p}`);
    if (el) el.classList.toggle("hidden", p !== page);
  });
  document.querySelectorAll("#mainTabs .tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.page === page);
  });
  if (page === "updates") renderUpdatesPage();
  if (page === "tests") renderTestsPage();
  if (page === "onboarding") { refreshOnboardings(); populateObTemplateSelect(); }
  if (page === "credentials") { refreshVault(); populateVaultClientSelect(); }
  if (page === "ledger") refreshLedger();      // ledger.js
  if (page === "flow") refreshFlow();          // flow.js
  if (page === "config") refreshConfigPage();  // config.js
  if (page === "catalog") refreshCatalogPage();    // catalog.js
  if (page === "pipeline") refreshPipelinePage();  // pipeline.js
}

// -- onboarding stepper ------------------------------------------------------

let obSteps = [];              // [{id,title,detail}] from /api/onboardings/steps
let obCurrent = null;          // deploy_name of the opened onboarding
let obRunning = false;

async function fetchObSteps() {
  if (obSteps.length) return obSteps;
  const r = await fetch("/api/onboardings/steps");
  obSteps = await r.json();
  return obSteps;
}

async function populateObTemplateSelect() {
  const sel = $("obTemplateSelect");
  if (!sel) return;
  const prev = sel.value;
  const names = await (await fetch("/api/client-names")).json().catch(() => []);
  sel.innerHTML = (names || []).map((n) =>
    `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("") ||
    `<option value="">(no clients yet — register/monitor one first)</option>`;
  if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

async function refreshOnboardings() {
  await fetchObSteps();
  const r = await fetch("/api/onboardings");
  const list = await r.json();
  const el = $("obList");
  if (!list.length) { el.innerHTML = `<p class="empty">None yet.</p>`; return; }
  el.innerHTML = list.map((o) => {
    const chips = obSteps.map((s) => {
      const st = o.steps[s.id] || "pending";
      return `<span class="ob-chip ob-${st}" title="${escapeHtml(s.title)}: ${st}">${escapeHtml(s.title)}</span>`;
    }).join("");
    const state = o.torn_down ? " (torn down)" : "";
    return `<div class="ob-card" data-name="${escapeHtml(o.deploy_name)}">
      <strong>${escapeHtml(o.display_name || o.deploy_name)}</strong>
      <span class="muted">${escapeHtml(o.hostname || "")} — ${o.progress}${state}</span>
      <div class="ob-chiprow">${chips}</div>
    </div>`;
  }).join("");
  el.querySelectorAll(".ob-card").forEach((card) => {
    card.addEventListener("click", () => openOnboarding(card.dataset.name));
  });
  if (obCurrent) renderStepper();
}

async function openOnboarding(name) {
  obCurrent = name;
  show("obDetail");
  await renderStepper();
  $("obDetail").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function renderStepper() {
  if (!obCurrent) return;
  await fetchObSteps();
  const r = await fetch(`/api/onboardings/${encodeURIComponent(obCurrent)}`);
  if (!r.ok) { hide("obDetail"); obCurrent = null; return; }
  const rec = await r.json();
  $("obDetailTitle").textContent =
    `${rec.bundle.display_name || rec.deploy_name} — ${rec.bundle.hostname || "no hostname yet"}` +
    (rec.torn_down ? " (torn down)" : "");
  const stepsEl = $("obStepper");
  stepsEl.innerHTML = obSteps.map((s, i) => {
    const st = rec.steps[s.id] || { status: "pending", detail: "" };
    return `<div class="ob-step ob-${st.status}">
      <span class="ob-step-num">${i + 1}</span>
      <span class="ob-step-body">
        <strong>${escapeHtml(s.title)}</strong>
        <span class="muted">${escapeHtml(st.detail || s.detail)}</span>
      </span>
      <span class="ob-step-status">${st.status}</span>
      <button type="button" class="ob-run" data-step="${s.id}"
        ${obRunning ? "disabled" : ""}>${st.status === "ok" ? "Re-run" : "Run"}</button>
    </div>`;
  }).join("");
  stepsEl.querySelectorAll(".ob-run").forEach((btn) => {
    btn.addEventListener("click", () => runObStep(btn.dataset.step));
  });
  // credentials step needs the vault multiselect visible
  const credsPick = $("obCredsPick");
  const credsPending = (rec.steps.credentials || {}).status !== "ok";
  credsPick.classList.toggle("hidden", !credsPending);
  if (credsPending) renderObCredsSets();
  const res = rec.result || {};
  $("obResultInfo").innerHTML = res.port ? `<p class="muted">Instance: port ${res.port},
    <code>${escapeHtml(res.remote_dir || "")}</code>${res.admin_password
      ? ` — admin password <code>${escapeHtml(res.admin_password)}</code>` : ""}</p>` : "";
}

async function renderObCredsSets() {
  const r = await fetch("/api/vault/sets");
  const sets = await r.json();
  const el = $("obCredsSets");
  if (sets.length) {
    el.innerHTML = sets.map((s) => `<label class="ob-set"><input type="checkbox" value="${escapeHtml(s.id)}" />
        ${escapeHtml(s.name)} <span class="muted">(${escapeHtml(s.kind)})</span></label>`).join("");
    return;
  }
  // Empty vault: offer the fix RIGHT HERE instead of sending the operator
  // off to another tab mid-process (2026-07-19 feedback: "all too
  // confusing"). One click imports the template client's working
  // credentials and re-renders the checkboxes in place.
  const rec = obCurrent ? await (await fetch(`/api/onboardings/${encodeURIComponent(obCurrent)}`)).json() : null;
  const tmpl = rec?.bundle?.template_client_name || (latestResults[0] || {}).name || "";
  el.innerHTML = `<p class="empty">No credentials stored yet.</p>
    <button type="button" id="obCredsImportBtn" class="primary">
      Import working credentials from ${escapeHtml(tmpl)}</button>
    <span class="muted" id="obCredsImportStatus"></span>`;
  const btn = document.getElementById("obCredsImportBtn");
  if (btn) btn.addEventListener("click", async () => {
    const st = document.getElementById("obCredsImportStatus");
    st.textContent = "importing…";
    const resp = await fetch("/api/vault/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_name: tmpl }) });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) { st.textContent = data.detail || data.error || "import failed"; return; }
    await renderObCredsSets();          // re-render: checkboxes appear
    document.querySelectorAll("#obCredsSets input[type=checkbox]").forEach((c) => { c.checked = true; });
  });
}

async function runObStep(stepId, opts = {}) {
  if ((obRunning && !opts.fromChain) || !obCurrent) return false;
  obRunning = true;
  let stepOk = false;
  const consoleEl = $("obConsole");
  consoleEl.textContent = `— running ${stepId} —\n`;
  show("obConsole");
  const setIds = [...document.querySelectorAll("#obCredsSets input:checked")].map((i) => i.value);
  try {
    const resp = await fetch(
      `/api/onboardings/${encodeURIComponent(obCurrent)}/step/${encodeURIComponent(stepId)}/run`,
      { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ set_ids: setIds }) });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        if (ev.type === "log") {
          consoleEl.textContent += ev.line + "\n";
          consoleEl.scrollTop = consoleEl.scrollHeight;
        } else if (ev.type === "result") {
          stepOk = !!ev.ok;
          consoleEl.textContent += ev.ok
            ? `\n✔ ${stepId} ok\n`
            : `\n✘ ${stepId} failed: ${ev.error || "see log above"}\n`;
        }
      }
    }
  } catch (e) {
    consoleEl.textContent += `\n(request failed: ${e})\n`;
  }
  obRunning = false;
  if (!opts.fromChain) {
    await renderStepper();
    await refreshOnboardings();
  }
  return stepOk;
}

// -- "Run remaining steps": the hands-off mode. Starts at the first step
// that isn't ok and runs forward. The DNS step is special-cased: a failure
// there usually just means propagation hasn't happened yet, so it retries
// every 20s (up to 30 attempts ≈ 10 minutes) with a visible countdown
// instead of stopping the chain. Any OTHER failure stops the chain — that's
// a real problem to look at, and every step stays individually re-runnable.
let obChainActive = false;

async function runObRemaining() {
  if (obChainActive || obRunning || !obCurrent) return;
  obChainActive = true;
  const btn = $("obRunAllBtn");
  btn.textContent = "Stop auto-run";
  const consoleEl = $("obConsole");
  show("obConsole");
  try {
    const r = await fetch(`/api/onboardings/${encodeURIComponent(obCurrent)}`);
    const rec = await r.json();
    await fetchObSteps();
    const pending = obSteps.map((st) => st.id)
      .filter((id) => (rec.steps[id] || {}).status !== "ok");
    for (const stepId of pending) {
      if (!obChainActive) { consoleEl.textContent += "\n(auto-run stopped)\n"; break; }
      if (stepId === "credentials"
          && !document.querySelectorAll("#obCredsSets input:checked").length) {
        consoleEl.textContent += "\nno credentials selected — importing working ones "
          + "from the template client automatically…\n";
        const rr = await fetch(`/api/onboardings/${encodeURIComponent(obCurrent)}`);
        const rrec = await rr.json();
        const tmpl = rrec?.bundle?.template_client_name || "";
        await fetch("/api/vault/import", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ client_name: tmpl }) }).catch(() => null);
        await renderObCredsSets();
        document.querySelectorAll("#obCredsSets input[type=checkbox]")
          .forEach((c) => { c.checked = true; });
        if (!document.querySelectorAll("#obCredsSets input:checked").length) {
          consoleEl.textContent += "⏸ nothing usable found in " + tmpl + "'s configuration "
            + "— add a credential set on the Credentials tab, then press the button again.\n";
          break;
        }
        consoleEl.textContent += "credentials imported — continuing.\n";
      }
      let ok = await runObStep(stepId, { fromChain: true });
      if (stepId === "dns") {
        let attempts = 0;
        while (!ok && obChainActive && attempts < 30) {
          attempts += 1;
          for (let sLeft = 20; sLeft > 0 && obChainActive; sLeft--) {
            btn.textContent = `Stop auto-run (DNS retry in ${sLeft}s)`;
            await new Promise((res) => setTimeout(res, 1000));
          }
          btn.textContent = "Stop auto-run";
          if (!obChainActive) break;
          consoleEl.textContent += `\n— DNS re-check ${attempts}/30 —\n`;
          ok = await runObStep("dns", { fromChain: true });
        }
      }
      if (!ok) {
        if (obChainActive) consoleEl.textContent +=
          `\n⏹ auto-run stopped at ${stepId} — fix and press Run remaining steps to resume.\n`;
        break;
      }
      await renderStepper();
    }
  } finally {
    obChainActive = false;
    btn.textContent = "Run remaining steps";
    await renderStepper();
    await refreshOnboardings();
  }
}

async function submitObForm(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = Object.fromEntries(fd.entries());
  body.medical = fd.get("medical") === "true";   // unchecked checkbox = absent = false
  const r = await fetch("/api/onboardings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert(`Could not save intake: ${err.detail || r.status}`);
    return;
  }
  const rec = await r.json();
  await refreshOnboardings();
  await openOnboarding(rec.deploy_name);
  // One-button flow (2026-07-19 feedback: "no flow"): saving the form IS
  // starting the deploy. Everything runs hands-off from here; the chain
  // only ever waits for DNS (auto-retry) and reports failures in place.
  runObRemaining();
}

let obTearingDown = false;

async function teardownCurrent() {
  if (obTearingDown) return;
  const confirmVal = $("obTeardownConfirm").value.trim();
  if (confirmVal !== obCurrent) {
    alert("Type the deploy name exactly to confirm teardown.");
    return;
  }
  obTearingDown = true;
  const goBtn = $("obTeardownGo");
  goBtn.disabled = true;
  const consoleEl = $("obConsole");
  // The old version buffered the WHOLE response before printing anything, so
  // for the 1-2 minutes the real teardown takes, the screen showed one header
  // line and nothing else — indistinguishable from "broken" (2026-07-19).
  // Now: stream line by line, exactly like the step runner does.
  consoleEl.textContent = "— teardown — (removing containers, files and web address; takes a minute or two)\n";
  show("obConsole");
  let finalOk = false;
  try {
    const resp = await fetch(`/api/onboardings/${encodeURIComponent(obCurrent)}/teardown`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: confirmVal }) });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      consoleEl.textContent += `✘ ${err.detail || `server answered ${resp.status}`}\n`;
    } else {
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.trim()) continue;
          let ev;
          try { ev = JSON.parse(line); } catch { continue; }
          if (ev.type === "log") {
            consoleEl.textContent += ev.line + "\n";
            consoleEl.scrollTop = consoleEl.scrollHeight;
          } else if (ev.type === "result") {
            finalOk = !!ev.ok;
            consoleEl.textContent += finalOk
              ? "✔ teardown complete\n"
              : `✘ teardown failed: ${ev.error || "see log above"}\n`;
          }
        }
      }
    }
  } catch (e) {
    consoleEl.textContent += `✘ request failed: ${e}\n`;
  }
  obTearingDown = false;
  goBtn.disabled = false;
  if (finalOk) {
    hide("obTeardownConfirm"); hide("obTeardownGo");
    // the record no longer exists — close the detail panel and clear the list
    obCurrent = null;
    hide("obDetail");
    await refreshOnboardings();
    refreshClients();
  }
  // on failure: everything stays on screen — the error line says what to fix,
  // and the Confirm button is live again for one retry.
}

// -- credentials vault -------------------------------------------------------
// Moved to credentials.js (vault v2, docs/TOKEN_ECONOMY_PLAN.md Phase 1) —
// the start of the app.js split. credentials.js loads AFTER this file and
// defines the globals switchPage() calls for the credentials page
// (refreshVault, populateVaultClientSelect) plus all its own wiring.

// -- wiring ------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("#mainTabs .tab").forEach((btn) => {
    btn.addEventListener("click", () => switchPage(btn.dataset.page));
  });
  // openLegacyCredsBtn + all vault element wiring: credentials.js

  $("obForm").addEventListener("submit", submitObForm);
  // Auto-suggest the subdomain from the deploy name while the subdomain
  // field is untouched — for most clients they're the same word.
  const obDeployInput = document.querySelector('#obForm input[name="deploy_name"]');
  const obSubInput = $("obSubdomain");
  let obSubTouched = false;
  obSubInput.addEventListener("input", () => { obSubTouched = !!obSubInput.value; });
  obDeployInput.addEventListener("input", () => {
    if (!obSubTouched) obSubInput.value = obDeployInput.value;
  });
  $("obRunAllBtn").addEventListener("click", () => {
    if (obChainActive) { obChainActive = false; return; }
    runObRemaining();
  });
  $("obTeardownBtn").addEventListener("click", () => {
    show("obTeardownConfirm"); show("obTeardownGo");
  });
  $("obTeardownGo").addEventListener("click", teardownCurrent);
  // "Run tests…" in the detail modal: jump to the Tests tab with this
  // client preselected — running is one deliberate click away, since the
  // chat round-trip costs real LLM tokens.
  $("detailTestsBtn").addEventListener("click", () => {
    const name = $("detailModal").dataset.name;
    if (!name) return;
    hide("detailModal");
    testSelected = new Set([name]);
    switchPage("tests");
  });
});

// ===========================================================================
// Updates tab — batch "commit updates" across the whole fleet. One window
// showing every client that's behind origin/master (with the actual pending
// commits, not just a count), select some or all, one typed confirmation
// ("update <N>"), then a single streamed run with a live per-client status
// column + console — instead of opening each client's detail modal, typing
// its name, deploying, waiting, and moving on to the next one by hand.
// Talks to POST /api/deploy-batch (NDJSON stream; parallel across hosts,
// sequential per host). Reuses $ / show / hide / escapeHtml / latestResults.
// ===========================================================================

let updSelected = new Set();   // client names checked in the Updates table
let updRunState = {};          // name -> {state: queued|running|ok|fail, stage, commit, error}
let updRunning = false;

// Rows shown in the table: behind > 0, OR touched by the current/last run
// (so a freshly updated client's ✔ row doesn't vanish mid-view the moment a
// background poll notices it's no longer behind).
function updEligibleRows() {
  return latestResults.filter((r) =>
    (r.version && r.version.ok && (r.version.behind || 0) > 0) ||
    Object.prototype.hasOwnProperty.call(updRunState, r.name));
}

function updBehindRows() {
  return latestResults.filter((r) => r.version && r.version.ok && (r.version.behind || 0) > 0);
}

function updStatusCell(name) {
  const s = updRunState[name];
  if (!s) return `<span class="muted">—</span>`;
  if (s.state === "queued") return `<span class="muted">queued…</span>`;
  if (s.state === "running") return `<span class="upd-running">⟳ updating…</span>`;
  if (s.state === "ok") return `<span class="smoke-ok">✔ updated${s.commit ? " → " + escapeHtml(s.commit) : ""}</span>`;
  return `<span class="smoke-fail" title="${escapeHtml(s.error || "")}">✘ failed (${escapeHtml(s.stage || "?")} stage)</span>`;
}

function renderUpdatesPage() {
  const tbody = $("updTableBody");
  if (!tbody) return;

  // Tab badge: how many clients need updates, visible from any tab.
  const behindCount = updBehindRows().length;
  const badge = $("updTabBadge");
  if (badge) {
    badge.textContent = behindCount || "";
    badge.classList.toggle("hidden", !behindCount);
  }

  const rows = updEligibleRows();
  const listed = new Set(rows.map((r) => r.name));
  updSelected = new Set([...updSelected].filter((n) => listed.has(n)));

  // Remember which per-row commit lists are expanded — this table is
  // re-rendered on every background poll, which would otherwise snap an
  // open <details> shut while you're reading it.
  const openDetails = new Set(
    [...tbody.querySelectorAll("details[open][data-name]")].map((d) => d.dataset.name));

  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">${
      latestResults.length
        ? "Everything is up to date — no client is behind origin/master."
        : "No clients configured yet — nothing to update."
    }</td></tr>`;
  } else {
    tbody.innerHTML = rows.map((r) => {
      const v = r.version || {};
      const behind = v.behind || 0;
      const checked = updSelected.has(r.name) ? "checked" : "";
      const disabled = updRunning || !behind ? "disabled" : "";
      const infraBadge = v.infra_risk
        ? `<span class="upd-infra" title="This range touches docker-compose/Dockerfile/deploy files — review before updating: ${escapeHtml((v.infra_files || []).join(", "))}">⚠ infra</span>`
        : "";
      const commitLines = (v.behind_commits || []).map((l) => `<li>${escapeHtml(l)}</li>`).join("");
      const moreNote = behind > (v.behind_commits || []).length
        ? `<p class="muted">…and ${behind - v.behind_commits.length} more not shown.</p>` : "";
      const pendingCell = behind
        ? `<details class="upd-commits" data-name="${escapeHtml(r.name)}"${openDetails.has(r.name) ? " open" : ""}>
             <summary>${behind} commit${behind === 1 ? "" : "s"}</summary>
             <ul class="behind-commits">${commitLines}</ul>${moreNote}
           </details>`
        : `<span class="muted">up to date</span>`;
      return `<tr>
        <td><input type="checkbox" class="upd-check" data-name="${escapeHtml(r.name)}" ${checked} ${disabled} /></td>
        <td><a href="#" class="upd-name" data-name="${escapeHtml(r.name)}">${escapeHtml(r.name)}</a></td>
        <td><code>${escapeHtml(v.commit || "?")}</code></td>
        <td>${behind ? behind : `<span class="muted">0</span>`} ${infraBadge}</td>
        <td>${pendingCell}</td>
        <td>${updStatusCell(r.name)}</td>
        <td><button type="button" class="upd-row-btn icon-btn" data-name="${escapeHtml(r.name)}" ${updRunning ? "disabled" : ""}>Update</button></td>
      </tr>`;
    }).join("");

    [...tbody.querySelectorAll(".upd-check")].forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) updSelected.add(cb.dataset.name);
        else updSelected.delete(cb.dataset.name);
        updSyncToolbar();
      });
    });
    [...tbody.querySelectorAll(".upd-row-btn")].forEach((btn) => {
      btn.addEventListener("click", () => showUpdConfirm([btn.dataset.name]));
    });
    [...tbody.querySelectorAll(".upd-name")].forEach((el) => {
      el.addEventListener("click", (e) => { e.preventDefault(); openDetail(el.dataset.name); });
    });
  }

  const othersEl = $("updOthers");
  if (othersEl) {
    const upToDate = latestResults.filter((r) =>
      r.version && r.version.ok && !(r.version.behind || 0) &&
      !Object.prototype.hasOwnProperty.call(updRunState, r.name)).length;
    const unknown = latestResults.filter((r) => !r.version || !r.version.ok).length;
    const parts = [];
    if (upToDate) parts.push(`${upToDate} client(s) already up to date`);
    if (unknown) parts.push(`${unknown} unreachable/unknown (no SSH check — see Dashboard)`);
    othersEl.textContent = parts.length ? `Not listed: ${parts.join("; ")}.` : "";
  }

  updSyncToolbar();
}

function updSyncToolbar() {
  const btn = $("updUpdateSelectedBtn");
  if (!btn) return;
  btn.disabled = updRunning || updSelected.size === 0;
  btn.textContent = updSelected.size ? `Update selected (${updSelected.size})` : "Update selected";
  $("updSelectAllBtn").disabled = updRunning;
  $("updSelectNoneBtn").disabled = updRunning;
  $("updRefreshBtn").disabled = updRunning;
  $("updClearBtn").classList.toggle("hidden", updRunning || !Object.keys(updRunState).length);
  $("updSummary").textContent = updRunning ? "updating — watch the console below" : "";
}

function showUpdConfirm(names) {
  if (updRunning || !names.length) return;
  const area = $("updConfirmArea");
  const phrase = `update ${names.length}`;
  const infraNames = names.filter((n) => {
    const r = latestResults.find((x) => x.name === n);
    return r && r.version && r.version.infra_risk;
  });
  const infraNotice = infraNames.length
    ? `<p class="infra-warning">⚠ ${infraNames.length} of these (${infraNames.map(escapeHtml).join(", ")})
        include infrastructure-file changes (docker-compose / Dockerfile / deploy/**) — worth actually
        looking at those commits first. The per-client pre-flight check still refuses container-name
        collisions, but that's the only class of infra problem it catches.</p>`
    : "";
  area.innerHTML = `
    <div class="deploy-confirm">
      <p class="muted">About to update <strong>${names.length}</strong> client(s):
        <strong>${names.map(escapeHtml).join(", ")}</strong>. Each one runs the same guarded pipeline as
        the individual Deploy button (<code>git pull --ff-only</code> → build → collision precheck →
        restart) — parallel across VPSes, strictly one at a time per VPS. A failure on one client never
        stops the others.</p>
      ${infraNotice}
      <label>Type "<strong>${phrase}</strong>" to confirm:
        <input id="updConfirmInput" type="text" autocomplete="off" spellcheck="false" />
      </label>
      <div class="modal-actions">
        <button id="updConfirmGoBtn" type="button" class="danger" disabled>Update ${names.length} client(s)</button>
        <button id="updConfirmCancelBtn" type="button">Cancel</button>
      </div>
    </div>`;
  show("updConfirmArea");
  const input = $("updConfirmInput");
  const goBtn = $("updConfirmGoBtn");
  input.addEventListener("input", () => {
    goBtn.disabled = input.value.trim().toLowerCase() !== phrase;
  });
  input.focus();
  goBtn.addEventListener("click", () => startBatchUpdate(names));
  $("updConfirmCancelBtn").addEventListener("click", () => hide("updConfirmArea"));
}

function updConsoleLine(text) {
  const el = $("updConsole");
  el.textContent += text + "\n";
  el.scrollTop = el.scrollHeight;
}

function handleUpdEvent(ev) {
  if (ev.type === "start") {
    updRunState[ev.name] = { state: "running" };
    updConsoleLine(`▶ ${ev.name}: updating (pull → build → precheck → restart)…`);
  } else if (ev.type === "result") {
    if (ev.ok) {
      updRunState[ev.name] = { state: "ok", commit: ev.commit };
      updConsoleLine(`✔ ${ev.name}: updated${ev.commit ? ", now at " + ev.commit : ""}`);
    } else {
      updRunState[ev.name] = { state: "fail", stage: ev.stage, error: ev.error };
      updConsoleLine(`✘ ${ev.name}: FAILED at the ${ev.stage || "?"} stage — ${ev.error || "no error text"}`);
      const tail = (ev.output || "").split("\n").slice(-12).map((l) => "    " + l).join("\n");
      if (tail.trim()) updConsoleLine(tail);
    }
  } else if (ev.type === "done") {
    updConsoleLine(`— done: ${ev.ok_count} ok, ${ev.fail_count} failed —`);
    if (ev.error) updConsoleLine(`✘ ${ev.error}`);
  }
  renderUpdatesPage();
}

async function startBatchUpdate(names) {
  if (updRunning || !names.length) return;
  updRunning = true;
  updRunState = {};
  names.forEach((n) => { updRunState[n] = { state: "queued" }; });
  hide("updConfirmArea");
  const consoleEl = $("updConsole");
  consoleEl.textContent = "";
  show("updConsole");
  updConsoleLine(`— updating ${names.length} client(s): ${names.join(", ")} —`);
  renderUpdatesPage();
  try {
    const resp = await fetch("/api/deploy-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names, confirm: `update ${names.length}` }),
    });
    if (!resp.ok) {
      // Precondition failures (bad name, etc.) come back as plain JSON
      // before any streaming starts — nothing was deployed.
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `server answered ${resp.status}`);
    }
    if (!resp.body) throw new Error("this browser doesn't support streaming responses");
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        handleUpdEvent(ev);
      }
    }
  } catch (e) {
    updConsoleLine(`✘ batch request failed: ${e.message || e}`);
    Object.keys(updRunState).forEach((n) => {
      const s = updRunState[n];
      if (s.state === "queued" || s.state === "running") {
        updRunState[n] = { state: "fail", stage: "request", error: String(e.message || e) };
      }
    });
  }
  updRunning = false;
  updSelected.clear();
  renderUpdatesPage();
  // Pull fresh statuses right away so behind-counts reflect the run instead
  // of waiting for the next poll tick.
  refreshClients();
}

document.addEventListener("DOMContentLoaded", () => {
  $("updRefreshBtn").addEventListener("click", refreshClients);
  $("updSelectAllBtn").addEventListener("click", () => {
    updBehindRows().forEach((r) => updSelected.add(r.name));
    renderUpdatesPage();
  });
  $("updSelectNoneBtn").addEventListener("click", () => {
    updSelected.clear();
    renderUpdatesPage();
  });
  $("updUpdateSelectedBtn").addEventListener("click", () => showUpdConfirm([...updSelected]));
  $("updClearBtn").addEventListener("click", () => {
    if (updRunning) return;
    updRunState = {};
    hide("updConsole");
    renderUpdatesPage();
  });
});

// ===========================================================================
// Tests tab — the fleet test runner, same shape as the Updates tab: every
// client in one table, select some or all, one click, a live per-client
// status column + console. Each client runs the full check suite (the
// 8-check smoke suite + live site_config.yaml validation) via
// POST /api/test-batch (NDJSON stream; parallel across hosts, sequential
// per host). Replaces the old, easy-to-miss "Run smoke suite"/"Validate
// live config" buttons that were buried at the bottom of the detail modal.
// ===========================================================================

let testSelected = new Set();  // client names checked in the Tests table
let testRunState = {};         // name -> {state, summary, checks, config, error}
let testRunning = false;

function testResultCell(name) {
  const s = testRunState[name];
  if (!s) return `<span class="muted">not run yet</span>`;
  if (s.state === "queued") return `<span class="muted">queued…</span>`;
  if (s.state === "running") return `<span class="upd-running">⟳ running checks…</span>`;

  const sum = s.summary || {};
  const checks = s.checks || [];
  const cfg = s.config || {};
  let headline;
  if (s.state === "ok") {
    headline = `<span class="smoke-ok">✔ ${sum.passed ?? "?"}/${sum.total ?? "?"} checks passed</span>`;
    if ((sum.warnings || []).length) {
      headline += ` <span class="smoke-warn">⚠ ${sum.warnings.length} warning(s)</span>`;
    }
  } else {
    const failed = (sum.failed_checks || []).join(", ");
    headline = `<span class="smoke-fail">✘ failed${failed ? ` — ${escapeHtml(failed)}` : ""}</span>`;
    if ((sum.config_errors || []).length) {
      headline += ` <span class="smoke-fail">+ ${sum.config_errors.length} config error(s)</span>`;
    }
  }
  if (!checks.length && s.error) {
    return `${headline}<div class="muted">${escapeHtml(s.error)}</div>`;
  }
  const checkRows = checks.map((c) =>
    `<div class="smoke-row ${c.ok ? "smoke-ok" : (c.severity === "warn" ? "smoke-warn" : "smoke-fail")}">
       ${c.ok ? "✔" : (c.severity === "warn" ? "⚠" : "✘")} <strong>${escapeHtml(c.check)}</strong>
       <span class="muted">${escapeHtml(c.detail || "")}</span></div>`).join("");
  const cfgRows = [
    ...(cfg.errors || []).map((x) => `<div class="smoke-row smoke-fail">✘ config: ${escapeHtml(x)}</div>`),
    ...(cfg.warnings || []).map((x) => `<div class="smoke-row smoke-warn">⚠ config: ${escapeHtml(x)}</div>`),
  ].join("") || `<div class="smoke-row smoke-ok">✔ <strong>site_config.yaml</strong> <span class="muted">valid</span></div>`;
  return `${headline}
    <details class="upd-commits" data-name="${escapeHtml(name)}">
      <summary>details</summary>${checkRows}${cfgRows}
    </details>`;
}

function renderTestsPage() {
  const tbody = $("testTableBody");
  if (!tbody) return;

  const listed = new Set(latestResults.map((r) => r.name));
  testSelected = new Set([...testSelected].filter((n) => listed.has(n)));

  // Keep expanded per-row details open across the background poll re-render,
  // same as the Updates table.
  const openDetails = new Set(
    [...tbody.querySelectorAll("details[open][data-name]")].map((d) => d.dataset.name));

  if (latestResults.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty">No clients configured yet — nothing to test.</td></tr>`;
  } else {
    tbody.innerHTML = latestResults.map((r) => {
      const checked = testSelected.has(r.name) ? "checked" : "";
      const h = r.health || {};
      const healthCell = h.up
        ? `<span class="ok">●</span> UP <span class="muted">${h.latency_ms}ms</span>`
        : `<span class="down">●</span> DOWN`;
      let resultHtml = testResultCell(r.name);
      if (openDetails.has(r.name)) {
        resultHtml = resultHtml.replace("<details class=\"upd-commits\"", "<details open class=\"upd-commits\"");
      }
      return `<tr>
        <td><input type="checkbox" class="test-check" data-name="${escapeHtml(r.name)}" ${checked} ${testRunning ? "disabled" : ""} /></td>
        <td><a href="#" class="test-name" data-name="${escapeHtml(r.name)}">${escapeHtml(r.name)}</a></td>
        <td>${healthCell}</td>
        <td class="test-result-cell">${resultHtml}</td>
        <td><button type="button" class="test-row-btn icon-btn" data-name="${escapeHtml(r.name)}" ${testRunning ? "disabled" : ""}>Test</button></td>
      </tr>`;
    }).join("");

    [...tbody.querySelectorAll(".test-check")].forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) testSelected.add(cb.dataset.name);
        else testSelected.delete(cb.dataset.name);
        testSyncToolbar();
      });
    });
    [...tbody.querySelectorAll(".test-row-btn")].forEach((btn) => {
      btn.addEventListener("click", () => startBatchTests([btn.dataset.name]));
    });
    [...tbody.querySelectorAll(".test-name")].forEach((el) => {
      el.addEventListener("click", (e) => { e.preventDefault(); openDetail(el.dataset.name); });
    });
  }

  testSyncToolbar();
}

function testSyncToolbar() {
  const btn = $("testRunBtn");
  if (!btn) return;
  btn.disabled = testRunning || testSelected.size === 0;
  btn.textContent = testSelected.size
    ? `Run tests on selected (${testSelected.size})` : "Run tests on selected";
  $("testSelectAllBtn").disabled = testRunning;
  $("testSelectNoneBtn").disabled = testRunning;
  $("testClearBtn").classList.toggle("hidden", testRunning || !Object.keys(testRunState).length);
  $("testSummary").textContent = testRunning ? "running — watch the console below" : "";
}

function testConsoleLine(text) {
  const el = $("testConsole");
  el.textContent += text + "\n";
  el.scrollTop = el.scrollHeight;
}

function handleTestEvent(ev) {
  if (ev.type === "start") {
    testRunState[ev.name] = { state: "running" };
    testConsoleLine(`▶ ${ev.name}: running checks (health, TLS, admin API, chat round-trip, config)…`);
  } else if (ev.type === "result") {
    const sum = ev.summary || {};
    if (ev.ok) {
      testRunState[ev.name] = { state: "ok", summary: sum, checks: ev.checks, config: ev.config };
      const warn = (sum.warnings || []).length ? ` (⚠ ${sum.warnings.join("; ")})` : "";
      testConsoleLine(`✔ ${ev.name}: ${sum.passed}/${sum.total} checks passed, config valid${warn}`);
    } else {
      testRunState[ev.name] = { state: "fail", summary: sum, checks: ev.checks, config: ev.config, error: ev.error };
      testConsoleLine(`✘ ${ev.name}: ${ev.error || "checks failed"}`);
      (ev.checks || []).filter((c) => !c.ok && c.severity !== "warn").forEach((c) => {
        testConsoleLine(`    ✘ ${c.check}: ${c.detail || ""}`);
      });
      ((ev.config || {}).errors || []).forEach((x) => testConsoleLine(`    ✘ config: ${x}`));
    }
  } else if (ev.type === "done") {
    testConsoleLine(`— done: ${ev.ok_count} ok, ${ev.fail_count} failed —`);
    if (ev.error) testConsoleLine(`✘ ${ev.error}`);
  }
  renderTestsPage();
}

async function startBatchTests(names) {
  if (testRunning || !names.length) return;
  testRunning = true;
  testRunState = {};
  names.forEach((n) => { testRunState[n] = { state: "queued" }; });
  const consoleEl = $("testConsole");
  consoleEl.textContent = "";
  show("testConsole");
  testConsoleLine(`— testing ${names.length} client(s): ${names.join(", ")} —`);
  renderTestsPage();
  try {
    const resp = await fetch("/api/test-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `server answered ${resp.status}`);
    }
    if (!resp.body) throw new Error("this browser doesn't support streaming responses");
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        handleTestEvent(ev);
      }
    }
  } catch (e) {
    testConsoleLine(`✘ test request failed: ${e.message || e}`);
    Object.keys(testRunState).forEach((n) => {
      const s = testRunState[n];
      if (s.state === "queued" || s.state === "running") {
        testRunState[n] = { state: "fail", error: String(e.message || e), summary: {} };
      }
    });
  }
  testRunning = false;
  renderTestsPage();
}

// -- backups setup (same page, same selection) -------------------------------

let bkRunning = false;

function bkConsoleLine(text) {
  const el = $("bkConsole");
  el.textContent += text + "\n";
  el.scrollTop = el.scrollHeight;
}

function showBkConfirm(names) {
  if (bkRunning) return;
  if (!names.length) {
    $("bkSummary").textContent = "select clients in the table above first";
    return;
  }
  const area = $("bkConfirmArea");
  const phrase = `backups ${names.length}`;
  area.innerHTML = `
    <div class="deploy-confirm">
      <p class="muted">About to install/repair the nightly backup timer on
        <strong>${names.length}</strong> client(s): <strong>${names.map(escapeHtml).join(", ")}</strong> —
        then run a real backup on each right now and verify the encrypted archive appears.
        Idempotent; an already-configured client just gets re-verified.</p>
      <label>Type "<strong>${phrase}</strong>" to confirm:
        <input id="bkConfirmInput" type="text" autocomplete="off" spellcheck="false" />
      </label>
      <div class="modal-actions">
        <button id="bkConfirmGoBtn" type="button" class="danger" disabled>Set up backups on ${names.length} client(s)</button>
        <button id="bkConfirmCancelBtn" type="button">Cancel</button>
      </div>
    </div>`;
  show("bkConfirmArea");
  const input = $("bkConfirmInput");
  const goBtn = $("bkConfirmGoBtn");
  input.addEventListener("input", () => {
    goBtn.disabled = input.value.trim().toLowerCase() !== phrase;
  });
  input.focus();
  goBtn.addEventListener("click", () => startBackupSetup(names));
  $("bkConfirmCancelBtn").addEventListener("click", () => hide("bkConfirmArea"));
}

async function startBackupSetup(names) {
  if (bkRunning || !names.length) return;
  bkRunning = true;
  hide("bkConfirmArea");
  $("bkSetupBtn").disabled = true;
  $("bkSummary").textContent = "running — each client takes a real backup, give it a minute";
  const consoleEl = $("bkConsole");
  consoleEl.textContent = "";
  show("bkConsole");
  bkConsoleLine(`— setting up backups on ${names.length} client(s): ${names.join(", ")} —`);
  try {
    const resp = await fetch("/api/backup-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ names, confirm: `backups ${names.length}` }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || `server answered ${resp.status}`);
    }
    if (!resp.body) throw new Error("this browser doesn't support streaming responses");
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        if (ev.type === "start") {
          bkConsoleLine(`▶ ${ev.name}: installing timer + running a test backup…`);
        } else if (ev.type === "result") {
          if (ev.ok) {
            const archiveNote = ev.newest ? ` — newest: ${ev.newest.split("/").pop()}` : "";
            bkConsoleLine(`✔ ${ev.name}: nightly backups ACTIVE, fresh archive verified `
              + `(${ev.archives} on disk${archiveNote})`);
            if (ev.error) bkConsoleLine(`    note: ${ev.error}`);
          } else {
            bkConsoleLine(`✘ ${ev.name}: failed at the ${ev.stage || "?"} stage — ${ev.error || "no error text"}`);
            const tail = (ev.output || "").split("\n").slice(-12).map((l) => "    " + l).join("\n");
            if (tail.trim()) bkConsoleLine(tail);
          }
        } else if (ev.type === "done") {
          bkConsoleLine(`— done: ${ev.ok_count} ok, ${ev.fail_count} failed —`);
          if (ev.error) bkConsoleLine(`✘ ${ev.error}`);
        }
      }
    }
  } catch (e) {
    bkConsoleLine(`✘ backup-setup request failed: ${e.message || e}`);
  }
  bkRunning = false;
  $("bkSetupBtn").disabled = false;
  $("bkSummary").textContent = "";
}

document.addEventListener("DOMContentLoaded", () => {
  $("testSelectAllBtn").addEventListener("click", () => {
    latestResults.forEach((r) => testSelected.add(r.name));
    renderTestsPage();
  });
  $("testSelectNoneBtn").addEventListener("click", () => {
    testSelected.clear();
    renderTestsPage();
  });
  $("testRunBtn").addEventListener("click", () => startBatchTests([...testSelected]));
  $("testClearBtn").addEventListener("click", () => {
    if (testRunning) return;
    testRunState = {};
    hide("testConsole");
    renderTestsPage();
  });
  $("bkSetupBtn").addEventListener("click", () => showBkConfirm([...testSelected]));
});
