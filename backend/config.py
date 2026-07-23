"""Config load/save for ops-console's own state: the list of monitored
client instances (clinics, law firms, whatever vertical — same underlying
product), the VPS/host(s) they run on (for Caddyfile site-discovery +
overall disk/mem checks), and the dashboard poll interval.

Path resolution: CLIENTS_CONFIG env var if set (the Docker image sets this
to /data/clients.json, the persistent volume mount — see Dockerfile), else
a clients.json sitting next to the project root (for plain `python -m
venv` local dev, no Docker involved).

Every save_* function reads the full existing payload first and only
overwrites the one key it's responsible for — clients.json holds
`poll_interval_seconds`, `clients`, AND `hosts` together, so a naive
"write just clients+interval" save would silently wipe out `hosts` (or
vice versa) on every client edit. That bug was caught here specifically
because it's the kind of thing that only shows up after this file already
has real, hand-fetched secrets in it — worth getting right the first time.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = Path(os.environ.get("CLIENTS_CONFIG", str(PROJECT_ROOT / "clients.json")))

_DEFAULT_PAYLOAD = {"poll_interval_seconds": 60, "clients": [], "hosts": [], "operator_token": ""}


def _read(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        return dict(_DEFAULT_PAYLOAD)
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    # Backfill any key an older config file predates (e.g. "hosts" didn't
    # exist before this feature) rather than letting a KeyError/missing
    # key silently mean "there are zero hosts" in a way that's hard to
    # distinguish from "the user configured zero on purpose".
    for key, default in _DEFAULT_PAYLOAD.items():
        data.setdefault(key, default)
    return data


def _write(data: dict[str, Any], config_path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_clients(config_path: str | Path = DEFAULT_CONFIG_PATH) -> list[dict[str, Any]]:
    return _read(config_path).get("clients", [])


def load_hosts(config_path: str | Path = DEFAULT_CONFIG_PATH) -> list[dict[str, Any]]:
    return _read(config_path).get("hosts", [])


def load_poll_interval(config_path: str | Path = DEFAULT_CONFIG_PATH, default: int = 60) -> int:
    return int(_read(config_path).get("poll_interval_seconds", default))


def save_clients(
    clients: list[dict[str, Any]],
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    poll_interval_seconds: int | None = None,
) -> None:
    data = _read(config_path)
    data["clients"] = clients
    if poll_interval_seconds is not None:
        data["poll_interval_seconds"] = poll_interval_seconds
    _write(data, config_path)


def save_hosts(hosts: list[dict[str, Any]], config_path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    data = _read(config_path)
    data["hosts"] = hosts
    _write(data, config_path)


def save_poll_interval(poll_interval_seconds: int, config_path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    data = _read(config_path)
    data["poll_interval_seconds"] = poll_interval_seconds
    _write(data, config_path)


def load_operator_token(config_path: str | Path = DEFAULT_CONFIG_PATH) -> str:
    """The fleet-wide operator token (empty string when unset). One value,
    stored once here, that every client inherits unless its own entry sets a
    per-client `operator_token` override — so managed-field config writes
    authenticate without a per-instance key to remember. Written only by the
    operator-key rotate flow (backend/operator_key.py)."""
    return str(_read(config_path).get("operator_token", "") or "")


def save_operator_token(token: str, config_path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    """Persist the fleet operator token, preserving clients/hosts/interval
    (same read-modify-write discipline as every other save_* here)."""
    data = _read(config_path)
    data["operator_token"] = token or ""
    _write(data, config_path)


def find_client(name: str, config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any] | None:
    for c in load_clients(config_path):
        if c.get("name") == name:
            return c
    return None
