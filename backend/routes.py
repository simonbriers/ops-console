"""API routes: client CRUD, live status, and the server-side SSH token
fetch (the browser itself can't SSH, so this proxies it — same command
`run_ssh` already runs for the version check, just aimed at .env instead
of git/docker)."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from datetime import datetime

from backend import config as cfg
from backend import core
from backend import deploy_log
from backend import env_tool
from backend import history
from backend import new_client as new_client_mod

router = APIRouter(prefix="/api")


class ClientIn(BaseModel):
    name: str
    base_url: str
    ssh_target: str = ""
    remote_dir: str = ""
    monthly_token_quota: int = 0
    admin_token: str = ""
    # Optional overrides for the "minutes saved" estimate — default
    # assumptions live in core.py (DEFAULT_MINUTES_PER_*); set any of
    # these per client if its real handling time differs.
    minutes_per_booking: int | None = None
    minutes_per_reschedule: int | None = None
    minutes_per_cancellation: int | None = None
    minutes_per_callback: int | None = None
    # Optional cost-estimate rates ($ per 1,000 tokens). Left at 0/unset,
    # the cost estimate is simply hidden rather than shown as $0.00 — this
    # tool has no way to know a clinic's real LLM pricing on its own.
    cost_per_1k_input_tokens: float | None = None
    cost_per_1k_cached_tokens: float | None = None
    cost_per_1k_output_tokens: float | None = None
    # Admin-API transport (see core._admin_get_json): when admin_via_ssh is
    # true, /admin/metrics and /admin/audit are fetched by curling the
    # instance's loopback port ON its VPS over SSH instead of public HTTPS —
    # required once that instance hides its admin surface behind
    # ADMIN_TUNNEL_ONLY, and keeps the admin token off the public internet.
    admin_via_ssh: bool = False
    admin_local_port: int | None = None


class SettingsIn(BaseModel):
    poll_interval_seconds: int


class FetchTokenIn(BaseModel):
    ssh_target: str
    remote_dir: str


class DeployIn(BaseModel):
    # Belt-and-suspenders: the frontend already gates the button behind
    # typing the exact client name, but the server checks it too rather
    # than trusting the browser alone for an action that mutates a live
    # instance — a stray/automated POST without a matching confirm_name
    # is rejected outright, never silently deployed.
    confirm_name: str


class EnvReadIn(BaseModel):
    ssh_target: str
    remote_dir: str


class EnvWriteIn(BaseModel):
    ssh_target: str
    remote_dir: str
    env: dict[str, str]
    # If true, recreates the app container right after a successful write
    # so the new values actually take effect (Compose only re-reads .env at
    # container creation, not on a bare file edit) — optional because a
    # brand-new instance provisioned by the New Client wizard is already
    # running on its placeholder .env, and a manual credential rotation on
    # a live client might deliberately want to review before restarting.
    restart_after: bool = False


class EnvTestIn(BaseModel):
    kind: str
    values: dict[str, str] = {}


class NewClientIn(BaseModel):
    deploy_name: str
    hostname: str
    display_name: str = ""
    # Name of an existing, already-configured client to clone the repo
    # from (same git origin, same VPS/ssh_target) — NOT copied into the new
    # client's .env; that stays the Credentials tool's job, on purpose.
    template_client_name: str


@router.get("/clients")
def list_clients() -> list[dict[str, Any]]:
    """Live-checks every configured client (health + version + usage),
    concurrently — see core.check_all. This is the one call the dashboard
    polls on its refresh timer. Every poll also appends a health/latency
    sample to ops-console's own local history log (see backend/history.py)
    and attaches each client's real uptime/latency stats computed from
    that log — this is the only place uptime samples are recorded, so a
    faster manual refresh via /clients/{name}/status doesn't double-count."""
    clients = cfg.load_clients()
    results = core.check_all(clients)
    history.append_checks(results)
    history.maybe_prune()
    for r in results:
        r["uptime"] = history.compute_uptime_stats(r["name"])
    return results


@router.get("/clients/{name}/status")
def get_client_status(name: str) -> dict[str, Any]:
    client = cfg.find_client(name)
    if client is None:
        raise HTTPException(status_code=404, detail=f"No client named {name!r}")
    result = core.check_client(client)
    result["uptime"] = history.compute_uptime_stats(name)
    return result


@router.post("/clients", status_code=201)
def add_client(body: ClientIn) -> dict[str, Any]:
    clients = cfg.load_clients()
    if any(c.get("name") == body.name for c in clients):
        raise HTTPException(status_code=409, detail=f"A client named {body.name!r} already exists")
    clients.append(body.model_dump())
    cfg.save_clients(clients)
    return body.model_dump()


@router.put("/clients/{name}")
def edit_client(name: str, body: ClientIn) -> dict[str, Any]:
    clients = cfg.load_clients()
    for i, c in enumerate(clients):
        if c.get("name") == name:
            clients[i] = body.model_dump()
            cfg.save_clients(clients)
            return body.model_dump()
    raise HTTPException(status_code=404, detail=f"No client named {name!r}")


@router.delete("/clients/{name}", status_code=204)
def remove_client(name: str) -> None:
    clients = cfg.load_clients()
    remaining = [c for c in clients if c.get("name") != name]
    if len(remaining) == len(clients):
        raise HTTPException(status_code=404, detail=f"No client named {name!r}")
    cfg.save_clients(remaining)


@router.post("/clients/{name}/deploy")
def deploy_client(name: str, body: DeployIn) -> dict[str, Any]:
    """The one mutating action in ops-console: pulls whatever's already on
    origin/master and rebuilds/restarts — scoped to exactly this client's
    own remote_dir/docker-compose.yml (see core.deploy_client's docstring
    for the full safety rationale). Requires confirm_name to exactly match
    the client name, checked server-side even though the browser UI
    already requires typing it — never trust confirmation state that lives
    only in the client. Every attempt, successful or not, is appended to
    the local deploy_log.jsonl audit trail."""
    client = cfg.find_client(name)
    if client is None:
        raise HTTPException(status_code=404, detail=f"No client named {name!r}")
    if body.confirm_name != name:
        raise HTTPException(status_code=400, detail="confirm_name did not match — deploy aborted, nothing was touched")

    result = core.deploy_client(client)
    deploy_log.append({
        "name": name,
        "requested_at": datetime.now().isoformat(timespec="seconds"),
        "ok": result.get("ok"),
        "stage": result.get("stage"),
        "commit": result.get("commit"),
        "error": result.get("error"),
    })
    return result


@router.get("/clients/{name}/deploy-log")
def get_deploy_log(name: str) -> list[dict[str, Any]]:
    return deploy_log.load_recent(name)


@router.post("/fetch-token")
def fetch_token(body: FetchTokenIn) -> dict[str, Any]:
    """Pulls ADMIN_PASSWORD from a client's .env over SSH, server-side —
    so a secret never has to be hand-typed/copy-pasted through the
    browser UI. Works before the client is ever saved (Add Client flow),
    since it takes ssh_target/remote_dir directly rather than a saved
    client name."""
    cmd = f"grep -m1 '^ADMIN_PASSWORD=' {body.remote_dir}/.env"
    ok, output = core.run_ssh(body.ssh_target, cmd)
    if not ok or "=" not in output:
        raise HTTPException(status_code=502, detail=f"SSH fetch failed: {output.strip()[-300:] or 'no output'}")
    value = output.strip().split("=", 1)[1].strip()
    return {"admin_token": value}


@router.post("/env/read")
def env_read(body: EnvReadIn) -> dict[str, Any]:
    """Reads {remote_dir}/.env over SSH for the "Copy credentials between
    deploys" tool — used for both the source (an existing, working deploy)
    and, optionally, the destination (to see what's already there before
    overwriting it). A missing file is NOT an error (see env_tool's
    docstring) — only a real SSH/connection failure raises here."""
    result = env_tool.read_remote_env(body.ssh_target, body.remote_dir)
    if not result["ok"]:
        raise HTTPException(status_code=502, detail=f"SSH read failed: {result['error']}")
    return result


@router.post("/env/write")
def env_write(body: EnvWriteIn) -> dict[str, Any]:
    """Writes the given key/value pairs as {remote_dir}/.env on the
    destination, backing up whatever was there first (see env_tool.
    write_remote_env's docstring for exactly how). This is a second
    deliberate exception to "almost everything here is read-only" — same
    trust boundary as /fetch-token and /clients/{name}/deploy: local-only,
    your own SSH key, no auth layer of its own yet.

    If restart_after is set, recreates the app container right after a
    successful write (core.restart_container) so the new values actually
    take effect — a write with no restart requested still succeeds/fails
    independently of the restart outcome, reported separately in the
    response so a restart failure never masks a successful write."""
    result = env_tool.write_remote_env(body.ssh_target, body.remote_dir, body.env)
    if not result["ok"]:
        raise HTTPException(status_code=502, detail=f"SSH write failed: {result['error']}")
    if body.restart_after:
        restart_result = core.restart_container(body.ssh_target, body.remote_dir)
        result["restarted"] = restart_result["ok"]
        result["restart_error"] = restart_result.get("error")
    return result


@router.post("/env/test")
def env_test(body: EnvTestIn) -> dict[str, Any]:
    """Runs one real, minimal, read-only API call to verify a credential
    actually works — see env_tool.run_credential_test for the per-kind
    implementations (Mistral/OpenRouter/NVIDIA models list, Twilio account
    fetch, SMTP connect+login, or the product's own /admin/metrics)."""
    return env_tool.run_credential_test(body.kind, body.values)


@router.post("/new-client")
def new_client(body: NewClientIn) -> dict[str, Any]:
    """Provisions a brand-new client end to end: clone the repo (from the
    template client's own git origin), generate its override file + a
    bootable placeholder .env + a starter site_config.yaml, first build/
    boot, swap the starter config into /data + reseed + restart, wire it
    into the shared Caddy (deploy/add_clinic_site.sh), and register it in
    clients.json — all over SSH, using whatever ssh_target the template
    client already has configured.

    Deliberately does NOT touch the new client's real secrets — the
    generated .env is bootable (a fresh admin password + backup
    passphrase, CORS pre-filled from the hostname) but has blank LLM/SMTP/
    Twilio keys on purpose; use the Credentials tool afterward (the new
    client is immediately selectable there once this returns ok) to copy
    real values in from an existing client and restart.

    Every stage gates the next (see new_client.create_new_client's
    docstring) — a failure partway through never silently proceeds, and
    the response's "stage" field says exactly where it stopped."""
    template = cfg.find_client(body.template_client_name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"No template client named {body.template_client_name!r}")
    ssh_target = template.get("ssh_target") or ""
    template_remote_dir = template.get("remote_dir") or ""
    if not ssh_target or not template_remote_dir:
        raise HTTPException(status_code=400, detail="template client has no ssh_target/remote_dir configured")

    display_name = body.display_name.strip() or body.deploy_name

    try:
        return _run_new_client(body, ssh_target, template_remote_dir, display_name)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: this is the
        # last line of defense before Starlette's own ServerErrorMiddleware
        # would take over and return a PLAIN-TEXT "Internal Server Error"
        # body (not JSON) — which is exactly what broke the frontend's
        # `await resp.json()` with "Unexpected token 'I', "Internal S"...".
        # Any unforeseen exception here (a malformed clients.json, a KeyError
        # on a provision dict field, whatever) now comes back as a normal,
        # diagnosable JSON error instead of a raw crash.
        return {
            "ok": False,
            "phase": "crash",
            "stage": "unhandled-exception",
            "error": f"unexpected error ({type(exc).__name__}): {exc}",
            "error_type": type(exc).__name__,
        }


def _run_new_client(
    body: NewClientIn, ssh_target: str, template_remote_dir: str, display_name: str
) -> dict[str, Any]:
    """The actual provisioning pipeline, split out from the route handler so
    the handler above can wrap the whole thing in one try/except (see its
    comment) without a giant indented block."""
    provision = new_client_mod.create_new_client(
        ssh_target, body.deploy_name, body.hostname, display_name, template_remote_dir,
    )
    if not provision["ok"]:
        return {**provision, "phase": "provision"}

    hosts = cfg.load_hosts()
    if not hosts:
        # NOTE: provision's own "ok"/"stage" MUST be spread first and
        # overridden after — a dict literal's later keys win, and
        # provision["ok"] is True/stage is "done" here (the instance really
        # did boot); putting the override keys first would get silently
        # clobbered back to "success" by the spread. Same reasoning applies
        # to every return below that mixes **provision with an override.
        return {**provision, "ok": False, "phase": "caddy", "stage": "no-host-configured",
                "error": "no host configured in Settings to find the shared Caddyfile on — "
                         f"the instance is up at 127.0.0.1:{provision['port']} on {ssh_target} but isn't "
                         "wired into Caddy yet. Add a host, then run deploy/add_clinic_site.sh by hand for this one."}
    # caddyfile_path looks like "~/dental-clinic-agent/deploy/Caddyfile" — the
    # primary checkout that owns the shared Caddy is everything before
    # "/deploy/Caddyfile".
    caddyfile_path = hosts[0].get("caddyfile_path", "")
    primary_remote_dir = caddyfile_path.rsplit("/deploy/Caddyfile", 1)[0] if caddyfile_path else ""
    if not primary_remote_dir:
        return {**provision, "ok": False, "phase": "caddy", "stage": "no-caddyfile-path",
                "error": "the configured host has no caddyfile_path to derive the primary checkout from — "
                         f"the instance is up at 127.0.0.1:{provision['port']} on {ssh_target} but isn't "
                         "wired into Caddy yet."}

    caddy = new_client_mod.wire_caddy(ssh_target, primary_remote_dir, body.hostname, provision["port"], body.deploy_name)
    if not caddy["ok"]:
        return {**provision, "ok": False, "phase": "caddy",
                "error": f"the instance is up at 127.0.0.1:{provision['port']} on {ssh_target}, but wiring it into "
                         f"Caddy failed: {caddy['error']}",
                "caddy_error": caddy["error"], "caddy_output": caddy["output"]}

    clients = cfg.load_clients()
    if any(c.get("name") == display_name for c in clients):
        # Extremely unlikely (deploy_name uniqueness was never checked
        # against client display names) but never silently skip
        # registration — the instance is live either way.
        return {**provision, "ok": True, "phase": "register", "stage": "name-collision", "caddy_output": caddy["output"],
                "error": f"a client named {display_name!r} already exists in clients.json — the instance is live, "
                         "add it under a different display name yourself in the Add Client form."}
    clients.append({
        "name": display_name,
        "base_url": f"https://{body.hostname}",
        "ssh_target": ssh_target,
        "remote_dir": provision["remote_dir"],
        "monthly_token_quota": 0,
        # Already known — the wizard generated this same value into the
        # instance's .env moments ago, no need to fetch it back over SSH.
        "admin_token": provision.get("admin_password") or "",
    })
    cfg.save_clients(clients)

    return {**provision, "ok": True, "phase": "done", "caddy_output": caddy["output"]}


def _run_new_client_stream(body: NewClientIn, ssh_target: str, template_remote_dir: str, display_name: str):
    """Streaming counterpart to _run_new_client — same branching/ordering
    rules as that function (see its comments, especially the dict-merge-
    ordering note), but drives new_client_mod's streaming generators and
    yields their progress events instead of just returning a final dict.

    Kept as a parallel implementation rather than merged with
    _run_new_client: the branching logic here is short and already covered
    by that function's regression tests, and a live console is additive —
    it doesn't replace the plain JSON contract POST /new-client's other
    callers (headless scripts, tests) still rely on."""
    provision = None
    for event in new_client_mod.create_new_client_stream(
        ssh_target, body.deploy_name, body.hostname, display_name, template_remote_dir,
    ):
        if event.get("type") == "result":
            provision = {k: v for k, v in event.items() if k != "type"}
        else:
            yield event
    if not provision["ok"]:
        yield {"type": "result", **provision, "phase": "provision"}
        return

    hosts = cfg.load_hosts()
    if not hosts:
        yield {"type": "result", **provision, "ok": False, "phase": "caddy", "stage": "no-host-configured",
               "error": "no host configured in Settings to find the shared Caddyfile on — "
                        f"the instance is up at 127.0.0.1:{provision['port']} on {ssh_target} but isn't "
                        "wired into Caddy yet. Add a host, then run deploy/add_clinic_site.sh by hand for this one."}
        return
    caddyfile_path = hosts[0].get("caddyfile_path", "")
    primary_remote_dir = caddyfile_path.rsplit("/deploy/Caddyfile", 1)[0] if caddyfile_path else ""
    if not primary_remote_dir:
        yield {"type": "result", **provision, "ok": False, "phase": "caddy", "stage": "no-caddyfile-path",
               "error": "the configured host has no caddyfile_path to derive the primary checkout from — "
                        f"the instance is up at 127.0.0.1:{provision['port']} on {ssh_target} but isn't "
                        "wired into Caddy yet."}
        return

    yield {"type": "phase", "label": "Wiring into shared Caddy"}
    caddy = None
    for event in new_client_mod.wire_caddy_stream(
        ssh_target, primary_remote_dir, body.hostname, provision["port"], body.deploy_name,
    ):
        if event.get("type") == "result":
            caddy = {k: v for k, v in event.items() if k != "type"}
        else:
            yield event
    if not caddy["ok"]:
        yield {"type": "result", **provision, "ok": False, "phase": "caddy",
               "error": f"the instance is up at 127.0.0.1:{provision['port']} on {ssh_target}, but wiring it into "
                        f"Caddy failed: {caddy['error']}",
               "caddy_error": caddy["error"], "caddy_output": caddy["output"]}
        return

    clients = cfg.load_clients()
    if any(c.get("name") == display_name for c in clients):
        yield {"type": "result", **provision, "ok": True, "phase": "register", "stage": "name-collision",
               "caddy_output": caddy["output"],
               "error": f"a client named {display_name!r} already exists in clients.json — the instance is live, "
                        "add it under a different display name yourself in the Add Client form."}
        return
    clients.append({
        "name": display_name,
        "base_url": f"https://{body.hostname}",
        "ssh_target": ssh_target,
        "remote_dir": provision["remote_dir"],
        "monthly_token_quota": 0,
        "admin_token": provision.get("admin_password") or "",
    })
    cfg.save_clients(clients)
    yield {"type": "result", **provision, "ok": True, "phase": "done", "caddy_output": caddy["output"]}


@router.post("/new-client/stream")
def new_client_stream(body: NewClientIn) -> StreamingResponse:
    """Streaming counterpart to POST /new-client — same end-to-end
    pipeline, but responds with newline-delimited JSON events as they
    happen instead of one big blocking response, so the frontend can
    render a live, terminal-like console. This exists because the plain
    route leaves the person staring at a blank spinner for however long the
    docker build takes (genuinely minutes) with zero visibility into
    whether it's actually doing anything.

    Each response line is one JSON object:
      {"type": "phase", "label": "..."}   a new stage started
      {"type": "log", "line": "..."}      one line of real command output
      {"type": "result", ...}             exactly one, always last — same
                                           shape POST /new-client returns
    """
    template = cfg.find_client(body.template_client_name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"No template client named {body.template_client_name!r}")
    ssh_target = template.get("ssh_target") or ""
    template_remote_dir = template.get("remote_dir") or ""
    if not ssh_target or not template_remote_dir:
        raise HTTPException(status_code=400, detail="template client has no ssh_target/remote_dir configured")
    display_name = body.display_name.strip() or body.deploy_name

    def gen():
        try:
            yield json.dumps({"type": "phase", "label": "Provisioning instance (clone, build, boot)"}) + "\n"
            for event in _run_new_client_stream(body, ssh_target, template_remote_dir, display_name):
                yield json.dumps(event) + "\n"
        except Exception as exc:  # noqa: BLE001 - same defensive rationale as
            # new_client()'s own top-level guard: turn ANY unexpected crash
            # into a diagnosable JSON event instead of the connection just
            # dying mid-stream with no explanation.
            yield json.dumps({
                "type": "result", "ok": False, "phase": "crash", "stage": "unhandled-exception",
                "error": f"unexpected error ({type(exc).__name__}): {exc}",
                "error_type": type(exc).__name__,
            }) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.get("/hosts")
def list_hosts() -> list[dict[str, Any]]:
    """Per configured host (the shared VPS, today): overall disk/mem/load
    plus every site discovered straight from its Caddyfile — including
    ones that aren't tracked as a client (e.g. a non-clinic marketing site
    sharing the same box) — cross-referenced against known clients so the
    dashboard can label each as tracked vs. unmanaged."""
    hosts = cfg.load_hosts()
    clients = cfg.load_clients()
    return core.check_all_hosts(hosts, clients)


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return {"poll_interval_seconds": cfg.load_poll_interval()}


@router.put("/settings")
def update_settings(body: SettingsIn) -> dict[str, Any]:
    if not (15 <= body.poll_interval_seconds <= 3600):
        raise HTTPException(status_code=422, detail="poll_interval_seconds must be between 15 and 3600")
    cfg.save_poll_interval(body.poll_interval_seconds)
    return {"poll_interval_seconds": body.poll_interval_seconds}
