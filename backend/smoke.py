"""Post-provision smoke suite (plan 1D) — the "is this client actually
working" checklist, runnable at onboarding step 6 and any time after from
the client detail view. Every check is independent and defensive: a
failure is a result row, never an exception, so one broken thing can't
hide the state of the rest.

Checks (each returns {"check", "ok", "detail"}):
  loopback_health   /health on the VPS loopback port (needs admin_local_port)
  env_file          the instance's .env is structurally sound + secure:
                    every line KEY=value, no space-after-comma in multi-key
                    values (broke real backups, 2026-07-20), ADMIN_PASSWORD
                    not the default, BACKUP_PASSPHRASE present. ENV != prod
                    is reported as a note, not a failure — it's a deliberate
                    tradeoff on voice-enabled non-medical instances (see
                    new_client.py's starter-config docstring), BUT it also
                    disables the product's default-admin-password boot
                    guard, which is exactly why the password check here is
                    unconditional. Secret VALUES never appear in the detail
                    text — only key names and line numbers.
  public_health     /health via the public hostname
  tls_cert          certificate matches hostname and isn't near expiry
  config            /config parses; site name present and not starter text
  admin_api         /admin/metrics reachable with the stored token (over
                    SSH for tunnel-only clients — core._admin_get_json)
  csp_embed         CSP frame-ancestors present (embed will work)
  chat_roundtrip    POST /chat gets a real non-empty reply (exercises the
                    LLM key end to end — the single best "it's alive")
  backup_timer      systemd <name>-backup.timer active (warn-level)
"""
from __future__ import annotations

import re
from typing import Any

import requests

from backend.core import (HTTP_TIMEOUT, _admin_get_json, _project_name,
                          _shell_remote_dir, run_ssh)

_CHAT_TIMEOUT = 45  # a cold LLM call can genuinely take a while

# Vars that may hold several comma-separated values (round-robin keys). A
# space after the comma in these is what broke the primary's backups on
# 2026-07-20 (anything sourcing .env as shell executes the second value as
# a command) — backup.sh is hardened now, but the format stays banned so
# nothing else ever trips on it.
_MULTI_KEY_VARS = {"NVIDIA_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY",
                   "OLLAMALOCAL_API_KEY"}
_ENV_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def analyze_env_text(text: str) -> tuple[list[str], list[str]]:
    """Pure, testable core of the env_file check. Returns (problems, notes):
    problems fail the check, notes don't. Neither ever contains a secret
    VALUE — only key names, line numbers, and known-default flags."""
    problems: list[str] = []
    notes: list[str] = []
    has_passphrase = False
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_KV_RE.match(line)
        if not m:
            problems.append(f"line {i}: malformed (not KEY=value — a wrapped value or stray text)")
            continue
        key, value = m.group(1), m.group(2).strip()
        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
        if key in _MULTI_KEY_VARS and re.search(r",\s", value):
            problems.append(f"{key}: space after comma in multi-key value — remove it "
                            "(keys stay comma-separated, rotation is unaffected)")
        elif (key != "DOMAIN" and not quoted and re.search(r"\s", value)):
            # The gap that let Acme's unquoted Gmail app password through on
            # this check's first day (its backups then failed on exactly that
            # line): ANY unquoted whitespace makes .env unsafe to read as
            # shell. DOMAIN is exempt — its "a, b" form is Caddy address-list
            # syntax, consumed only by compose/Caddy.
            problems.append(f"{key}: unquoted space in value — wrap it in double quotes")
        if key == "ADMIN_PASSWORD" and value.strip("'\"") == "admin":
            problems.append("ADMIN_PASSWORD is the default 'admin' — set a real one "
                            "(ENV=dev disables the boot guard that would refuse this)")
        if key == "BACKUP_PASSPHRASE" and value:
            has_passphrase = True
        if key == "ENV" and value != "prod":
            notes.append(f"ENV={value or '(empty)'} — deliberate on voice-enabled "
                         "non-medical instances, but it also disables the "
                         "default-admin-password boot guard")
    if not has_passphrase:
        problems.append("no BACKUP_PASSPHRASE — nightly backups cannot run")
    return problems, notes


def _row(check: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": check, "ok": bool(ok), "detail": (detail or "")[:400]}


def run_smoke(client: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base = (client.get("base_url") or "").rstrip("/")
    hostname = base.split("://", 1)[-1].split("/", 1)[0] if base else ""
    ssh_target = client.get("ssh_target") or ""
    port = client.get("admin_local_port")
    remote_dir = client.get("remote_dir") or ""
    project = _project_name(remote_dir) if remote_dir else ""

    # 1. loopback health -----------------------------------------------------
    if ssh_target and port:
        ok, out = run_ssh(ssh_target, f"curl -fsS -m 8 http://127.0.0.1:{int(port)}/health")
        rows.append(_row("loopback_health", ok and '"status"' in out,
                         out.strip()[-200:] if out else "no output"))
    else:
        rows.append(_row("loopback_health", False,
                         "skipped — no ssh_target/admin_local_port configured"))

    # 2. .env sanity ---------------------------------------------------------
    if ssh_target and remote_dir:
        shell_dir = _shell_remote_dir(remote_dir.rstrip("/"))
        okc, out = run_ssh(ssh_target, f"cat {shell_dir}/.env 2>/dev/null")
        if not okc or not out.strip():
            rows.append(_row("env_file", False, "couldn't read .env over SSH"))
        else:
            problems, env_notes = analyze_env_text(out)
            detail = "; ".join(problems + [f"note: {n}" for n in env_notes]) \
                     or "well-formed, no known hazards"
            rows.append(_row("env_file", not problems, detail))
    else:
        rows.append(_row("env_file", False, "skipped — no ssh_target/remote_dir"))

    # 3. public health -------------------------------------------------------
    try:
        r = requests.get(f"{base}/health", timeout=HTTP_TIMEOUT)
        rows.append(_row("public_health", r.status_code == 200 and "status" in r.text,
                         f"HTTP {r.status_code}: {r.text[:120]}"))
    except Exception as e:
        rows.append(_row("public_health", False, str(e)))

    # 4. TLS cert ------------------------------------------------------------
    if ssh_target and hostname:
        cmd = (f"echo | openssl s_client -servername {hostname} -connect {hostname}:443 "
               f"2>/dev/null | openssl x509 -noout -enddate -checkend 1209600 2>&1")
        ok, out = run_ssh(ssh_target, cmd, timeout=25)
        # -checkend 1209600 (14 days): prints "Certificate will not expire" on ok
        good = ok and "will not expire" in out
        rows.append(_row("tls_cert", good, out.strip()[-200:] or "no output"))
    else:
        rows.append(_row("tls_cert", False, "skipped — no ssh_target/hostname"))

    # 5. /config sanity ------------------------------------------------------
    try:
        r = requests.get(f"{base}/config", timeout=HTTP_TIMEOUT)
        data = r.json()
        name = str(data.get("name") or data.get("site_name") or "")
        starterish = (not name.strip()) or "placeholder" in name.lower() or "starter" in name.lower()
        rows.append(_row("config", r.status_code == 200 and not starterish,
                         f"site name: {name!r}" if name else f"HTTP {r.status_code}, no name field"))
    except Exception as e:
        rows.append(_row("config", False, str(e)))

    # 6. admin API (works for both public and tunnel-only clients) -----------
    try:
        data = _admin_get_json(client, "/admin/metrics",
                               {"start": "2026-01-01T00:00:00", "end": "2026-01-02T00:00:00"})
        rows.append(_row("admin_api", isinstance(data, dict),
                         "metrics endpoint answered with the stored token"))
    except Exception as e:
        rows.append(_row("admin_api", False, str(e)))

    # 7. CSP / embeddability -------------------------------------------------
    try:
        r = requests.get(f"{base}/", timeout=HTTP_TIMEOUT)
        csp = r.headers.get("Content-Security-Policy", "")
        rows.append(_row("csp_embed", "frame-ancestors" in csp,
                         csp[:300] or "no Content-Security-Policy header"))
    except Exception as e:
        rows.append(_row("csp_embed", False, str(e)))

    # 8. chat round-trip ------------------------------------------------------
    try:
        r = requests.post(f"{base}/chat", json={"message": "hello", "language": "en"},
                          timeout=_CHAT_TIMEOUT)
        reply = (r.json() or {}).get("reply", "") if r.status_code == 200 else ""
        rows.append(_row("chat_roundtrip", bool(str(reply).strip()),
                         (str(reply)[:160] or f"HTTP {r.status_code}: {r.text[:120]}")))
    except Exception as e:
        rows.append(_row("chat_roundtrip", False, str(e)))

    # 9. backup timer (warn-level — reported, but onboarding may proceed) ----
    if ssh_target and project:
        ok, out = run_ssh(ssh_target, f"systemctl is-active {project}-backup.timer 2>&1")
        rows.append(_row("backup_timer", ok and out.strip() == "active",
                         out.strip()[-120:] or "no output"))
    else:
        rows.append(_row("backup_timer", False, "skipped — no ssh_target/remote_dir"))

    return rows
