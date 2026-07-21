"""Pure status/usage-fetching logic for ops-console — no FastAPI/HTTP-layer
code in this file, so it's testable and runnable headlessly:

    python -m backend.core --check

Ported from the earlier Tkinter prototype's core.py (same 10-case mocked
test suite passed there); renamed "clinic" -> "client" throughout since
ops-console covers any vertical running this product, not just clinics.

Every check function is defensive: network/SSH failures are captured in
the returned dict's "error" field rather than raised, so one client being
unreachable can never crash a whole-fleet poll or an API request covering
every client.

Almost everything here is read-only by design: HTTP calls are GET only,
and most SSH commands never pull/build/restart. The one deliberate
exception is deploy_client() — a guarded, single-client "pull the commits
that are already on origin/master and restart" action, added because the
dashboard is the one place staleness is visible in the first place. It's
scoped to exactly one client's own remote_dir/docker-compose.yml (same
scoping check_client_resources already uses), uses `git pull --ff-only`
(refuses to touch anything if history has diverged — never force/reset),
and never restarts if the build step fails. It does NOT replace
deploy.ps1's job of committing/pushing local changes — it only syncs a
client to whatever's already on origin/master.
"""
from __future__ import annotations

import argparse
import json
import queue
import shlex
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from datetime import datetime
from typing import Any

import requests

from backend import config as cfg

HTTP_TIMEOUT = 10
SSH_TIMEOUT = 20
DEPLOY_TIMEOUT = 300  # a docker compose build can genuinely take minutes
MAX_PARALLEL_CHECKS = 8
# Batch updates deploy at most this many HOSTS at once. Within one host,
# deploys are strictly sequential — see deploy_batch_stream's docstring.
MAX_PARALLEL_DEPLOY_HOSTS = 3

# The literal placeholder shipped in clients.example.json. If a client still
# has this value (never replaced with a real token, e.g. right after a fresh
# `copy clients.example.json clients.json`), treat it exactly like "no token
# configured" rather than sending it as a real X-Admin-Token on every poll.
# Sending it for real trips the monitored instance's OWN brute-force lockout
# (backend/security.py + admin.py's require_admin on that product — 3 bad
# attempts locks the polling machine's source IP out for 15 minutes) on a
# fixed ~poll_interval_seconds cadence, which is exactly the kind of
# self-inflicted incident this guard exists to prevent — see the real one
# that happened onboarding Main Clinic + Clinica Valor locally.
PLACEHOLDER_ADMIN_TOKEN = "REPLACE_ME_OR_USE_FETCH_VIA_SSH"


def _real_token(client: dict[str, Any]) -> str:
    """The client's admin_token, or "" if unset/still the shipped placeholder."""
    token = client.get("admin_token") or ""
    return "" if token == PLACEHOLDER_ADMIN_TOKEN else token

# Rough, editable-per-client assumptions for how many minutes a human
# receptionist would spend on each task type, used only to turn audit-log
# counts into a "time saved" estimate for the invoicing/sales narrative —
# never claimed as precise, just a reasonable industry-standard default a
# client override (client["minutes_per_*"]) can replace.
DEFAULT_MINUTES_PER_BOOKING = 6
DEFAULT_MINUTES_PER_RESCHEDULE = 4
DEFAULT_MINUTES_PER_CANCELLATION = 3
DEFAULT_MINUTES_PER_CALLBACK = 5

# Markers used to split one combined SSH command's stdout into sections
# without multiple separate SSH round trips (each one is a real network
# round trip, worth bundling rather than paying 3x latency per client).
_MARK_COMMIT = "===OPSCONSOLE_COMMIT==="
_MARK_BEHIND = "===OPSCONSOLE_BEHIND==="
_MARK_BEHIND_LOG = "===OPSCONSOLE_BEHIND_LOG==="
_MARK_BEHIND_FILES = "===OPSCONSOLE_BEHIND_FILES==="
_MARK_DOCKER = "===OPSCONSOLE_DOCKER==="
BEHIND_LOG_LIMIT = 20  # cap how many "what's behind" commit lines we pull per check
# Paths that change how containers are built/named/networked rather than just
# application behavior — a collision like the Caddyfile/container-name one
# this flag exists to catch is a docker-compose/deploy-config problem, not a
# code problem, so it needs a human look before syncing, not just a routine
# "N commits behind" glance.
_INFRA_PATH_RE = re.compile(r"(^|/)(docker-compose[^/]*\.ya?ml|Dockerfile[^/]*)$|^deploy/")
_MARK_HOST_DISK = "===OPSCONSOLE_HOST_DISK==="
_MARK_HOST_MEM = "===OPSCONSOLE_HOST_MEM==="
_MARK_HOST_LOAD = "===OPSCONSOLE_HOST_LOAD==="
_MARK_HOST_BREAKDOWN = "===OPSCONSOLE_HOST_BREAKDOWN==="
_MARK_HOST_DOCKER_DF = "===OPSCONSOLE_HOST_DOCKER_DF==="
_MARK_CLIENT_STATS = "===OPSCONSOLE_CLIENT_STATS==="
_MARK_CLIENT_DISK = "===OPSCONSOLE_CLIENT_DISK==="
_MARK_DEPLOY_PULL = "===OPSCONSOLE_DEPLOY_PULL==="
_MARK_DEPLOY_BUILD = "===OPSCONSOLE_DEPLOY_BUILD==="
_MARK_DEPLOY_PRECHECK = "===OPSCONSOLE_DEPLOY_PRECHECK==="
_MARK_DEPLOY_UP = "===OPSCONSOLE_DEPLOY_UP==="
_MARK_DEPLOY_STATUS = "===OPSCONSOLE_DEPLOY_STATUS==="
_DEPLOY_OUTPUT_CHAR_LIMIT = 4000  # docker build logs can be long; keep the tail, not the whole thing
# `docker inspect`'s Go template for reading a container's compose-project
# label — a plain string, not an f-string, so its braces stay literal when
# spliced into the f-string that builds the deploy command below (an
# f-string containing this text directly would need every brace doubled).
_INSPECT_PROJECT_LABEL_FMT = '{{index .Config.Labels "com.docker.compose.project"}}'


def _shell_remote_dir(remote_dir: str) -> str:
    """Expands a leading '~' to '$HOME' before remote_dir gets spliced into
    an SSH command string. Bash only tilde-expands a literal '~' written
    directly in the command text at the START of a word — never the result
    of a variable/parameter expansion. A remote_dir configured with a
    literal '~' (e.g. "~/dental-clinic-agent", the natural way to type it)
    broke exactly this way once already: deploy_client assigns compose file
    paths into a shell variable (compose_files) before using them, so
    "docker compose $compose_files ..." left the '~' un-expanded, and
    Docker's own path resolution then treated it as a literal directory
    name relative to the SSH session's cwd (its home dir), producing paths
    like "/home/deploy/~/dental-clinic-agent/docker-compose.yml" — a real
    incident, not a hypothetical. '$HOME' doesn't have this limitation — it
    expands via ordinary parameter expansion regardless of whether it flows
    through a variable first — so this swaps the two at the one shared
    source every remote_dir-using function already goes through, rather
    than trusting every call site to remember not to introduce a variable
    indirection."""
    if remote_dir == "~":
        return "$HOME"
    if remote_dir.startswith("~/"):
        return "$HOME" + remote_dir[1:]
    return remote_dir


def _project_name(remote_dir: str) -> str:
    """Derives a stable, explicit Compose project name from a client's own
    remote_dir — its last path segment (e.g. "~/primeconnect-chatbot" ->
    "primeconnect-chatbot", "$HOME/dental-clinic-agent" -> "dental-clinic-
    agent"). Passed as `-p <name>` on every docker compose invocation below.

    Exists because of a real incident: none of these commands `cd` into
    remote_dir before running `docker compose` (each SSH command is a
    standalone one-shot, landing in whatever the login shell's default cwd
    happens to be), so Compose's own project-name inference — based on
    cwd, or a COMPOSE_PROJECT_NAME in a .env it may not even be reading
    from the right directory — was never guaranteed to be stable or
    unique per client. Two different clients ended up resolving to the
    same project identity, and a `docker compose up -d` meant for ONE
    client recreated/removed a container belonging to a COMPLETELY
    DIFFERENT client sharing the same VPS (a production outage, not a
    hypothetical). Pinning an explicit, per-client-unique project name
    removes that ambiguity entirely regardless of cwd or .env resolution.

    This also matches what deploy_client's own collision-precheck script
    already assumed independently (its `this_project=$(basename
    {remote_dir})` line) — this function just makes that same assumption
    actually true everywhere else too, instead of true in one place and
    hoped-for elsewhere."""
    return remote_dir.rstrip("/").rsplit("/", 1)[-1]


# --------------------------------------------------------------------------
# Health (public /health, no auth)
# --------------------------------------------------------------------------

def check_health(client: dict[str, Any]) -> dict[str, Any]:
    """GET {base_url}/health — the endpoint already shipped in the
    product's backend/api.py. Never raises."""
    base = (client.get("base_url") or "").rstrip("/")
    if not base:
        return {"up": False, "latency_ms": None, "voice_enabled": None,
                "voice_active_sessions": None, "error": "no base_url configured"}
    started = time.monotonic()
    try:
        resp = requests.get(f"{base}/health", timeout=HTTP_TIMEOUT)
        latency_ms = int((time.monotonic() - started) * 1000)
        resp.raise_for_status()
        data = resp.json()
        voice = data.get("voice") or {}
        return {
            "up": data.get("status") == "ok",
            "latency_ms": latency_ms,
            "voice_enabled": voice.get("enabled", False),
            "voice_active_sessions": voice.get("active_sessions", 0),
            "error": None,
        }
    except Exception as e:
        return {"up": False, "latency_ms": None, "voice_enabled": None,
                "voice_active_sessions": None, "error": str(e)}


# --------------------------------------------------------------------------
# Version / infra (SSH, read-only commands only)
# --------------------------------------------------------------------------

def run_ssh(ssh_target: str, remote_command: str, timeout: int = SSH_TIMEOUT) -> tuple[bool, str]:
    """Runs one command over ssh via the OS ssh binary — same mechanism
    deploy.ps1 already uses on Windows; inside the Docker image this is
    openssh-client talking through the mounted ~/.ssh (see Dockerfile /
    docker-compose.local.yml). BatchMode=yes fails fast instead of hanging
    on a password prompt if key auth isn't set up.

    StrictHostKeyChecking=accept-new: a fresh container's ~/.ssh/known_hosts
    doesn't carry the host-key trust decision a person already made once,
    interactively, on their own machine's ssh client — BatchMode then
    refuses the connection outright ("Host key verification failed")
    instead of prompting, since prompting is impossible in batch mode.
    accept-new auto-trusts a host's key the first time this container talks
    to it (still errors loudly on a MISMATCH later, which is the actual
    security property worth keeping — this only skips the "first sight"
    prompt, not ongoing verification)."""
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=accept-new", ssh_target, remote_command],
            capture_output=True, text=True, timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output
    except FileNotFoundError:
        return False, "ssh executable not found on PATH — install OpenSSH client"
    except subprocess.TimeoutExpired:
        return False, f"ssh timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def stream_ssh(ssh_target: str, remote_command: str, result_holder: dict[str, Any], timeout: int = SSH_TIMEOUT):
    """Generator variant of run_ssh — yields each line of the remote
    command's combined stdout/stderr AS IT ARRIVES over the connection,
    instead of blocking until the whole thing finishes and returning it all
    at once. Exists so a long-running remote command (a `docker compose
    build` in particular can run for minutes) can be shown live in a
    terminal-like console, instead of the caller staring at a blank
    spinner the whole time — the exact complaint this was added for.

    A generator's own `return` value isn't reachable from a plain
    `for line in gen():` loop (only via StopIteration.value, which is
    awkward to consume), so instead the caller passes in a plain dict
    up front; this fills in result_holder["ok"]/result_holder["output"]
    (same shape as run_ssh's own (ok, output) tuple) once the process
    exits, readable right after the loop ends. Same defensive contract as
    run_ssh: never raises, and a timeout kills the subprocess rather than
    hanging forever."""
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=accept-new", ssh_target, remote_command],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except FileNotFoundError:
        result_holder["ok"] = False
        result_holder["output"] = "ssh executable not found on PATH — install OpenSSH client"
        return
    except Exception as e:
        result_holder["ok"] = False
        result_holder["output"] = str(e)
        return

    timed_out = False

    def _on_timeout():
        nonlocal timed_out
        timed_out = True
        proc.kill()

    timer = threading.Timer(timeout, _on_timeout)
    timer.start()
    try:
        assert proc.stdout is not None
        for raw_line in iter(proc.stdout.readline, ""):
            lines.append(raw_line)
            yield raw_line.rstrip("\n")
        proc.wait()
    except Exception as e:
        lines.append(str(e))
    finally:
        timer.cancel()

    output = "".join(lines)
    if timed_out:
        extra = f"ssh timed out after {timeout}s"
        result_holder["ok"] = False
        result_holder["output"] = output + "\n" + extra
        yield extra
    else:
        result_holder["ok"] = (proc.returncode == 0)
        result_holder["output"] = output


def _parse_docker_ps(raw: str) -> list[dict[str, Any]]:
    """`docker compose ps --format json` output varies by version: some
    print one JSON object per line, others print a single JSON array.
    Handle both rather than assuming."""
    raw = raw.strip()
    if not raw:
        return []
    containers: list[dict[str, Any]] = []
    try:
        parsed = json.loads(raw)
        rows = parsed if isinstance(parsed, list) else [parsed]
        for row in rows:
            containers.append({
                "name": row.get("Name") or row.get("Service") or "?",
                "state": row.get("State", "?"),
                "health": row.get("Health", ""),
            })
        return containers
    except json.JSONDecodeError:
        pass
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            containers.append({
                "name": row.get("Name") or row.get("Service") or "?",
                "state": row.get("State", "?"),
                "health": row.get("Health", ""),
            })
        except json.JSONDecodeError:
            continue
    return containers


def check_version(client: dict[str, Any]) -> dict[str, Any]:
    """SSH in and run READ-ONLY commands only: git fetch + rev-parse +
    rev-list + log (never pull/reset/checkout), docker compose ps (never
    up/restart). ops-console never mutates a monitored instance.

    "behind" being non-zero only tells you THAT the deployed commit isn't
    origin/master's HEAD — it doesn't tell you whether that gap actually
    matters. behind_commits carries the actual commit subjects (newest
    first, capped at BEHIND_LOG_LIMIT) so you can judge staleness instead
    of just trusting a bare count. infra_risk/infra_files flag when the
    behind range touches docker-compose*.yml, a Dockerfile, or deploy/**
    — a docker-compose/deploy-config change is a different kind of risk
    than an application code change (the exact class of bug that caused a
    live container-name collision once already), so it's surfaced
    distinctly rather than blending in with routine commits."""
    remote_dir = (client.get("remote_dir") or "").rstrip("/") or None
    ssh_target = client.get("ssh_target") or None
    empty_behind = {"commit": None, "behind": None, "behind_commits": [], "containers": [],
                     "infra_risk": False, "infra_files": []}
    if not ssh_target or not remote_dir:
        return {"ok": False, "error": "no ssh_target/remote_dir configured", **empty_behind}
    remote_dir = _shell_remote_dir(remote_dir)
    project = _project_name(remote_dir)

    cmd = (
        f"git -C {remote_dir} fetch -q origin >/dev/null 2>&1; "
        f"echo '{_MARK_COMMIT}'; git -C {remote_dir} rev-parse --short HEAD 2>&1; "
        f"echo '{_MARK_BEHIND}'; git -C {remote_dir} rev-list HEAD..origin/master --count 2>&1; "
        f"echo '{_MARK_BEHIND_LOG}'; git -C {remote_dir} log --oneline HEAD..origin/master 2>&1 | head -{BEHIND_LOG_LIMIT}; "
        f"echo '{_MARK_BEHIND_FILES}'; git -C {remote_dir} diff --name-only HEAD..origin/master 2>&1; "
        f"echo '{_MARK_DOCKER}'; docker compose -p {project} -f {remote_dir}/docker-compose.yml ps --format json 2>/dev/null"
    )
    ok, output = run_ssh(ssh_target, cmd)
    if not ok:
        return {"ok": False, "error": output.strip()[-500:] or "ssh failed", **empty_behind}

    commit, behind, docker_raw = None, None, ""
    behind_commits: list[str] = []
    infra_files: list[str] = []
    try:
        after_commit = output.split(_MARK_COMMIT, 1)[1]
        commit_part, after_behind = after_commit.split(_MARK_BEHIND, 1)
        behind_part, after_log = after_behind.split(_MARK_BEHIND_LOG, 1)
        log_part, after_files = after_log.split(_MARK_BEHIND_FILES, 1)
        files_part, docker_raw = after_files.split(_MARK_DOCKER, 1)
        commit_candidate = commit_part.strip()
        # A real short SHA, not a git error message.
        commit = commit_candidate if commit_candidate and " " not in commit_candidate else None
        behind_str = behind_part.strip()
        behind = int(behind_str) if behind_str.isdigit() else None
        behind_commits = [line.strip() for line in log_part.strip().splitlines() if line.strip()]
        changed_files = [line.strip() for line in files_part.strip().splitlines() if line.strip()]
        infra_files = [f for f in changed_files if _INFRA_PATH_RE.search(f)]
    except (IndexError, ValueError):
        pass

    containers = _parse_docker_ps(docker_raw)

    if commit is None:
        return {"ok": False, "error": "could not parse git output from remote",
                **empty_behind, "containers": containers}
    return {"ok": True, "error": None, "commit": commit, "behind": behind,
            "behind_commits": behind_commits, "containers": containers,
            "infra_risk": bool(infra_files), "infra_files": infra_files}


# --------------------------------------------------------------------------
# Deploy — the one deliberate exception to "read-only". Syncs exactly one
# client to whatever's already on origin/master; never touches anything
# else on the box. See the module docstring for the full safety rationale.
# --------------------------------------------------------------------------

def deploy_client(client: dict[str, Any]) -> dict[str, Any]:
    """git pull --ff-only, then (only if that succeeded) docker compose
    build, then (only if THAT succeeded) a pre-flight container-name
    collision check, then (only if THAT'S clear) docker compose up -d —
    all scoped to this client's own remote_dir/docker-compose.yml. Each
    stage gates the next: a failed pull never triggers a build, a failed
    build never triggers a restart, so a bad build can never take down a
    container that was working. --ff-only means a diverged/dirty tree
    fails loudly instead of silently discarding anything.

    The precheck stage exists because of a real incident: a compose file
    can declare an explicit container_name that collides with a container
    already running under a DIFFERENT compose project on the same host
    (e.g. a shared Caddy container another client's compose file already
    owns) — Docker refuses that at `up` time with a raw "Conflict" error,
    which is safe (nothing gets torn down) but unhelpful. This stage finds
    that collision first and refuses cleanly, with the actual conflicting
    container name and which project owns it, before ever attempting `up`.

    Override-aware: when remote_dir/docker-compose.override.yml exists,
    this is a satellite client sharing a VPS with a primary instance — the
    documented pattern (see each such client's own runbook) is that the
    override remaps its `app` service onto its own port/name and
    deliberately never starts its own `caddy` service, since it shares the
    primary's. Build/up are scoped to just `app` with both files loaded in
    that case — a bare, unscoped `up -d` is exactly what caused the
    original incident this whole precheck/scoping exists to prevent.
    Never raises."""
    ssh_target = client.get("ssh_target") or None
    remote_dir = (client.get("remote_dir") or "").rstrip("/") or None
    empty = {"ok": False, "error": None, "stage": None, "output": "", "commit": None}
    if not ssh_target or not remote_dir:
        return {**empty, "error": "no ssh_target/remote_dir configured", "stage": "config"}
    remote_dir = _shell_remote_dir(remote_dir)
    project = _project_name(remote_dir)

    compose_file = f"{remote_dir}/docker-compose.yml"
    override_file = f"{remote_dir}/docker-compose.override.yml"
    # Resolved inside the remote script itself (compose_files/service_scope
    # shell vars), not decided here in Python — the override's existence is
    # only knowable on the remote host, and this all still needs to be one
    # SSH round trip like every other bundled command in this file.
    #
    # `-p {project}` is pinned explicitly here (see _project_name's
    # docstring for the incident this fixes) — every use of $compose_files
    # below (build/config/up) now carries it for free.
    setup_script = (
        f"if [ -f {override_file} ]; then "
        f"compose_files=\"-p {project} -f {compose_file} -f {override_file}\"; service_scope=\"app\"; "
        f"else compose_files=\"-p {project} -f {compose_file}\"; service_scope=\"\"; fi"
    )
    # Satellite clients (override present) only ever start `app` — the
    # override is what actually determines that service's real
    # container_name, and the base file's caddy service is never touched,
    # so only the override's own declarations matter here. Primary clients
    # (no override) check every declared name in the full resolved config,
    # same as before this fix.
    precheck_script = (
        f"this_project=\"{project}\"; "
        f"if [ -n \"$service_scope\" ]; then "
        f"declared=$(awk -F': *' '/container_name:/{{print $2}}' {override_file}); "
        f"else "
        f"declared=$(docker compose $compose_files config 2>/dev/null | awk -F': *' '/container_name:/{{print $2}}'); "
        f"fi; "
        f"conflict=\"\"; "
        f"for name in $declared; do "
        f"cid=$(docker ps -a -q --filter \"name=^/$name$\" 2>/dev/null); "
        f"if [ -n \"$cid\" ]; then "
        f"owner=$(docker inspect --format '{_INSPECT_PROJECT_LABEL_FMT}' \"$cid\" 2>/dev/null); "
        f"if [ -n \"$owner\" ] && [ \"$owner\" != \"$this_project\" ]; then "
        f"conflict=\"$conflict$name (already owned by compose project '$owner'); \"; "
        f"fi; fi; done; "
        f"if [ -n \"$conflict\" ]; then echo \"COLLISION: $conflict\"; precheck_status=1; "
        f"else echo 'no container-name collisions detected'; precheck_status=0; fi"
    )
    cmd = (
        f"echo '{_MARK_DEPLOY_PULL}'; "
        # Name any local changes up front — a dirty tree makes the ff-only
        # pull refuse (correctly), but git's own "commit your changes or
        # stash them" message doesn't say WHICH file, which cost a real
        # diagnosis round trip (2026-07-20: our own backup-setup's chmod +x
        # dirtied deploy/backup.sh's mode bit on every instance because the
        # repo shipped it non-executable).
        f"git -C {remote_dir} status --porcelain 2>/dev/null | sed 's/^/local change: /'; "
        f"git -C {remote_dir} fetch -q origin 2>&1 && git -C {remote_dir} pull --ff-only origin master 2>&1; "
        f"pull_status=$?; "
        f"{setup_script}; "
        f"echo '{_MARK_DEPLOY_BUILD}'; "
        f"if [ $pull_status -eq 0 ]; then docker compose $compose_files build $service_scope 2>&1; build_status=$?; "
        f"else echo '(skipped — pull failed)'; build_status=1; fi; "
        f"echo '{_MARK_DEPLOY_PRECHECK}'; "
        f"if [ $pull_status -eq 0 ] && [ $build_status -eq 0 ]; then {precheck_script}; "
        f"else echo '(skipped — pull or build failed)'; precheck_status=1; fi; "
        f"echo '{_MARK_DEPLOY_UP}'; "
        f"if [ $pull_status -eq 0 ] && [ $build_status -eq 0 ] && [ $precheck_status -eq 0 ]; then "
        f"docker compose $compose_files up -d $service_scope 2>&1; up_status=$?; "
        f"else echo '(skipped — pull, build, or precheck failed)'; up_status=1; fi; "
        f"echo '{_MARK_DEPLOY_STATUS}'; "
        f"echo \"pull=$pull_status build=$build_status precheck=$precheck_status up=$up_status\"; "
        f"git -C {remote_dir} rev-parse --short HEAD 2>&1"
    )
    ok, output = run_ssh(ssh_target, cmd, timeout=DEPLOY_TIMEOUT)
    if not ok:
        return {**empty, "error": output.strip()[-1000:] or "ssh failed", "stage": "ssh"}

    try:
        pull_text, after_pull = output.split(_MARK_DEPLOY_BUILD, 1)
        pull_text = pull_text.split(_MARK_DEPLOY_PULL, 1)[-1]
        build_text, after_build = after_pull.split(_MARK_DEPLOY_PRECHECK, 1)
        precheck_text, after_precheck = after_build.split(_MARK_DEPLOY_UP, 1)
        up_text, status_text = after_precheck.split(_MARK_DEPLOY_STATUS, 1)
    except (IndexError, ValueError):
        return {**empty, "error": "could not parse deploy output from remote", "stage": "parse",
                "output": output.strip()[-_DEPLOY_OUTPUT_CHAR_LIMIT:]}

    m = re.search(r"pull=(\d+)\s+build=(\d+)\s+precheck=(\d+)\s+up=(\d+)", status_text)
    pull_ok = m and m.group(1) == "0"
    build_ok = m and m.group(2) == "0"
    precheck_ok = m and m.group(3) == "0"
    up_ok = m and m.group(4) == "0"
    # status_text is "\npull=W build=X precheck=Y up=Z\n<commit>\n" — take
    # the last non-empty line rather than the literal last split() element,
    # which is an empty string whenever the remote's output ends in a
    # newline (it always does, since `git rev-parse` itself prints one).
    status_lines = [line.strip() for line in status_text.strip().splitlines() if line.strip()]
    commit_candidate = status_lines[-1] if len(status_lines) > 1 else None
    new_commit = commit_candidate if commit_candidate and " " not in (commit_candidate or "") else None

    combined_output = (
        f"--- git pull ---\n{pull_text.strip()}\n\n"
        f"--- docker compose build ---\n{build_text.strip()}\n\n"
        f"--- pre-flight container-name check ---\n{precheck_text.strip()}\n\n"
        f"--- docker compose up -d ---\n{up_text.strip()}"
    )[-_DEPLOY_OUTPUT_CHAR_LIMIT:]

    if not m:
        return {**empty, "error": "could not determine deploy stage results", "stage": "parse",
                "output": combined_output}
    if not pull_ok:
        return {"ok": False, "error": "git pull failed (see output) — nothing was built or restarted",
                "stage": "pull", "output": combined_output, "commit": new_commit}
    if not build_ok:
        return {"ok": False, "error": "docker compose build failed — container was NOT restarted, old version still running",
                "stage": "build", "output": combined_output, "commit": new_commit}
    if not precheck_ok:
        return {"ok": False, "error": "a container name this deploy would create is already in use by a DIFFERENT "
                                       "compose project — restart was refused before touching anything (see output "
                                       "for which container/project). This is a config problem in the compose file, "
                                       "not something to retry.",
                "stage": "precheck", "output": combined_output, "commit": new_commit}
    if not up_ok:
        return {"ok": False, "error": "docker compose up failed after a successful build — check container logs",
                "stage": "up", "output": combined_output, "commit": new_commit}

    return {"ok": True, "error": None, "stage": "done", "output": combined_output, "commit": new_commit}


def restart_container(ssh_target: str, remote_dir: str) -> dict[str, Any]:
    """Recreates just the app container (override-aware, same scoping as
    deploy_client above) so freshly-written .env values actually take
    effect — Compose only re-reads .env at container creation, not on a
    bare file edit on disk. Never pulls or builds anything; assumes the
    image already exists from an earlier build. Used by the Credentials
    tool's optional "restart after writing" step, and by nothing else."""
    if not ssh_target or not remote_dir:
        return {"ok": False, "error": "no ssh_target/remote_dir configured"}
    shell_dir = _shell_remote_dir(remote_dir.rstrip("/"))
    project = _project_name(shell_dir)
    compose_file = f"{shell_dir}/docker-compose.yml"
    override_file = f"{shell_dir}/docker-compose.override.yml"
    # `-p {project}` pinned explicitly — see _project_name's docstring.
    # This is the exact command that caused a real incident: without a
    # pinned project name, `up -d` for THIS client ended up recreating/
    # removing a container belonging to a completely different client on
    # the same VPS, taking it down.
    cmd = (
        f"if [ -f {override_file} ]; then compose_files=\"-p {project} -f {compose_file} -f {override_file}\"; service_scope=\"app\"; "
        f"else compose_files=\"-p {project} -f {compose_file}\"; service_scope=\"\"; fi; "
        f"docker compose $compose_files up -d $service_scope 2>&1"
    )
    ok, output = run_ssh(ssh_target, cmd, timeout=DEPLOY_TIMEOUT)
    if not ok:
        return {"ok": False, "error": output.strip()[-1000:] or "ssh failed"}
    return {"ok": True, "error": None, "output": output.strip()[-2000:]}


def batch_stream(clients: list[dict[str, Any]], job):
    """Runs `job(client)` for several clients in one run, yielding progress
    events as they happen (a generator, so the API layer can stream them
    live). `job` must return a dict with at least an "ok" bool — its whole
    result is spread into that client's result event:

      {"type": "start",  "name": ...}                   one per client
      {"type": "result", "name": ..., <job's fields>}   one per client
      {"type": "done",   "ok_count": N, "fail_count": M}  always last

    Concurrency model: grouped by ssh_target — clients on DIFFERENT hosts
    run in parallel (capped at MAX_PARALLEL_DEPLOY_HOSTS), but clients
    SHARING a host run strictly one at a time. Written for batch deploys
    (a `docker compose build` is genuinely heavy for a small VPS, and the
    clients most likely to be batch-updated together are exactly the ones
    sharing a box — several simultaneous builds on one host is how a
    routine update becomes an outage) and reused as-is for the batch test
    runner, whose SSH-heavy checks benefit from the same discipline.

    Never raises, and can never hang the stream: the worker wraps job() so
    an unforeseen bug still emits a failed result event for that client
    instead of silently eating it (the generator counts result events to
    know when it's finished)."""
    if not clients:
        yield {"type": "done", "ok_count": 0, "fail_count": 0}
        return

    events: queue.Queue[dict[str, Any]] = queue.Queue()
    groups: dict[str, list[dict[str, Any]]] = {}
    for idx, client in enumerate(clients):
        # A client with no ssh_target still gets its own group so it flows
        # through the job and comes back as a normal per-client config
        # error, never a silent skip.
        key = client.get("ssh_target") or f"__no-ssh-target-{idx}"
        groups.setdefault(key, []).append(client)

    def run_group(group: list[dict[str, Any]]) -> None:
        for client in group:
            name = client.get("name") or client.get("base_url") or "?"
            events.put({"type": "start", "name": name})
            try:
                result = job(client)
            except Exception as exc:  # noqa: BLE001 — see docstring
                result = {"ok": False, "stage": "internal",
                          "error": f"unexpected error ({type(exc).__name__}): {exc}",
                          "output": "", "commit": None}
            events.put({"type": "result", "name": name, **result})

    ok_count = 0
    fail_count = 0
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_DEPLOY_HOSTS, len(groups))) as pool:
        for group in groups.values():
            pool.submit(run_group, group)
        results_seen = 0
        while results_seen < len(clients):
            event = events.get()
            if event.get("type") == "result":
                results_seen += 1
                if event.get("ok"):
                    ok_count += 1
                else:
                    fail_count += 1
            yield event
    yield {"type": "done", "ok_count": ok_count, "fail_count": fail_count}


def deploy_batch_stream(clients: list[dict[str, Any]]):
    """Batch deploy = the generic runner with deploy_client as the job.
    See batch_stream for the event dialect and the per-host concurrency
    rationale; per-client safety is unchanged — every deploy still goes
    through deploy_client() with all of its gates (ff-only pull,
    build-before-restart, the container-name collision precheck)."""
    yield from batch_stream(clients, deploy_client)


# --------------------------------------------------------------------------
# Backup setup — installs/repairs the nightly encrypted-backup systemd timer
# on a client's VPS, then PROVES it by running a real backup immediately and
# checking a fresh archive landed. Born from a real incident (2026-07-20):
# the primary's backup timer had fired nightly for 17 days while producing
# nothing — the unit pointed at a nonexistent /opt path (status=203/EXEC,
# zero journal output), and once that was fixed, a malformed .env line
# killed the script anyway. "Timer active" alone proves nothing; only a
# fresh archive on disk does, which is why the test run is not optional.
# --------------------------------------------------------------------------

BACKUP_SETUP_TIMEOUT = 240  # includes a real backup run (sqlite snapshot + gpg)
_MARK_BK_PRECHECK = "===OPSCONSOLE_BK_PRECHECK==="
_MARK_BK_INSTALL = "===OPSCONSOLE_BK_INSTALL==="
_MARK_BK_RUN = "===OPSCONSOLE_BK_RUN==="
_MARK_BK_STATUS = "===OPSCONSOLE_BK_STATUS==="


def setup_backup(client: dict[str, Any]) -> dict[str, Any]:
    """One SSH round trip: precheck (BACKUP_PASSPHRASE present in .env,
    passwordless sudo available, deploy/backup.sh exists) → install
    correctly-named unit files (<checkout-dir>-backup.service/.timer, paths
    derived from the client's own remote_dir — the naming the smoke suite's
    backup_timer check expects) → enable the timer → run a real backup NOW
    via systemd → verify the archive count in {remote_dir}/backups grew.
    Idempotent: re-running overwrites the units with identical content and
    just takes another backup. Each stage gates the next; a precheck
    failure installs nothing. Requires the SSH user to have passwordless
    sudo (deploy/setup-server.sh on the product side grants exactly this).
    Never raises."""
    ssh_target = client.get("ssh_target") or None
    remote_dir = (client.get("remote_dir") or "").rstrip("/") or None
    empty = {"ok": False, "error": None, "stage": None, "output": "",
             "timer": None, "archives": None, "newest": None, "verified": False}
    if not ssh_target or not remote_dir:
        return {**empty, "error": "no ssh_target/remote_dir configured", "stage": "config"}
    shell_dir = _shell_remote_dir(remote_dir)
    name = _project_name(shell_dir)

    cmd = f'''dir={shell_dir}; name={name}
echo '{_MARK_BK_PRECHECK}'
pass_ok=0; grep -Eq '^BACKUP_PASSPHRASE=..+' "$dir/.env" 2>/dev/null && pass_ok=1
[ $pass_ok -eq 1 ] && echo 'BACKUP_PASSPHRASE: present' || echo 'BACKUP_PASSPHRASE: MISSING or empty in .env'
sudo_ok=0; sudo -n true 2>/dev/null && sudo_ok=1
[ $sudo_ok -eq 1 ] && echo 'passwordless sudo: ok' || echo 'passwordless sudo: NOT available for this SSH user'
script_ok=0; [ -f "$dir/deploy/backup.sh" ] && script_ok=1 && chmod +x "$dir/deploy/backup.sh" 2>/dev/null
[ $script_ok -eq 1 ] && echo 'deploy/backup.sh: present' || echo 'deploy/backup.sh: MISSING'
echo '{_MARK_BK_INSTALL}'
install_ok=0
if [ $pass_ok -eq 1 ] && [ $sudo_ok -eq 1 ] && [ $script_ok -eq 1 ]; then
sudo -n tee /etc/systemd/system/$name-backup.service >/dev/null <<UNITEOF
[Unit]
Description=Encrypted backup of $name
Wants=network-online.target
After=network-online.target docker.service
[Service]
Type=oneshot
WorkingDirectory=$dir
ExecStart=$dir/deploy/backup.sh
StandardOutput=journal
StandardError=journal
UNITEOF
sudo -n tee /etc/systemd/system/$name-backup.timer >/dev/null <<UNITEOF
[Unit]
Description=Nightly encrypted backup for $name
[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=600
Persistent=true
[Install]
WantedBy=timers.target
UNITEOF
sudo -n systemctl daemon-reload 2>&1 && sudo -n systemctl enable --now $name-backup.timer 2>&1 && install_ok=1
echo "unit files written + timer enabled: install_ok=$install_ok"
else echo 'skipped - precheck failed, nothing was installed'; fi
echo '{_MARK_BK_RUN}'
run_ok=0; before=0; after=0; newest=''
if [ $install_ok -eq 1 ]; then
before=$(ls -1 "$dir"/backups/*.tar.gz.gpg 2>/dev/null | wc -l)
sudo -n systemctl start $name-backup.service 2>&1 && run_ok=1
after=$(ls -1 "$dir"/backups/*.tar.gz.gpg 2>/dev/null | wc -l)
newest=$(ls -1t "$dir"/backups/*.tar.gz.gpg 2>/dev/null | head -1)
if [ $run_ok -eq 0 ]; then echo '--- journal tail ---'; sudo -n journalctl -u $name-backup.service -n 20 --no-pager 2>/dev/null; fi
else echo 'skipped'; fi
echo '{_MARK_BK_STATUS}'
timer_state=$(systemctl is-active $name-backup.timer 2>/dev/null)
echo "pass=$pass_ok sudo=$sudo_ok script=$script_ok install=$install_ok run=$run_ok before=$before after=$after timer=$timer_state newest=$newest"
'''
    ok, output = run_ssh(ssh_target, cmd, timeout=BACKUP_SETUP_TIMEOUT)
    if not ok:
        return {**empty, "error": output.strip()[-1000:] or "ssh failed", "stage": "ssh"}

    try:
        after_pre = output.split(_MARK_BK_PRECHECK, 1)[1]
        pre_text, after_install = after_pre.split(_MARK_BK_INSTALL, 1)
        install_text, after_run = after_install.split(_MARK_BK_RUN, 1)
        run_text, status_text = after_run.split(_MARK_BK_STATUS, 1)
    except (IndexError, ValueError):
        return {**empty, "error": "could not parse backup-setup output from remote",
                "stage": "parse", "output": output.strip()[-3000:]}

    m = re.search(r"pass=(\d) sudo=(\d) script=(\d) install=(\d) run=(\d) "
                  r"before=(\d+) after=(\d+) timer=(\S*) newest=(.*)", status_text)
    combined = (f"--- precheck ---\n{pre_text.strip()}\n\n"
                f"--- install ---\n{install_text.strip()}\n\n"
                f"--- test backup run ---\n{run_text.strip()}")[-3000:]
    if not m:
        return {**empty, "error": "could not determine backup-setup stage results",
                "stage": "parse", "output": combined}
    pass_ok, sudo_ok, script_ok, install_ok, run_ok = (m.group(i) == "1" for i in range(1, 6))
    before, after = int(m.group(6)), int(m.group(7))
    timer_state = m.group(8) or None
    newest = m.group(9).strip() or None
    base = {**empty, "output": combined, "timer": timer_state,
            "archives": after, "newest": newest}

    if not pass_ok:
        return {**base, "stage": "passphrase",
                "error": "no BACKUP_PASSPHRASE in this instance's .env — set one via the "
                         "Credentials tool first (and store it in a password manager: without "
                         "it, backups are unrecoverable). Nothing was installed."}
    if not sudo_ok:
        return {**base, "stage": "sudo",
                "error": "the SSH user has no passwordless sudo on this host, which installing "
                         "systemd units requires — run deploy/setup-server.sh's sudoers step "
                         "there, or install the units by hand (DEPLOYMENT.md §7)."}
    if not script_ok:
        return {**base, "stage": "script",
                "error": "deploy/backup.sh not found in this client's checkout — deploy the "
                         "latest code to it first (Updates tab), then re-run this."}
    if not install_ok:
        return {**base, "stage": "install",
                "error": "writing/enabling the systemd units failed — see output."}
    if not run_ok:
        return {**base, "stage": "run",
                "error": "units installed and timer enabled, but the immediate test backup "
                         "FAILED — see output (journal tail included). The timer will keep "
                         "failing the same way nightly until this is fixed."}

    verified = after > before
    return {**base, "ok": True, "stage": "done", "verified": verified,
            "error": None if verified else
            "backup ran cleanly but the archive count did not grow — check the backups "
            "directory manually (a same-second retention prune can cause this)."}


# --------------------------------------------------------------------------
# Caddyfile site discovery — finds EVERYTHING served from a host, not just
# whatever's been manually added to clients.json.
# --------------------------------------------------------------------------

_TOP_BLOCK_OPEN_RE = re.compile(r"^(\S+)\s*\{\s*$")
_VAR_RE = re.compile(r"\{\$(\w+)\}")


def _parse_caddyfile(text: str, env_vars: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Brace-depth-tracking scanner — a plain top-level regex breaks on the
    nested header {}/log {} blocks every real site block contains here.
    Only a line matching a bare `<token> {` at depth 0 starts a new site
    block; the file's leading global-options block (a bare `{ ... }`, no
    token before the brace) is skipped since it has no hostname to key off
    of. `env_vars` resolves Caddy's `{$VARNAME}` substitution (used for the
    main site's hostname, which comes from DOMAIN= in .env rather than
    being written literally in the Caddyfile)."""
    env_vars = env_vars or {}
    depth = 0
    current_host: str | None = None
    current_block_lines: list[str] = []
    blocks: list[tuple[str, str]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            continue
        if depth == 0:
            m = _TOP_BLOCK_OPEN_RE.match(line)
            if m and m.group(1) != "{":
                current_host = m.group(1)
                current_block_lines = []
                depth += line.count("{") - line.count("}")
                continue
            elif line == "{":
                current_host = None  # bare global-options block — skip its contents
                depth += 1
                continue
            else:
                continue  # blank line / comment remainder / stray content
        else:
            if current_host is not None:
                current_block_lines.append(raw_line)
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                if current_host is not None:
                    blocks.append((current_host, "\n".join(current_block_lines)))
                current_host = None
                current_block_lines = []
                depth = 0

    def resolve(hostname: str) -> str:
        m = _VAR_RE.match(hostname)
        if m:
            return env_vars.get(m.group(1), hostname)
        return hostname

    sites = []
    for hostname, block_text in blocks:
        proxy_m = re.search(r"reverse_proxy\s+127\.0\.0\.1:(\d+)", block_text)
        redir_m = re.search(r"redir\s+(\S+)", block_text)
        if proxy_m:
            sites.append({"hostname": resolve(hostname), "type": "proxy",
                          "port": int(proxy_m.group(1)), "target": None})
        elif redir_m:
            sites.append({"hostname": resolve(hostname), "type": "redirect",
                          "port": None, "target": redir_m.group(1)})
        else:
            sites.append({"hostname": resolve(hostname), "type": "unknown",
                          "port": None, "target": None})
    return sites


def discover_sites(host: dict[str, Any]) -> dict[str, Any]:
    """SSH in, read the Caddyfile + the .env vars it substitutes, and
    return every site block found — the full picture of what's served from
    this host, not just whatever's been manually added as a client."""
    ssh_target = host.get("ssh_target") or None
    caddyfile_path = host.get("caddyfile_path") or None
    env_path = host.get("env_path") or None
    if not ssh_target or not caddyfile_path:
        return {"ok": False, "error": "no ssh_target/caddyfile_path configured", "sites": []}

    env_grep = f"grep -E '^(DOMAIN|ACME_EMAIL)=' {env_path} 2>/dev/null" if env_path else "true"
    cmd = f"cat {caddyfile_path} 2>&1; echo '{_MARK_HOST_LOAD}'; {env_grep}"
    # (Reusing _MARK_HOST_LOAD as a cheap separator here is intentional —
    # this SSH call is otherwise standalone from check_host_resources.)
    ok, output = run_ssh(ssh_target, cmd)
    if not ok:
        return {"ok": False, "error": output.strip()[-500:] or "ssh failed", "sites": []}

    try:
        caddyfile_text, env_text = output.split(_MARK_HOST_LOAD, 1)
    except ValueError:
        caddyfile_text, env_text = output, ""

    env_vars: dict[str, str] = {}
    for line in env_text.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env_vars[k.strip()] = v.strip()

    sites = _parse_caddyfile(caddyfile_text, env_vars)
    return {"ok": True, "error": None, "sites": sites}


# --------------------------------------------------------------------------
# Resource usage — overall host (disk/mem/load) and per-client (containers'
# CPU/mem + that client's own /data disk usage).
# --------------------------------------------------------------------------

def check_host_resources(host: dict[str, Any]) -> dict[str, Any]:
    """One SSH round trip: disk (df), memory (free), load average
    (/proc/loadavg), a disk-usage breakdown of the deploy user's home
    directory (du, biggest first — "what's actually taking up space"),
    and Docker's own accounting (docker system df — images/containers/
    volumes/build cache, a common disk hog invisible to a plain `du` on
    project directories). All read-only inspection commands; none of
    these need root, which the `deploy` SSH user likely isn't."""
    ssh_target = host.get("ssh_target") or None
    if not ssh_target:
        return {"ok": False, "error": "no ssh_target configured", "disk": None, "memory": None,
                "load_avg": None, "disk_breakdown": [], "docker_df": None}

    cmd = (
        f"echo '{_MARK_HOST_DISK}'; df -B1 / | tail -1 | awk '{{print $2, $3, $4}}'; "
        f"echo '{_MARK_HOST_MEM}'; free -b | awk '/^Mem:/ {{print $2, $3, $7}}'; "
        f"echo '{_MARK_HOST_LOAD}'; cat /proc/loadavg 2>/dev/null; "
        f"echo '{_MARK_HOST_BREAKDOWN}'; du -sh ~/*/ 2>/dev/null | sort -rh; "
        f"echo '{_MARK_HOST_DOCKER_DF}'; docker system df 2>/dev/null"
    )
    ok, output = run_ssh(ssh_target, cmd)
    if not ok:
        return {"ok": False, "error": output.strip()[-500:] or "ssh failed", "disk": None, "memory": None,
                "load_avg": None, "disk_breakdown": [], "docker_df": None}

    disk, memory, load_avg = None, None, None
    disk_breakdown: list[dict[str, str]] = []
    docker_df: str | None = None
    try:
        after_disk = output.split(_MARK_HOST_DISK, 1)[1]
        disk_line, after_mem = after_disk.split(_MARK_HOST_MEM, 1)
        mem_line, after_load = after_mem.split(_MARK_HOST_LOAD, 1)
        load_line, after_breakdown = after_load.split(_MARK_HOST_BREAKDOWN, 1)
        breakdown_text, docker_df_text = after_breakdown.split(_MARK_HOST_DOCKER_DF, 1)

        d_total, d_used, d_avail = (int(x) for x in disk_line.split())
        disk = {"total": d_total, "used": d_used, "avail": d_avail,
                "pct": round(d_used / d_total * 100, 1) if d_total else 0.0}

        m_total, m_used, m_avail = (int(x) for x in mem_line.split())
        m_used_effective = m_total - m_avail  # "available" already accounts for reclaimable cache
        memory = {"total": m_total, "used": m_used_effective, "avail": m_avail,
                  "pct": round(m_used_effective / m_total * 100, 1) if m_total else 0.0}

        load_parts = load_line.split()
        if len(load_parts) >= 3:
            load_avg = [float(load_parts[0]), float(load_parts[1]), float(load_parts[2])]

        # `du -sh path/` lines look like "1.2G\t/home/deploy/dental-clinic-agent/"
        for bline in breakdown_text.strip().splitlines():
            parts = bline.split(maxsplit=1)
            if len(parts) == 2:
                disk_breakdown.append({"size": parts[0], "path": parts[1].strip()})

        docker_df = docker_df_text.strip() or None
    except (IndexError, ValueError):
        return {"ok": False, "error": "could not parse df/free output from remote",
                "disk": disk, "memory": memory, "load_avg": load_avg,
                "disk_breakdown": disk_breakdown, "docker_df": docker_df}

    return {"ok": True, "error": None, "disk": disk, "memory": memory, "load_avg": load_avg,
            "disk_breakdown": disk_breakdown, "docker_df": docker_df}


def _parse_docker_stats_lines(raw: str) -> list[dict[str, Any]]:
    """`docker stats --format '{{json .}}'` prints one JSON object per
    line; tolerate a single JSON array too, same defensive spirit as
    _parse_docker_ps."""
    raw = raw.strip()
    if not raw:
        return []
    rows: list[dict[str, Any]] = []
    try:
        parsed = json.loads(raw)
        candidates = parsed if isinstance(parsed, list) else [parsed]
        for row in candidates:
            rows.append(_stats_row(row))
        return rows
    except json.JSONDecodeError:
        pass
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(_stats_row(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return rows


def _stats_row(row: dict[str, Any]) -> dict[str, Any]:
    def pct(field: str) -> float | None:
        val = (row.get(field) or "").rstrip("%")
        try:
            return float(val)
        except ValueError:
            return None
    return {
        "name": row.get("Name") or row.get("Container") or "?",
        "cpu_pct": pct("CPUPerc"),
        "mem_pct": pct("MemPerc"),
        "mem_usage": row.get("MemUsage", ""),
    }


def check_client_resources(client: dict[str, Any]) -> dict[str, Any]:
    """Per-container CPU/mem (docker stats, scoped to exactly this
    client's compose project via its own remote_dir — see the plan's note
    on why this is more robust than port->container inference under
    network_mode: host) plus that client's own view of its /data usage
    (run inside its app container, so no host-level root access is
    needed). Only runs when remote_dir/ssh_target are configured."""
    ssh_target = client.get("ssh_target") or None
    remote_dir = (client.get("remote_dir") or "").rstrip("/") or None
    empty = {"ok": False, "error": None, "containers": [], "data_disk_usage": None}
    if not ssh_target or not remote_dir:
        return {**empty, "error": "no ssh_target/remote_dir configured"}
    remote_dir = _shell_remote_dir(remote_dir)
    project = _project_name(remote_dir)

    compose_file = f"{remote_dir}/docker-compose.yml"
    cmd = (
        f"echo '{_MARK_CLIENT_STATS}'; "
        f"cids=$(docker compose -p {project} -f {compose_file} ps -q 2>/dev/null); "
        f"if [ -n \"$cids\" ]; then docker stats --no-stream --format '{{{{json .}}}}' $cids 2>/dev/null; fi; "
        f"echo '{_MARK_CLIENT_DISK}'; "
        f"docker compose -p {project} -f {compose_file} exec -T app du -sh /data 2>/dev/null"
    )
    ok, output = run_ssh(ssh_target, cmd)
    if not ok:
        return {**empty, "error": output.strip()[-500:] or "ssh failed"}

    try:
        after_stats = output.split(_MARK_CLIENT_STATS, 1)[1]
        stats_text, disk_text = after_stats.split(_MARK_CLIENT_DISK, 1)
    except (IndexError, ValueError):
        return {**empty, "error": "could not parse docker stats/du output from remote"}

    containers = _parse_docker_stats_lines(stats_text)
    disk_text = disk_text.strip()
    data_disk_usage = disk_text.split()[0] if disk_text else None

    return {"ok": True, "error": None, "containers": containers, "data_disk_usage": data_disk_usage}


def check_all_hosts(hosts: list[dict[str, Any]], clients: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Bundles resources + discovered sites per host, concurrently across
    hosts (same reasoning as check_all — each host check is a real SSH
    round trip). Cross-references each discovered site's hostname against
    known clients' base_url so the dashboard can label a site as a
    tracked client vs. an unmanaged one found only via the Caddyfile."""
    clients = clients or []
    client_by_hostname = {}
    for c in clients:
        base = (c.get("base_url") or "").rstrip("/")
        hostname = base.split("://", 1)[-1] if base else ""
        if hostname:
            client_by_hostname[hostname.lower()] = c.get("name")

    def _check_one(host: dict[str, Any]) -> dict[str, Any]:
        resources = check_host_resources(host)
        discovered = discover_sites(host)
        sites = []
        for site in discovered.get("sites", []):
            # A Caddy site block's hostname can itself be a comma/space-joined
            # list (e.g. `{$DOMAIN}` resolving to "chat.briers.eu,
            # chat.my-ai-receptionist.com" when one env var covers two
            # hostnames for the same site) — check each individual hostname
            # against known clients rather than the raw combined string,
            # which never matches anything even when one of its parts does.
            raw_hostname = site.get("hostname") or ""
            matched = None
            for single in re.split(r"[,\s]+", raw_hostname):
                single = single.strip().lower()
                if single and single in client_by_hostname:
                    matched = client_by_hostname[single]
                    break
            sites.append({**site, "matched_client": matched})
        return {
            "name": host.get("name") or host.get("ssh_target") or "?",
            "resources": resources,
            "sites_ok": discovered.get("ok", False),
            "sites_error": discovered.get("error"),
            "sites": sites,
        }

    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CHECKS, len(hosts))) as pool:
        return list(pool.map(_check_one, hosts))


# --------------------------------------------------------------------------
# Usage (admin API, reuses the already-shipped /admin/metrics endpoint)
# --------------------------------------------------------------------------

def _month_bounds(now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now()
    start = datetime(now.year, now.month, 1)
    return start.isoformat(), now.isoformat()



def _admin_get_json(client: dict[str, Any], path: str, params: dict[str, Any]) -> Any:
    """GET an admin-API path on a monitored instance and parse the JSON.

    Two transports:
    - default: plain HTTPS to {base_url}{path} with the X-Admin-Token header —
      works while the instance's admin surface is publicly reachable.
    - "admin_via_ssh": true (+ "admin_local_port"): run curl ON the VPS via
      run_ssh against the instance's loopback port. This is the only way in
      once an instance sets ADMIN_TUNNEL_ONLY (its admin surface then 404s
      through the reverse proxy), and is preferable anyway — the admin token
      never crosses the public internet. Uses the same mounted ~/.ssh key as
      every other SSH check in this file.
    Raises on any transport or parse failure; callers already catch broadly.
    """
    token = _real_token(client)
    # Managed instances (product Phase 6): the operator token, when this
    # client has one recorded, rides along on EVERY admin call — GETs need
    # it too (managed instances redact SMTP/Twilio secrets from non-operator
    # reads). Harmless on unmanaged instances, which ignore the header.
    op_token = client.get("operator_token") or ""
    if client.get("admin_via_ssh"):
        port = client.get("admin_local_port")
        ssh_target = client.get("ssh_target") or ""
        if not port:
            raise RuntimeError("admin_via_ssh is set but admin_local_port is missing")
        if not ssh_target:
            raise RuntimeError("admin_via_ssh is set but ssh_target is missing")
        url = f"http://127.0.0.1:{int(port)}{path}?{urlencode(params)}"
        cmd = ("curl -fsS -m 15 -H " + shlex.quote(f"X-Admin-Token: {token}")
               + (" -H " + shlex.quote(f"X-Operator-Token: {op_token}") if op_token else "")
               + " " + shlex.quote(url))
        ok, out = run_ssh(ssh_target, cmd, timeout=SSH_TIMEOUT + 10)
        if not ok:
            raise RuntimeError(f"ssh admin fetch failed: {out[:300]}")
        return json.loads(out)
    base = (client.get("base_url") or "").rstrip("/")
    headers = {"X-Admin-Token": token}
    if op_token:
        headers["X-Operator-Token"] = op_token
    resp = requests.get(f"{base}{path}", params=params,
                        headers=headers, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _admin_put_json(client: dict[str, Any], path: str, payload: dict[str, Any]) -> Any:
    """PUT a JSON body to an instance's admin API — the write twin of
    _admin_get_json, same two transports (plain HTTPS with X-Admin-Token, or
    curl over the SSH loopback for ADMIN_TUNNEL_ONLY instances). Used by the
    ledger's plan push (Phase 5) and, later, the config manager (Phase 7):
    writes go through the instance's OWN validated endpoint (PUT
    /admin/config), never raw file edits. Raises on transport/parse failure;
    callers catch broadly."""
    token = _real_token(client)
    # Operator token (managed mode, product Phase 6): required for writes
    # touching MANAGED_CONFIG_FIELDS on a managed instance; ignored by
    # unmanaged ones. Sent whenever the client record has one.
    op_token = client.get("operator_token") or ""
    body = json.dumps(payload)
    if client.get("admin_via_ssh"):
        port = client.get("admin_local_port")
        ssh_target = client.get("ssh_target") or ""
        if not port:
            raise RuntimeError("admin_via_ssh is set but admin_local_port is missing")
        if not ssh_target:
            raise RuntimeError("admin_via_ssh is set but ssh_target is missing")
        url = f"http://127.0.0.1:{int(port)}{path}"
        cmd = ("curl -fsS -m 20 -X PUT -H " + shlex.quote(f"X-Admin-Token: {token}")
               + (" -H " + shlex.quote(f"X-Operator-Token: {op_token}") if op_token else "")
               + " -H " + shlex.quote("Content-Type: application/json")
               + " --data " + shlex.quote(body) + " " + shlex.quote(url))
        ok, out = run_ssh(ssh_target, cmd, timeout=SSH_TIMEOUT + 15)
        if not ok:
            raise RuntimeError(f"ssh admin put failed: {out[:300]}")
        return json.loads(out)
    base = (client.get("base_url") or "").rstrip("/")
    headers = {"X-Admin-Token": token, "Content-Type": "application/json"}
    if op_token:
        headers["X-Operator-Token"] = op_token
    resp = requests.put(f"{base}{path}", data=body,
                        headers=headers,
                        timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def check_usage(client: dict[str, Any], start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """GET {base_url}/admin/metrics — the token-usage endpoint that already
    ships in the product's backend/admin.py (get_metrics). Nothing new on
    the monitored instance's side; this just consumes it. Defaults to
    month-to-date."""
    base = (client.get("base_url") or "").rstrip("/")
    token = _real_token(client)
    empty = {"ok": False, "error": None, "chats": 0, "input_tokens": 0,
             "cached_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if not base:
        return {**empty, "error": "no base_url configured"}
    if not token:
        reason = ("admin_token is still the example placeholder — fetch or set the real one"
                   if client.get("admin_token") == PLACEHOLDER_ADMIN_TOKEN
                   else "no admin_token configured")
        return {**empty, "error": reason}

    if start is None or end is None:
        start, end = _month_bounds()

    try:
        data = _admin_get_json(client, "/admin/metrics", {"start": start, "end": end})
        overall = data.get("overall") or {}
        return {
            "ok": True,
            "error": None,
            "chats": len(data.get("chats") or []),
            "input_tokens": overall.get("input", 0),
            "cached_tokens": overall.get("cached", 0),
            "output_tokens": overall.get("output", 0),
            "total_tokens": overall.get("total", 0),
        }
    except Exception as e:
        return {**empty, "error": str(e)}


# --------------------------------------------------------------------------
# Interaction funnel + "minutes saved" (admin API, reuses /admin/audit)
# --------------------------------------------------------------------------

def _cfg_int(client: dict[str, Any], key: str, default: int) -> int:
    """Like dict.get(key, default), but also treats an explicitly-stored
    None the same as "missing" — the API's ClientIn model always includes
    these optional keys (as None when unset), so a plain .get(key, default)
    would never fall back to the default once a client's been saved via
    the API even once."""
    value = client.get(key)
    return default if value is None else value


def check_interactions(client: dict[str, Any], start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Best-effort breakdown of what people have actually been asking the
    bot to do this month — bookings, reschedules, cancellations, callbacks,
    registrations — read from the product's own audit ledger
    (GET {base_url}/admin/audit), plus a rough "receptionist minutes saved"
    estimate from configurable per-task-type minute assumptions.

    Deliberately tolerant of a couple of reasonable field-name variants
    for the action/timestamp fields on each row (this tool only consumes
    /admin/audit, it doesn't own that schema) — if NO row can be parsed at
    all, this reports an error instead of silently showing all-zero counts,
    so "nothing happened this month" and "we couldn't read the log" never
    look the same."""
    base = (client.get("base_url") or "").rstrip("/")
    token = _real_token(client)
    empty = {"ok": False, "error": None, "bookings": 0, "reschedules": 0,
             "cancellations": 0, "callbacks": 0, "registrations": 0,
             "other": 0, "minutes_saved": 0}
    if not base:
        return {**empty, "error": "no base_url configured"}
    if not token:
        reason = ("admin_token is still the example placeholder — fetch or set the real one"
                   if client.get("admin_token") == PLACEHOLDER_ADMIN_TOKEN
                   else "no admin_token configured")
        return {**empty, "error": reason}

    if start is None or end is None:
        start, end = _month_bounds()
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        return {**empty, "error": "invalid start/end"}

    try:
        data = _admin_get_json(client, "/admin/audit", {"limit": 1000, "page": 1})
        rows = data.get("rows") if isinstance(data, dict) else None
        if rows is None:
            rows = data if isinstance(data, list) else []
    except Exception as e:
        return {**empty, "error": str(e)}

    counts = {"bookings": 0, "reschedules": 0, "cancellations": 0,
              "callbacks": 0, "registrations": 0, "other": 0}
    recognized_any_row = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or row.get("event") or "").lower()
        ts_raw = row.get("created_at") or row.get("timestamp") or row.get("ts") or row.get("time")
        if not action or not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            if ts.tzinfo is not None:
                ts = ts.astimezone().replace(tzinfo=None)
        except ValueError:
            continue
        recognized_any_row = True
        if not (start_dt <= ts <= end_dt):
            continue
        if "cancel" in action:
            counts["cancellations"] += 1
        elif "reschedule" in action:
            counts["reschedules"] += 1
        elif "book" in action:
            counts["bookings"] += 1
        elif "callback" in action:
            counts["callbacks"] += 1
        elif "register" in action:
            counts["registrations"] += 1
        else:
            counts["other"] += 1

    if rows and not recognized_any_row:
        return {**empty, **counts,
                "error": "audit rows returned but no action/timestamp fields were recognized "
                         "— check /admin/audit's response shape"}

    minutes_saved = (
        counts["bookings"] * _cfg_int(client, "minutes_per_booking", DEFAULT_MINUTES_PER_BOOKING)
        + counts["reschedules"] * _cfg_int(client, "minutes_per_reschedule", DEFAULT_MINUTES_PER_RESCHEDULE)
        + counts["cancellations"] * _cfg_int(client, "minutes_per_cancellation", DEFAULT_MINUTES_PER_CANCELLATION)
        + counts["callbacks"] * _cfg_int(client, "minutes_per_callback", DEFAULT_MINUTES_PER_CALLBACK)
    )

    return {"ok": True, "error": None, **counts, "minutes_saved": minutes_saved}


def compute_cost_estimate(usage: dict[str, Any], client: dict[str, Any]) -> dict[str, Any]:
    """Turns already-fetched token usage into a rough $/month figure, using
    per-1K-token rates the user configures per client (this tool has no
    way to know a clinic's actual LLM provider/pricing on its own, so it
    never guesses — `configured: False` means no rate has been set and the
    dashboard should just hide the number rather than show a misleading
    $0.00)."""
    if not usage.get("ok"):
        return {"ok": False, "error": usage.get("error") or "usage unavailable",
                "estimated_usd": None, "configured": False}

    rate_in = client.get("cost_per_1k_input_tokens") or 0.0
    rate_cached = client.get("cost_per_1k_cached_tokens") or 0.0
    rate_out = client.get("cost_per_1k_output_tokens") or 0.0
    if not (rate_in or rate_cached or rate_out):
        return {"ok": True, "error": None, "estimated_usd": None, "configured": False}

    cost = (
        usage.get("input_tokens", 0) / 1000 * rate_in
        + usage.get("cached_tokens", 0) / 1000 * rate_cached
        + usage.get("output_tokens", 0) / 1000 * rate_out
    )
    return {"ok": True, "error": None, "estimated_usd": round(cost, 4), "configured": True}


# --------------------------------------------------------------------------
# Bundled per-client check
# --------------------------------------------------------------------------

def check_client(client: dict[str, Any]) -> dict[str, Any]:
    """Bundles health + version + usage for one client into a single status
    dict — the one function the API (or a future cron/alerting mode)
    calls per client."""
    health = check_health(client)
    version = check_version(client)
    usage = check_usage(client)
    resources = check_client_resources(client)
    interactions = check_interactions(client)
    cost = compute_cost_estimate(usage, client)

    # Phase 5 fix: the ledger's plan store is the quota authority now — the
    # legacy clients.json field only ever seeds it, so reading clients.json
    # here left the dashboard's "over quota" warning permanently dead once
    # plans migrated. Lazy import: ledger imports core at module level, so a
    # top-level import here would be circular.
    try:
        from backend import ledger as _ledger
        _plan = _ledger.get_plan(client.get("name", "")) or {}
        quota = _plan.get("allowance_tokens") or client.get("monthly_token_quota") or 0
    except Exception:
        quota = client.get("monthly_token_quota") or 0
    over_quota = bool(quota) and usage.get("ok") and usage.get("total_tokens", 0) > quota

    if not health.get("up"):
        overall_status = "down"
    elif over_quota or (version.get("ok") and (version.get("behind") or 0) > 0):
        overall_status = "warning"
    elif not version.get("ok") or not usage.get("ok"):
        overall_status = "warning"
    else:
        overall_status = "ok"

    return {
        "name": client.get("name") or client.get("base_url") or "?",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "status": overall_status,
        "health": health,
        "version": version,
        "usage": usage,
        "resources": resources,
        "interactions": interactions,
        "cost": cost,
        "quota": quota,
        "over_quota": over_quota,
        "client": client,  # lets a caller re-check/edit without a second config lookup
    }


def check_all(clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Checks every client concurrently (each check involves real network/SSH
    round trips — sequential would mean N clients paying full latency each,
    which gets slow fast). Order of the input list is preserved in the
    output regardless of which check finishes first."""
    if not clients:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CHECKS, len(clients))) as pool:
        return list(pool.map(check_client, clients))


# --------------------------------------------------------------------------
# Headless CLI mode — verification path, and the seed of a future
# unattended/alerting mode.
# --------------------------------------------------------------------------

def _print_table(results: list[dict[str, Any]]) -> None:
    for r in results:
        h, v, u = r["health"], r["version"], r["usage"]
        print(f"\n{r['name']}  [{r['status'].upper()}]")

        latency = f" ({h['latency_ms']}ms)" if h.get("latency_ms") is not None else ""
        voice = f"  voice_active={h['voice_active_sessions']}" if h.get("voice_enabled") else ""
        err = f"  ERROR: {h['error']}" if h.get("error") else ""
        print(f"  health : {'UP' if h['up'] else 'DOWN'}{latency}{voice}{err}")

        if v.get("ok"):
            behind = v.get("behind")
            behind_str = f", {behind} commit(s) behind origin/master" if behind else ", up to date"
            print(f"  version: {v['commit']}{behind_str}")
            for line in v.get("behind_commits") or []:
                print(f"    - {line}")
            for c in v.get("containers") or []:
                print(f"    container {c['name']}: {c['state']} {c.get('health', '')}".rstrip())
        else:
            print(f"  version: unknown ({v.get('error')})")

        if u.get("ok"):
            over = "  *** OVER QUOTA ***" if r["over_quota"] else ""
            quota_str = f" / quota {r['quota']:,}" if r["quota"] else ""
            print(f"  usage  : {u['chats']} chats, {u['total_tokens']:,} tokens this month{quota_str}{over}")
        else:
            print(f"  usage  : unknown ({u.get('error')})")

        res = r.get("resources") or {}
        if res.get("ok"):
            containers_str = ", ".join(
                f"{c['name']} (cpu {c['cpu_pct']}%, mem {c['mem_pct']}%)" for c in res.get("containers") or []
            ) or "no containers found"
            print(f"  resources: {containers_str}")
            if res.get("data_disk_usage"):
                print(f"    /data usage: {res['data_disk_usage']}")
        elif res.get("error"):
            print(f"  resources: unknown ({res['error']})")

        i = r.get("interactions") or {}
        if i.get("ok"):
            print(f"  interactions: {i['bookings']} booked, {i['reschedules']} rescheduled, "
                  f"{i['cancellations']} cancelled, {i['callbacks']} callbacks, "
                  f"{i['registrations']} registrations  (~{i['minutes_saved']} min saved)")
        elif i.get("error"):
            print(f"  interactions: unknown ({i['error']})")

        c = r.get("cost") or {}
        if c.get("ok") and c.get("configured"):
            print(f"  est. cost: ${c['estimated_usd']:.2f} this month")

        up = r.get("uptime") or {}
        if up.get("uptime_7d_pct") is not None:
            lat = f", p95 {up['latency_p95_ms']}ms" if up.get("latency_p95_ms") is not None else ""
            print(f"  uptime (7d): {up['uptime_7d_pct']}% ({up['samples_7d']} samples{lat})")
    print()


def _print_host_table(host_results: list[dict[str, Any]]) -> None:
    for hr in host_results:
        print(f"\n=== {hr['name']} ===")
        res = hr.get("resources") or {}
        if res.get("ok"):
            d, m = res["disk"], res["memory"]
            load = res.get("load_avg")
            load_str = f", load {load[0]}/{load[1]}/{load[2]}" if load else ""
            print(f"  disk  : {d['pct']}% used ({d['used']:,} / {d['total']:,} bytes)")
            print(f"  memory: {m['pct']}% used ({m['used']:,} / {m['total']:,} bytes){load_str}")
        else:
            print(f"  resources: unknown ({res.get('error')})")

        if hr.get("sites_ok"):
            print("  sites (from Caddyfile):")
            for s in hr.get("sites") or []:
                label = s.get("matched_client") or "unmanaged"
                if s["type"] == "proxy":
                    print(f"    {s['hostname']} -> 127.0.0.1:{s['port']}  [{label}]")
                elif s["type"] == "redirect":
                    print(f"    {s['hostname']} -> redirect to {s['target']}  [{label}]")
                else:
                    print(f"    {s['hostname']} -> (unrecognized block)  [{label}]")
        else:
            print(f"  sites : unknown ({hr.get('sites_error')})")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Headless client status check (no server) — sanity-check "
                     "the config before running the dashboard.")
    parser.add_argument("--config", default=str(cfg.DEFAULT_CONFIG_PATH), help="Path to clients.json")
    parser.add_argument("--check", action="store_true", help="Run the check (present for symmetry/clarity; default action)")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a table")
    args = parser.parse_args(argv)

    clients = cfg.load_clients(args.config)
    hosts = cfg.load_hosts(args.config)
    if not clients and not hosts:
        print(f"No clients or hosts configured in {args.config}", file=sys.stderr)
        return 1

    results = check_all(clients) if clients else []
    host_results = check_all_hosts(hosts, clients) if hosts else []

    if args.json:
        print(json.dumps({"clients": results, "hosts": host_results}, indent=2))
    else:
        if host_results:
            _print_host_table(host_results)
        if results:
            _print_table(results)

    return 1 if any(r["status"] == "down" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# Onboarding v2 additions (docs/ONBOARDING_V2_PLAN.md)
# ---------------------------------------------------------------------------

def vps_public_ip(ssh_target: str) -> dict[str, Any]:
    """The VPS's own public IPv4, asked from the VPS itself (so NAT/proxies
    on the operator's side can't skew it)."""
    ok, out = run_ssh(ssh_target, "curl -4fsS --max-time 8 ifconfig.me 2>/dev/null || hostname -I")
    ip = (out or "").strip().split()[0] if out and out.strip() else ""
    if not ok or not ip:
        return {"ok": False, "error": out.strip()[-200:] or "ssh failed", "ip": None}
    return {"ok": True, "error": None, "ip": ip}


def check_dns(ssh_target: str, hostname: str) -> dict[str, Any]:
    """DNS precheck (plan 1A): does <hostname> resolve — as seen FROM the
    VPS, which is whose resolver Let's Encrypt effectively needs to agree
    with — to the VPS's own public IP? Gate Caddy wiring on this instead of
    letting cert issuance fail in ways that read like script bugs."""
    if not hostname or "/" in hostname or " " in hostname:
        return {"ok": False, "error": f"invalid hostname {hostname!r}", "match": False,
                "expected": None, "resolved": []}
    ip_res = vps_public_ip(ssh_target)
    if not ip_res["ok"]:
        return {"ok": False, "error": f"couldn't determine VPS IP: {ip_res['error']}",
                "match": False, "expected": None, "resolved": []}
    expected = ip_res["ip"]
    ok, out = run_ssh(ssh_target,
                      f"getent ahostsv4 {hostname} 2>/dev/null | awk '{{print $1}}' | sort -u")
    resolved = sorted({line.strip() for line in (out or "").splitlines() if line.strip()}) if ok else []
    match = expected in resolved
    error = None
    if not resolved:
        error = f"{hostname} does not resolve yet (create an A record -> {expected})"
    elif not match:
        error = f"{hostname} resolves to {resolved}, expected {expected}"
    return {"ok": bool(resolved) and match, "error": error, "match": match,
            "expected": expected, "resolved": resolved}


def recreate_app(ssh_target: str, remote_dir: str) -> dict[str, Any]:
    """Recreate (NOT restart) a client's app container so a changed .env is
    actually re-read — container env is fixed at CREATE time; a plain
    `docker restart` silently keeps the old environment (bit us live,
    2026-07-19). `cd` into the checkout so compose finds the base +
    override files, and pin -p as always (see the project-name incident in
    README)."""
    if not ssh_target or not remote_dir:
        return {"ok": False, "error": "no ssh_target/remote_dir configured"}
    shell_dir = _shell_remote_dir(remote_dir.rstrip("/"))
    project = _project_name(remote_dir)
    cmd = (f"cd {shell_dir} && docker compose -p {project} up -d app 2>&1 "
           f"&& echo ===RECREATE_OK===")
    ok, out = run_ssh(ssh_target, cmd, timeout=120)
    if not ok or "===RECREATE_OK===" not in out:
        return {"ok": False, "error": out.strip()[-500:] or "ssh failed"}
    return {"ok": True, "error": None}


def reseed_client(ssh_target: str, remote_dir: str) -> dict[str, Any]:
    """Nuke-and-reseed a single client instance's database over SSH.

    Runs `python -m backend.db.seed --demo` inside the running `app`
    container — the app's OWN seed module (single source of truth). --demo
    WIPES the entire database (conversations, appointments, clients, callbacks,
    everything) and rebuilds it as a populated SHOWCASE: starter consultants/
    services PLUS generated demo clients, conversations, appointments and
    callbacks, so the instance looks alive again for the next demo. Intended
    for shared demo boxes that accumulate junk as people play with them and
    need a regular repopulating wipe. Destructive; the HTTP layer gates it
    behind a type-the-name confirmation.

    (Note: --demo, not --reset. --reset wipes to a BARE base with no demo
    clients/chats — that left the demo box looking empty. --demo is the
    "reset to a full-looking demo" the button is actually for.)

    Restarts the `app` container afterwards because active chat sessions live
    in memory (dev mode) and would otherwise keep showing a stale live-tail
    after the persisted rows are gone.

    Pins `-p {project}` on every compose call for the same reason recreate_app
    does — see _project_name's docstring (a shared-VPS cross-client incident).
    Never raises: SSH/exec failures come back in the "error" field."""
    if not ssh_target or not remote_dir:
        return {"ok": False, "error": "no ssh_target/remote_dir configured"}
    shell_dir = _shell_remote_dir(remote_dir.rstrip("/"))
    project = _project_name(shell_dir)
    cmd = (f"cd {shell_dir} && "
           f"docker compose -p {project} exec -T app "
           f"python -m backend.db.seed --demo 2>&1 "
           f"&& docker compose -p {project} restart app 2>&1 "
           f"&& echo ===RESEED_OK===")
    ok, out = run_ssh(ssh_target, cmd, timeout=DEPLOY_TIMEOUT)
    if not ok or "===RESEED_OK===" not in out:
        return {"ok": False, "error": out.strip()[-1000:] or "ssh failed"}
    return {"ok": True, "error": None, "output": out.strip()[-2000:]}
