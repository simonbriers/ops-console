"""A fake VPS for end-to-end testing the ops-console WITHOUT a real server.

Boots the real FastAPI app with backend.core.run_ssh / stream_ssh (and the
outbound HTTP used by the smoke suite / credential tests) replaced at the
module boundary by a stateful simulator that answers every command the
console sends the way the real VPS does — including the provisioning
marker protocol, teardown markers, DNS lookups, and .env round-trips.

Purpose: the acceptance test ("three fields -> Deploy -> all green -> tear
down, zero extra clicks") must be provable on demand, on any machine, with
no VPS and no SSH key. Run:

    python -m tests.fake_vps          # serves the console on :8199

then drive the UI (see tests/e2e_browser.mjs for the automated version).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

# --- isolated config store, seeded with one template client ---------------
TMP = tempfile.mkdtemp(prefix="opsconsole-e2e-")
os.environ["CLIENTS_CONFIG"] = os.path.join(TMP, "clients.json")
VPS_IP = "46.225.234.151"
TEMPLATE = "Primary Demo (fake VPS)"

with open(os.environ["CLIENTS_CONFIG"], "w", encoding="utf-8") as f:
    json.dump({
        "poll_interval_seconds": 3600,
        "clients": [{
            "name": TEMPLATE, "base_url": "https://chat.fake.test",
            "ssh_target": "deploy@fake.test", "remote_dir": "~/dental-clinic-agent",
            "monthly_token_quota": 0, "admin_token": "admin",
        }],
        "hosts": [{"name": "Fake VPS", "ssh_target": "deploy@fake.test",
                    "caddyfile_path": "~/dental-clinic-agent/deploy/Caddyfile",
                    "env_path": "~/dental-clinic-agent/.env"}],
    }, f)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import core, env_tool, new_client, smoke, vault  # noqa: E402

# --- the simulator ---------------------------------------------------------
STATE = {"dirs": set(), "seeded": set()}

TEMPLATE_ENV = ("MISTRAL_API_KEY=sk-faketest-key\nADMIN_PASSWORD=admin\n"
                "SMTP_HOST=mail.fake.test\nSMTP_PORT=587\nSMTP_USERNAME=u\n"
                "SMTP_PASSWORD=p\nSMTP_USE_TLS=True\n")


def fake_ssh(ssh_target, cmd, timeout=None):
    """Answer one SSH command the way the real VPS would."""
    c = cmd

    if "ifconfig.me" in c:
        return True, VPS_IP
    if c.startswith("getent ahostsv4"):
        return True, VPS_IP  # every *.my-ai-receptionist.com resolves (wildcard)
    if "docker-compose.override.yml" in c and "grep -rhoE" in c:
        return True, "127.0.0.1:8001:8000\n:8000 \n:8001 \n:8002 \n"
    if "git remote get-url origin" in c or "remote get-url" in c:
        return True, "git@github.com:simonbriers/dental-clinic-agent.git"
    if "echo EXISTS || echo MISSING" in c or "echo RESUMABLE || echo FOREIGN" in c or "echo MISSING; fi" in c:
        name = c.split("/")[-1].split(" ")[0].split("]")[0]
        for d in STATE["dirs"]:
            if d in c:
                return True, "RESUMABLE"
        return True, "MISSING"
    if "cat .env" in c or (".env ]; then cat" in c):
        return True, TEMPLATE_ENV
    if env_tool._ENV_WRITE_OK_MARKER in c:
        return True, env_tool._ENV_WRITE_OK_MARKER
    if "grep '^ADMIN_PASSWORD='" in c:
        return True, "generated-e2e-password"
    if "===RECREATE_OK===" in c:
        return True, " Container proof Recreated\n===RECREATE_OK==="
    if "openssl s_client" in c:
        return True, "notAfter=Oct 17 20:00:00 2026 GMT\nCertificate will not expire"
    if "systemctl is-active" in c:
        return True, "active"
    if "curl -fsS -m 8 http://127.0.0.1:" in c:
        return True, '{"status": "ok", "voice": {"enabled": false}}'
    if "cat /data/site_config.yaml" in c:
        from backend.new_client import _generate_starter_site_config
        return True, _generate_starter_site_config("Proof Clinic")
    if "DIRTY=$(git status" in c:
        return True, "DIRTY=0\nAHEAD=0"
    if "git status --porcelain deploy/Caddyfile" in c:
        return True, ""
    return True, ""


def fake_stream(ssh_target, cmd, holder, timeout=None):
    """Streaming variant: provisioning combined command, Caddy wiring,
    teardown — with the same marker protocol the real scripts use."""
    c = cmd
    lines = []
    if new_client._MARK_CLONE in c:
        # the combined clone/files/build/up/swap command
        deploy_dir = c.split("git clone ")[-1].split(" ")[1].rstrip(";") if "git clone" in c else ""
        lines = [
            new_client._MARK_CLONE, "Cloning into 'fake checkout'...",
            new_client._MARK_FILES, "(.env written)",
            new_client._MARK_BUILD, "#18 exporting to image", " Image dental-clinic-agent:latest Built ",
            new_client._MARK_UP, " Container proof-clinic Started ",
            new_client._MARK_SWAP, "Seeded (reset=True): {'consultants': 1, 'services': 1}",
            " Container proof-clinic Started ",
            new_client._MARK_STATUS, "clone=0 files=0 build=0 up=0 swap=0",
        ]
        STATE["dirs"].add(deploy_dir or "proof")
    elif "add_clinic_site.sh" in c:
        host = c.split("add_clinic_site.sh ")[-1].split(" ")[0]
        lines = [f"[backup] Caddyfile.bak", "Valid configuration",
                 "[restart] restarting caddy...", f"[OK] {host} is live."]
    elif "down -v" in c:
        lines = [" Container stopping ", " Volume removed ", "DIR_REMOVED"]
    elif "CADDY_REMOVED" in c or "CADDY_ABSENT" in c:
        lines = [" Container dental-agent-caddy Recreated ", "CADDY_REMOVED"]
    for line in lines:
        yield line
    holder["ok"] = True
    holder["output"] = "\n".join(lines)


class _FakeResp:
    def __init__(self, status=200, body=None, text_body=None, headers=None):
        self.status_code = status
        self._body = body
        self.text = text_body or (json.dumps(body) if body is not None else "")
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeRequests:
    """Outbound HTTP as seen by the smoke suite, aimed at the fake client."""

    @staticmethod
    def get(url, **kw):
        if url.endswith("/health"):
            return _FakeResp(200, {"status": "ok"})
        if url.endswith("/config"):
            return _FakeResp(200, {"name": "Proof Clinic"})
        return _FakeResp(200, {}, text_body="<html>chat</html>",
                         headers={"Content-Security-Policy": "frame-ancestors 'self' https://proof.example"})

    @staticmethod
    def post(url, **kw):
        if url.endswith("/chat"):
            return _FakeResp(200, {"reply": "Hello! I am the receptionist — how can I help?"})
        return _FakeResp(200, {})


def _ok_test(**kw):
    return {"ok": True, "error": None}


def install():
    for mod in (core, env_tool, new_client, vault):
        if hasattr(mod, "run_ssh"):
            mod.run_ssh = fake_ssh
        if hasattr(mod, "stream_ssh"):
            mod.stream_ssh = fake_stream
    smoke.run_ssh = fake_ssh
    smoke.requests = _FakeRequests()
    smoke._admin_get_json = lambda client, path, params: {"overall": {}}
    env_tool.test_mistral = lambda key: _ok_test()
    env_tool.test_smtp = lambda *a, **k: _ok_test()
    env_tool.test_twilio = lambda *a, **k: _ok_test()


if __name__ == "__main__":
    install()
    import uvicorn
    from backend.main import app
    print(f"fake-VPS console on http://127.0.0.1:8199  (config in {TMP})")
    uvicorn.run(app, host="127.0.0.1", port=8199, log_level="warning")
