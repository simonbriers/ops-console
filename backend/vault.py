"""Credentials vault (plan UX-3, restructured 2026-07-20 per
docs/TOKEN_ECONOMY_PLAN.md Phase 1) — store credentials ONCE, assign them
to clients many times, and REMEMBER the assignments.

Storage: <config-dir>/vault.json — same persistent location and trust
level as clients.json (which already holds every client's admin token).
Never commit either file; both are gitignored by the same rule.

v2 layout (v1 files are upgraded in place on first load):

  {"version": 2,
   "sets": [{id, name, kind, role, provider, owner, tier, notes,
             values{ENVKEY: value}, updated, content_b64?}],
   "assignments": [{client, role, set_id, applied, source}]}

Sets are still keyed operationally by *kind* (which env keys they own,
which tester verifies them); role/provider/owner/tier are metadata on
top, per the function-first split (llm/stt/tts/email/sms):

  mistral      llm/mistral       MISTRAL_API_KEY
  openrouter   llm/openrouter    OPENROUTER_API_KEY
  nvidia       llm/nvidia        NVIDIA_API_KEY
  smtp         email/smtp        SMTP_HOST SMTP_PORT SMTP_USERNAME
                                 SMTP_PASSWORD SMTP_USE_TLS
  twilio       sms/twilio        TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN
                                 TWILIO_FROM_NUMBER
  file/google_tts  tts/google    (special: `content_b64` — the
                                 google_tts.json service-account file,
                                 uploaded to the checkout root)

owner: "ours" | "client" (BYOK — client-owned key we merely custody).
tier:  "paid" | "free" | "local" — informational sourcing metadata; the
vault is deliberately source-agnostic (TOKEN_ECONOMY_PLAN.md D-notes):
free/paid/local sources mix freely, policy lives with the operator.

Assignments are the memory apply_sets never had: one active record per
(client, role). They are written on every successful apply (including
the onboarding credentials step, which calls apply_sets too) and can be
backfilled/refreshed from reality with reconcile_client(), which reads
the client's remote .env and value-matches it against the stored sets —
that same pass reports drift (an env key that matches NO stored set).

alias_for(): "key_" + sha256(key)[:6], the exact alias the product's
backend/providers/llm.py:get_llm_info() reports in /admin/metrics
`by_key` — the join between vault credentials and metered usage that
Phase 2's ledger builds on.

apply_sets() merges the chosen sets into the client's existing remote
.env (read-modify-write through env_tool, which backs the old file up),
uploads any file credential, runs the existing per-kind credential
tests, and finishes with core.recreate_app — a RECREATE, never `docker
restart`, because container env is fixed at create time (2026-07-19
lesson).
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from backend import env_tool
from backend.config import DEFAULT_CONFIG_PATH
from backend.core import _admin_get_json, _shell_remote_dir, recreate_app, run_ssh

ROLES = ("llm", "stt", "tts", "email", "sms")

# kind -> operational + default metadata. Adding a provider = one row here
# (+ a tester in env_tool if none exists) — nothing else to touch.
# "roles" = every role this ONE credential can serve (2026-07-20 lesson:
# the same MISTRAL_API_KEY powers the LLM *and* Voxtral STT — one key,
# several roles). The FIRST role is the primary one used for grouping.
KIND_META: dict[str, dict[str, Any]] = {
    # mistral serves three roles on one key: LLM, Voxtral STT, and Voxtral
    # TTS (backend/voice/mistral_tts.py — the EU-compliant TTS option)
    "mistral": {"roles": ["llm", "stt", "tts"], "provider": "mistral", "keys": ["MISTRAL_API_KEY"]},
    "openrouter": {"roles": ["llm"], "provider": "openrouter", "keys": ["OPENROUTER_API_KEY"]},
    "nvidia": {"roles": ["llm"], "provider": "nvidia", "keys": ["NVIDIA_API_KEY"]},
    "smtp": {"roles": ["email"], "provider": "smtp",
             "keys": ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_USE_TLS"]},
    "twilio": {"roles": ["sms"], "provider": "twilio",
               "keys": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"]},
    # provider "google" = the literal voice_tts_provider value instances
    # report (confirmed 2026-07-20 via /admin/config on two live clients)
    "file/google_tts": {"roles": ["tts"], "provider": "google", "keys": []},
}
for _m in KIND_META.values():
    _m["role"] = _m["roles"][0]   # primary role (grouping, back-compat)

# Back-compat export — earlier code (and its callers) used KIND_KEYS.
KIND_KEYS: dict[str, list[str]] = {k: list(m["keys"]) for k, m in KIND_META.items()}

OWNERS = ("ours", "client")
TIERS = ("paid", "free", "local")

# The single env key whose value identifies a set of this kind during
# reconcile (and whose hash is the metrics alias for llm kinds). SMTP is
# identified by host+username+password, twilio by sid+token — see
# _set_matches_env().
_ID_KEY = {"mistral": "MISTRAL_API_KEY", "openrouter": "OPENROUTER_API_KEY",
           "nvidia": "NVIDIA_API_KEY"}


def _vault_path() -> Path:
    return Path(DEFAULT_CONFIG_PATH).parent / "vault.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def alias_for(key: str) -> str:
    """The api_key_alias the product reports for this key in
    /admin/metrics by_key: "key_" + sha256(key)[:6], or "local" for
    keyless/local providers — mirrors backend/providers/llm.py:
    get_llm_info() exactly. For comma-separated multi-key values the
    product hashes whichever single key resolve_llm() picked, so we
    hash the FIRST and callers should treat multi-key sets as "one of
    several aliases"."""
    key = (key or "").split(",")[0].strip()
    if not key or key.lower() in ("ollama", "local", "none", "null"):
        return "local"
    return "key_" + hashlib.sha256(key.encode()).hexdigest()[:6]


def _upgrade(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """v1 -> v2 in memory. Returns (data, changed)."""
    changed = False
    if "assignments" not in data:
        data["assignments"] = []
        changed = True
    for s in data.get("sets", []):
        meta = KIND_META.get(s.get("kind"), {})
        for field, default in (("role", meta.get("role", "llm")),
                               ("provider", meta.get("provider", s.get("kind", ""))),
                               ("owner", "ours"), ("tier", "paid"), ("notes", "")):
            if field not in s:
                s[field] = default
                changed = True
    if data.get("version") != 2:
        data["version"] = 2
        changed = True
    return data, changed


def load_vault() -> dict[str, Any]:
    p = _vault_path()
    if not p.exists():
        return {"version": 2, "sets": [], "assignments": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("sets", [])
        data, changed = _upgrade(data)
        if changed:
            save_vault(data)
        return data
    except (OSError, ValueError):
        return {"version": 2, "sets": [], "assignments": []}


def save_vault(data: dict[str, Any]) -> None:
    _vault_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def _assigned_clients(vault: dict[str, Any], set_id: str) -> list[str]:
    return sorted({a["client"] for a in vault.get("assignments", []) if a.get("set_id") == set_id})


def list_sets(redact: bool = True) -> list[dict[str, Any]]:
    vault = load_vault()
    out = []
    for s in vault["sets"]:
        c = dict(s)
        idk = _ID_KEY.get(s.get("kind", ""))
        if idk:
            # A comma-separated value is the product's primary+fallback key
            # pair (resolve_llm key_index 1/2) — ONE env value, but each key
            # meters under its OWN alias in /admin/metrics by_key. Surface
            # every alias so the ledger join is visible per key.
            raw = (s.get("values") or {}).get(idk, "")
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            c["aliases"] = [alias_for(p) for p in parts]
            c["key_count"] = len(parts)
            c["alias"] = c["aliases"][0] if c["aliases"] else None
        else:
            c["aliases"], c["key_count"], c["alias"] = [], 0, None
        c["assigned_to"] = _assigned_clients(vault, s["id"])
        c["roles"] = list(KIND_META.get(s.get("kind"), {}).get("roles", [s.get("role", "llm")]))
        if redact:
            c["values"] = {k: (v[:4] + "…" if isinstance(v, str) and len(v) > 8 else "•••")
                           for k, v in (s.get("values") or {}).items()}
            c.pop("content_b64", None)
        c["has_file"] = bool(s.get("content_b64"))
        out.append(c)
    return out


def reveal_set(set_id: str) -> dict[str, Any]:
    """Full, unredacted values for one set. Local-trust boundary — same
    file class as clients.json's admin tokens; exposed via POST so the
    values never sit in a GET URL/access log."""
    for s in load_vault()["sets"]:
        if s["id"] == set_id:
            c = dict(s)
            c["has_file"] = bool(s.get("content_b64"))
            c.pop("content_b64", None)  # the file itself stays server-side
            return {"ok": True, "error": None, "set": c}
    return {"ok": False, "error": f"no set with id {set_id!r}", "set": None}


def upsert_set(name: str, kind: str, values: dict[str, str] | None,
               content_b64: str | None = None, set_id: str | None = None,
               owner: str | None = None, tier: str | None = None,
               notes: str | None = None) -> dict[str, Any]:
    if kind not in KIND_META:
        return {"ok": False, "error": f"unknown kind {kind!r} (allowed: {sorted(KIND_META)})"}
    if not (name or "").strip():
        return {"ok": False, "error": "set name is required"}
    if owner is not None and owner not in OWNERS:
        return {"ok": False, "error": f"owner must be one of {OWNERS}"}
    if tier is not None and tier not in TIERS:
        return {"ok": False, "error": f"tier must be one of {TIERS}"}
    allowed = set(KIND_META[kind]["keys"])
    values = {k: v for k, v in (values or {}).items() if k in allowed and str(v).strip()}
    if kind != "file/google_tts" and not values and set_id is None:
        return {"ok": False, "error": f"no usable values for kind {kind!r} (expected keys: {sorted(allowed)})"}
    if kind == "file/google_tts" and not content_b64 and set_id is None:
        return {"ok": False, "error": "file/google_tts set needs content_b64 (the JSON file, base64)"}

    meta = KIND_META[kind]
    vault = load_vault()
    if set_id:
        for s in vault["sets"]:
            if s["id"] == set_id:
                s.update({"name": name.strip(), "kind": kind,
                          "role": meta["role"], "provider": meta["provider"],
                          "updated": _now()})
                if values:  # an edit may resend only metadata; keep old values then
                    s["values"] = values
                if content_b64:
                    s["content_b64"] = content_b64
                if owner is not None:
                    s["owner"] = owner
                if tier is not None:
                    s["tier"] = tier
                if notes is not None:
                    s["notes"] = notes
                save_vault(vault)
                # Rotation helper: tell the caller who runs on this set so
                # the UI can offer "re-apply to N clients" in one click.
                return {"ok": True, "error": None, "id": set_id,
                        "assigned_to": _assigned_clients(vault, set_id)}
        return {"ok": False, "error": f"no set with id {set_id!r}"}
    new = {"id": secrets.token_hex(6), "name": name.strip(), "kind": kind,
           "role": meta["role"], "provider": meta["provider"],
           "owner": owner or "ours", "tier": tier or "paid",
           "notes": notes or "", "values": values, "updated": _now()}
    if content_b64:
        new["content_b64"] = content_b64
    vault["sets"].append(new)
    save_vault(vault)
    return {"ok": True, "error": None, "id": new["id"], "assigned_to": []}


def delete_set(set_id: str) -> dict[str, Any]:
    vault = load_vault()
    before = len(vault["sets"])
    vault["sets"] = [s for s in vault["sets"] if s["id"] != set_id]
    if len(vault["sets"]) == before:
        return {"ok": False, "error": f"no set with id {set_id!r}"}
    # Assignments pointing at a deleted set describe reality (the client
    # still RUNS on whatever was applied) — keep them, flagged, rather
    # than pretending the client has nothing.
    for a in vault["assignments"]:
        if a.get("set_id") == set_id:
            a["set_deleted"] = True
    save_vault(vault)
    return {"ok": True, "error": None}


# ---------------------------------------------------------------------------
# Assignments — one active record per (client, role)
# ---------------------------------------------------------------------------

def record_assignment(client_name: str, role: str, set_id: str, source: str) -> None:
    vault = load_vault()
    vault["assignments"] = [a for a in vault["assignments"]
                            if not (a.get("client") == client_name and a.get("role") == role)]
    vault["assignments"].append({"client": client_name, "role": role, "set_id": set_id,
                                 "applied": _now(), "source": source})
    save_vault(vault)


def clear_assignment(client_name: str, role: str) -> None:
    vault = load_vault()
    vault["assignments"] = [a for a in vault["assignments"]
                            if not (a.get("client") == client_name and a.get("role") == role)]
    save_vault(vault)


def list_assignments() -> list[dict[str, Any]]:
    vault = load_vault()
    names = {s["id"]: s["name"] for s in vault["sets"]}
    kinds = {s["id"]: s["kind"] for s in vault["sets"]}
    out = []
    for a in vault["assignments"]:
        c = dict(a)
        c["set_name"] = names.get(a.get("set_id"), "(deleted set)")
        c["kind"] = kinds.get(a.get("set_id"), "")
        out.append(c)
    return out


def _set_matches_env(s: dict[str, Any], env: dict[str, str]) -> bool:
    """Does this stored set's identity match what the client's .env
    actually contains? Value comparison — we hold the raw secrets, no
    hashing needed. Only 'identity' keys count: hosts/ports/flags may
    legitimately drift without meaning a different credential."""
    values = s.get("values") or {}
    kind = s.get("kind")
    if kind in _ID_KEY:
        want = (values.get(_ID_KEY[kind]) or "").strip()
        have = (env.get(_ID_KEY[kind]) or "").strip()
        return bool(want) and want == have
    if kind == "smtp":
        return all((values.get(k) or "").strip() == (env.get(k) or "").strip()
                   and (values.get(k) or "").strip()
                   for k in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD"))
    if kind == "twilio":
        return all((values.get(k) or "").strip() == (env.get(k) or "").strip()
                   and (values.get(k) or "").strip()
                   for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"))
    return False  # file kinds can't be identified from .env


def _active_providers(client: dict[str, Any]) -> dict[str, str]:
    """Best-effort: which provider is ACTIVE per role, from the instance's
    own admin API (GET /admin/config → llm_provider, voice_*_provider,
    sms_provider). Crucial nuance: an instance's .env legitimately holds
    SEVERAL LLM keys at once (mistral + openrouter + nvidia — the fallback
    stack), but which one the bot actually burns is decided by
    site_config.yaml's llm.provider. "Present in .env" ≠ "active".
    Returns {} when the admin API is unreachable (no/placeholder token,
    instance down) — callers then fall back to present-key matching."""
    try:
        data = _admin_get_json(client, "/admin/config", {})
        return {k: v for k, v in {
            "llm": (data.get("llm_provider") or "").strip(),
            "tts": (data.get("voice_tts_provider") or "").strip(),
            "stt": (data.get("voice_stt_provider") or "").strip(),
            "sms": (data.get("sms_provider") or "").strip(),
            "email": "smtp",
        }.items() if v}
    except Exception:
        return {}


def reconcile_client(client: dict[str, Any]) -> dict[str, Any]:
    """Read the client's remote .env (and, best-effort, its active
    provider config) and align assignments with reality.

    Per role: among the stored sets whose values match the live .env,
    the one matching the instance's ACTIVE provider gets the assignment
    (source="reconcile"); other matching keys are reported as "standby"
    (present in .env — e.g. fallback keys — but not what the bot burns).
    If the active provider can't be determined, first match wins, as
    before. Env keys that look like credentials but match NO stored set
    are reported as drift (with the metrics alias for LLM keys, so the
    operator can still connect them to usage). Read-only toward the
    instance — never writes or restarts anything on the VPS."""
    name = client.get("name", "?")
    res = env_tool.read_remote_env(client.get("ssh_target", ""), client.get("remote_dir", ""))
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "couldn't read remote .env",
                "client": name, "matched": [], "standby": [], "drift": [], "active": {}}
    env = res.get("env") or {}
    vault = load_vault()
    active = _active_providers(client)
    matched, standby, drift, cleared = [], [], [], []
    set_by_id = {s["id"]: s for s in vault["sets"]}
    for role in ROLES:
        # a set is a candidate for every role its kind can SERVE (one
        # mistral key = llm AND stt), not just its primary role
        candidates = [s for s in vault["sets"]
                      if role in KIND_META.get(s.get("kind"), {}).get("roles", [s.get("role")])
                      and _set_matches_env(s, env)]
        want = active.get(role)
        existing = next((a for a in vault["assignments"]
                         if a.get("client") == name and a.get("role") == role), None)
        if not candidates:
            # No stored set matches — if we KNOW the active provider and a
            # recorded assignment contradicts it, that record is stale
            # guesswork: clear it rather than let the matrix mislead.
            if want and existing:
                ex_set = set_by_id.get(existing.get("set_id"))
                if not ex_set or (ex_set.get("provider") or "") != want:
                    clear_assignment(name, role)
                    vault = load_vault()
                    cleared.append({"role": role,
                                    "set_name": ex_set.get("name") if ex_set else "(deleted set)"})
            continue
        if want:
            chosen = next((s for s in candidates if (s.get("provider") or "") == want), None)
        else:
            chosen = candidates[0]
        for s in candidates:
            if chosen is None or s["id"] != chosen["id"]:
                standby.append({"role": role, "set_id": s["id"], "set_name": s["name"],
                                "provider": s.get("provider")})
        if chosen is None:
            # active provider known, but its key isn't stored → drift below;
            # clear any contradicting stale record too
            if existing:
                ex_set = set_by_id.get(existing.get("set_id"))
                if not ex_set or (ex_set.get("provider") or "") != want:
                    clear_assignment(name, role)
                    vault = load_vault()
                    cleared.append({"role": role,
                                    "set_name": ex_set.get("name") if ex_set else "(deleted set)"})
            continue
        if not existing or existing.get("set_id") != chosen["id"]:
            record_assignment(name, role, chosen["id"], "reconcile")
            vault = load_vault()
        matched.append({"role": role, "set_id": chosen["id"], "set_name": chosen["name"],
                        "provider": chosen.get("provider"), "active_provider": want or None})
    # drift: credential-looking env keys whose value matches no stored set
    matched_roles = {m["role"] for m in matched}
    for env_key, kind in [("MISTRAL_API_KEY", "mistral"), ("OPENROUTER_API_KEY", "openrouter"),
                          ("NVIDIA_API_KEY", "nvidia")]:
        val = (env.get(env_key) or "").strip()
        if val and not any(s.get("kind") == kind and _set_matches_env(s, env)
                           for s in vault["sets"]):
            is_active = active.get("llm") == KIND_META[kind]["provider"]
            drift.append({"role": "llm", "env_key": env_key, "alias": alias_for(val),
                          "active": is_active})
    if (env.get("SMTP_HOST") or "").strip() and "email" not in matched_roles:
        drift.append({"role": "email", "env_key": "SMTP_*", "alias": None, "active": False})
    if (env.get("TWILIO_ACCOUNT_SID") or "").strip() and "sms" not in matched_roles:
        drift.append({"role": "sms", "env_key": "TWILIO_*", "alias": None, "active": False})
    return {"ok": True, "error": None, "client": name, "matched": matched,
            "standby": standby, "drift": drift, "cleared": cleared, "active": active}


def import_from_client(client: dict[str, Any]) -> dict[str, Any]:
    """Seeding: read a client's remote .env and create a set per kind that
    has real values there. IDEMPOTENT — if an existing set already value-
    matches what's in the .env, no duplicate is created; the client is
    simply assigned to that set. So re-running import (e.g. after new
    kinds like nvidia/openrouter are added) only picks up what's new.
    Records assignments either way — what it read IS what this client is
    running on."""
    res = env_tool.read_remote_env(client.get("ssh_target", ""), client.get("remote_dir", ""))
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "couldn't read remote .env",
                "created": [], "matched": []}
    env = res.get("env") or {}
    created, matched_existing = [], []
    label = client.get("name", "imported")
    vault = load_vault()
    # role -> [(provider, set_id, is_primary_role)] — assignments deferred
    # until all candidates are known (an .env holds several LLM keys at
    # once, and one key can serve several roles).
    role_candidates: dict[str, list[tuple[str, str, bool]]] = {}
    for kind, meta in KIND_META.items():
        keys = meta["keys"]
        if not keys:
            continue  # file kinds can't come from .env
        values = {k: env[k] for k in keys if str(env.get(k, "")).strip()}
        # SMTP only counts if the essentials are there, not just the port default
        if kind == "smtp" and not (values.get("SMTP_HOST") and values.get("SMTP_PASSWORD")):
            continue
        if kind in _ID_KEY and not values.get(_ID_KEY[kind]):
            continue
        if not values:
            continue
        existing = next((s for s in vault["sets"]
                         if s.get("kind") == kind and _set_matches_env(s, env)), None)
        if existing:
            set_id = existing["id"]
            matched_existing.append({"id": set_id, "kind": kind, "name": existing["name"]})
        else:
            r = upsert_set(f"{kind} (from {label})", kind, values)
            if not r["ok"]:
                continue
            set_id = r["id"]
            created.append({"id": set_id, "kind": kind})
            vault = load_vault()  # keep the dedupe view current for later kinds
        for r_role in meta["roles"]:
            role_candidates.setdefault(r_role, []).append(
                (meta["provider"], set_id, r_role == meta["roles"][0]))
    if role_candidates:
        active = _active_providers(client)
        for r_role, cands in role_candidates.items():
            want = active.get(r_role)
            if want:
                chosen = next((sid for prov, sid, _p in cands if prov == want), None)
                # active provider known but its key wasn't importable —
                # leave unassigned; reconcile flags it as (active) drift.
            else:
                # active unknown: only a kind's PRIMARY role is a safe
                # guess (mistral → llm yes, stt only if config says so)
                chosen = next((sid for _prov, sid, primary in cands if primary), None)
            if chosen:
                record_assignment(label, r_role, chosen, "import")
    return {"ok": True, "error": None, "created": created, "matched": matched_existing}


def apply_sets(client: dict[str, Any], set_ids: list[str]) -> dict[str, Any]:
    """Merge the chosen sets into the client's remote .env, upload file
    credentials, test what landed, recreate the container, and RECORD the
    assignments. Returns {"ok", "error", "applied": [kinds],
    "tests": [...], "recreated": bool}."""
    ssh_target = client.get("ssh_target") or ""
    remote_dir = client.get("remote_dir") or ""
    if not ssh_target or not remote_dir:
        return {"ok": False, "error": "client has no ssh_target/remote_dir", "applied": [], "tests": []}
    chosen = [s for s in load_vault()["sets"] if s["id"] in set(set_ids)]
    if not chosen:
        return {"ok": False, "error": "no matching vault sets", "applied": [], "tests": []}

    current = env_tool.read_remote_env(ssh_target, remote_dir)
    if not current.get("ok"):
        return {"ok": False, "error": f"couldn't read current .env: {current.get('error')}",
                "applied": [], "tests": []}
    env = dict(current.get("env") or {})

    applied, tests = [], []
    for s in chosen:
        if s["kind"] == "file/google_tts":
            b64 = s.get("content_b64", "")
            shell_dir = _shell_remote_dir(remote_dir.rstrip("/"))
            ok, out = run_ssh(ssh_target,
                              f"printf '%s' '{b64}' | base64 -d > {shell_dir}/google_tts.json "
                              f"&& chmod 600 {shell_dir}/google_tts.json && echo FILE_OK")
            if not ok or "FILE_OK" not in out:
                return {"ok": False, "error": f"google_tts.json upload failed: {out.strip()[-300:]}",
                        "applied": applied, "tests": tests}
            applied.append(s["kind"])
            continue
        env.update(s.get("values") or {})
        applied.append(s["kind"])

    write = env_tool.write_remote_env(ssh_target, remote_dir, env)
    if not write.get("ok"):
        return {"ok": False, "error": f".env write failed: {write.get('error')}",
                "applied": applied, "tests": tests}

    # test what landed, per kind, with the existing testers
    for s in chosen:
        kind = s["kind"]
        if kind == "mistral":
            tests.append({"kind": kind, **env_tool.test_mistral(env.get("MISTRAL_API_KEY", ""))})
        elif kind == "openrouter":
            tests.append({"kind": kind, **env_tool.test_openrouter(env.get("OPENROUTER_API_KEY", ""))})
        elif kind == "nvidia":
            tests.append({"kind": kind, **env_tool.test_nvidia(env.get("NVIDIA_API_KEY", ""))})
        elif kind == "smtp":
            tests.append({"kind": kind, **env_tool.test_smtp(
                env.get("SMTP_HOST", ""), env.get("SMTP_PORT", "587"),
                env.get("SMTP_USERNAME", ""), env.get("SMTP_PASSWORD", ""),
                str(env.get("SMTP_USE_TLS", "True")).lower() != "false")})
        elif kind == "twilio":
            tests.append({"kind": kind, **env_tool.test_twilio(
                env.get("TWILIO_ACCOUNT_SID", ""), env.get("TWILIO_AUTH_TOKEN", ""))})

    rec = recreate_app(ssh_target, remote_dir)
    # The .env now contains these sets whether or not the recreate
    # succeeded — record reality either way.
    client_name = client.get("name", "")
    if client_name:
        for s in chosen:
            record_assignment(client_name, s.get("role", KIND_META[s["kind"]]["role"]),
                              s["id"], "apply")
    return {"ok": rec.get("ok", False),
            "error": None if rec.get("ok") else f"applied + written, but recreate failed: {rec.get('error')}",
            "applied": applied, "tests": tests, "recreated": rec.get("ok", False)}
