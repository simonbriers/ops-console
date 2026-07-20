// ===========================================================================
// Flow tab — the tanks-and-pipes visual (docs/TOKEN_ECONOMY_PLAN.md Phase 4).
// Loads AFTER app.js/ledger.js; provides refreshFlow() for switchPage().
//
// Left column: SOURCE tanks — one per credential that saw flow this month.
//   Finite sources (a cap is set on the Tokens tab: free-tier quota, prepaid
//   credits) drain visibly; PAYG pipes show ∞ and never drain. Local models
//   render as a little well (€0, bottomless).
// Right column: CLIENT tanks — fill = % of plan allowance left this cycle;
//   amber under 20%, red at empty (overage territory).
// Pipes connect source → client wherever tokens flowed (the api_key_alias
// join), stroke width ∝ share of flow, dash animation speed ∝ the client's
// recent burn. Click any tank for the drill-down panel underneath.
// Pure SVG + CSS animation — the graph is a handful of nodes; no library.
// ===========================================================================

const FLOW_ROLE_COLORS = { llm: "#3b82a0", stt: "#7c6bb0", tts: "#b08a3b",
  email: "#5a9a68", sms: "#a05b78", unknown: "#999" };

function flowTok(n) {
  if (n === null || n === undefined) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

async function refreshFlow(reuseData) {
  const status = $("flowStatus");
  if (!reuseData || !ledgerData) {
    status.textContent = "loading…";
    try {
      const r = await fetch("/api/ledger/summary");
      ledgerData = await r.json();
    } catch { status.textContent = "couldn't load ledger"; return; }
  }
  status.textContent = `month ${ledgerData.month} · fleet ${flowTok(ledgerData.fleet.total_tokens)} tokens`
    + ` · margin €${(ledgerData.fleet.margin_eur ?? 0).toFixed(2)}`;
  drawFlow();
}

function drawFlow() {
  const sources = ledgerData.sources || [];
  const clients = ledgerData.clients || [];
  const el = $("flowCanvas");
  if (!sources.length && !clients.length) {
    el.innerHTML = `<p class="empty">No flow yet — collect snapshots on the Tokens tab first.</p>`;
    return;
  }
  const TANK_W = 130, TANK_H = 72, GAP = 34, PAD = 46;
  const SRC_X = 30, CLI_X = 640, WIDTH = 810;
  const rows = Math.max(sources.length, clients.length);
  const height = PAD * 2 + rows * (TANK_H + GAP) - GAP;
  const maxFlow = Math.max(1, ...sources.flatMap((s) => Object.values(s.clients || {})));
  const maxBurn = Math.max(1, ...clients.map((c) => c.burn_24h || 0));

  const srcY = (i) => PAD + i * (TANK_H + GAP);
  const cliY = (i) => PAD + i * (TANK_H + GAP);
  const cliIndex = {};
  clients.forEach((c, i) => { cliIndex[c.name] = i; });

  let defs = "", pipes = "", tanks = "";

  // --- source tanks (left) ---
  sources.forEach((s, i) => {
    const y = srcY(i);
    const finite = s.cap_left_pct !== null && s.cap_left_pct !== undefined;
    const fillPct = finite ? s.cap_left_pct : 1;
    const fillH = Math.max(2, Math.round((TANK_H - 14) * fillPct));
    const isLocal = !s.set_id && (s.aliases || []).includes("local");
    const color = s.tier === "free" ? "var(--ok)" : (isLocal ? "#888" : "#3b82a0");
    const cls = finite && fillPct <= 0.2 ? "flow-tank-warn" : "";
    tanks += `
    <g class="flow-tank flow-src ${cls}" data-kind="source" data-key="${escapeHtml(s.set_id || s.set_name)}">
      <rect x="${SRC_X}" y="${y}" width="${TANK_W}" height="${TANK_H}" rx="8" class="flow-tank-shell"/>
      <rect x="${SRC_X + 4}" y="${y + 10 + (TANK_H - 14 - fillH)}" width="${TANK_W - 8}"
        height="${fillH}" rx="5" fill="${color}" opacity="0.55"/>
      <text x="${SRC_X + TANK_W / 2}" y="${y - 6}" class="flow-label">${escapeHtml(s.set_name.slice(0, 22))}</text>
      <text x="${SRC_X + TANK_W / 2}" y="${y + TANK_H / 2 + 4}" class="flow-value">
        ${finite ? Math.round(fillPct * 100) + "% left" : (isLocal ? "local · €0" : "∞ PAYG")}</text>
      <text x="${SRC_X + TANK_W / 2}" y="${y + TANK_H - 6}" class="flow-sub">${flowTok(s.usage.total)} drawn</text>
    </g>`;
    // pipes to clients
    Object.entries(s.clients || {}).forEach(([cname, flow]) => {
      const ci = cliIndex[cname];
      if (ci === undefined) return;
      const y1 = y + TANK_H / 2, y2 = cliY(ci) + TANK_H / 2;
      const x1 = SRC_X + TANK_W, x2 = CLI_X;
      const midX = (x1 + x2) / 2;
      const w = 1.5 + 6 * (flow / maxFlow);
      const burn = clients[ci].burn_24h || 0;
      const speed = burn <= 0 ? "" : (burn > maxBurn * 0.66 ? "flow-fast"
        : (burn > maxBurn * 0.25 ? "flow-med" : "flow-slow"));
      pipes += `<path d="M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}"
        class="flow-pipe ${speed}" style="stroke-width:${w.toFixed(1)}"
        data-tip="${escapeHtml(s.set_name)} → ${escapeHtml(cname)}: ${flowTok(flow)} tokens">
        <title>${escapeHtml(s.set_name)} → ${escapeHtml(cname)}: ${flowTok(flow)} tokens this month</title></path>`;
    });
  });

  // --- client tanks (right) ---
  clients.forEach((c, i) => {
    const y = cliY(i);
    const pct = c.pct_left;
    const hasPlan = pct !== null && pct !== undefined;
    const fillH = Math.max(2, Math.round((TANK_H - 14) * (hasPlan ? pct : 1)));
    const color = !hasPlan ? "#bbb" : (pct <= 0 ? "var(--down)" : (pct < 0.2 ? "var(--warn)" : "var(--ok)"));
    const over = hasPlan && pct <= 0;
    tanks += `
    <g class="flow-tank flow-cli ${over ? "flow-tank-over" : ""}" data-kind="client" data-key="${escapeHtml(c.name)}">
      <rect x="${CLI_X}" y="${y}" width="${TANK_W}" height="${TANK_H}" rx="8" class="flow-tank-shell"/>
      <rect x="${CLI_X + 4}" y="${y + 10 + (TANK_H - 14 - fillH)}" width="${TANK_W - 8}"
        height="${fillH}" rx="5" fill="${color}" opacity="0.55"/>
      <text x="${CLI_X + TANK_W / 2}" y="${y - 6}" class="flow-label">${escapeHtml(c.name.slice(0, 22))}${c.frozen ? " ❄" : ""}</text>
      <text x="${CLI_X + TANK_W / 2}" y="${y + TANK_H / 2 + 4}" class="flow-value">
        ${hasPlan ? Math.round(pct * 100) + "% left" : "no plan"}</text>
      <text x="${CLI_X + TANK_W / 2}" y="${y + TANK_H - 6}" class="flow-sub">
        ${flowTok(c.usage.total)} used${over ? " · OVERAGE" : ""}</text>
    </g>`;
  });

  el.innerHTML = `<svg viewBox="0 0 ${WIDTH} ${height}" xmlns="http://www.w3.org/2000/svg"
    class="flow-svg" role="img" aria-label="token flow from sources to clients">
    ${defs}<g>${pipes}</g><g>${tanks}</g>
    <text x="${SRC_X + TANK_W / 2}" y="${PAD - 26}" class="flow-col-title">SOURCES</text>
    <text x="${CLI_X + TANK_W / 2}" y="${PAD - 26}" class="flow-col-title">CLIENTS</text>
  </svg>`;

  el.querySelectorAll(".flow-tank").forEach((g) => {
    g.addEventListener("click", () => flowDrill(g.dataset.kind, g.dataset.key));
  });
}

function flowDrill(kind, key) {
  const el = $("flowDetail");
  if (kind === "client") {
    const c = (ledgerData.clients || []).find((x) => x.name === key);
    if (!c) return;
    const models = Object.entries(c.by_model || {}).map(([m, u]) =>
      `<div class="kv-row"><span class="kv-key">${escapeHtml(m)}</span><span>${flowTok(u.total)} tok</span></div>`).join("");
    const aliases = (c.per_alias || []).map((a) =>
      `<div class="kv-row"><span class="kv-key">${escapeHtml(a.set_name)} <code class="vault-alias">${escapeHtml(a.alias)}</code></span>
       <span>${flowTok(a.total)} tok${a.buy_eur !== null && a.buy_eur !== undefined ? ` · buy €${a.buy_eur.toFixed(2)}` : ""}</span></div>`).join("");
    el.innerHTML = `<h4>${escapeHtml(c.name)}</h4>
      <div class="detail-cols">
        <div class="detail-card"><h3>Draw by source</h3>${aliases || "<p class='muted'>none</p>"}</div>
        <div class="detail-card"><h3>By model</h3>${models || "<p class='muted'>none</p>"}</div>
      </div>
      <p class="muted">burn ${flowTok(c.burn_24h)}/day
        ${c.projected_empty ? ` · projected empty ${escapeHtml(c.projected_empty)}` : ""}
        · sell ${c.eur_used !== null ? "€" + c.eur_used.toFixed(2) : "—"}
        · buy €${(c.buy_eur ?? 0).toFixed(2)}
        ${c.margin_eur !== null && c.margin_eur !== undefined ? ` · margin €${c.margin_eur.toFixed(2)}` : ""}</p>`;
  } else {
    const s = (ledgerData.sources || []).find((x) => (x.set_id || x.set_name) === key);
    if (!s) return;
    const clients = Object.entries(s.clients || {}).map(([n, t]) =>
      `<div class="kv-row"><span class="kv-key">${escapeHtml(n)}</span><span>${flowTok(t)} tok</span></div>`).join("");
    el.innerHTML = `<h4>${escapeHtml(s.set_name)} <span class="muted">${escapeHtml(s.provider || "")}</span></h4>
      <div class="detail-card"><h3>Drawn by</h3>${clients || "<p class='muted'>no flow</p>"}</div>
      <p class="muted">${flowTok(s.usage.total)} tokens this month
        ${s.cap_tokens ? ` · tank ${Math.round((s.cap_left_pct ?? 0) * 100)}% left of ${flowTok(s.cap_tokens)}` : " · PAYG (no cap set)"}
        ${s.buy_eur !== null && s.buy_eur !== undefined ? ` · buy €${s.buy_eur.toFixed(2)}` : " · buy rates not set"}</p>`;
  }
  el.classList.remove("hidden");
}

document.addEventListener("DOMContentLoaded", () => {
  $("flowRefreshBtn").addEventListener("click", () => refreshFlow(false));
});
