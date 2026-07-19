"""Onboarding v2 — the per-client step-state store.

Each onboarding is one JSON file under <config-dir>/onboardings/ (the same
persistent location as clients.json — the Docker volume in the Docker path,
the project root in the venv path), so an onboarding survives browser
closes, console restarts, and container rebuilds. The record holds the
intake "bundle" (everything the operator typed) plus a status per step, so
the UI can always show "acme-dental — step 4/7, waiting on credentials"
and any step can be re-run after a failure without redoing the ones before
it. See docs/ONBOARDING_V2_PLAN.md (UX-2) for the step definitions.

Deliberately no locking: this is a single-operator tool (same stance as
clients.json's read-modify-write everywhere else in this codebase).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.config import DEFAULT_CONFIG_PATH

# Ordered step ids — the stepper's spine. "done" is a derived state (all
# steps ok), not a step itself.
STEPS: list[dict[str, str]] = [
    {"id": "intake", "title": "Intake", "detail": "Client details saved"},
    {"id": "dns", "title": "DNS", "detail": "Hostname resolves to the VPS"},
    {"id": "provision", "title": "Provision", "detail": "Clone, build, boot, config, Caddy, register"},
    {"id": "credentials", "title": "Credentials", "detail": "Vault sets applied, tested, container recreated"},
    {"id": "config", "title": "Config check", "detail": "Live site_config.yaml validated"},
    {"id": "verify", "title": "Verify", "detail": "Smoke suite green"},
]
STEP_IDS = [s["id"] for s in STEPS]
_VALID_STATUS = {"pending", "running", "ok", "failed", "skipped"}

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

# The standing naming rule (2026-07-19, see dental repo CLAUDE.md
# conventions): every chatbot instance lives at <sub>.my-ai-receptionist.com.
# The intake therefore only asks for the SUBDOMAIN; anything else that gets
# posted is normalized/validated here so a typo'd domain (underscores, wrong
# suffix) can never reach DNS checks or Caddy wiring.
DEFAULT_DOMAIN = "my-ai-receptionist.com"
_SUB_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def normalize_hostname(value):
    """Returns (hostname, None) or (None, error). Accepts a bare subdomain
    ("acme") or a full hostname already ending in the default domain;
    rejects everything else with a message that says what to type."""
    v = (value or "").strip().lower().rstrip(".")
    if not v:
        return None, "subdomain is required"
    if v.endswith("." + DEFAULT_DOMAIN):
        v = v[: -(len(DEFAULT_DOMAIN) + 1)]
    if "." in v or "_" in v or not _SUB_RE.match(v):
        return None, (repr(value) + " is not a valid subdomain - use lowercase letters, "
                      "digits and hyphens only (it becomes <sub>." + DEFAULT_DOMAIN +
                      "; the domain part is added automatically, don't type it)")
    return v + "." + DEFAULT_DOMAIN, None



def _store_dir() -> Path:
    d = Path(DEFAULT_CONFIG_PATH).parent / "onboardings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(deploy_name: str) -> Path:
    return _store_dir() / f"{deploy_name}.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_record(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "deploy_name": bundle.get("deploy_name", ""),
        "bundle": bundle,
        "steps": {sid: {"status": "pending", "detail": "", "updated": None} for sid in STEP_IDS},
        "torn_down": False,
        "result": {},          # provision output: port, remote_dir, admin_password…
        "created": _now(),
        "updated": _now(),
    }


def valid_name(deploy_name: str) -> bool:
    return bool(deploy_name and _NAME_RE.match(deploy_name))


def load(deploy_name: str) -> dict[str, Any] | None:
    if not valid_name(deploy_name):
        return None
    p = _path(deploy_name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_all() -> list[dict[str, Any]]:
    records = []
    for p in sorted(_store_dir().glob("*.json")):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue  # a corrupt record shouldn't hide the healthy ones
    return records


def save(record: dict[str, Any]) -> dict[str, Any]:
    name = record.get("deploy_name", "")
    if not valid_name(name):
        raise ValueError(f"invalid deploy_name {name!r}")
    record["updated"] = _now()
    _path(name).write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def set_step(deploy_name: str, step_id: str, status: str, detail: str = "") -> dict[str, Any] | None:
    """Update one step's status (creating nothing — the record must exist).
    Returns the updated record, or None if the record/step is unknown."""
    if step_id not in STEP_IDS or status not in _VALID_STATUS:
        return None
    record = load(deploy_name)
    if record is None:
        return None
    record["steps"].setdefault(step_id, {})
    record["steps"][step_id].update(
        {"status": status, "detail": (detail or "")[-2000:], "updated": _now()})
    return save(record)


def merge_result(deploy_name: str, **kv: Any) -> None:
    """Fold provisioning outputs (port, remote_dir, admin_password, …) into
    the record so the Done sheet can show them later."""
    record = load(deploy_name)
    if record is None:
        return
    record.setdefault("result", {}).update({k: v for k, v in kv.items() if v is not None})
    save(record)


def summary(record: dict[str, Any]) -> dict[str, Any]:
    """Compact shape for the list view: name + step statuses + progress."""
    steps = record.get("steps", {})
    ok = sum(1 for sid in STEP_IDS if steps.get(sid, {}).get("status") == "ok")
    current = next((sid for sid in STEP_IDS
                    if steps.get(sid, {}).get("status") not in ("ok", "skipped")), None)
    return {
        "deploy_name": record.get("deploy_name"),
        "display_name": record.get("bundle", {}).get("display_name", ""),
        "hostname": record.get("bundle", {}).get("hostname", ""),
        "progress": f"{ok}/{len(STEP_IDS)}",
        "current_step": current,
        "torn_down": record.get("torn_down", False),
        "updated": record.get("updated"),
        "steps": {sid: steps.get(sid, {}).get("status", "pending") for sid in STEP_IDS},
    }
