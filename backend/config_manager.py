"""Config manager (TOKEN_ECONOMY_PLAN.md Phase 7) — the console-side window
onto each instance's live site_config.yaml.

Three jobs, three trust levels:

1. **API-first read/write.** `get_live_config()` reads `GET /admin/config`;
   `write_config()` writes `PUT /admin/config` — both through
   `core._admin_get_json`/`_admin_put_json`, which attach the client's
   `operator_token` as X-Operator-Token so managed instances (product Phase
   6) accept infra fields and return unredacted secrets. Going through the
   instance's own validated endpoint gets us its field validation (EU voice
   guard, brand_color regex, ...), hot reload, and its audit trail for free.

2. **SSH fallback + drift detection.** `read_live_yaml()` cats the live
   `/data/site_config.yaml` out of the app container (read-only — same
   command the config validator already uses); `drift_check()` diffs it
   against the checkout's shipped `backend/site_config.yaml` (keys the
   volume copy never received — the DEPLOYMENT.md §10 "stale config" gap,
   a.k.a. the purple-chatbot incident) and against the console's own
   last-written state (out-of-band edits of fields we wrote).

3. **The managed flag itself.** `set_managed()` flips `site.managed` by
   editing the YAML *inside the container* (the API deliberately never
   exposes this flag — see the product's config.managed_mode() docstring),
   ensuring OPERATOR_TOKEN exists in the instance's .env first, then
   recreating the app container so both changes take effect together.

Boundary rules: this module knows machines (SSH, docker) but NO prices —
plans/tariffs stay in ledger.py. State lives next to clients.json
(config_state.json), same volume, same trust class.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from backend import core
from backend import env_tool
from backend.config import DEFAULT_CONFIG_PATH, load_clients, save_clients

# ---------------------------------------------------------------------------
# Field catalog — mirrors the product's cfg-* tabs / SiteConfigPatch.
# `managed` tags must match backend/admin.py's MANAGED_CONFIG_FIELDS in the
# dental repo (the instance is authoritative; a mismatch here surfaces as a
# clear 403 from the instance, never a silent write). `path` is the field's
# dotted location in site_config.yaml, used by drift_check() — None means
# the value doesn't live in the YAML (Twilio creds live in .env).
# ---------------------------------------------------------------------------

def _f(name: str, label: str, ftype: str = "text", managed: bool = False,
       path: str | None = "", options: list[str] | None = None,
       hint: str = "", role: str | None = None,
       provider_field: str | None = None) -> dict[str, Any]:
    if path == "":  # default: site.<name> — the common case
        path = f"site.{name}"
    d = {"name": name, "label": label, "type": ftype, "managed": managed,
         "path": path}
    if options:
        d["options"] = options
    if hint:
        d["hint"] = hint
    if role:  # type:"model" fields — which model_catalog role to offer, and
        d["role"] = role  # which sibling field holds the provider to filter by
    if provider_field:
        d["provider_field"] = provider_field
    return d


FIELD_GROUPS: list[dict[str, Any]] = [
    {"key": "identity", "title": "Identity", "fields": [
        _f("site_name", "Site name", path="site.name"),
        _f("business_type", "Business type"),
        _f("language", "Language", "select", options=["es", "en"]),
        _f("brand_color", "Brand color (#rrggbb)"),
        _f("welcome_headline", "Welcome headline"),
        _f("welcome_paragraph", "Welcome paragraph", "textarea"),
        _f("welcome_button_text", "Welcome button text"),
        _f("welcome_emoji", "Welcome emoji"),
    ]},
    {"key": "booking", "title": "Booking & policies", "fields": [
        _f("booking_horizon_days", "Booking horizon (days)", "number"),
        _f("min_lead_minutes", "Min lead (minutes)", "number"),
        _f("max_upcoming_per_client", "Max upcoming per client", "number"),
        _f("emergency_number", "Emergency number"),
        _f("disclose_prices", "Disclose prices", "checkbox"),
        _f("privacy_policy_url", "Privacy policy URL"),
        _f("max_input_chars", "Max input chars", "number", managed=True),
        _f("scheduling_granularity_minutes", "Scheduling granularity (min)",
           "number", managed=True),
        _f("demo_mode", "Demo mode (no registration gate)", "checkbox",
           managed=True),
        _f("allowed_embed_domains", "Allowed embed domains (comma list)",
           managed=True),
    ]},
    {"key": "llm", "title": "LLM", "fields": [
        _f("llm_provider", "Provider", "select", managed=True,
           path="llm.provider",
           options=["mistral", "nvidia", "openrouter", "ollama"]),
        _f("llm_model", "Model", "model", managed=True, path="llm.model",
           role="llm", provider_field="llm_provider",
           hint="Fleet policy: mistral-small (2.25M tok/min, 5 req/s) by "
                "default; medium/large only as named exceptions — see the "
                "LLM source notes shown above the picker."),
        _f("llm_temperature", "Temperature", "number", managed=True,
           path="llm.temperature"),
    ]},
    {"key": "security", "title": "Security", "fields": [
        _f("security_max_attempts", "Max failed attempts", "number",
           managed=True, path="security.max_attempts"),
        _f("security_lockout_minutes", "Lockout (minutes)", "number",
           managed=True, path="security.lockout_minutes"),
    ]},
    {"key": "email", "title": "Email (SMTP)", "fields": [
        _f("email_enabled", "Notifications enabled", "checkbox",
           path="site.email_notifications.enabled"),
        _f("email_from_name", "From name", managed=True,
           path="site.email_notifications.from_name"),
        _f("email_from_email", "From email", managed=True,
           path="site.email_notifications.from_email"),
        _f("email_smtp_host", "SMTP host", managed=True,
           path="site.email_notifications.smtp_host"),
        _f("email_smtp_port", "SMTP port", "number", managed=True,
           path="site.email_notifications.smtp_port"),
        _f("email_smtp_use_tls", "Use TLS", "checkbox", managed=True,
           path="site.email_notifications.smtp_use_tls"),
        _f("email_smtp_username", "SMTP username", managed=True,
           path="site.email_notifications.smtp_username"),
        _f("email_smtp_password", "SMTP password", "password", managed=True,
           path="site.email_notifications.smtp_password"),
        _f("email_override_recipient", "Override recipient (testing)",
           managed=True, path="site.email_notifications.override_recipient"),
    ]},
    {"key": "sms", "title": "SMS (Twilio)", "fields": [
        _f("sms_enabled", "Notifications enabled", "checkbox",
           path="site.sms_notifications.enabled"),
        _f("sms_provider", "Provider", managed=True, path=None),
        _f("sms_twilio_account_sid", "Twilio account SID", managed=True,
           path=None),
        _f("sms_twilio_auth_token", "Twilio auth token", "password",
           managed=True, path=None),
        _f("sms_twilio_from_number", "From number", managed=True,
           path="site.sms_notifications.from_number"),
        _f("sms_override_recipient", "Override recipient (testing)",
           managed=True, path="site.sms_notifications.override_recipient"),
    ]},
    {"key": "voice", "title": "Voice", "fields": [
        _f("voice_greeting_es", "Greeting (ES)", "textarea",
           path="voice.greeting_es"),
        _f("voice_greeting_en", "Greeting (EN)", "textarea",
           path="voice.greeting_en"),
        _f("voice_enabled", "Voice enabled", "checkbox", managed=True,
           path="voice.enabled"),
        _f("voice_max_session_minutes", "Max session (min)", "number",
           managed=True, path="voice.max_session_minutes"),
        _f("voice_stt_provider", "STT provider", managed=True,
           path="voice.stt.provider"),
        _f("voice_stt_model", "STT model", "model", managed=True,
           path="voice.stt.model", role="stt",
           provider_field="voice_stt_provider"),
        _f("voice_tts_provider", "TTS provider", managed=True,
           path="voice.tts.provider"),
        _f("voice_tts_voice_es", "TTS voice (ES)", managed=True,
           path="voice.tts.voice_es"),
        _f("voice_tts_voice_en", "TTS voice (EN)", managed=True,
           path="voice.tts.voice_en"),
        _f("voice_llm_provider", "Voice LLM provider", managed=True,
           path="voice.llm.provider"),
        _f("voice_llm_model", "Voice LLM model", "model", managed=True,
           path="voice.llm.model", role="llm",
           provider_field="voice_llm_provider"),
    ]},
    {"key": "plan", "title": "Plan (pushed by Tokens tab)", "fields": [
        _f("plan_included_tokens", "Included tokens (0 clears the gauge)",
           "number", managed=True, path="plan.included_tokens",
           hint="Normally written by Tokens → Push plan; edit here only to "
                "correct a bad push."),
        _f("plan_anchor_day", "Billing anchor day", "number", managed=True,
           path="plan.anchor_day"),
        _f("plan_overage_note", "Overage note", managed=True,
           path="plan.overage_note"),
    ]},
]

FIELD_BY_NAME: dict[str, dict[str, Any]] = {
    f["name"]: f for g in FIELD_GROUPS for f in g["fields"]
}


# ---------------------------------------------------------------------------
# Console-side "last written" state (config_state.json, next to clients.json)
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    return Path(DEFAULT_CONFIG_PATH).parent / "config_state.json"


def _load_state() -> dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def record_written(client_name: str, fields: dict[str, Any]) -> None:
    """Merge the just-written fields into this client's last-written record —
    drift_check() compares these against the live file later."""
    state = _load_state()
    rec = state.setdefault(client_name, {"fields": {}, "written_at": {}})
    now = datetime.now().isoformat(timespec="seconds")
    for k, v in fields.items():
        rec["fields"][k] = v
        rec.setdefault("written_at", {})[k] = now
    rec["updated"] = now
    _save_state(state)


def get_written(client_name: str) -> dict[str, Any]:
    return _load_state().get(client_name, {"fields": {}, "written_at": {}})


# ---------------------------------------------------------------------------
# Read: API first, SSH file as fallback
# ---------------------------------------------------------------------------

def get_live_config(client: dict[str, Any]) -> dict[str, Any]:
    """The instance's current config as the flat /admin/config shape.
    API-first; when the API is unreachable, falls back to the SSH file read
    (source "ssh", read-only — the raw nested YAML, NOT the flat shape, so
    the UI shows it as a document rather than editable fields)."""
    try:
        data = core._admin_get_json(client, "/admin/config", {})
        return {"ok": True, "error": None, "source": "api", "config": data,
                "managed": bool(data.get("managed"))}
    except Exception as api_err:
        ssh = read_live_yaml(client)
        if ssh["ok"]:
            parsed = ssh.get("parsed") or {}
            return {"ok": True, "error": f"admin API unreachable ({api_err}); "
                                         "showing the on-disk file read-only",
                    "source": "ssh", "config": None,
                    "raw_yaml": ssh["raw"],
                    "managed": bool((parsed.get("site") or {}).get("managed"))}
        return {"ok": False, "source": None, "config": None,
                "error": f"admin API failed ({api_err}); SSH read failed "
                         f"({ssh['error']})"}


def read_live_yaml(client: dict[str, Any]) -> dict[str, Any]:
    """cat the LIVE /data/site_config.yaml out of the running app container
    (same command the config validator uses). Read-only."""
    remote_dir = (client.get("remote_dir") or "").rstrip("/")
    ssh_target = client.get("ssh_target") or ""
    if not ssh_target or not remote_dir:
        return {"ok": False, "error": "no ssh_target/remote_dir configured",
                "raw": None, "parsed": None}
    shell = core._shell_remote_dir(remote_dir)
    proj = core._project_name(remote_dir)
    ok, out = core.run_ssh(
        ssh_target,
        f"cd {shell} && docker compose -p {proj} exec -T app cat /data/site_config.yaml",
        timeout=40)
    if not ok:
        return {"ok": False, "error": out.strip()[-300:] or "ssh failed",
                "raw": None, "parsed": None}
    try:
        parsed = yaml.safe_load(out) or {}
    except yaml.YAMLError as e:
        return {"ok": False, "error": f"live file is not valid YAML: {e}",
                "raw": out, "parsed": None}
    return {"ok": True, "error": None, "raw": out, "parsed": parsed}


def read_shipped_yaml(client: dict[str, Any]) -> dict[str, Any]:
    """The checkout's shipped backend/site_config.yaml — the defaults of the
    code version this instance actually runs (not our local copy)."""
    remote_dir = (client.get("remote_dir") or "").rstrip("/")
    ssh_target = client.get("ssh_target") or ""
    if not ssh_target or not remote_dir:
        return {"ok": False, "error": "no ssh_target/remote_dir configured",
                "parsed": None}
    shell = core._shell_remote_dir(remote_dir)
    ok, out = core.run_ssh(ssh_target,
                           f"cat {shell}/backend/site_config.yaml", timeout=30)
    if not ok:
        return {"ok": False, "error": out.strip()[-300:] or "ssh failed",
                "parsed": None}
    try:
        return {"ok": True, "error": None, "parsed": yaml.safe_load(out) or {}}
    except yaml.YAMLError as e:
        return {"ok": False, "error": f"shipped file is not valid YAML: {e}",
                "parsed": None}


# ---------------------------------------------------------------------------
# Write: through the instance's validated endpoint
# ---------------------------------------------------------------------------

def write_config(client: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """PUT the given flat fields to the instance's /admin/config (the
    operator token rides along via core._admin_put_json), verify by echo,
    and record them as last-written for drift detection."""
    unknown = sorted(k for k in fields if k not in FIELD_BY_NAME)
    if unknown:
        return {"ok": False, "error": f"unknown config fields: {', '.join(unknown)}"}
    if not fields:
        return {"ok": False, "error": "nothing to write"}
    try:
        core._admin_put_json(client, "/admin/config", fields)
    except Exception as e:
        # Surface the instance's structured 403 (managed_fields) or 422
        # (validation) body when we can get at it — "403" alone is useless.
        detail = None
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = (resp.text or "")[:300]
        return {"ok": False, "error": f"write failed: {detail or e}"}
    # Echo-verify: an un-upgraded instance silently ignores unknown fields
    # (same failure mode ledger_push_plan guards against).
    mismatches = []
    try:
        echo = core._admin_get_json(client, "/admin/config", {})
        for k, v in fields.items():
            got = echo.get(k)
            if not _echo_matches(k, v, got):
                mismatches.append({"field": k, "sent": v, "echoed": got})
    except Exception as e:
        mismatches.append({"field": "(echo read failed)", "sent": None,
                           "echoed": str(e)})
    record_written(client.get("name", ""), fields)
    return {"ok": True, "error": None, "written": sorted(fields),
            "mismatches": mismatches}


def _echo_matches(name: str, sent: Any, got: Any) -> bool:
    """Loose comparison — the instance normalizes values (strips strings,
    joins embed-domain lists with ', ', lowercases brand colors...)."""
    if sent is None:
        return True
    if isinstance(sent, bool) or isinstance(got, bool):
        return bool(sent) == bool(got)
    if isinstance(sent, (int, float)) and isinstance(got, (int, float)):
        return abs(float(sent) - float(got)) < 1e-9
    if name == "allowed_embed_domains":
        norm = lambda s: sorted(p.strip() for p in str(s or "").replace(",", " ").split() if p.strip())  # noqa: E731
        return norm(sent) == norm(got)
    if name == "plan_included_tokens" and int(sent or 0) == 0:
        return not got  # 0 clears the plan block; echo reads back 0/absent
    return str(sent).strip().lower() == str(got or "").strip().lower()


# ---------------------------------------------------------------------------
# Drift detection (closes dental deploy/DEPLOYMENT.md §10)
# ---------------------------------------------------------------------------

_DRIFT_IGNORE_TOP = {"consultants", "services"}  # per-clinic data, not defaults


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    """Nested dict -> {dotted.path: leaf}. Lists are treated as leaves."""
    out: dict[str, Any] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten(v, p))
            else:
                out[p] = v
    return out


def drift_check(client: dict[str, Any]) -> dict[str, Any]:
    """Three-way diff: live volume file vs shipped defaults vs the console's
    last-written state.

    - missing_defaults: dotted keys the shipped site_config.yaml has that the
      live /data copy lacks — the classic stale-config gap (a new default
      shipped in a code update never lands in the volume copy).
    - out_of_band: fields this console previously wrote whose live YAML value
      no longer matches — someone (clinic panel, SSH vim, another tool)
      changed them behind our back.
    Shipped-vs-live VALUE differences are deliberately NOT reported: nearly
    every value legitimately differs per clinic (name, colors, hours) — only
    structurally missing keys signal drift."""
    live = read_live_yaml(client)
    if not live["ok"]:
        return {"ok": False, "error": f"live read failed: {live['error']}"}
    shipped = read_shipped_yaml(client)
    if not shipped["ok"]:
        return {"ok": False, "error": f"shipped read failed: {shipped['error']}"}

    live_flat = _flatten(live["parsed"])
    shipped_flat = _flatten(shipped["parsed"])
    missing = sorted(
        k for k in shipped_flat
        if k not in live_flat and k.split(".", 1)[0] not in _DRIFT_IGNORE_TOP)

    written = get_written(client.get("name", ""))
    out_of_band = []
    for fname, sent in (written.get("fields") or {}).items():
        meta = FIELD_BY_NAME.get(fname)
        if not meta or not meta.get("path"):
            continue  # not YAML-backed (e.g. Twilio .env creds)
        live_val = live_flat.get(meta["path"])
        if not _echo_matches(fname, sent, live_val):
            out_of_band.append({
                "field": fname, "path": meta["path"],
                "console_wrote": sent, "live_value": live_val,
                "written_at": (written.get("written_at") or {}).get(fname)})

    return {"ok": True, "error": None,
            "missing_defaults": missing,
            "out_of_band": out_of_band,
            "live_managed": bool((live["parsed"].get("site") or {}).get("managed")),
            "checked": datetime.now().isoformat(timespec="seconds")}


# ---------------------------------------------------------------------------
# The managed flag (SSH-only by design) + operator token provisioning
# ---------------------------------------------------------------------------

# Python one-liner executed INSIDE the app container (its own user owns the
# file, so no host-ownership 500s — the wizard's docker-cp lesson). Double
# quotes only, so shlex.quote can wrap it in single quotes for the shell.
_SET_MANAGED_PY = (
    'import yaml; p="/data/site_config.yaml"; '
    "d=yaml.safe_load(open(p)) or {}; "
    'd.setdefault("site", {})["managed"]={value}; '
    'f=open(p, "w"); '
    "yaml.safe_dump(d, f, default_flow_style=False, allow_unicode=True); "
    "f.close(); "
    'print("===MANAGED_SET_OK===")'
)


def set_managed(client: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Enable/disable managed mode on an instance, end to end:
    1. (enable only) ensure OPERATOR_TOKEN exists in the instance's .env and
       matches the console's record for this client (generated on first use,
       stored in clients.json — same trust class as admin_token).
    2. flip site.managed in the LIVE /data/site_config.yaml, inside the
       container (the admin API deliberately can't — see product Phase 6).
    3. recreate the app container (env is fixed at create time; this also
       reloads the config), then verify via GET /admin/config.
    Every step is reported; a failed step stops the sequence."""
    import shlex
    name = client.get("name", "")
    ssh_target = client.get("ssh_target") or ""
    remote_dir = (client.get("remote_dir") or "").rstrip("/")
    steps: list[dict[str, Any]] = []
    if not ssh_target or not remote_dir:
        return {"ok": False, "error": "no ssh_target/remote_dir configured",
                "steps": steps}

    # -- step 1: operator token (enable only) --------------------------------
    if enabled:
        token = client.get("operator_token") or ""
        if not token:
            token = secrets.token_urlsafe(24)
        env_read = env_tool.read_remote_env(ssh_target, remote_dir)
        if not env_read["ok"]:
            return {"ok": False, "steps": steps,
                    "error": f".env read failed: {env_read['error']}"}
        env = env_read["env"]
        if env.get("OPERATOR_TOKEN") and not client.get("operator_token"):
            # .env already has one (e.g. re-enable) — adopt it instead of
            # rotating, so an already-distributed token keeps working.
            token = env["OPERATOR_TOKEN"]
        if env.get("OPERATOR_TOKEN") != token:
            env["OPERATOR_TOKEN"] = token
            written = env_tool.write_remote_env(ssh_target, remote_dir, env)
            if not written["ok"]:
                return {"ok": False, "steps": steps,
                        "error": f".env write failed: {written['error']}"}
            steps.append({"step": "operator_token_env", "ok": True,
                          "detail": "OPERATOR_TOKEN written to .env"})
        else:
            steps.append({"step": "operator_token_env", "ok": True,
                          "detail": "OPERATOR_TOKEN already present"})
        if client.get("operator_token") != token:
            clients = load_clients()
            for c in clients:
                if c.get("name") == name:
                    c["operator_token"] = token
            save_clients(clients)
            client["operator_token"] = token
            steps.append({"step": "operator_token_saved", "ok": True,
                          "detail": "recorded in clients.json"})

    # -- step 2: flip site.managed in the live volume file -------------------
    shell = core._shell_remote_dir(remote_dir)
    proj = core._project_name(remote_dir)
    # .replace, NOT .format: the snippet contains literal {} braces (yaml
    # `or {}`, setdefault("site", {})) that .format() would misread as
    # positional fields (IndexError). Only the {value} token is substituted.
    py = _SET_MANAGED_PY.replace("{value}", "True" if enabled else "False")
    cmd = (f"cd {shell} && docker compose -p {proj} exec -T app "
           f"python -c {shlex.quote(py)}")
    ok, out = core.run_ssh(ssh_target, cmd, timeout=40)
    if not ok or "===MANAGED_SET_OK===" not in out:
        return {"ok": False, "steps": steps,
                "error": f"couldn't set site.managed: {out.strip()[-300:]}"}
    steps.append({"step": "yaml_flag", "ok": True,
                  "detail": f"site.managed = {enabled}"})

    # -- step 3: recreate + verify -------------------------------------------
    rec = core.recreate_app(ssh_target, remote_dir)
    steps.append({"step": "recreate", "ok": rec["ok"],
                  "detail": rec.get("error") or "app container recreated"})
    if not rec["ok"]:
        return {"ok": False, "steps": steps,
                "error": f"recreate failed: {rec.get('error')}"}
    verified, verify_detail = False, ""
    for _ in range(5):
        time.sleep(4)
        try:
            echo = core._admin_get_json(client, "/admin/config", {})
            verified = bool(echo.get("managed")) == enabled
            verify_detail = f"instance reports managed={echo.get('managed')}"
            if verified:
                break
        except Exception as e:
            verify_detail = f"config echo not up yet: {e}"
    steps.append({"step": "verify", "ok": verified, "detail": verify_detail})
    return {"ok": verified, "steps": steps,
            "error": None if verified else f"verification failed: {verify_detail}"}


# ---------------------------------------------------------------------------
# LLM source context for the model picker (vault join)
# ---------------------------------------------------------------------------

def llm_source_info(client_name: str) -> dict[str, Any] | None:
    """The vault set currently assigned to this client's llm role — name,
    provider, tier and (crucially) the NOTES, which carry the rate-limit
    facts the model picker displays (D-notes: facts live on the source)."""
    from backend import vault as vault_mod
    assignment = next(
        (a for a in vault_mod.list_assignments()
         if a.get("client") == client_name and a.get("role") == "llm"), None)
    if not assignment:
        return None
    s = next((x for x in vault_mod.list_sets(redact=True)
              if x["id"] == assignment.get("set_id")), None)
    if not s:
        return {"set_name": assignment.get("set_name"), "provider": None,
                "tier": None, "notes": "(set deleted)"}
    return {"set_name": s.get("name"), "provider": s.get("provider"),
            "tier": s.get("tier"), "owner": s.get("owner"),
            "notes": s.get("notes") or "", "applied": assignment.get("applied")}
