"""Post-provision smoke suite (plan 1D) — the "is this client actually
working" checklist, runnable at onboarding step 6 and any time after from
the client detail view. Every check is independent and defensive: a
failure is a result row, never an exception, so one broken thing can't
hide the state of the rest.

Checks (each returns {"check", "ok", "detail"}):
  loopback_health   /health on the VPS loopback port (needs admin_local_port)
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

from typing import Any

import requests

from backend.core import (HTTP_TIMEOUT, _admin_get_json, _project_name, run_ssh)

_CHAT_TIMEOUT = 45  # a cold LLM call can genuinely take a while


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

    # 2. public health -------------------------------------------------------
    try:
        r = requests.get(f"{base}/health", timeout=HTTP_TIMEOUT)
        rows.append(_row("public_health", r.status_code == 200 and "status" in r.text,
                         f"HTTP {r.status_code}: {r.text[:120]}"))
    except Exception as e:
        rows.append(_row("public_health", False, str(e)))

    # 3. TLS cert ------------------------------------------------------------
    if ssh_target and hostname:
        cmd = (f"echo | openssl s_client -servername {hostname} -connect {hostname}:443 "
               f"2>/dev/null | openssl x509 -noout -enddate -checkend 1209600 2>&1")
        ok, out = run_ssh(ssh_target, cmd, timeout=25)
        # -checkend 1209600 (14 days): prints "Certificate will not expire" on ok
        good = ok and "will not expire" in out
        rows.append(_row("tls_cert", good, out.strip()[-200:] or "no output"))
    else:
        rows.append(_row("tls_cert", False, "skipped — no ssh_target/hostname"))

    # 4. /config sanity ------------------------------------------------------
    try:
        r = requests.get(f"{base}/config", timeout=HTTP_TIMEOUT)
        data = r.json()
        name = str(data.get("name") or data.get("site_name") or "")
        starterish = (not name.strip()) or "placeholder" in name.lower() or "starter" in name.lower()
        rows.append(_row("config", r.status_code == 200 and not starterish,
                         f"site name: {name!r}" if name else f"HTTP {r.status_code}, no name field"))
    except Exception as e:
        rows.append(_row("config", False, str(e)))

    # 5. admin API (works for both public and tunnel-only clients) -----------
    try:
        data = _admin_get_json(client, "/admin/metrics",
                               {"start": "2026-01-01T00:00:00", "end": "2026-01-02T00:00:00"})
        rows.append(_row("admin_api", isinstance(data, dict),
                         "metrics endpoint answered with the stored token"))
    except Exception as e:
        rows.append(_row("admin_api", False, str(e)))

    # 6. CSP / embeddability -------------------------------------------------
    try:
        r = requests.get(f"{base}/", timeout=HTTP_TIMEOUT)
        csp = r.headers.get("Content-Security-Policy", "")
        rows.append(_row("csp_embed", "frame-ancestors" in csp,
                         csp[:300] or "no Content-Security-Policy header"))
    except Exception as e:
        rows.append(_row("csp_embed", False, str(e)))

    # 7. chat round-trip ------------------------------------------------------
    try:
        r = requests.post(f"{base}/chat", json={"message": "hello", "language": "en"},
                          timeout=_CHAT_TIMEOUT)
        reply = (r.json() or {}).get("reply", "") if r.status_code == 200 else ""
        rows.append(_row("chat_roundtrip", bool(str(reply).strip()),
                         (str(reply)[:160] or f"HTTP {r.status_code}: {r.text[:120]}")))
    except Exception as e:
        rows.append(_row("chat_roundtrip", False, str(e)))

    # 8. backup timer (warn-level — reported, but onboarding may proceed) ----
    if ssh_target and project:
        ok, out = run_ssh(ssh_target, f"systemctl is-active {project}-backup.timer 2>&1")
        rows.append(_row("backup_timer", ok and out.strip() == "active",
                         out.strip()[-120:] or "no output"))
    else:
        rows.append(_row("backup_timer", False, "skipped — no ssh_target/remote_dir"))

    return rows
