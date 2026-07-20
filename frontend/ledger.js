// ===========================================================================
// Tokens tab — metering ledger UI (docs/TOKEN_ECONOMY_PLAN.md Phase 2).
// Second file of the app.js split; loads AFTER app.js and credentials.js,
// reuses $ / escapeHtml. Provides refreshLedger() which app.js's
// switchPage() calls for the "ledger" page.
//
// Reads /api/ledger/summary (fed by the backend's background snapshot
// collector), renders: per-client plan balances (allowance, used, % left,
// 24h burn, projected-empty), a plan editor (incl. the FROZEN flag that
// hard-blocks credential applies server-side), and per-source totals with
// buy-rate editing. "Collect now" forces a synchronous metrics pull.
// ===========================================================================

let ledgerData = null;

function eur(x) {
  if (x === null || x === undefined) return "—";
  return "€" + Number(x).toFixed(2);
}

function tok(n) {
  if (n === null || n === undefined) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

async function refreshLedger() {
  const status = $("ledgerStatus");
  status.textContent = "loading…";
  let r;
  try {
    r = await fetch("/api/ledger/summary");
    ledgerData = await r.json();
  } catch { status.textContent = "couldn't load ledger"; return; }
  status.textContent = `month ${ledgerData.month} · snapshot ${ledgerData.generated}`;
  renderLedgerFleet();
  renderLedgerAlerts();
  renderLedgerClients();
  renderLedgerSources();
  populateLedgerPlanSelect();
  if (typeof refreshFlow === "function" && !document.getElementById("page-flow").classList.contains("hidden")) {
    refreshFlow(true);   // keep the Flow tab in sync if it's the one visible
  }
}

function renderLedgerFleet() {
  const f = ledgerData.fleet || {};
  $("ledgerFleet").innerHTML = `
    <span title="sum of billed clients' € allowances">sold ${eur(f.sold_allowance_eur)}</span> ·
    <span title="usage priced at sell rates (billed clients)">consumed ${eur(f.consumed_sell_eur)}</span> ·
    <span title="sold but unused — pure margin" class="ok">breakage ${eur(f.breakage_eur)}</span> ·
    <span title="billed beyond allowances">overage ${eur(f.overage_eur)}</span> ·
    <span title="what the providers charge us (needs buy rates)">buy ${eur(f.buy_eur)}</span> ·
    <strong title="allowances + overage − buy cost">margin ${eur(f.margin_eur)}</strong> ·
    <span class="muted">${tok(f.total_tokens)} tok total</span>`;
}

function renderLedgerAlerts() {
  const el = $("ledgerAlerts");
  const alerts = ledgerData.alerts || [];
  if (!alerts.length) { el.innerHTML = ""; return; }
  el.innerHTML = alerts.map((a) =>
    `<div class="ledger-alert ${a.threshold >= 100 ? "ledger-alert-100" : ""}">
       ${escapeHtml(a.ts.slice(0, 16))} — <strong>${escapeHtml(a.client)}</strong>
       crossed ${a.threshold}% of allowance (at ${(a.pct_used * 100).toFixed(0)}%)
     </div>`).join("");
}

// -- clients & plans ---------------------------------------------------------

function renderLedgerClients() {
  const body = $("ledgerClientsBody");
  const rows = (ledgerData.clients || []).map((c) => {
    const p = c.plan;
    const allowance = p.allowance_eur
      ? eur(p.allowance_eur) + "/mo"
      : (p.allowance_tokens ? tok(p.allowance_tokens) + " tok/mo" : "—");
    const pct = c.pct_left === null || c.pct_left === undefined ? null : Math.round(c.pct_left * 100);
    const pctCls = pct === null ? "" : (pct <= 0 ? "ledger-pct-empty" : (pct < 20 ? "ledger-pct-warn" : ""));
    const bar = pct === null ? `<span class="muted">no allowance set</span>`
      : `<div class="ledger-bar ${pctCls}" title="${pct}% of allowance left">
           <div class="ledger-bar-fill" style="width:${Math.max(0, Math.min(100, pct))}%"></div>
         </div><span class="ledger-pct ${pctCls}">${pct}%</span>`;
    const models = Object.entries(c.by_model || {}).map(([m, u]) =>
      `${escapeHtml(m)}: ${tok(u.total)}`).join(" · ");
    const econ = [];
    if (c.breakage_eur !== null && c.breakage_eur !== undefined)
      econ.push(`<span class="ok" title="sold but unused">brk ${eur(c.breakage_eur)}</span>`);
    if (c.overage_eur) econ.push(`<span title="billed beyond allowance">ovg ${eur(c.overage_eur)}</span>`);
    if (c.buy_eur || c.buy_eur === 0)
      econ.push(`<span title="provider cost of this client's draw${c.buy_unpriced ? " (some sources unpriced)" : ""}">buy ${eur(c.buy_eur)}${c.buy_unpriced ? "?" : ""}</span>`);
    if (c.margin_eur !== null && c.margin_eur !== undefined)
      econ.push(`<strong title="margin this month">${eur(c.margin_eur)}</strong>`);
    return `<tr>
      <td><strong>${escapeHtml(c.name)}</strong>
        ${c.frozen ? `<span class="vault-badge vault-badge-client" title="frozen — credential applies are blocked server-side">FROZEN</span>` : ""}
        <div class="muted ledger-sub">${escapeHtml(p.plan_type)}${p.anchor_day ? ` · resets day ${p.anchor_day}` : ""}</div></td>
      <td>${allowance}</td>
      <td>${tok(c.usage.total)} tok<div class="muted ledger-sub">${eur(c.eur_used)} at sell rates</div>
        <div class="muted ledger-sub" title="per model">${models || ""}</div></td>
      <td>${bar}</td>
      <td class="ledger-sub">${econ.join("<br>") || "—"}</td>
      <td>${tok(c.burn_24h)}/day</td>
      <td>${c.projected_empty ? escapeHtml(c.projected_empty) : "—"}</td>
      <td class="vault-actions">
        <button type="button" class="ledger-edit" data-client="${escapeHtml(c.name)}">Edit plan</button>
        <button type="button" class="ledger-statement" data-client="${escapeHtml(c.name)}"
          title="markdown usage statement (this month)">Statement</button>
      </td>
    </tr>`;
  });
  body.innerHTML = rows.join("") ||
    `<tr><td colspan="8" class="empty">No clients yet.</td></tr>`;
  body.querySelectorAll(".ledger-edit").forEach((btn) => {
    btn.addEventListener("click", () => ledgerLoadPlan(btn.dataset.client));
  });
  body.querySelectorAll(".ledger-statement").forEach((btn) => {
    btn.addEventListener("click", () =>
      window.open(`/api/ledger/statement/${encodeURIComponent(btn.dataset.client)}`, "_blank"));
  });
}

function populateLedgerPlanSelect() {
  const sel = $("ledgerPlanClient");
  const prev = sel.value;
  sel.innerHTML = (ledgerData.clients || []).map((c) =>
    `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}</option>`).join("");
  if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

function ledgerLoadPlan(name) {
  const c = (ledgerData.clients || []).find((x) => x.name === name);
  if (!c) return;
  const p = c.plan;
  $("ledgerPlanClient").value = name;
  const form = $("ledgerPlanForm");
  form.querySelector('[name="plan_type"]').value = p.plan_type || "standard";
  form.querySelector('[name="frozen"]').checked = !!p.frozen;
  form.querySelector('[name="allowance_eur"]').value = p.allowance_eur ?? "";
  form.querySelector('[name="allowance_tokens"]').value = p.allowance_tokens ?? "";
  form.querySelector('[name="anchor_day"]').value = p.anchor_day ?? 1;
  form.querySelector('[name="sell_in"]').value = p.sell_in ?? 0;
  form.querySelector('[name="sell_cached"]').value = p.sell_cached ?? 0;
  form.querySelector('[name="sell_out"]').value = p.sell_out ?? 0;
  form.querySelector('[name="overage_mult"]').value = p.overage_mult ?? 1;
  form.querySelector('[name="notes"]').value = p.notes || "";
  $("ledgerPlanStatus").textContent = `editing plan of ${name}`;
  form.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function submitLedgerPlan(e) {
  e.preventDefault();
  const form = e.target;
  const name = $("ledgerPlanClient").value;
  const status = $("ledgerPlanStatus");
  const num = (sel) => {
    const v = form.querySelector(`[name="${sel}"]`).value;
    return v === "" ? null : Number(v);
  };
  const body = {
    plan_type: form.querySelector('[name="plan_type"]').value,
    frozen: form.querySelector('[name="frozen"]').checked,
    allowance_eur: num("allowance_eur"),
    clear_allowance_eur: form.querySelector('[name="allowance_eur"]').value === "",
    allowance_tokens: num("allowance_tokens"),
    clear_allowance_tokens: form.querySelector('[name="allowance_tokens"]').value === "",
    anchor_day: num("anchor_day"),
    sell_in: num("sell_in"), sell_cached: num("sell_cached"), sell_out: num("sell_out"),
    overage_mult: num("overage_mult"),
    notes: form.querySelector('[name="notes"]').value,
  };
  status.textContent = "saving…";
  const r = await fetch(`/api/ledger/plan/${encodeURIComponent(name)}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  status.textContent = r.ok ? `saved plan of ${name}` : (data.detail || "failed");
  if (r.ok) refreshLedger();
}

// -- sources -----------------------------------------------------------------

function renderLedgerSources() {
  const body = $("ledgerSourcesBody");
  const rows = (ledgerData.sources || []).map((s, i) => {
    const clients = Object.entries(s.clients || {}).map(([n, t]) =>
      `${escapeHtml(n)}: ${tok(t)}`).join("<br>");
    const badges = `<span class="vault-badge vault-badge-${escapeHtml(s.tier || "paid")}">${escapeHtml(s.tier || "?")}</span>`
      + (s.owner === "client" ? `<span class="vault-badge vault-badge-client">BYOK</span>` : "");
    const r = s.rates || {};
    const capInfo = s.cap_tokens
      ? `<div class="muted ledger-sub" title="monthly cap — this source is a finite tank">tank: ${tok(s.usage.total)} / ${tok(s.cap_tokens)} used · ${Math.round((s.cap_left_pct ?? 0) * 100)}% left</div>`
      : "";
    const rateInputs = s.set_id ? `
      <input type="number" step="any" class="ledger-rate" id="rate_in_${i}" value="${r.buy_in ?? ""}" placeholder="in €/1k" title="buy € per 1k input tokens" />
      <input type="number" step="any" class="ledger-rate" id="rate_cached_${i}" value="${r.buy_cached ?? ""}" placeholder="cached" title="buy € per 1k cached tokens" />
      <input type="number" step="any" class="ledger-rate" id="rate_out_${i}" value="${r.buy_out ?? ""}" placeholder="out" title="buy € per 1k output tokens" />
      <input type="number" class="ledger-rate ledger-cap" id="rate_cap_${i}" value="${s.cap_tokens ?? ""}" placeholder="cap tok/mo" title="optional monthly token cap (free-tier quota, prepaid credits) — turns this source into a finite tank in the Flow view" />
      <button type="button" class="ledger-rate-save" data-i="${i}" data-set="${escapeHtml(s.set_id)}">Save</button>${capInfo}`
      : `<span class="muted">—</span>`;
    return `<tr>
      <td><strong>${escapeHtml(s.set_name)}</strong>
        <div class="muted ledger-sub">${escapeHtml(s.provider || "")} ${badges}</div>
        <div class="muted ledger-sub">${(s.aliases || []).map((a) => `<code class="vault-alias">${escapeHtml(a)}</code>`).join(" ")}</div></td>
      <td class="ledger-sub">${clients || "—"}</td>
      <td>${tok(s.usage.total)} tok
        <div class="muted ledger-sub">${tok(s.usage.input)} in · ${tok(s.usage.cached)} cached · ${tok(s.usage.output)} out</div></td>
      <td>${s.buy_eur === null || s.buy_eur === undefined ? `<span class="muted">set rates →</span>` : eur(s.buy_eur)}</td>
      <td class="ledger-rates-cell">${rateInputs}</td>
    </tr>`;
  });
  body.innerHTML = rows.join("") ||
    `<tr><td colspan="5" class="empty">No usage snapshots yet — press "Collect now" (needs each client's admin token).</td></tr>`;
  body.querySelectorAll(".ledger-rate-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const i = btn.dataset.i;
      const capVal = document.getElementById(`rate_cap_${i}`).value;
      const r = await fetch("/api/ledger/source-rates", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          set_id: btn.dataset.set,
          buy_in: Number(document.getElementById(`rate_in_${i}`).value || 0),
          buy_cached: Number(document.getElementById(`rate_cached_${i}`).value || 0),
          buy_out: Number(document.getElementById(`rate_out_${i}`).value || 0),
          cap_tokens: capVal === "" ? null : Number(capVal),
        }) });
      if (r.ok) refreshLedger();
    });
  });
}

async function ledgerCollectNow() {
  const status = $("ledgerStatus");
  status.textContent = "pulling /admin/metrics from every client…";
  const r = await fetch("/api/ledger/collect-now", { method: "POST" });
  const data = await r.json().catch(() => ({}));
  const fails = (data.reports || []).filter((x) => !x.ok);
  if (fails.length) {
    status.textContent = "some pulls failed: "
      + fails.map((f) => `${f.client} (${(f.error || "").slice(0, 80)})`).join("; ");
  }
  await refreshLedger();
  if (!fails.length) status.textContent += " · collected ✓";
}

// -- wiring ------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  $("ledgerCollectBtn").addEventListener("click", ledgerCollectNow);
  $("ledgerPlanForm").addEventListener("submit", submitLedgerPlan);
  $("ledgerPlanClient").addEventListener("change", () =>
    ledgerLoadPlan($("ledgerPlanClient").value));
});
