"""Metering ledger (docs/TOKEN_ECONOMY_PLAN.md Phase 2) — the economic
layer's memory. Knows NOTHING about SSH, Docker, or deployments (that
boundary is what keeps the console from tangling): it consumes three
inputs — usage snapshots pulled from each instance's /admin/metrics,
credential assignments from the vault (joined via the api_key_alias
hash), and price/plan configuration — and emits balances, burn rates,
projections, and per-source totals.

Storage: <config-dir>/ledger.sqlite — same volume as clients.json and
vault.json, never in git. Tables:

  snap_key    month-to-date absolutes per (client, api_key_alias)
  snap_model  month-to-date absolutes per (client, model)
  plans       per client: type (standard|trial|demo|byok), frozen flag,
              allowance (in € and/or tokens), billing anchor day,
              sell-rates €/1k tokens (in/cached/out), overage multiplier
  source_rates  per vault set: buy-rates €/1k tokens

Snapshots store what /admin/metrics reports: MONTH-TO-DATE totals, one
row per change (an insert only happens when the numbers moved), so the
series stays tiny while still yielding burn rates and projections by
differencing. The instance remains the source of truth for the current
month; this ledger is the history and the joins.

Collection: a daemon thread (start_collector(), called from main.py)
pulls every LEDGER_POLL_SECONDS (default 300). Browser-driven dashboard
polling is NOT relied on — metering must not stop when the operator
closes the tab. Instances that error (down, placeholder token) are
skipped until the next round; metering is read-only and safe for frozen
clients.

Billing cycles: Phase 2 works in CALENDAR months (what /admin/metrics
serves). The plan's anchor_day is stored and displayed but cycle-shifted
accounting arrives with the statement work (Phase 3).

clients.json migration: the legacy per-client `monthly_token_quota` and
`cost_per_1k_*` fields seed a client's plan row on first touch
(allowance_tokens and sell rates respectively); after that, the plan row
here is authoritative and those fields are ignored by the ledger.
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
from backend import vault as vault_mod
from backend.core import _admin_get_json, _month_bounds

PLAN_TYPES = ("standard", "trial", "demo", "byok")

_PLAN_FIELDS = {
    "plan_type": str, "frozen": int, "allowance_eur": float,
    "allowance_tokens": int, "anchor_day": int,
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
# Plans
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


def set_source_rates(set_id: str, buy_in: float, buy_cached: float, buy_out: float) -> dict[str, Any]:
    with _db() as conn:
        conn.execute(
            "INSERT INTO source_rates (set_id, buy_in, buy_cached, buy_out) VALUES (?,?,?,?) "
            "ON CONFLICT(set_id) DO UPDATE SET buy_in=excluded.buy_in, "
            "buy_cached=excluded.buy_cached, buy_out=excluded.buy_out",
            (set_id, float(buy_in or 0), float(buy_cached or 0), float(buy_out or 0)))
    return {"ok": True, "error": None}


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


def fetch_and_record(client: dict[str, Any]) -> dict[str, Any]:
    """Pull /admin/metrics (month-to-date) for one client and snapshot the
    by_key/by_model aggregations. Read-only toward the instance."""
    name = client.get("name", "?")
    try:
        start, end = _month_bounds()
        data = _admin_get_json(client, "/admin/metrics", {"start": start, "end": end})
        written = record_metrics(name, data.get("by_key") or {}, data.get("by_model") or {})
        return {"ok": True, "error": None, "client": name, "rows_written": written}
    except Exception as e:
        return {"ok": False, "error": str(e), "client": name, "rows_written": 0}


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
# Summary math
# ---------------------------------------------------------------------------

def _latest_per(conn: sqlite3.Connection, table: str, col: str, client: str,
                month: str, cutoff_ts: str | None = None) -> dict[str, dict[str, int]]:
    """Latest row per alias/model for a client+month, optionally 'as of'
    a cutoff timestamp (for burn-rate differencing)."""
    where, params = "client=? AND month=?", [client, month]
    if cutoff_ts:
        where += " AND ts<=?"
        params.append(cutoff_ts)
    rows = conn.execute(
        f"SELECT {col} AS k, input, cached, output, total, ts FROM {table} "
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


def summary() -> dict[str, Any]:
    """Everything the Tokens tab renders: per-client balances against
    their plan, and per-source (vault set) totals across all clients."""
    month = _month()
    clients = cfg.load_clients()
    vault = vault_mod.load_vault()
    # alias -> set (a set with a comma-separated key pair owns several aliases)
    alias_to_set: dict[str, dict[str, Any]] = {}
    for s in vault["sets"]:
        idk = vault_mod._ID_KEY.get(s.get("kind", ""))
        if not idk:
            continue
        raw = (s.get("values") or {}).get(idk, "")
        for part in raw.split(","):
            if part.strip():
                alias_to_set[vault_mod.alias_for(part)] = s

    cutoff = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    client_rows, source_totals = [], {}
    with _db() as conn:
        for c in clients:
            name = c.get("name", "?")
            plan = ensure_plan(c)
            now_by_alias = _latest_per(conn, "snap_key", "alias", name, month)
            then_by_alias = _latest_per(conn, "snap_key", "alias", name, month, cutoff)
            by_model = _latest_per(conn, "snap_model", "model", name, month)
            usage = _sum(list(now_by_alias.values()))
            usage_then = _sum(list(then_by_alias.values()))
            burn_24h = max(0, usage["total"] - usage_then["total"])
            eur_used = _eur(usage, plan["sell_in"], plan["sell_cached"], plan["sell_out"])
            # % left against € allowance if set, else token allowance
            pct_left = None
            remaining_tokens = None
            if plan["allowance_eur"]:
                pct_left = max(0.0, round(1 - eur_used / plan["allowance_eur"], 4))
            elif plan["allowance_tokens"]:
                remaining_tokens = max(0, plan["allowance_tokens"] - usage["total"])
                pct_left = max(0.0, round(remaining_tokens / plan["allowance_tokens"], 4))
            projected_empty = None
            if pct_left is not None and burn_24h > 0 and plan["allowance_tokens"]:
                remaining = plan["allowance_tokens"] - usage["total"]
                if remaining > 0:
                    days = remaining / burn_24h
                    projected_empty = (datetime.now() + timedelta(days=days)).date().isoformat()
            per_alias = []
            for alias, tokens in sorted(now_by_alias.items()):
                s = alias_to_set.get(alias)
                per_alias.append({"alias": alias, "set_id": s["id"] if s else None,
                                  "set_name": s["name"] if s else
                                  ("local model" if alias == "local" else "(unknown key)"),
                                  **tokens})
                key = s["id"] if s else alias
                st = source_totals.setdefault(key, {
                    "set_id": s["id"] if s else None,
                    "set_name": s["name"] if s else
                    ("local model" if alias == "local" else f"(unknown) {alias}"),
                    "provider": (s or {}).get("provider", ""),
                    "tier": (s or {}).get("tier", ""), "owner": (s or {}).get("owner", ""),
                    "aliases": set(), "clients": {}, "usage": {"input": 0, "cached": 0,
                                                               "output": 0, "total": 0}})
                st["aliases"].add(alias)
                st["clients"][name] = st["clients"].get(name, 0) + tokens["total"]
                for k in st["usage"]:
                    st["usage"][k] += tokens.get(k, 0)
            client_rows.append({
                "name": name, "plan": plan, "usage": usage, "by_model": by_model,
                "per_alias": per_alias, "eur_used": eur_used, "pct_left": pct_left,
                "remaining_tokens": remaining_tokens, "burn_24h": burn_24h,
                "projected_empty": projected_empty, "frozen": bool(plan["frozen"]),
            })
        rates = {r["set_id"]: dict(r) for r in
                 conn.execute("SELECT * FROM source_rates").fetchall()}
    sources = []
    for st in source_totals.values():
        rate = rates.get(st["set_id"] or "", {})
        st["aliases"] = sorted(st["aliases"])
        st["buy_eur"] = _eur(st["usage"], rate.get("buy_in", 0),
                             rate.get("buy_cached", 0), rate.get("buy_out", 0)) if rate else None
        st["rates"] = {k: rate.get(k, 0) for k in ("buy_in", "buy_cached", "buy_out")} if rate else None
        sources.append(st)
    sources.sort(key=lambda s: -s["usage"]["total"])
    return {"month": month, "clients": client_rows, "sources": sources,
            "generated": _now()}
