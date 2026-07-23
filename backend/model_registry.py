"""Operator-controlled MODEL registry + per-client model assignments — the
model-side analogue of vault.py's credential sets + assignments.

Governing principle (operator decision, 2026-07-21): operators are ALWAYS in
control of which models may run. Clients never pick models freely — that way
lies "50 clients each on their own model, unmanageable."

Billing note (DUPLICATION_AUDIT 4.2): the assignment records INTENT/governance
("this client is meant to run model X"), NOT the billing truth. The ledger
bills each client off the actual METERED draw (its live per-alias / per-model
usage snapshots), never off this assignment record — so a client whose live
.env has drifted from its assignment still bills on what it actually used. Treat
this file as "what should be running," and the ledger's snapshots as "what did."

  * REGISTRY  — the operator-approved allow-list of models. Seeded once from
    model_catalog.MODEL_CATALOG (the built-in fleet), then grown by the operator
    (e.g. picking from a provider's live GET /models via the config UI's "add to
    registry" flow). A model a client runs MUST be in here. The registry is a
    pure allow-list — it does NOT store prices (those live once in model_catalog
    and the ledger's model_rates; see DUPLICATION_AUDIT 2.1).

  * ASSIGNMENTS — one active record per (client, SLOT): which approved model a
    client is meant to run in that slot. Direct analogue of vault.py's
    (client, role) -> set_id credential assignment, kept in a SEPARATE file
    (models.json) so the working credential logic in vault.py is untouched.
    Operator intent + the reconcile/governance ("is anything off-allow-list?")
    join — not the billing join (see the Billing note above).

A "slot" is a concrete model-bearing config field on the instance, not just a
role — the same role fills several slots (an llm-role model serves both the
TEXT agent's llm.model and the VOICE agent's voice.llm.model):

    slot         config field        required role   provider field
    llm          llm_model           llm             llm_provider
    voice_llm    voice_llm_model     llm             voice_llm_provider
    voice_stt    voice_stt_model     stt             voice_stt_provider

assign_model() writes BOTH the model field AND its provider field to the
instance (via config_manager.write_config -> the instance's validated
PUT /admin/config), so provider and model can never drift apart, then records
the assignment. reconcile_models() reads each instance's LIVE site_config and
records what's actually running — flagging any live model NOT in the registry
as a governance violation (the un-approved-model / "cacophony" detector).

Storage: <config-dir>/models.json — same persistent location and trust class
as vault.json and clients.json. Never committed.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from backend import config_manager
from backend import model_catalog
from backend.config import DEFAULT_CONFIG_PATH

# slot -> the instance config field it writes, the model role it requires, and
# the sibling provider field written alongside it (so provider/model stay in
# lockstep). Adding voice_tts later = one row here (+ a voice.tts.model field on
# the product's admin.py).
#
# The write-side field, the model role, the sibling provider field, AND the
# read-side YAML paths are all DERIVED from that one config field's entry in
# config_manager.FIELD_GROUPS (which already carries role / provider_field /
# dotted path). Before this they were restated as two hand-kept dicts (SLOTS +
# _SLOT_YAML) that could silently disagree — a typo made assign_model write one
# path while reconcile_models read another, invisibly. See DUPLICATION_AUDIT 3.2.
#
# slot -> the config field (a type:"model" field in FIELD_GROUPS) whose value is
# the slot's model. Adding a slot = one line here; its config field must already
# exist in FIELD_GROUPS. (voice_tts is intentionally NOT a slot yet.)
_SLOT_FIELD: dict[str, str] = {
    "llm": "llm_model",
    "voice_llm": "voice_llm_model",
    "voice_stt": "voice_stt_model",
}


def _catalog_field(name: str) -> dict[str, Any]:
    for group in config_manager.FIELD_GROUPS:
        for f in group["fields"]:
            if f["name"] == name:
                return f
    raise KeyError(f"config field {name!r} missing from FIELD_GROUPS — slot wiring is stale")


def _build_slots() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, tuple[str, ...]]]]:
    slots: dict[str, dict[str, str]] = {}
    slot_yaml: dict[str, dict[str, tuple[str, ...]]] = {}
    for slot, field_name in _SLOT_FIELD.items():
        f = _catalog_field(field_name)
        provider_field = f["provider_field"]
        pf = _catalog_field(provider_field)
        slots[slot] = {"field": field_name, "role": f["role"], "provider_field": provider_field}
        slot_yaml[slot] = {"model": tuple(f["path"].split(".")),
                           "provider": tuple(pf["path"].split("."))}
    return slots, slot_yaml


# SLOTS: slot -> {field, role, provider_field} (write side, unchanged shape).
# _SLOT_YAML: slot -> {model: yaml-path-tuple, provider: yaml-path-tuple} (read
# side). Both built from the SAME config-field entries, so they cannot drift.
SLOTS, _SLOT_YAML = _build_slots()

# Reuse the model roles from the catalog rather than restating them (3.4) —
# add_model validates against this, so a role added to the catalog is honored here.
ROLES = model_catalog.ROLES

# Legacy price fields that older registries copied from the catalog. The
# registry is no longer a price source (DUPLICATION_AUDIT 2.1); this list is now
# used only to STRIP any such leftover fields from an existing models.json on
# load, so the served registry never re-exposes a stale price.
_PRICE_FIELDS = ("unit", "buy_in_per_m", "buy_cached_per_m", "buy_out_per_m", "buy_per_unit")


def _models_path() -> Path:
    return Path(DEFAULT_CONFIG_PATH).parent / "models.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _seed_registry() -> list[dict[str, Any]]:
    """Built-in fleet from model_catalog, normalized to registry entries. The
    registry is a pure allow-list (id / provider / role / label) — it no longer
    copies the catalog's PRICE fields. Price lives once, in model_catalog (and
    the ledger's model_rates, seeded from it); copying it onto registry entries
    made a third stale copy that nothing authoritative read (DUPLICATION_AUDIT 2.1)."""
    return [
        {"id": m["id"], "provider": m.get("provider", ""), "role": m.get("role", "llm"),
         "label": m.get("label", m["id"]), "notes": m.get("notes", ""),
         "source": "builtin", "added": _now()}
        for m in model_catalog.MODEL_CATALOG
    ]


def load_models() -> dict[str, Any]:
    p = _models_path()
    if not p.exists():
        data = {"version": 1, "registry": _seed_registry(), "assignments": []}
        save_models(data)
        return data
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("registry", [])
        data.setdefault("assignments", [])
        data.setdefault("version", 1)
        # Strip any legacy price fields an older version copied onto entries —
        # the registry is an allow-list, not a price source (2.1).
        for entry in data["registry"]:
            for pf in _PRICE_FIELDS:
                entry.pop(pf, None)
        return data
    except (OSError, ValueError):
        return {"version": 1, "registry": [], "assignments": []}


def save_models(data: dict[str, Any]) -> None:
    _models_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Registry — the operator allow-list
# ---------------------------------------------------------------------------

def list_registry(role: str | None = None, provider: str | None = None) -> list[dict[str, Any]]:
    out = []
    for m in load_models()["registry"]:
        if role and m.get("role") != role:
            continue
        if provider and m.get("provider") != provider:
            continue
        out.append(dict(m))
    return out


def add_model(model_id: str, provider: str, role: str,
              label: str | None = None, notes: str | None = None,
              price: dict[str, Any] | None = None) -> dict[str, Any]:
    """Approve a model (add/update it in the registry). Idempotent on
    (id, provider). `price` is accepted for backward-compat but IGNORED — the
    registry is an allow-list, not a price store; per-model rates live in the
    ledger (set_model_rate) and defaults in model_catalog (DUPLICATION_AUDIT 2.1)."""
    model_id = (model_id or "").strip()
    provider = (provider or "").strip()
    role = (role or "").strip()
    if role not in ROLES:
        return {"ok": False, "error": f"role must be one of {ROLES}"}
    # An empty model_id APPROVES A PROVIDER for a role with no specific model —
    # for voice-only TTS providers (google, piper) whose "model" is really the
    # voice. A provider is required in that case.
    if not model_id and not provider:
        return {"ok": False, "error": "need a model id, or a provider for a voice-only approval"}
    data = load_models()
    for m in data["registry"]:
        if m["id"] == model_id and m.get("provider") == provider:
            m["role"] = role
            m["label"] = label or m.get("label") or model_id or provider
            if notes is not None:
                m["notes"] = notes
            save_models(data)
            return {"ok": True, "error": None, "id": model_id, "updated": True}
    entry = {"id": model_id, "provider": provider, "role": role,
             "label": label or model_id or provider, "notes": notes or "",
             "source": "operator", "added": _now()}
    data["registry"].append(entry)
    save_models(data)
    return {"ok": True, "error": None, "id": model_id, "updated": False}


def remove_model(model_id: str, provider: str | None = None) -> dict[str, Any]:
    data = load_models()
    before = len(data["registry"])
    data["registry"] = [m for m in data["registry"]
                        if not (m["id"] == model_id and (provider is None or m.get("provider") == provider))]
    if len(data["registry"]) == before:
        return {"ok": False, "error": f"no registry model {model_id!r}"}
    save_models(data)
    return {"ok": True, "error": None}


def _find_model(data: dict[str, Any], model_id: str, role: str | None = None) -> dict[str, Any] | None:
    for m in data["registry"]:
        if m["id"] == model_id and (role is None or m.get("role") == role):
            return m
    return None


# ---------------------------------------------------------------------------
# Assignments — one active per (client, slot); the billing join key
# ---------------------------------------------------------------------------

def record_model_assignment(client_name: str, slot: str, model_id: str, source: str) -> None:
    data = load_models()
    data["assignments"] = [a for a in data["assignments"]
                          if not (a.get("client") == client_name and a.get("slot") == slot)]
    data["assignments"].append({"client": client_name, "slot": slot, "model_id": model_id,
                               "applied": _now(), "source": source})
    save_models(data)


def clear_model_assignment(client_name: str, slot: str) -> None:
    data = load_models()
    data["assignments"] = [a for a in data["assignments"]
                          if not (a.get("client") == client_name and a.get("slot") == slot)]
    save_models(data)


def list_model_assignments() -> list[dict[str, Any]]:
    """Every (client, slot) -> model record, joined to the registry so the UI
    (and billing) sees provider + whether the model is still approved."""
    data = load_models()
    by_id = {m["id"]: m for m in data["registry"]}
    out = []
    for a in data["assignments"]:
        c = dict(a)
        m = by_id.get(a.get("model_id"))
        c["provider"] = m.get("provider") if m else None
        c["in_registry"] = m is not None
        out.append(c)
    return out


def assign_model(client: dict[str, Any], slot: str, model_id: str) -> dict[str, Any]:
    """Assign an APPROVED model to a client's slot: verify it's in the registry
    and its role fits the slot, write the model + its provider to the instance
    (config_manager.write_config -> validated PUT /admin/config), and record the
    assignment. Operator governance: a model not in the registry is refused
    here and never written — that's what stops the per-client cacophony."""
    if slot not in SLOTS:
        return {"ok": False, "error": f"unknown slot {slot!r} (one of {sorted(SLOTS)})"}
    spec = SLOTS[slot]
    m = _find_model(load_models(), model_id, role=spec["role"])
    if not m:
        return {"ok": False, "error": f"model {model_id!r} is not an approved "
                f"{spec['role']} model in the registry — add it to the registry first"}
    fields = {spec["field"]: model_id, spec["provider_field"]: m.get("provider", "")}
    res = config_manager.write_config(client, fields)
    if not res.get("ok"):
        return {"ok": False, "error": f"instance write failed: {res.get('error')}", "written": None}
    record_model_assignment(client.get("name", ""), slot, model_id, "assign")
    return {"ok": True, "error": None, "written": sorted(fields),
            "mismatches": res.get("mismatches", [])}


def _dig(d: Any, path: tuple[str, ...]) -> Any:
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def reconcile_models(client: dict[str, Any]) -> dict[str, Any]:
    """Read the client's LIVE site_config and record what each slot is actually
    running (source="reconcile"), flagging any live model NOT in the operator
    registry as an un-approved (governance-violating) model. Read-only toward
    the instance. This is what keeps the per-client billing registry truthful
    and surfaces anything running off-allow-list."""
    name = client.get("name", "?")
    live = config_manager.read_live_yaml(client)
    if not live.get("ok"):
        return {"ok": False, "error": live.get("error") or "couldn't read live config",
                "client": name, "matched": [], "unapproved": []}
    parsed = live.get("parsed") or {}
    reg_ids = {m["id"] for m in load_models()["registry"]}
    matched, unapproved = [], []
    for slot, ypaths in _SLOT_YAML.items():
        model_id = _dig(parsed, ypaths["model"])
        provider = _dig(parsed, ypaths["provider"])
        if not model_id:
            continue
        if model_id in reg_ids:
            record_model_assignment(name, slot, model_id, "reconcile")
            matched.append({"slot": slot, "model_id": model_id, "provider": provider})
        else:
            unapproved.append({"slot": slot, "model_id": model_id, "provider": provider})
    return {"ok": True, "error": None, "client": name,
            "matched": matched, "unapproved": unapproved}
