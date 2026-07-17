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
  "OLLAMALOCAL_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY",
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
  const cls = up.uptime_7d_pct >= 99 ? "ok" : up.uptime_7d_pct >= 95 ? "warning" : "down";
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
      totalCost += c.estimated_usd || 0;
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
        <div class="impact-value">$${totalCost.toFixed(2)}</div>
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

function applyDetail(r) {
  $("detailModal").dataset.name = r.name;
  $("detailName").textContent = r.name;
  const pill = $("detailStatus");
  pill.textContent = r.status.toUpperCase();
  pill.className = `status-pill ${r.status}`;

  $("detailHealth").textContent = fmtHealth(r.health);

  const v = r.version;
  let versionHtml = fmtVersion(v);
  if (v.ok && v.infra_risk) {
    const files = (v.infra_files || []).map((f) => `<li>${escapeHtml(f)}</li>`).join("");
    versionHtml += `<div class="infra-warning">⚠ Includes changes to infrastructure files — review before deploying:<ul>${files}</ul></div>`;
  }
  if (v.ok && v.behind_commits && v.behind_commits.length) {
    const shown = v.behind_commits
      .map((line) => `<li>${escapeHtml(line)}</li>`)
      .join("");
    const truncatedNote = v.behind > v.behind_commits.length
      ? `<p class="muted">…and ${v.behind - v.behind_commits.length} more not shown.</p>` : "";
    versionHtml += `<p class="muted" style="margin:0.5rem 0 0.2rem;">What's not deployed yet:</p>` +
      `<ul class="behind-commits">${shown}</ul>${truncatedNote}`;
  }
  if (v.ok && v.containers && v.containers.length) {
    versionHtml += "<br>" + v.containers.map((c) => `${escapeHtml(c.name)}: ${escapeHtml(c.state)} ${escapeHtml(c.health || "")}`).join("<br>");
  }
  $("detailVersion").innerHTML = versionHtml;
  renderDeployArea(r);

  const u = r.usage;
  let usageHtml;
  if (u.ok) {
    usageHtml = `Chats: ${u.chats}<br>Tokens — input: ${u.input_tokens.toLocaleString()}, cached: ${u.cached_tokens.toLocaleString()}, output: ${u.output_tokens.toLocaleString()}<br>Total: ${u.total_tokens.toLocaleString()}`;
    if (r.quota) {
      usageHtml += `<br>Quota: ${r.quota.toLocaleString()} tokens/month`;
      if (r.over_quota) usageHtml += "<br><strong>*** OVER QUOTA — billable overage ***</strong>";
    } else {
      usageHtml += "<br>No quota configured.";
    }
  } else {
    usageHtml = `unknown — ${escapeHtml(u.error || "check failed")}`;
  }
  $("detailUsage").innerHTML = usageHtml;

  const bar = $("detailQuotaBar");
  const pct = r.quota ? Math.min(100, (u.total_tokens / r.quota) * 100) : 0;
  bar.style.width = pct + "%";
  bar.className = "progress-fill" + (r.over_quota ? " over" : "");

  const res = r.resources || {};
  let resourcesHtml;
  if (res.ok) {
    const containerLines = (res.containers || [])
      .map((c) => `${escapeHtml(c.name)}: cpu ${c.cpu_pct != null ? c.cpu_pct + "%" : "?"}, mem ${c.mem_pct != null ? c.mem_pct + "%" : "?"} (${escapeHtml(c.mem_usage || "")})`)
      .join("<br>");
    resourcesHtml = containerLines || "No containers found.";
    if (res.data_disk_usage) resourcesHtml += `<br>/data usage: ${escapeHtml(res.data_disk_usage)}`;
  } else {
    resourcesHtml = `unknown — ${escapeHtml(res.error || "check failed")}`;
  }
  $("detailResources").innerHTML = resourcesHtml;

  const i = r.interactions || {};
  let interactionsHtml;
  if (i.ok) {
    interactionsHtml = `Bookings: ${i.bookings}<br>Reschedules: ${i.reschedules}<br>Cancellations: ${i.cancellations}<br>` +
      `Callbacks (human handoff): ${i.callbacks}<br>Registrations: ${i.registrations}` +
      (i.other ? `<br>Other audited actions: ${i.other}` : "") +
      `<br><strong>Est. receptionist time saved: ${fmtHM(i.minutes_saved)}</strong>`;
  } else {
    interactionsHtml = `unknown — ${escapeHtml(i.error || "check failed")}`;
  }
  $("detailInteractions").innerHTML = interactionsHtml;

  const c = r.cost || {};
  let costHtml;
  if (!c.ok) {
    costHtml = `unknown — ${escapeHtml(c.error || "check failed")}`;
  } else if (!c.configured) {
    costHtml = `<span class="muted">No cost-per-token rate configured for this client — edit it to see an estimate.</span>`;
  } else {
    costHtml = `$${c.estimated_usd.toFixed(4)} this month (from configured rates)`;
  }
  $("detailCost").innerHTML = costHtml;

  const up = r.uptime || {};
  let uptimeHtml;
  if (up.uptime_7d_pct == null) {
    uptimeHtml = `<span class="muted">Not enough history yet — ops-console records a sample on every poll.</span>`;
  } else {
    uptimeHtml = `Last 24h: ${up.uptime_24h_pct != null ? up.uptime_24h_pct + "%" : "—"} (${up.samples_24h} samples)<br>` +
      `Last 7d: ${up.uptime_7d_pct}% (${up.samples_7d} samples)`;
    if (up.latency_p50_ms != null) uptimeHtml += `<br>Latency p50: ${up.latency_p50_ms}ms, p95: ${up.latency_p95_ms}ms`;
  }
  $("detailUptime").innerHTML = uptimeHtml;

  $("detailChecked").textContent = "Last checked: " + r.checked_at;
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
  show("settingsModal");
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
  $("editForm").addEventListener("submit", submitEditForm);
  $("fetchTokenBtn").addEventListener("click", fetchTokenViaSSH);
  $("detailRefreshBtn").addEventListener("click", refreshOneDetail);
  $("detailEditBtn").addEventListener("click", openEditModal);
  $("detailDeleteBtn").addEventListener("click", deleteCurrentClient);
  $("credsBtn").addEventListener("click", () => openCredsModal());
  $("credsLoadSourceBtn").addEventListener("click", loadCredsSource);
  $("credsLoadDestBtn").addEventListener("click", loadCredsDest);
  $("credsAddRowBtn").addEventListener("click", addCredsRow);
  $("credsWriteBtn").addEventListener("click", writeCredsEnv);
  wireCredsSelect("credsSource");
  wireCredsSelect("credsDest");
  $("newClientBtn").addEventListener("click", openNewClientModal);
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
