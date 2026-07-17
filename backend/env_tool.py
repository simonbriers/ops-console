"""Read/write a client's .env over SSH, and run a real, lightweight API call
against whatever credential is in it.

This exists because copying keys from one deploy's .env into a new one meant
SSHing in twice and hand copy-pasting through vim — slow and easy to fat-
finger a secret. This reuses core.py's own `run_ssh` (the exact mechanism
already trusted for the version check) to read and write the file directly,
and adds one new thing core.py doesn't do: verifying a credential actually
works with a real, minimal, read-only call to whatever service it belongs
to, before you trust it in production.

Deliberately generic, the same way the rest of ops-console is: nothing here
is tied to one client's install. The key -> test-kind mapping lives in the
frontend (frontend/app.js's CRED_TEST_KIND) and just names PRODUCT env-var
conventions (dental-clinic-agent's own .env.example / env.clinica-valor),
not anything client-specific.
"""
from __future__ import annotations

import base64
import re
import smtplib
from typing import Any

import requests

from backend.core import HTTP_TIMEOUT, _shell_remote_dir, run_ssh

_ENV_MISSING_MARKER = "===OPSCONSOLE_ENV_MISSING==="
_ENV_WRITE_OK_MARKER = "===OPSCONSOLE_ENV_WRITE_OK==="

_ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_env(text: str) -> dict[str, str]:
    """Parses simple KEY=VALUE .env content — same format dental-clinic-
    agent's own .env.example uses: one assignment per line, '#' comments and
    blank lines ignored, no multi-line values, optional surrounding quotes
    stripped. Lines that don't match KEY=VALUE at all (stray text) are
    silently skipped rather than raising — a hand-edited .env can have
    trailing junk and this should still read whatever it can."""
    env: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


def read_remote_env(ssh_target: str, remote_dir: str) -> dict[str, Any]:
    """cat's {remote_dir}/.env over SSH. A missing file is reported as
    exists=False with an empty env — not an "ok": False error — since a
    brand-new client checkout legitimately has no .env yet; that's the
    normal starting point for this tool's "load destination" step, not a
    failure."""
    if not ssh_target or not remote_dir:
        return {"ok": False, "error": "no ssh_target/remote_dir configured", "env": {}, "exists": False}
    shell_dir = _shell_remote_dir(remote_dir.rstrip("/"))
    cmd = f"if [ -f {shell_dir}/.env ]; then cat {shell_dir}/.env; else echo '{_ENV_MISSING_MARKER}'; fi"
    ok, output = run_ssh(ssh_target, cmd)
    if not ok:
        return {"ok": False, "error": output.strip()[-500:] or "ssh failed", "env": {}, "exists": False}
    if output.strip() == _ENV_MISSING_MARKER:
        return {"ok": True, "error": None, "env": {}, "exists": False}
    return {"ok": True, "error": None, "env": parse_env(output), "exists": True}


def write_remote_env(ssh_target: str, remote_dir: str, env: dict[str, str]) -> dict[str, Any]:
    """Writes the given key/value pairs as {remote_dir}/.env, replacing
    whatever was there wholesale — the UI always seeds its table from the
    union of the source .env, the destination's own existing .env (if
    "Load existing .env" was used), and the product's known env-var names,
    so a "replace" here means "this is the complete file", the same mental
    model as saving a file in an editor rather than a partial patch.

    Backs up any existing .env first (.env.bak-<timestamp>, via the remote
    shell's own `date`) so a mistake is always recoverable directly on the
    VPS — automatic instead of a habit you have to remember before
    overwriting by hand in vim.

    Content goes over the wire as base64, not spliced into the command as
    raw text: a value containing a $, backtick, quote, or newline would
    otherwise need per-character shell escaping to survive the SSH-command
    round trip, and getting that wrong risks corrupting the file or, worse,
    executing part of a pasted secret as a shell command. Base64's alphabet
    (A-Za-z0-9+/=) contains none of bash's special characters, so the
    outer command needs no escaping logic at all."""
    if not ssh_target or not remote_dir:
        return {"ok": False, "error": "no ssh_target/remote_dir configured"}
    shell_dir = _shell_remote_dir(remote_dir.rstrip("/"))
    lines = [f"{key}={value}" for key, value in env.items()]
    content = "\n".join(lines) + ("\n" if lines else "")
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    cmd = (
        f"mkdir -p {shell_dir} && "
        f"([ -f {shell_dir}/.env ] && cp {shell_dir}/.env {shell_dir}/.env.bak-$(date +%Y%m%d%H%M%S) || true) && "
        f"printf '%s' '{b64}' | base64 -d > {shell_dir}/.env && "
        f"chmod 600 {shell_dir}/.env && echo '{_ENV_WRITE_OK_MARKER}'"
    )
    ok, output = run_ssh(ssh_target, cmd, timeout=30)
    if not ok or _ENV_WRITE_OK_MARKER not in output:
        return {"ok": False, "error": output.strip()[-500:] or "ssh failed"}
    return {"ok": True, "error": None}


# ---------------------------------------------------------------------------
# Credential tests — one real, minimal, read-only API call per kind. Every
# function is defensive: network/auth failures are captured in the returned
# dict rather than raised, matching every other check in core.py.
# ---------------------------------------------------------------------------

def test_mistral(key: str) -> dict[str, Any]:
    if not key:
        return {"ok": False, "message": "no key provided"}
    # MISTRAL_API_KEY may hold several comma-separated keys for round-robin
    # (same pattern as NVIDIA_API_KEY below) — test the first.
    key = key.split(",")[0].strip()
    try:
        resp = requests.get(
            "https://api.mistral.ai/v1/models",
            headers={"Authorization": f"Bearer {key}"}, timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            n = len(resp.json().get("data", []))
            return {"ok": True, "message": f"OK — {n} model(s) visible"}
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def test_openrouter(key: str) -> dict[str, Any]:
    if not key:
        return {"ok": False, "message": "no key provided"}
    try:
        resp = requests.get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {key}"}, timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            label = data.get("label") or "key"
            limit = data.get("limit")
            extra = f", limit {limit}" if limit is not None else ""
            return {"ok": True, "message": f"OK — {label}{extra}"}
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def test_nvidia(key: str) -> dict[str, Any]:
    if not key:
        return {"ok": False, "message": "no key provided"}
    # NVIDIA_API_KEY may hold several comma-separated keys (see
    # .env.example's red-team-harness round-robin note) — test the first.
    key = key.split(",")[0].strip()
    try:
        resp = requests.get(
            "https://integrate.api.nvidia.com/v1/models",
            headers={"Authorization": f"Bearer {key}"}, timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            n = len(resp.json().get("data", []))
            return {"ok": True, "message": f"OK — {n} model(s) visible"}
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def test_twilio(account_sid: str, auth_token: str) -> dict[str, Any]:
    if not account_sid or not auth_token:
        return {"ok": False, "message": "need both Account SID and Auth Token"}
    try:
        resp = requests.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}.json",
            auth=(account_sid, auth_token), timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            return {"ok": True, "message": f"OK — account status: {data.get('status', '?')}"}
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def test_smtp(host: str, port: str | int, username: str, password: str, use_tls: bool = True) -> dict[str, Any]:
    if not host or not username:
        return {"ok": False, "message": "need at least SMTP host and username"}
    server = None
    try:
        port_int = int(port) if str(port).strip() else 587
        server = smtplib.SMTP(host, port_int, timeout=HTTP_TIMEOUT)
        server.ehlo()
        if use_tls:
            server.starttls()
            server.ehlo()
        if password:
            server.login(username, password)
            return {"ok": True, "message": "OK — connected and authenticated"}
        return {"ok": True, "message": "OK — connected (no password set, login skipped)"}
    except Exception as e:
        return {"ok": False, "message": str(e)}
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def test_admin_token(base_url: str, token: str) -> dict[str, Any]:
    if not base_url or not token:
        return {"ok": False, "message": "need both the client's base URL and an admin token"}
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/admin/metrics",
            headers={"X-Admin-Token": token}, timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            return {"ok": True, "message": "OK — token accepted"}
        if resp.status_code in (401, 403):
            return {"ok": False, "message": f"HTTP {resp.status_code}: rejected — wrong token"}
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def run_credential_test(kind: str, values: dict[str, str]) -> dict[str, Any]:
    if kind == "mistral":
        return test_mistral(values.get("key", ""))
    if kind == "openrouter":
        return test_openrouter(values.get("key", ""))
    if kind == "nvidia":
        return test_nvidia(values.get("key", ""))
    if kind == "twilio":
        return test_twilio(values.get("account_sid", ""), values.get("auth_token", ""))
    if kind == "smtp":
        return test_smtp(
            values.get("host", ""), values.get("port", "587"),
            values.get("username", ""), values.get("password", ""),
            str(values.get("use_tls", "true")).strip().lower() != "false",
        )
    if kind == "admin_token":
        return test_admin_token(values.get("base_url", ""), values.get("token", ""))
    return {"ok": False, "message": f"unknown test kind {kind!r}"}
