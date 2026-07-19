"""Credentials vault (plan UX-3) — store credentials ONCE, assign them to
clients many times, instead of copying between clients' remote .env files
key-by-key.

Storage: <config-dir>/vault.json — same persistent location and trust
level as clients.json (which already holds every client's admin token).
Never commit either file; both are gitignored by the same rule.

A "set" is {id, name, kind, values{ENVKEY: value}, updated}. Kinds and the
env keys they own:

  mistral    MISTRAL_API_KEY
  smtp       SMTP_HOST SMTP_PORT SMTP_USERNAME SMTP_PASSWORD SMTP_USE_TLS
  twilio     TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_FROM_NUMBER
  file/google_tts   (special: `content_b64` — the google_tts.json service
                     account file, uploaded to the checkout root)

apply_sets() merges the chosen sets into the client's existing remote
.env (read-modify-write through env_tool, which backs the old file up),
uploads any file credential, runs the existing per-kind credential tests,
and finishes with core.recreate_app — a RECREATE, never `docker restart`,
because container env is fixed at create time (2026-07-19 lesson).
"""
from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from backend import env_tool
from backend.config import DEFAULT_CONFIG_PATH
from backend.core import _shell_remote_dir, recreate_app, run_ssh

KIND_KEYS: dict[str, list[str]] = {
    "mistral": ["MISTRAL_API_KEY"],
    "smtp": ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_USE_TLS"],
    "twilio": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"],
    "file/google_tts": [],  # file-type: carries content_b64 instead of env keys
}


def _vault_path() -> Path:
    return Path(DEFAULT_CONFIG_PATH).parent / "vault.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_vault() -> dict[str, Any]:
    p = _vault_path()
    if not p.exists():
        return {"sets": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("sets", [])
        return data
    except (OSError, ValueError):
        return {"sets": []}


def save_vault(data: dict[str, Any]) -> None:
    _vault_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_sets(redact: bool = True) -> list[dict[str, Any]]:
    sets = load_vault()["sets"]
    if not redact:
        return sets
    out = []
    for s in sets:
        c = dict(s)
        c["values"] = {k: (v[:4] + "…" if isinstance(v, str) and len(v) > 8 else "•••")
                       for k, v in (s.get("values") or {}).items()}
        c.pop("content_b64", None)
        c["has_file"] = bool(s.get("content_b64"))
        out.append(c)
    return out


def upsert_set(name: str, kind: str, values: dict[str, str] | None,
               content_b64: str | None = None, set_id: str | None = None) -> dict[str, Any]:
    if kind not in KIND_KEYS:
        return {"ok": False, "error": f"unknown kind {kind!r} (allowed: {sorted(KIND_KEYS)})"}
    if not (name or "").strip():
        return {"ok": False, "error": "set name is required"}
    allowed = set(KIND_KEYS[kind])
    values = {k: v for k, v in (values or {}).items() if k in allowed and str(v).strip()}
    if kind != "file/google_tts" and not values:
        return {"ok": False, "error": f"no usable values for kind {kind!r} (expected keys: {sorted(allowed)})"}
    if kind == "file/google_tts" and not content_b64:
        return {"ok": False, "error": "file/google_tts set needs content_b64 (the JSON file, base64)"}

    vault = load_vault()
    if set_id:
        for s in vault["sets"]:
            if s["id"] == set_id:
                s.update({"name": name.strip(), "kind": kind, "values": values, "updated": _now()})
                if content_b64:
                    s["content_b64"] = content_b64
                save_vault(vault)
                return {"ok": True, "error": None, "id": set_id}
        return {"ok": False, "error": f"no set with id {set_id!r}"}
    new = {"id": secrets.token_hex(6), "name": name.strip(), "kind": kind,
           "values": values, "updated": _now()}
    if content_b64:
        new["content_b64"] = content_b64
    vault["sets"].append(new)
    save_vault(vault)
    return {"ok": True, "error": None, "id": new["id"]}


def delete_set(set_id: str) -> dict[str, Any]:
    vault = load_vault()
    before = len(vault["sets"])
    vault["sets"] = [s for s in vault["sets"] if s["id"] != set_id]
    if len(vault["sets"]) == before:
        return {"ok": False, "error": f"no set with id {set_id!r}"}
    save_vault(vault)
    return {"ok": True, "error": None}


def import_from_client(client: dict[str, Any]) -> dict[str, Any]:
    """One-time seeding: read a client's remote .env and create a set per
    kind that has real values there. The troublesome copy flow, run exactly
    once more, ever."""
    res = env_tool.read_remote_env(client.get("ssh_target", ""), client.get("remote_dir", ""))
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "couldn't read remote .env", "created": []}
    env = res.get("env") or {}
    created = []
    label = client.get("name", "imported")
    for kind, keys in KIND_KEYS.items():
        if not keys:
            continue  # file kinds can't come from .env
        values = {k: env[k] for k in keys if str(env.get(k, "")).strip()}
        # SMTP only counts if the essentials are there, not just the port default
        if kind == "smtp" and not (values.get("SMTP_HOST") and values.get("SMTP_PASSWORD")):
            continue
        if values:
            r = upsert_set(f"{kind} (from {label})", kind, values)
            if r["ok"]:
                created.append({"id": r["id"], "kind": kind})
    return {"ok": True, "error": None, "created": created}


def apply_sets(client: dict[str, Any], set_ids: list[str]) -> dict[str, Any]:
    """Merge the chosen sets into the client's remote .env, upload file
    credentials, test what landed, recreate the container. Returns
    {"ok", "error", "applied": [kinds], "tests": [...], "recreated": bool}."""
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
        if s["kind"] == "mistral":
            tests.append({"kind": "mistral", **env_tool.test_mistral(env.get("MISTRAL_API_KEY", ""))})
        elif s["kind"] == "smtp":
            tests.append({"kind": "smtp", **env_tool.test_smtp(
                env.get("SMTP_HOST", ""), env.get("SMTP_PORT", "587"),
                env.get("SMTP_USERNAME", ""), env.get("SMTP_PASSWORD", ""),
                str(env.get("SMTP_USE_TLS", "True")).lower() != "false")})
        elif s["kind"] == "twilio":
            tests.append({"kind": "twilio", **env_tool.test_twilio(
                env.get("TWILIO_ACCOUNT_SID", ""), env.get("TWILIO_AUTH_TOKEN", ""))})

    rec = recreate_app(ssh_target, remote_dir)
    return {"ok": rec.get("ok", False),
            "error": None if rec.get("ok") else f"applied + written, but recreate failed: {rec.get('error')}",
            "applied": applied, "tests": tests, "recreated": rec.get("ok", False)}
