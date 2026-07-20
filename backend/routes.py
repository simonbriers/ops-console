"""API routes: client CRUD, live status, and the server-side SSH token
fetch (the browser itself can't SSH, so this proxies it — same command
`run_ssh` already runs for the version check, just aimed at .env instead
of git/docker)."""
from __future__ import annotations

import json
import time
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
from backend import onboarding as onboarding_mod
from backend import smoke as smoke_mod
from backend import validator as validator_mod
from backend import vault as vault_mod

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


class DeployBatchIn(BaseModel):
    names: list[str]
    # The single-deploy guard ("type the client's exact name") doesn't scale
    # to a batch — removing that per-client typing is this endpoint's whole
    # reason to exist. The belt-and-suspenders becomes "update <count>":
    # still impossible to fire with a stray/automated POST that doesn't know
    # exactly how many clients it's about to touch, still checked
    # server-side, typed ONCE regardless of whether the batch is 2 clients
    # or 500.
    confirm: str


@router.post("/deploy-batch")
def deploy_batch(body: DeployBatchIn) -> StreamingResponse:
    """Deploys several clients in one request, streaming NDJSON progress
    events (same dialect as /new-client/stream) so the Updates tab can show
    a live per-client status instead of one long blank spinner:

      {"type": "start",  "name": ...}                          a client began
      {"type": "result", "name": ..., ok, stage, commit, ...}  a client done
      {"type": "done",   "ok_count": N, "fail_count": M}       always last

    Every name must resolve to a configured client and `confirm` must be
    exactly "update <N>" (N = number of clients) — both checked BEFORE
    anything is touched, so a bad request deploys nothing rather than
    half the list. Concurrency: parallel across hosts, strictly sequential
    per host (see core.deploy_batch_stream — several simultaneous docker
    builds on one small VPS is how an update becomes an outage). Each
    client's outcome is appended to the same deploy_log.jsonl the single
    Deploy button uses, so per-client history stays in one place."""
    # Dedupe while preserving order — double-submitting a name must not
    # deploy it twice in one run.
    names = list(dict.fromkeys(n for n in body.names if n))
    if not names:
        raise HTTPException(status_code=400, detail="no client names given")
    expected = f"update {len(names)}"
    if body.confirm.strip().lower() != expected:
        raise HTTPException(
            status_code=400,
            detail=f"confirmation text did not match {expected!r} — nothing was deployed")
    clients = []
    for n in names:
        client = cfg.find_client(n)
        if client is None:
            raise HTTPException(status_code=404,
                                detail=f"No client named {n!r} — nothing was deployed")
        clients.append(client)

    def gen():
        try:
            for event in core.deploy_batch_stream(clients):
                if event.get("type") == "result":
                    deploy_log.append({
                        "name": event.get("name"),
                        "requested_at": datetime.now().isoformat(timespec="seconds"),
                        "ok": event.get("ok"),
                        "stage": event.get("stage"),
                        "commit": event.get("commit"),
                        "error": event.get("error"),
                        "batch": True,
                    })
                yield event
        except Exception as exc:  # noqa: BLE001 — same defensive rationale as
            # /new-client/stream: any unexpected crash becomes a diagnosable
            # final event instead of the connection dying mid-stream.
            yield {"type": "done", "ok_count": 0, "fail_count": len(clients),
                   "error": f"unexpected error ({type(exc).__name__}): {exc}"}

    return _ndjson(gen())


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
        # Loopback port: lets the smoke suite and (if ADMIN_TUNNEL_ONLY is
        # ever enabled for this client) SSH-based metrics work immediately.
        "admin_local_port": provision.get("port"),
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


# ---------------------------------------------------------------------------
# Onboarding v2 (docs/ONBOARDING_V2_PLAN.md): the stepper API, teardown,
# smoke suite, config validation, and the credentials vault. Appended as one
# block; every streaming endpoint speaks the same NDJSON dialect as
# /new-client/stream ({"type": "log"|"result", ...}).
# ---------------------------------------------------------------------------
import base64 as _b64

from fastapi.responses import JSONResponse


class OnboardingIn(BaseModel):
    deploy_name: str
    display_name: str = ""
    hostname: str = ""
    template_client_name: str = ""
    notes: str = ""
    medical: bool = False


class StepRunIn(BaseModel):
    set_ids: list[str] = []          # used by the credentials step only


class TeardownIn(BaseModel):
    confirm: str                     # must equal the deploy_name, typed


class VaultSetIn(BaseModel):
    name: str
    kind: str
    values: dict[str, str] = {}
    content_b64: str | None = None
    id: str | None = None
    # Phase 1 (docs/TOKEN_ECONOMY_PLAN.md): sourcing metadata. None = leave
    # unchanged on edit / use defaults on create (ours/paid).
    owner: str | None = None    # "ours" | "client" (BYOK)
    tier: str | None = None     # "paid" | "free" | "local"
    notes: str | None = None


class VaultImportIn(BaseModel):
    client_name: str


class VaultReconcileIn(BaseModel):
    client_name: str | None = None   # None = reconcile every client


class VaultAssignIn(BaseModel):
    client_name: str
    role: str
    set_id: str


class ApplyCredsIn(BaseModel):
    set_ids: list[str]


def _ndjson(gen):
    def encode():
        for event in gen:
            yield json.dumps(event) + "\n"
    return StreamingResponse(encode(), media_type="application/x-ndjson")


def _onboarding_or_404(deploy_name: str) -> dict[str, Any]:
    record = onboarding_mod.load(deploy_name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No onboarding named {deploy_name!r}")
    return record


def _client_for_onboarding(record: dict[str, Any]) -> dict[str, Any] | None:
    display = record.get("bundle", {}).get("display_name") or record.get("deploy_name")
    return cfg.find_client(display)


def _primary_remote_dir() -> str:
    hosts = cfg.load_hosts()
    caddyfile_path = hosts[0].get("caddyfile_path", "") if hosts else ""
    return caddyfile_path.rsplit("/deploy/Caddyfile", 1)[0] if caddyfile_path else ""


@router.get("/client-names")
def client_names() -> list[str]:
    """Just the configured client names, instantly — for dropdowns. The full
    GET /api/clients runs live health checks over SSH/HTTPS and takes
    seconds; a <select> racing that fetch is how a form once showed
    '(no clients yet)' next to a dashboard showing four."""
    return [c.get("name", "") for c in cfg.load_clients() if c.get("name")]


@router.get("/onboardings")
def list_onboardings() -> list[dict[str, Any]]:
    return [onboarding_mod.summary(r) for r in onboarding_mod.load_all()]


@router.get("/onboardings/steps")
def onboarding_steps() -> list[dict[str, str]]:
    return onboarding_mod.STEPS


@router.get("/onboardings/{deploy_name}")
def get_onboarding(deploy_name: str) -> dict[str, Any]:
    return _onboarding_or_404(deploy_name)


@router.post("/onboardings", status_code=201)
def upsert_onboarding(body: OnboardingIn) -> dict[str, Any]:
    """Create (or update the intake of) an onboarding. Saving a complete
    intake marks the intake step ok — this is the stepper's step 1."""
    name_error = new_client_mod.validate_deploy_name(body.deploy_name)
    if name_error:
        raise HTTPException(status_code=400, detail=name_error)
    hostname = ""
    if body.hostname.strip():
        hostname, host_error = onboarding_mod.normalize_hostname(body.hostname)
        if host_error:
            raise HTTPException(status_code=400, detail=host_error)
    record = onboarding_mod.load(body.deploy_name)
    # Only NON-EMPTY submitted values overwrite what's saved — a re-submitted
    # form with a blank/unloaded field (e.g. the template dropdown racing the
    # client-list fetch, 2026-07-19 incident) must never wipe a good value.
    updates = {"deploy_name": body.deploy_name}
    if body.display_name.strip():
        updates["display_name"] = body.display_name.strip()
    if hostname:
        updates["hostname"] = hostname
    if body.template_client_name.strip():
        updates["template_client_name"] = body.template_client_name.strip()
    if body.notes:
        updates["notes"] = body.notes
    updates["medical"] = bool(body.medical)
    if record is None:
        bundle = {"display_name": body.deploy_name, "hostname": "",
                  "template_client_name": "", "notes": ""}
        bundle.update(updates)
        record = onboarding_mod.new_record(bundle)
        onboarding_mod.save(record)
    else:
        record["bundle"].update(updates)
        onboarding_mod.save(record)
    saved = (onboarding_mod.load(body.deploy_name) or {}).get("bundle", {})
    complete = bool(saved.get("hostname") and saved.get("template_client_name"))
    onboarding_mod.set_step(body.deploy_name, "intake",
                            "ok" if complete else "pending",
                            "intake saved" if complete else "hostname/template still missing")
    return onboarding_mod.load(body.deploy_name)


@router.delete("/onboardings/{deploy_name}", status_code=204)
def delete_onboarding(deploy_name: str) -> None:
    """Removes only the RECORD (the paperwork). The instance, if provisioned,
    stays — use /teardown to remove the instance itself."""
    record = _onboarding_or_404(deploy_name)
    onboarding_mod._path(record["deploy_name"]).unlink(missing_ok=True)


@router.post("/onboardings/{deploy_name}/step/{step_id}/run")
def run_onboarding_step(deploy_name: str, step_id: str, body: StepRunIn | None = None):
    """Run (or RE-run — every step is safe to repeat) one step of an
    onboarding, streaming progress and recording the outcome in the
    persistent record, so an interrupted onboarding resumes exactly where
    it left off."""
    record = _onboarding_or_404(deploy_name)
    if step_id not in onboarding_mod.STEP_IDS:
        raise HTTPException(status_code=404, detail=f"unknown step {step_id!r}")
    bundle = record.get("bundle", {})
    set_ids = (body.set_ids if body else []) or []

    def gen():
        onboarding_mod.set_step(deploy_name, step_id, "running")
        ok, detail = False, ""
        try:
            if step_id == "intake":
                complete = bool(bundle.get("hostname") and bundle.get("template_client_name"))
                ok, detail = complete, ("intake saved" if complete
                                        else "fill hostname + template client first")
                yield {"type": "log", "line": detail}

            elif step_id == "dns":
                template = cfg.find_client(bundle.get("template_client_name", ""))
                ssh_target = (template or {}).get("ssh_target", "")
                yield {"type": "log", "line": f"$ checking DNS for {bundle.get('hostname')} from the VPS…"}
                res = core.check_dns(ssh_target, bundle.get("hostname", ""))
                ok = res["ok"]
                detail = (f"resolves to {res['expected']}" if ok else (res["error"] or "DNS check failed"))
                yield {"type": "log", "line": f"expected {res.get('expected')}, resolved {res.get('resolved')}"}

            elif step_id == "provision":
                template = cfg.find_client(bundle.get("template_client_name", ""))
                if template is None:
                    detail = f"template client {bundle.get('template_client_name')!r} not found"
                else:
                    ssh_target = template.get("ssh_target", "")
                    tdir = template.get("remote_dir", "")
                    display = bundle.get("display_name") or deploy_name
                    provision_result = None
                    for ev in new_client_mod.create_new_client_stream(
                            ssh_target, deploy_name, bundle.get("hostname", ""), display, tdir,
                            medical=bool(bundle.get("medical", False))):
                        if ev.get("type") == "result":
                            provision_result = ev
                        else:
                            yield ev
                    if not provision_result or not provision_result.get("ok"):
                        detail = (provision_result or {}).get("error") or "provisioning failed"
                    else:
                        onboarding_mod.merge_result(
                            deploy_name, port=provision_result.get("port"),
                            remote_dir=provision_result.get("remote_dir"),
                            admin_password=provision_result.get("admin_password"))
                        primary = _primary_remote_dir()
                        if not primary:
                            detail = ("instance is up, but no host/caddyfile_path configured "
                                      "in Settings to wire Caddy — fix Settings, re-run this step")
                        else:
                            caddy_result = None
                            for ev in new_client_mod.wire_caddy_stream(
                                    ssh_target, primary, bundle.get("hostname", ""),
                                    provision_result["port"], deploy_name):
                                if ev.get("type") == "result":
                                    caddy_result = ev
                                else:
                                    yield ev
                            if not caddy_result or not caddy_result.get("ok"):
                                detail = f"Caddy wiring failed: {(caddy_result or {}).get('error')}"
                            else:
                                clients = cfg.load_clients()
                                if not any(c.get("name") == display for c in clients):
                                    clients.append({
                                        "name": display,
                                        "base_url": f"https://{bundle.get('hostname')}",
                                        "ssh_target": ssh_target,
                                        "remote_dir": provision_result["remote_dir"],
                                        "monthly_token_quota": 0,
                                        "admin_token": provision_result.get("admin_password") or "",
                                        "admin_local_port": provision_result.get("port"),
                                    })
                                    cfg.save_clients(clients)
                                    yield {"type": "log", "line": f"registered {display!r} in clients.json"}
                                ok, detail = True, f"live at 127.0.0.1:{provision_result['port']}, Caddy wired"

            elif step_id == "credentials":
                client = _client_for_onboarding(record)
                if client is None:
                    detail = "client not registered yet — run the provision step first"
                elif not set_ids:
                    detail = "no vault sets chosen — pick at least one credential set"
                else:
                    yield {"type": "log", "line": f"$ applying {len(set_ids)} vault set(s) + recreate…"}
                    res = vault_mod.apply_sets(client, set_ids)
                    for t in res.get("tests", []):
                        yield {"type": "log",
                               "line": f"test {t.get('kind')}: {'ok' if t.get('ok') else t.get('error')}"}
                    ok = res["ok"] and all(t.get("ok") for t in res.get("tests", []))
                    detail = (f"applied {', '.join(res.get('applied', []))}; container recreated"
                              if ok else (res.get("error") or "a credential test failed — see log"))

            elif step_id == "config":
                client = _client_for_onboarding(record)
                if client is None:
                    detail = "client not registered yet — run the provision step first"
                else:
                    shell = core._shell_remote_dir((client.get("remote_dir") or "").rstrip("/"))
                    proj = core._project_name(client.get("remote_dir") or "")
                    okc, out = core.run_ssh(
                        client.get("ssh_target", ""),
                        f"cd {shell} && docker compose -p {proj} exec -T app cat /data/site_config.yaml",
                        timeout=40)
                    if not okc:
                        detail = f"couldn't read live config: {out.strip()[-200:]}"
                    else:
                        res = validator_mod.validate_yaml_text(out)
                        for e in res.get("errors", []):
                            yield {"type": "log", "line": f"ERROR: {e}"}
                        for w in res.get("warnings", []):
                            yield {"type": "log", "line": f"warning: {w}"}
                        ok = res["ok"]
                        detail = ("config valid"
                                  + (f" ({len(res.get('warnings', []))} warning(s))" if res.get("warnings") else "")
                                  if ok else f"{len(res.get('errors', []))} validation error(s)")

            elif step_id == "verify":
                client = _client_for_onboarding(record)
                if client is None:
                    detail = "client not registered yet — run the provision step first"
                else:
                    # Readiness gate (2026-07-19): the credentials step recreates
                    # the app container and uvicorn needs ~30-60s to boot. Running
                    # the smoke suite before it listens produced 6 false FAILs on
                    # a perfectly healthy deploy. So: wait for /health to answer
                    # on the loopback port first, with a visible countdown, and
                    # only then run the checks.
                    r_target = client.get("ssh_target", "")
                    r_port = client.get("admin_local_port")
                    if r_target and r_port:
                        yield {"type": "log",
                               "line": "waiting for the app to come up (it restarts after the credentials step — up to 2 minutes)…"}
                        ready = False
                        for attempt in range(24):
                            r_ok, r_out = core.run_ssh(
                                r_target,
                                f"curl -fsS -m 8 http://127.0.0.1:{int(r_port)}/health",
                                timeout=20)
                            if r_ok and '"status"' in r_out:
                                ready = True
                                yield {"type": "log",
                                       "line": f"app is up (after ~{attempt * 5}s) — running checks now"}
                                break
                            yield {"type": "log",
                                   "line": f"  not answering yet — retrying in 5s ({attempt + 1}/24)"}
                            time.sleep(5)
                        if not ready:
                            yield {"type": "log",
                                   "line": "app did not come up within 2 minutes — running the checks anyway so the failure details are visible"}
                    yield {"type": "log", "line": "$ running smoke suite…"}
                    rows = smoke_mod.run_smoke(client)
                    for r in rows:
                        yield {"type": "log",
                               "line": f"{'PASS' if r['ok'] else 'FAIL'} {r['check']}: {r['detail']}"}
                    primary = _primary_remote_dir()
                    caddy_gate = {"ok": False, "detail": "no primary checkout configured"}
                    if primary:
                        caddy_gate = new_client_mod.caddyfile_git_status(
                            client.get("ssh_target", ""), primary)
                    yield {"type": "log", "line": f"Caddyfile git status: {caddy_gate.get('detail')}"}
                    # Verify goes red ONLY for things that affect the client
                    # actually working. Housekeeping (backup timer, Caddyfile
                    # git state) is reported as warnings, never as failure —
                    # per the 2026-07-19 acceptance bar: a clean deploy shows
                    # NO red. (Caddyfile commit is auto-attempted at wiring
                    # time now; see wire_caddy_stream.)
                    critical = [r for r in rows if r["check"] != "backup_timer"]
                    warn_rows = [r for r in rows if r["check"] == "backup_timer" and not r["ok"]]
                    caddy_ok = bool(caddy_gate.get("clean")) and bool(caddy_gate.get("pushed"))
                    ok = all(r["ok"] for r in critical)
                    parts = []
                    if not ok:
                        parts.append(f"{sum(1 for r in critical if not r['ok'])} smoke check(s) failing")
                    warnings = []
                    if not caddy_ok:
                        warnings.append(f"Caddyfile {caddy_gate.get('detail')}")
                    if warn_rows:
                        warnings.append("backup timer not installed")
                    detail = ("all green" if ok and not warnings
                              else ("; ".join(parts) if not ok
                                    else "working — housekeeping notes: " + "; ".join(warnings)))
        except Exception as exc:  # noqa: BLE001 — a step crash must land in the record, not a 500
            ok, detail = False, f"unexpected error ({type(exc).__name__}): {exc}"
            yield {"type": "log", "line": detail}
        onboarding_mod.set_step(deploy_name, step_id, "ok" if ok else "failed", detail)
        yield {"type": "result", "ok": ok, "error": None if ok else detail, "step": step_id,
               "record": onboarding_mod.summary(onboarding_mod.load(deploy_name) or record)}

    return _ndjson(gen())


@router.post("/onboardings/{deploy_name}/teardown")
def teardown_onboarding(deploy_name: str, body: TeardownIn):
    """Removes the INSTANCE (containers, volumes, checkout, Caddy block) and
    its clients.json entry; the onboarding record stays, marked torn down,
    so the paper trail survives. Requires the deploy name typed back as
    confirmation — the same guard the Deploy button uses."""
    record = _onboarding_or_404(deploy_name)
    if body.confirm != deploy_name:
        raise HTTPException(status_code=400, detail="confirmation text does not match the deploy name")
    bundle = record.get("bundle", {})
    template = cfg.find_client(bundle.get("template_client_name", ""))
    ssh_target = ((template or {}).get("ssh_target")
                  or (_client_for_onboarding(record) or {}).get("ssh_target") or "")
    if not ssh_target:
        raise HTTPException(status_code=400, detail="no ssh_target derivable for this onboarding")
    primary = _primary_remote_dir()
    protected = [c.get("remote_dir", "") for c in cfg.load_clients()
                 if c.get("name") != (bundle.get("display_name") or deploy_name)]
    if primary:
        protected.append(primary)

    def gen():
        final = {"ok": False, "error": "teardown produced no result"}
        for ev in new_client_mod.teardown_client_stream(
                ssh_target, deploy_name, primary or "~/dental-clinic-agent",
                bundle.get("hostname", ""), protected):
            if ev.get("type") == "result":
                final = ev
            else:
                yield ev
        if final.get("ok"):
            display = bundle.get("display_name") or deploy_name
            clients = [c for c in cfg.load_clients() if c.get("name") != display]
            cfg.save_clients(clients)
            # Removed means GONE (2026-07-19): no residual half-done card.
            # A fresh deploy starts from a fresh form, full stop.
            onboarding_mod._path(deploy_name).unlink(missing_ok=True)
            yield {"type": "log", "line": f"removed {display!r} everywhere - monitoring, records, all of it"}
        yield {"type": "result", "ok": final.get("ok", False), "error": final.get("error")}

    return _ndjson(gen())


# --- smoke + validate on any client ----------------------------------------

@router.post("/clients/{name}/smoke")
def client_smoke(name: str) -> dict[str, Any]:
    client = cfg.find_client(name)
    if client is None:
        raise HTTPException(status_code=404, detail=f"No client named {name!r}")
    rows = smoke_mod.run_smoke(client)
    return {"ok": all(r["ok"] for r in rows if r["check"] != "backup_timer"), "checks": rows}


def _validate_live_config(client: dict[str, Any]) -> dict[str, Any]:
    """Reads a client's LIVE /data/site_config.yaml out of its running app
    container over SSH and runs the local validator on it. Shared by the
    single validate endpoint below and the batch test runner."""
    shell = core._shell_remote_dir((client.get("remote_dir") or "").rstrip("/"))
    proj = core._project_name(client.get("remote_dir") or "")
    ok, out = core.run_ssh(client.get("ssh_target", ""),
                           f"cd {shell} && docker compose -p {proj} exec -T app cat /data/site_config.yaml",
                           timeout=40)
    if not ok:
        return {"ok": False, "errors": [f"couldn't read live config: {out.strip()[-300:]}"], "warnings": []}
    return validator_mod.validate_yaml_text(out)


@router.post("/clients/{name}/validate-config")
def client_validate_config(name: str) -> dict[str, Any]:
    client = cfg.find_client(name)
    if client is None:
        raise HTTPException(status_code=404, detail=f"No client named {name!r}")
    return _validate_live_config(client)


class TestBatchIn(BaseModel):
    names: list[str]


@router.post("/test-batch")
def test_batch(body: TestBatchIn) -> StreamingResponse:
    """Runs the full check suite — the 8-check smoke suite (health, TLS,
    admin API, CSP, a real chat round-trip, backup timer) plus live
    site_config.yaml validation — across several clients in one request,
    streaming NDJSON events in the same dialect as /deploy-batch:

      {"type": "start",  "name"}                              a client began
      {"type": "result", "name", ok, checks, config, summary}  a client done
      {"type": "done",   "ok_count", "fail_count"}             always last

    Per-client verdict mirrors the single smoke endpoint's: every check
    except backup_timer must pass, AND the live config must have no
    validation errors. backup_timer failures and config warnings are
    carried as `summary.warnings` — reported, never fatal (the same
    warn-not-fail bar the onboarding verify step uses).

    No typed confirmation: everything here is read-only against the
    monitored instances (the chat round-trip does burn a few LLM tokens
    per client, which is the cost of actually proving the key works).
    Concurrency is batch_stream's usual: parallel across hosts, strictly
    sequential per host — the checks are SSH-heavy and the chat
    round-trip alone can take ~30s per client."""
    names = list(dict.fromkeys(n for n in body.names if n))
    if not names:
        raise HTTPException(status_code=400, detail="no client names given")
    clients = []
    for n in names:
        client = cfg.find_client(n)
        if client is None:
            raise HTTPException(status_code=404,
                                detail=f"No client named {n!r} — nothing was run")
        clients.append(client)

    def job(client: dict[str, Any]) -> dict[str, Any]:
        checks = smoke_mod.run_smoke(client)
        config_res = _validate_live_config(client)
        critical_fails = [r["check"] for r in checks
                          if not r["ok"] and r["check"] != "backup_timer"]
        warnings: list[str] = []
        if any(r["check"] == "backup_timer" and not r["ok"] for r in checks):
            warnings.append("backup timer not active")
        warnings.extend(f"config: {w}" for w in config_res.get("warnings") or [])
        config_errors = config_res.get("errors") or []
        ok = not critical_fails and not config_errors
        return {
            "ok": ok,
            "checks": checks,
            "config": config_res,
            "summary": {
                "passed": sum(1 for r in checks if r["ok"]),
                "total": len(checks),
                "failed_checks": critical_fails,
                "config_errors": config_errors,
                "warnings": warnings,
            },
            "error": None if ok else (
                "; ".join(filter(None, [
                    f"{len(critical_fails)} check(s) failing: {', '.join(critical_fails)}" if critical_fails else "",
                    f"{len(config_errors)} config error(s)" if config_errors else "",
                ]))),
        }

    def gen():
        try:
            yield from core.batch_stream(clients, job)
        except Exception as exc:  # noqa: BLE001 — same defensive rationale as
            # /deploy-batch: any unexpected crash becomes a diagnosable final
            # event instead of the connection dying mid-stream.
            yield {"type": "done", "ok_count": 0, "fail_count": len(clients),
                   "error": f"unexpected error ({type(exc).__name__}): {exc}"}

    return _ndjson(gen())


class BackupBatchIn(BaseModel):
    names: list[str]
    # Same belt-and-suspenders shape as /deploy-batch: this mutates the
    # instances' hosts (installs systemd units, runs a backup), so it takes
    # one typed confirmation — "backups <count>" — checked server-side.
    confirm: str


@router.post("/backup-batch")
def backup_batch(body: BackupBatchIn) -> StreamingResponse:
    """Installs/repairs the nightly encrypted-backup timer on each selected
    client's VPS and PROVES it with an immediate real backup run — see
    core.setup_backup for the stages and the incident that made "prove it"
    non-optional (a timer that fired nightly for 17 days producing nothing).
    Streams the same NDJSON dialect as /deploy-batch and /test-batch; each
    result is appended to deploy_log.jsonl (action "backup-setup") since
    this mutates the instances' hosts. Idempotent — safe to re-run on a
    client that's already set up (it just takes another backup)."""
    names = list(dict.fromkeys(n for n in body.names if n))
    if not names:
        raise HTTPException(status_code=400, detail="no client names given")
    expected = f"backups {len(names)}"
    if body.confirm.strip().lower() != expected:
        raise HTTPException(
            status_code=400,
            detail=f"confirmation text did not match {expected!r} — nothing was touched")
    clients = []
    for n in names:
        client = cfg.find_client(n)
        if client is None:
            raise HTTPException(status_code=404,
                                detail=f"No client named {n!r} — nothing was touched")
        clients.append(client)

    def gen():
        try:
            for event in core.batch_stream(clients, core.setup_backup):
                if event.get("type") == "result":
                    deploy_log.append({
                        "name": event.get("name"),
                        "requested_at": datetime.now().isoformat(timespec="seconds"),
                        "ok": event.get("ok"),
                        "stage": event.get("stage"),
                        "commit": None,
                        "error": event.get("error"),
                        "action": "backup-setup",
                    })
                yield event
        except Exception as exc:  # noqa: BLE001 — same rationale as the others
            yield {"type": "done", "ok_count": 0, "fail_count": len(clients),
                   "error": f"unexpected error ({type(exc).__name__}): {exc}"}

    return _ndjson(gen())


# --- credentials vault ------------------------------------------------------

@router.get("/vault/sets")
def vault_list() -> list[dict[str, Any]]:
    return vault_mod.list_sets(redact=True)


@router.post("/vault/sets")
def vault_upsert(body: VaultSetIn) -> dict[str, Any]:
    res = vault_mod.upsert_set(body.name, body.kind, body.values, body.content_b64, body.id,
                               owner=body.owner, tier=body.tier, notes=body.notes)
    if not res["ok"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.post("/vault/sets/{set_id}/reveal")
def vault_reveal(set_id: str) -> dict[str, Any]:
    """Unredacted values for one set. POST (not GET) so secrets never sit
    in a URL/access-log line. Same local trust boundary as /fetch-token."""
    res = vault_mod.reveal_set(set_id)
    if not res["ok"]:
        raise HTTPException(status_code=404, detail=res["error"])
    return res


@router.get("/vault/assignments")
def vault_assignments() -> list[dict[str, Any]]:
    return vault_mod.list_assignments()


@router.post("/vault/assignments")
def vault_assign_manual(body: VaultAssignIn) -> dict[str, Any]:
    """Record-only assignment: document that a client already runs on a
    set WITHOUT touching the server (no .env write, no recreate). Exists
    for file credentials (invisible to reconcile) and for frozen clients
    where an apply's container recreate is off-limits."""
    if cfg.find_client(body.client_name) is None:
        raise HTTPException(status_code=404, detail=f"No client named {body.client_name!r}")
    if body.role not in vault_mod.ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {vault_mod.ROLES}")
    if not any(s["id"] == body.set_id for s in vault_mod.load_vault()["sets"]):
        raise HTTPException(status_code=404, detail=f"no set with id {body.set_id!r}")
    vault_mod.record_assignment(body.client_name, body.role, body.set_id, "manual")
    return {"ok": True, "error": None}


@router.post("/vault/reconcile")
def vault_reconcile(body: VaultReconcileIn) -> dict[str, Any]:
    """Backfill/refresh assignments from each client's live remote .env.
    Read-only toward the instances (a cat over SSH) — safe for frozen
    clients too."""
    if body.client_name:
        client = cfg.find_client(body.client_name)
        if client is None:
            raise HTTPException(status_code=404, detail=f"No client named {body.client_name!r}")
        clients = [client]
    else:
        clients = cfg.load_clients()
    reports = [vault_mod.reconcile_client(c) for c in clients]
    return {"ok": all(r.get("ok") for r in reports) if reports else True, "reports": reports}


@router.delete("/vault/sets/{set_id}", status_code=204)
def vault_delete(set_id: str) -> None:
    res = vault_mod.delete_set(set_id)
    if not res["ok"]:
        raise HTTPException(status_code=404, detail=res["error"])


@router.post("/vault/import")
def vault_import(body: VaultImportIn) -> dict[str, Any]:
    client = cfg.find_client(body.client_name)
    if client is None:
        raise HTTPException(status_code=404, detail=f"No client named {body.client_name!r}")
    return vault_mod.import_from_client(client)


@router.post("/clients/{name}/apply-credentials")
def client_apply_credentials(name: str, body: ApplyCredsIn) -> dict[str, Any]:
    client = cfg.find_client(name)
    if client is None:
        raise HTTPException(status_code=404, detail=f"No client named {name!r}")
    return vault_mod.apply_sets(client, body.set_ids)
