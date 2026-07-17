"""ops-console's own lightweight check-history log — NOT part of the
monitored product at all, purely local state this tool keeps about itself
so uptime %/latency numbers reflect a real trend instead of just "whatever
the instant you happened to refresh looked like".

Every poll (routes.list_clients) appends one line per client to an
append-only JSONL file. Deliberately dependency-free (no sqlite3 needed at
this volume — a few clients, one line per poll, default 60s interval is
~1,440 lines/day) and self-pruning so the file doesn't grow forever.

Path resolution follows the same env-var-or-project-root pattern as
config.py's clients.json, so the Docker image's persistent /data volume
covers this too if HISTORY_FILE is pointed there.
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend import config as cfg

HISTORY_PATH = Path(os.environ.get("HISTORY_FILE", str(cfg.PROJECT_ROOT / "history.jsonl")))
RETENTION_DAYS = 30
_PRUNE_PROBABILITY = 0.02  # prune on ~1 in 50 polls rather than every poll


def append_checks(results: list[dict[str, Any]], config_path: str | Path = HISTORY_PATH) -> None:
    """One line per client per poll: {name, checked_at, up, latency_ms}.
    Never raises — a history-logging failure must never break a live poll."""
    if not results:
        return
    try:
        path = Path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for r in results:
                health = r.get("health") or {}
                line = {
                    "name": r.get("name"),
                    "checked_at": r.get("checked_at"),
                    "up": bool(health.get("up")),
                    "latency_ms": health.get("latency_ms"),
                }
                f.write(json.dumps(line) + "\n")
    except OSError:
        pass


def _load_recent(name: str, since: datetime, config_path: str | Path = HISTORY_PATH) -> list[dict[str, Any]]:
    path = Path(config_path)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("name") != name:
                    continue
                try:
                    ts = datetime.fromisoformat(row["checked_at"])
                except (KeyError, ValueError, TypeError):
                    continue
                if ts >= since:
                    rows.append(row)
    except OSError:
        return []
    return rows


def compute_uptime_stats(name: str, now: datetime | None = None, config_path: str | Path = HISTORY_PATH) -> dict[str, Any]:
    """Real uptime % and latency percentiles over the last 24h/7d, computed
    from whatever history has actually been logged so far — samples_24h/7d
    tell the caller how much signal backs the percentage (e.g. "100%
    uptime" off 2 samples right after ops-console started is meaningfully
    different from 100% off 1,000)."""
    now = now or datetime.now()
    rows_24h = _load_recent(name, now - timedelta(hours=24), config_path)
    rows_7d = _load_recent(name, now - timedelta(days=7), config_path)

    def uptime_pct(rows: list[dict[str, Any]]) -> float | None:
        if not rows:
            return None
        up = sum(1 for r in rows if r.get("up"))
        return round(up / len(rows) * 100, 1)

    def latency_percentile(rows: list[dict[str, Any]], pct: float) -> int | None:
        latencies = sorted(r["latency_ms"] for r in rows if r.get("up") and r.get("latency_ms") is not None)
        if not latencies:
            return None
        idx = min(len(latencies) - 1, int(len(latencies) * pct))
        return latencies[idx]

    return {
        "samples_24h": len(rows_24h),
        "samples_7d": len(rows_7d),
        "uptime_24h_pct": uptime_pct(rows_24h),
        "uptime_7d_pct": uptime_pct(rows_7d),
        "latency_p50_ms": latency_percentile(rows_7d, 0.50),
        "latency_p95_ms": latency_percentile(rows_7d, 0.95),
    }


def maybe_prune(now: datetime | None = None, config_path: str | Path = HISTORY_PATH) -> None:
    """Called opportunistically (a small random chance per poll) rather
    than rewriting the whole file every time — dropping lines older than
    RETENTION_DAYS keeps the file from growing forever without needing a
    separate scheduled job."""
    if random.random() >= _PRUNE_PROBABILITY:
        return
    prune(now, config_path)


def prune(now: datetime | None = None, config_path: str | Path = HISTORY_PATH) -> None:
    path = Path(config_path)
    if not path.exists():
        return
    now = now or datetime.now()
    cutoff = now - timedelta(days=RETENTION_DAYS)
    try:
        kept: list[str] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                    ts = datetime.fromisoformat(row["checked_at"])
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    continue
                if ts >= cutoff:
                    kept.append(stripped)
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except OSError:
        pass
