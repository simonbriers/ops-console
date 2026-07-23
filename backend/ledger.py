"""Metering ledger (docs/TOKEN_ECONOMY_PLAN.md Phases 2+3) — the economic
layer's memory and math. Knows NOTHING about SSH, Docker, or deployments
(that boundary is what keeps the console from tangling): it consumes
three inputs — usage snapshots pulled from each instance's
/admin/metrics, credential assignments from the vault (joined via the
api_key_alias hash), and price/plan configuration — and emits balances,
burn rates, projections, per-source totals, and the business numbers:
breakage, overage, margin, statements, threshold alerts.

Storage: <config-dir>/ledger.sqlite — same volume as clients.json and
vault.json, never in git. Tables:

  snap_key      month-to-date absolutes per (client, api_key_alias)
  snap_model    month-to-date absolutes per (client, model)
  plans         per client: type (standard|trial|demo|byok), frozen flag,
                allowance (€ and/or tokens), billing anchor day,
                sell-rates €/1k tokens, overage multiplier
  source_rates  per vault set: buy-rates €/1k tokens + optional monthly
                token cap (free-tier tanks — e.g. a free-tier key's quota)
  alerts        one row per (month, client, threshold) crossing — 80/100%
                of allowance, written by the collector, shown in the UI

Economics (Phase 3), all € and all derived at read time so re-pricing
never touches history:

  eur_used   = usage × the client's SELL rates
  included   = min(eur_used, allowance_eur)
  overage    = max(0, eur_used − allowance_eur) × overage_mult
  breakage   = max(0, allowance_eur − eur_used)   (sold but unused = margin)
  buy_eur    = usage × each source's BUY rates (per-alias join)
  margin     = allowance_eur + overage − buy_eur  (subscription model)
               — for plans without a € allowance, margin falls back to
               eur_used − buy_eur (pure metered resale view)

Snapshots store month-to-date absolutes, one row per change, so the
series stays tiny while still yielding burn rates by differencing.
Collection runs in a daemon thread (start_collector(), from main.py)
every LEDGER_POLL_SECONDS (default 300) — metering must not depend on a
browser tab. Billing cycles: calendar months for now (what
/admin/metrics serves); anchor_day is stored for the Phase 5+ cycle
shift. clients.json's legacy `monthly_token_quota` / `cost_per_1k_*`
seed a plan row on first touch; after that this ledger is authoritative.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend import config as cfg
from backend import model_catalog
from backend import vault as vault_mod
from backend.core import _admin_get_json, _month_bounds

PLAN_TYPES = ("standard", "trial", "demo", "byok")
ALERT_THRESHOLDS = (80, 100)

_PLAN_FIELDS = {
    "plan_type": str, "frozen": int, "base_fee_eur": float,
    "allowance_eur": float, "allowance_tokens": int, "anchor_day": int,
    "sell_in": float, "sell_cached": float, "sell_out": float,
    "overage_mult": float, "notes": str,
}


def _db_path() -> Path:
    return Path(cfg.DEFAULT_CONFIG_PATH).parent / "ledger.sqlite"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS snap_key (
        ts TEXT NOT NULL, month TEXT NOT NULL, client TEXT NOT NULL,
        alias TEXT NOT NULL, input INTEGER, cached INTEGER, output INTEGER,
        total INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS snap_model (
        ts TEXT NOT NULL, month TEXT NOT NULL, client TEXT NOT NULL,
        model TEXT NOT NULL, input INTEGER, cached INTEGER, output INTEGER,
        total INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS plans (
        client TEXT PRIMARY KEY, plan_type TEXT DEFAULT 'standard',
        frozen INTEGER DEFAULT 0, allowance_eur REAL, allowance_tokens INTEGER,
        anchor_day INTEGER DEFAULT 1, sell_in REAL DEFAULT 0,
        sell_cached REAL DEFAULT 0, sell_out REAL DEFAULT 0,
        overage_mult REAL DEFAULT 1.0, notes TEXT DEFAULT '')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS source_rates (
        set_id TEXT PRIMARY KEY, buy_in REAL DEFAULT 0,
        buy_cached REAL DEFAULT 0, buy_out REAL DEFAULT 0)""")
    try:  # Phase 3: optional monthly token cap → a source becomes a real tank
        conn.execute("ALTER TABLE source_rates ADD COLUMN cap_tokens INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:  # base subscription fee, separate from usage value — enables the
        # "base price includes N tokens; beyond that, cost+margin per token"
        # plan shape (2026-07-20)
        conn.execute("ALTER TABLE plans ADD COLUMN base_fee_eur REAL")
    except sqlite3.OperationalError:
        pass
    # Per-model buy rates (€/1k tokens), keyed by model id — the item-#9
    # refinement: usage is already snapshotted by-model, so a paid source can
    # be priced at each model's real rate instead of one blended per-source
    # number. model_catalog is the ONE price source: catalog-derived rows are
    # REFRESHED from it on every connect, so correcting a price in the catalog
    # actually reaches billing. Rows an operator has edited (operator_set=1, via
    # set_model_rate) are never touched by the refresh. This replaced a plain
    # INSERT OR IGNORE that froze every rate at first-seed (DUPLICATION_AUDIT 2.1).
    conn.execute("""CREATE TABLE IF NOT EXISTS model_rates (
        model TEXT PRIMARY KEY, provider TEXT DEFAULT '',
        buy_in REAL DEFAULT 0, buy_cached REAL DEFAULT 0,
        buy_out REAL DEFAULT 0, operator_set INTEGER DEFAULT 0)""")
    try:  # existing DBs: mark which rows an operator overrode so the refresh
        conn.execute("ALTER TABLE model_rates ADD COLUMN operator_set INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    for m in model_catalog.llm_models_per_1k():
        conn.execute(
            "INSERT INTO model_rates (model, provider, buy_in, buy_cached, buy_out) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(model) DO UPDATE SET provider=excluded.provider, "
            "buy_in=excluded.buy_in, buy_cached=excluded.buy_cached, "
            "buy_out=excluded.buy_out WHERE model_rates.operator_set=0",
            (m["id"], m["provider"], m["buy_in"], m["buy_cached"], m["buy_out"]))
    conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
        ts TEXT NOT NULL, month TEXT NOT NULL, client TEXT NOT NULL,
        threshold INTEGER NOT NULL, pct_used REAL,
        UNIQUE (month, client, threshold))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_snap_key
        ON snap_key (client, month, alias, ts)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_snap_model
        ON snap_model (client, month, model, ts)""")
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _month() -> str:
    return datetime.now().strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Plans & source rates
# ---------------------------------------------------------------------------

def ensure_plan(client: dict[str, Any]) -> dict[str, Any]:
    """Get (lazily creating) the plan row for a client dict, seeding from
    the legacy clients.json fields on first touch."""
    name = client.get("name", "")
    with _db() as conn:
        row = conn.execute("SELECT * FROM plans WHERE client=?", (name,)).fetchone()
        if row:
            return dict(row)
        seed_tokens = client.get("monthly_token_quota") or None
        conn.execute(
            "INSERT INTO plans (client, allowance_tokens, sell_in, sell_cached, sell_out) "
            "VALUES (?,?,?,?,?)",
            (name, seed_tokens,
             client.get("cost_per_1k_input_tokens") or 0,
             client.get("cost_per_1k_cached_tokens") or 0,
             client.get("cost_per_1k_output_tokens") or 0))
        return dict(conn.execute("SELECT * FROM plans WHERE client=?", (name,)).fetchone())


def get_plan(client_name: str) -> dict[str, Any] | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM plans WHERE client=?", (client_name,)).fetchone()
        return dict(row) if row else None


def set_plan(client_name: str, fields: dict[str, Any]) -> dict[str, Any]:
    updates, values = [], []
    for key, caster in _PLAN_FIELDS.items():
        if key in fields and fields[key] is not None:
            value = fields[key]
            if key == "plan_type" and value not in PLAN_TYPES:
                return {"ok": False, "error": f"plan_type must be one of {PLAN_TYPES}"}
            if key == "anchor_day" and not (1 <= int(value) <= 28):
                return {"ok": False, "error": "anchor_day must be 1–28"}
            updates.append(f"{key}=?")
            values.append(caster(value))
        elif key in fields and fields[key] is None and key in ("allowance_eur", "allowance_tokens"):
            updates.append(f"{key}=NULL")
    if not updates:
        return {"ok": False, "error": "nothing to update"}
    with _db() as conn:
        conn.execute("INSERT OR IGNORE INTO plans (client) VALUES (?)", (client_name,))
        conn.execute(f"UPDATE plans SET {', '.join(updates)} WHERE client=?",
                     (*values, client_name))
    return {"ok": True, "error": None, "plan": get_plan(client_name)}


def is_frozen(client_name: str) -> bool:
    plan = get_plan(client_name)
    return bool(plan and plan.get("frozen"))


def set_source_rates(set_id: str, buy_in: float, buy_cached: float, buy_out: float,
                     cap_tokens: int | None = None) -> dict[str, Any]:
    with _db() as conn:
        conn.execute(
            "INSERT INTO source_rates (set_id, buy_in, buy_cached, buy_out, cap_tokens) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(set_id) DO UPDATE SET buy_in=excluded.buy_in, "
            "buy_cached=excluded.buy_cached, buy_out=excluded.buy_out, "
            "cap_tokens=excluded.cap_tokens",
            (set_id, float(buy_in or 0), float(buy_cached or 0), float(buy_out or 0),
             int(cap_tokens) if cap_tokens else None))
    return {"ok": True, "error": None}


def set_model_rate(model: str, provider: str = "", buy_in: float = 0,
                   buy_cached: float = 0, buy_out: float = 0) -> dict[str, Any]:
    """Upsert one model's buy rate (€/1k tokens). Lets an operator override a
    catalog default or price a model the catalog doesn't ship."""
    with _db() as conn:
        # operator_set=1 marks this row as an operator override so the
        # catalog-refresh in _db() never clobbers it (DUPLICATION_AUDIT 2.1).
        conn.execute(
            "INSERT INTO model_rates (model, provider, buy_in, buy_cached, buy_out, operator_set) "
            "VALUES (?,?,?,?,?,1) "
            "ON CONFLICT(model) DO UPDATE SET provider=excluded.provider, "
            "buy_in=excluded.buy_in, buy_cached=excluded.buy_cached, "
            "buy_out=excluded.buy_out, operator_set=1",
            (model, provider or "", float(buy_in or 0), float(buy_cached or 0),
             float(buy_out or 0)))
    return {"ok": True, "error": None}


def get_model_rates() -> list[dict[str, Any]]:
    with _db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT model, provider, buy_in, buy_cached, buy_out "
            "FROM model_rates ORDER BY model")]


# ---------------------------------------------------------------------------
# Snapshot collection
# ---------------------------------------------------------------------------

def _bucket(d: dict[str, Any]) -> tuple[int, int, int, int]:
    return (int(d.get("input", 0) or 0), int(d.get("cached", 0) or 0),
            int(d.get("output", 0) or 0), int(d.get("total", 0) or 0))


def record_metrics(client_name: str, by_key: dict[str, Any], by_model: dict[str, Any]) -> int:
    """Insert one row per (alias|model) whose month-to-date numbers moved
    since the last snapshot. Returns the number of rows written."""
    ts, month, written = _now(), _month(), 0
    with _db() as conn:
        for table, data, col in (("snap_key", by_key or {}, "alias"),
                                 ("snap_model", by_model or {}, "model")):
            for key, vals in data.items():
                new = _bucket(vals)
                last = conn.execute(
                    f"SELECT input, cached, output, total FROM {table} "
                    f"WHERE client=? AND month=? AND {col}=? ORDER BY ts DESC LIMIT 1",
                    (client_name, month, key)).fetchone()
                if last and tuple(last) == new:
                    continue
                conn.execute(
                    f"INSERT INTO {table} (ts, month, client, {col}, input, cached, output, total) "
                    "VALUES (?,?,?,?,?,?,?,?)", (ts, month, client_name, key, *new))
                written += 1
    return written


def _check_thresholds(client: dict[str, Any]) -> list[int]:
    """After a snapshot lands: record 80%/100%-of-allowance crossings, once
    per (client, month, threshold). Console-side alerting (the clinic-facing
    warning emails are the product's job, Phase 5)."""
    name = client.get("name", "?")
    month = _month()
    econ = _client_economics(client, month)
    pct_used = econ.get("pct_used")
    if pct_used is None:
        return []
    fired = []
    with _db() as conn:
        for threshold in ALERT_THRESHOLDS:
            if pct_used * 100 >= threshold:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO alerts (ts, month, client, threshold, pct_used) "
                    "VALUES (?,?,?,?,?)", (_now(), month, name, threshold, round(pct_used, 4)))
                if cur.rowcount:
                    fired.append(threshold)
    return fired


def fetch_and_record(client: dict[str, Any]) -> dict[str, Any]:
    """Pull /admin/metrics (month-to-date) for one client, snapshot the
    by_key/by_model aggregations, evaluate alert thresholds. Read-only
    toward the instance."""
    name = client.get("name", "?")
    try:
        start, end = _month_bounds()
        data = _admin_get_json(client, "/admin/metrics", {"start": start, "end": end})
        written = record_metrics(name, data.get("by_key") or {}, data.get("by_model") or {})
        fired = _check_thresholds(client) if written else []
        return {"ok": True, "error": None, "client": name,
                "rows_written": written, "alerts_fired": fired}
    except Exception as e:
        return {"ok": False, "error": str(e), "client": name,
                "rows_written": 0, "alerts_fired": []}


def collect_all() -> list[dict[str, Any]]:
    return [fetch_and_record(c) for c in cfg.load_clients()]


_collector_started = False


def start_collector() -> None:
    """Idempotent daemon-thread starter (called from main.py at boot).
    Metering must not depend on a browser tab being open."""
    global _collector_started
    if _collector_started:
        return
    _collector_started = True
    interval = max(60, int(os.environ.get("LEDGER_POLL_SECONDS", "300")))

    def loop() -> None:
        time.sleep(15)  # let the app finish booting before the first pull
        while True:
            try:
                collect_all()
            except Exception:
                pass  # never let the collector die; next round retries
            time.sleep(interval)

    threading.Thread(target=loop, name="ledger-collector", daemon=True).start()


# ---------------------------------------------------------------------------
# Joins & math helpers
# ---------------------------------------------------------------------------

def _alias_map() -> dict[str, dict[str, Any]]:
    """alias -> vault set (a comma-separated key pair owns several aliases).
    Delegates to vault.alias_to_set_map so the id-key comma-split lives in one
    place instead of being re-implemented here off vault's private _ID_KEY
    (DUPLICATION_AUDIT 4.3)."""
    return vault_mod.alias_to_set_map()


def _source_rate_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {r["set_id"]: dict(r) for r in conn.execute("SELECT * FROM source_rates")}


def _model_rate_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {r["model"]: dict(r) for r in conn.execute("SELECT * FROM model_rates")}


def _latest_per(conn: sqlite3.Connection, table: str, col: str, client: str,
                month: str, cutoff_ts: str | None = None) -> dict[str, dict[str, int]]:
    where, params = "client=? AND month=?", [client, month]
    if cutoff_ts:
        where += " AND ts<=?"
        params.append(cutoff_ts)
    rows = conn.execute(
        f"SELECT {col} AS k, input, cached, output, total FROM {table} "
        f"WHERE {where} ORDER BY ts ASC", params).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:  # ascending → last write per key wins
        out[r["k"]] = {"input": r["input"], "cached": r["cached"],
                       "output": r["output"], "total": r["total"]}
    return out


def _eur(tokens: dict[str, int], rate_in: float, rate_cached: float, rate_out: float) -> float:
    return round(tokens.get("input", 0) / 1000 * (rate_in or 0)
                 + tokens.get("cached", 0) / 1000 * (rate_cached or 0)
                 + tokens.get("output", 0) / 1000 * (rate_out or 0), 4)


def _sum(buckets: list[dict[str, int]]) -> dict[str, int]:
    out = {"input": 0, "cached": 0, "output": 0, "total": 0}
    for b in buckets:
        for k in out:
            out[k] += b.get(k, 0)
    return out


def _client_economics(client: dict[str, Any], month: str) -> dict[str, Any]:
    """The full per-client picture for one month: usage, per-alias split,
    sell-side €, buy-side €, breakage/overage/margin, % and projections.
    Single source of truth — summary(), statements, and the alert check
    all read this."""
    name = client.get("name", "?")
    plan = ensure_plan(client)
    alias_to_set = _alias_map()
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    with _db() as conn:
        now_by_alias = _latest_per(conn, "snap_key", "alias", name, month)
        then_by_alias = _latest_per(conn, "snap_key", "alias", name, month, cutoff)
        by_model = _latest_per(conn, "snap_model", "model", name, month)
        rates = _source_rate_map(conn)
        model_rates = _model_rate_map(conn)
    usage = _sum(list(now_by_alias.values()))
    burn_24h = max(0, usage["total"] - _sum(list(then_by_alias.values()))["total"])

    per_alias, buy_eur, unpriced = [], 0.0, False
    for alias, tokens in sorted(now_by_alias.items()):
        s = alias_to_set.get(alias)
        rate = rates.get(s["id"]) if s else None
        alias_buy = _eur(tokens, rate["buy_in"], rate["buy_cached"], rate["buy_out"]) if rate else None
        if alias_buy is None and alias != "local":
            unpriced = True
        buy_eur += alias_buy or 0.0
        per_alias.append({"alias": alias, "set_id": s["id"] if s else None,
                          "set_name": s["name"] if s else
                          ("local model" if alias == "local" else "(unknown key)"),
                          "buy_eur": alias_buy, **tokens})

    # Per-model buy breakdown (item #9). Priced from model_rates, keyed by
    # model id. Additive/informational — the authoritative buy_eur above stays
    # the per-source (tier-aware, free-tier-€0) number so the verified Phase 3
    # margin math is unchanged; this shows what each model WOULD cost at its
    # own paid rate, and is what a later authoritative-per-model flip builds on.
    buy_by_model = []
    for m, tokens in sorted(by_model.items()):
        mr = model_rates.get(m)
        buy_by_model.append({
            "model": m, **tokens,
            "buy_eur": (_eur(tokens, mr["buy_in"], mr["buy_cached"], mr["buy_out"])
                        if mr else None),
            "priced": mr is not None})

    sell_raw = _eur(usage, plan["sell_in"], plan["sell_cached"], plan["sell_out"])
    allowance_eur = plan["allowance_eur"]
    base_fee = plan.get("base_fee_eur") or 0.0
    included = overage = breakage = margin = None
    eur_used = sell_raw
    pct_used = pct_left = remaining_tokens = None
    if allowance_eur:
        included = round(min(sell_raw, allowance_eur), 4)
        overage = round(max(0.0, sell_raw - allowance_eur) * (plan["overage_mult"] or 1.0), 4)
        breakage = round(max(0.0, allowance_eur - sell_raw), 4)
        # revenue = the plan price (base fee if set, else the € allowance
        # doubling as the price) + overage
        margin = round((base_fee or allowance_eur) + overage - buy_eur, 4)
        eur_used = round(included + overage, 4)
        pct_used = round(sell_raw / allowance_eur, 4)
        pct_left = max(0.0, round(1 - pct_used, 4))
    elif plan["allowance_tokens"]:
        # Token-allowance plan: base fee includes N tokens; usage beyond N
        # is billed at sell rates × overage_mult. The over-quota portion's
        # in/cached/out composition isn't tracked separately, so overage is
        # pro-rated by token share — exact enough for billing, and stated
        # as such on statements.
        remaining_tokens = max(0, plan["allowance_tokens"] - usage["total"])
        pct_used = round(usage["total"] / plan["allowance_tokens"], 4)
        pct_left = max(0.0, round(remaining_tokens / plan["allowance_tokens"], 4))
        over_tokens = max(0, usage["total"] - plan["allowance_tokens"])
        if over_tokens and usage["total"]:
            factor = over_tokens / usage["total"]
            overage = round(sell_raw * factor * (plan["overage_mult"] or 1.0), 4)
            included = round(sell_raw * (1 - factor), 4)
        else:
            overage = 0.0
            included = round(sell_raw, 4)
        eur_used = round(included + overage, 4)
        margin = round(base_fee + overage - buy_eur, 4) if (base_fee or overage or buy_eur) else None
    else:
        margin = round(base_fee + sell_raw - buy_eur, 4) if (base_fee or sell_raw or buy_eur) else None
    if plan["plan_type"] in ("demo", "byok"):
        # demo: ours, never billed; byok: their key, no resale — economics
        # reduce to "what does this cost us" (demo) / "zero flow" (byok)
        included = overage = breakage = None
        margin = round(-buy_eur, 4) if plan["plan_type"] == "demo" else margin

    projected_empty = None
    if pct_left is not None and burn_24h > 0 and plan["allowance_tokens"]:
        remaining = plan["allowance_tokens"] - usage["total"]
        if remaining > 0:
            days = remaining / burn_24h
            projected_empty = (datetime.now() + timedelta(days=days)).date().isoformat()

    return {"name": name, "plan": plan, "usage": usage, "by_model": by_model,
            "buy_by_model": buy_by_model,
            "per_alias": per_alias, "eur_used": eur_used, "sell_raw": sell_raw,
            "buy_eur": round(buy_eur, 4), "buy_unpriced": unpriced,
            "included_eur": included, "overage_eur": overage,
            "breakage_eur": breakage, "margin_eur": margin,
            "pct_used": pct_used, "pct_left": pct_left,
            "remaining_tokens": remaining_tokens, "burn_24h": burn_24h,
            "projected_empty": projected_empty, "frozen": bool(plan["frozen"])}


# ---------------------------------------------------------------------------
# Summary, statements
# ---------------------------------------------------------------------------

def summary() -> dict[str, Any]:
    """Everything the Tokens and Flow tabs render: per-client economics,
    per-source totals (with caps → tank fill levels), fleet totals, and
    this month's alerts."""
    month = _month()
    clients = cfg.load_clients()
    client_rows = [_client_economics(c, month) for c in clients]

    with _db() as conn:
        rates = _source_rate_map(conn)
        alert_rows = [dict(r) for r in conn.execute(
            "SELECT ts, client, threshold, pct_used FROM alerts WHERE month=? "
            "ORDER BY ts DESC", (month,))]

    source_totals: dict[str, dict[str, Any]] = {}
    for row in client_rows:
        for pa in row["per_alias"]:
            key = pa["set_id"] or pa["alias"]
            st = source_totals.setdefault(key, {
                "set_id": pa["set_id"], "set_name": pa["set_name"],
                "provider": "", "tier": "", "owner": "", "aliases": set(),
                "clients": {}, "usage": {"input": 0, "cached": 0, "output": 0, "total": 0}})
            st["aliases"].add(pa["alias"])
            st["clients"][row["name"]] = st["clients"].get(row["name"], 0) + pa["total"]
            for k in st["usage"]:
                st["usage"][k] += pa.get(k, 0)
    # enrich with vault metadata + rates + cap → fill level
    sets_by_id = {s["id"]: s for s in vault_mod.load_vault()["sets"]}
    sources = []
    for st in source_totals.values():
        s = sets_by_id.get(st["set_id"] or "")
        if s:
            st["provider"], st["tier"], st["owner"] = (s.get("provider", ""),
                                                       s.get("tier", ""), s.get("owner", ""))
        rate = rates.get(st["set_id"] or "")
        st["aliases"] = sorted(st["aliases"])
        st["buy_eur"] = _eur(st["usage"], rate["buy_in"], rate["buy_cached"],
                             rate["buy_out"]) if rate else None
        st["rates"] = ({k: rate[k] for k in ("buy_in", "buy_cached", "buy_out")}
                       if rate else None)
        st["cap_tokens"] = rate["cap_tokens"] if rate and rate["cap_tokens"] else None
        st["cap_left_pct"] = (max(0.0, round(1 - st["usage"]["total"] / st["cap_tokens"], 4))
                              if st["cap_tokens"] else None)
        sources.append(st)
    sources.sort(key=lambda s: -s["usage"]["total"])

    billed = [r for r in client_rows if r["plan"]["plan_type"] not in ("demo", "byok")]
    fleet = {
        "sold_allowance_eur": round(sum(r["plan"]["allowance_eur"] or 0 for r in billed), 2),
        "consumed_sell_eur": round(sum(r["sell_raw"] or 0 for r in billed), 2),
        "overage_eur": round(sum(r["overage_eur"] or 0 for r in billed), 2),
        "breakage_eur": round(sum(r["breakage_eur"] or 0 for r in billed), 2),
        "buy_eur": round(sum(r["buy_eur"] or 0 for r in client_rows), 2),
        "margin_eur": round(sum(r["margin_eur"] or 0 for r in client_rows
                                if r["margin_eur"] is not None), 2),
        "total_tokens": sum(r["usage"]["total"] for r in client_rows),
    }
    return {"month": month, "clients": client_rows, "sources": sources,
            "fleet": fleet, "alerts": alert_rows, "generated": _now()}


def statement(client_name: str, month: str | None = None) -> dict[str, Any] | None:
    """One client's month in full — the artifact behind an invoice.
    Works for past months too (snapshots are kept per month)."""
    client = cfg.find_client(client_name)
    if client is None:
        return None
    month = month or _month()
    econ = _client_economics(client, month)
    chats_hint = None  # avg cost per conversation needs chats count; Phase 5+
    return {"client": client_name, "month": month, "generated": _now(),
            "economics": econ, "chats_hint": chats_hint}


def statement_markdown(data: dict[str, Any]) -> str:
    """Render a statement dict as operator-readable markdown (the client-
    facing layout/branding is a later concern; the numbers are these)."""
    e = data["economics"]
    p = e["plan"]
    lines = [
        f"# Usage statement — {data['client']}",
        f"Period: {data['month']} (calendar month, month-to-date if current)  ",
        f"Generated: {data['generated']}  ",
        f"Plan: **{p['plan_type']}**"
        + (f" · allowance €{p['allowance_eur']:.2f}/month" if p["allowance_eur"] else "")
        + (f" · allowance {p['allowance_tokens']:,} tokens/month" if p["allowance_tokens"] else ""),
        "",
        "## Usage",
        f"- Tokens: {e['usage']['total']:,} "
        f"(input {e['usage']['input']:,} · cached {e['usage']['cached']:,} "
        f"· output {e['usage']['output']:,})",
    ]
    buy_by_model = {b["model"]: b.get("buy_eur") for b in e.get("buy_by_model") or []}
    for m, u in sorted(e["by_model"].items()):
        bm = buy_by_model.get(m)
        suffix = f" · buy €{bm:.4f}" if bm is not None else ""
        lines.append(f"  - {m}: {u['total']:,}{suffix}")
    base_fee = p.get("base_fee_eur") or 0.0
    lines += ["", "## Charges"]
    if p["allowance_eur"]:
        plan_price = base_fee or p["allowance_eur"]
        lines += [
            f"- Plan fee: €{plan_price:.2f}",
            f"- Included usage: €{e['included_eur']:.2f} of €{p['allowance_eur']:.2f}",
            f"- Overage: €{e['overage_eur']:.2f}"
            + (f" (at ×{p['overage_mult']:.2f} of sell rates)" if e["overage_eur"] else ""),
            f"- **Total this period: €{(plan_price + (e['overage_eur'] or 0)):.2f}**",
        ]
    elif p["allowance_tokens"]:
        lines += [
            (f"- Plan fee: €{base_fee:.2f}" if base_fee else "- Plan fee: (none set)"),
            f"- Included tokens used: {min(e['usage']['total'], p['allowance_tokens']):,} "
            f"of {p['allowance_tokens']:,}",
            f"- Overage: €{(e['overage_eur'] or 0):.2f}"
            + (f" (usage beyond allowance, pro-rated, ×{p['overage_mult']:.2f})"
               if e["overage_eur"] else ""),
            f"- **Total this period: €{(base_fee + (e['overage_eur'] or 0)):.2f}**",
        ]
    else:
        lines.append(f"- Metered value at sell rates: €{e['sell_raw']:.2f}")
    if e["pct_left"] is not None:
        lines.append(f"- Allowance remaining: {round(e['pct_left'] * 100)}%")
    lines += ["", "_Internal (never client-facing):_",
              f"_buy cost €{e['buy_eur']:.2f}"
              + (" (some sources unpriced)" if e["buy_unpriced"] else "")
              + (f" · breakage €{e['breakage_eur']:.2f}" if e["breakage_eur"] is not None else "")
              + (f" · margin €{e['margin_eur']:.2f}" if e["margin_eur"] is not None else "")
              + "_"]
    return "\n".join(lines) + "\n"
