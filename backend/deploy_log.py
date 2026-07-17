"""Audit trail for deploy_client() — the one action in ops-console that
actually mutates a monitored instance. Every attempt (success or failure)
is appended here, mirroring the spirit of the product's own AuditLog: a
mutation without a record of who/when/what is worse than no mutation
capability at all. Purely local state, same append-only-JSONL pattern as
history.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from backend import config as cfg

DEPLOY_LOG_PATH = Path(os.environ.get("DEPLOY_LOG_FILE", str(cfg.PROJECT_ROOT / "deploy_log.jsonl")))


def append(entry: dict[str, Any], config_path: str | Path = DEPLOY_LOG_PATH) -> None:
    """entry: {name, requested_at, ok, stage, commit, error}. Never raises —
    a logging failure must never block reporting the deploy result back to
    the user, even though losing an audit entry is worth avoiding."""
    try:
        path = Path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def load_recent(name: str, limit: int = 10, config_path: str | Path = DEPLOY_LOG_PATH) -> list[dict[str, Any]]:
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
                if row.get("name") == name:
                    rows.append(row)
    except OSError:
        return []
    return rows[-limit:][::-1]  # most recent first
