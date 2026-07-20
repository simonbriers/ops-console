"""Provisions a brand-new client instance end to end over SSH: clone the
repo, generate its override file + a starter site_config.yaml + a bootable
placeholder .env, first build/boot, swap the starter config into the data
volume, wire it into the shared Caddy, and register it in clients.json.

Exists because standing up a new client by hand (Clinica Valor's and
PrimeConnect AI's onboardings) meant a human working through a 10+ step
runbook over SSH every single time — slow, and every manual step is a place
to typo a hostname or forget `--build`. This automates everything that's
purely mechanical (clone, port picking, file templating, build/boot,
Caddy wiring, registration) and deliberately leaves the one genuinely
judgment-requiring step — which real secrets go in .env, and where they
come from — to the existing Credentials tool (env_tool.py) rather than
guessing at it here. The starter site_config.yaml is intentionally generic
placeholder content, editable afterward in the admin Configuracion tab (a
live edit, no redeploy) rather than something this tool has any business
inventing on your behalf.

Every stage gates the next, same pattern as core.deploy_client: a failed
clone never attempts a build, a failed build never attempts up, etc. Never
raises — every function returns a dict with "ok"/"error"/"stage".
"""
from __future__ import annotations

import base64
import re
import secrets
from typing import Any

from backend.core import DEPLOY_TIMEOUT, _shell_remote_dir, run_ssh, stream_ssh

_DEPLOY_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

_MARK_EXISTS = "===OPSCONSOLE_NC_EXISTS==="
_MARK_CLONE = "===OPSCONSOLE_NC_CLONE==="
_MARK_FILES = "===OPSCONSOLE_NC_FILES==="
_MARK_BUILD = "===OPSCONSOLE_NC_BUILD==="
_MARK_UP = "===OPSCONSOLE_NC_UP==="
_MARK_SWAP = "===OPSCONSOLE_NC_SWAP==="
_MARK_STATUS = "===OPSCONSOLE_NC_STATUS==="
_OUTPUT_CHAR_LIMIT = 4000


def validate_deploy_name(deploy_name: str) -> str | None:
    """Returns an error message if the name is unsafe to use as a directory
    name, container name, and Compose project name all at once — None if
    it's fine. Lowercase letters/digits/hyphens only, starting with a
    letter, matching the convention every existing client already uses
    (clinica-valor, primeconnect-ai)."""
    if not deploy_name or not _DEPLOY_NAME_RE.match(deploy_name):
        return ("deploy name must be lowercase letters, digits, and hyphens only, "
                "starting with a letter (e.g. 'acme-dental') — this becomes a "
                "directory name, container name, and Compose project name all at once")
    return None


def next_free_port(ssh_target: str) -> dict[str, Any]:
    """Scans every sibling checkout's docker-compose.override.yml for its
    assigned port and returns one past the highest in use — 8001 if none
    exist yet (8000 is always the primary, via network_mode: host, so the
    first satellite starts at 8001, matching every existing client's
    onboarding).

    Matches two patterns, on purpose. Older satellite checkouts (anything
    provisioned before the voice-networking fix — see _generate_override_yaml's
    docstring for the incident that prompted it) publish a loopback port
    mapping under plain bridge networking (127.0.0.1:PORT:8000); every
    override generated since that fix runs on host networking instead, which
    has no ports: mapping at all, so it carries an explicit
    '# opsconsole_assigned_port: PORT' marker comment for exactly this scan
    to find. Matching both means this correctly avoids colliding with every
    client on the box regardless of which era it was provisioned in, without
    needing to touch already-live older checkouts."""
    cmd = (
        # Two sources, unioned: (a) sibling override files (both the old
        # loopback-publish pattern and the newer assigned-port marker), and
        # (b) what is ACTUALLY LISTENING on 8xxx right now (ss) — added
        # 2026-07-19 after a hand-crafted override matched neither file
        # pattern and the scan handed out PrimeConnect AI's live port 8002
        # to a new client. The kernel cannot be fooled by file formats.
        r"{ grep -rhoE '(127\.0\.0\.1:[0-9]+:8000|opsconsole_assigned_port: *[0-9]+)' "
        r"$HOME/*/docker-compose.override.yml 2>/dev/null; "
        r"ss -tln 2>/dev/null | grep -oE '[:.](8[0-9]{3}) ' ; } || true"
    )
    ok, output = run_ssh(ssh_target, cmd)
    if not ok:
        return {"ok": False, "error": output.strip()[-300:] or "ssh failed", "port": None}
    ports = [int(m) for m in re.findall(r":(\d+):8000", output)]
    ports += [int(m) for m in re.findall(r"opsconsole_assigned_port: *(\d+)", output)]
    # live listeners from the ss half of the command (":8002 " / ".8002 ")
    ports += [int(m) for m in re.findall(r"[:.](8\d{3}) ", output)]
    # First FREE port in the chatbot range (8001-8099; 8000 is always the
    # primary, 8100+ belongs to other things on the box, e.g. the marketing
    # site). "max+1" would let one unrelated high listener (ss now sees
    # everything) push allocations out of the range for no reason.
    used = set(ports)
    free = next((p for p in range(8001, 8100) if p not in used), None)
    if free is None:
        return {"ok": False, "error": "no free port left in 8001-8099", "port": None}
    return {"ok": True, "error": None, "port": free}


def get_repo_origin(ssh_target: str, template_remote_dir: str) -> dict[str, Any]:
    """git remote get-url origin from an already-working checkout, so the
    new client is cloned from the same place — reuses the deploy key
    already trusted on this server, no new credential needed."""
    shell_dir = _shell_remote_dir(template_remote_dir.rstrip("/"))
    cmd = f"git -C {shell_dir} remote get-url origin"
    ok, output = run_ssh(ssh_target, cmd)
    origin = output.strip().splitlines()[-1].strip() if output.strip() else ""
    if not ok or not origin:
        return {"ok": False, "error": output.strip()[-300:] or "no output", "origin": None}
    return {"ok": True, "error": None, "origin": origin}


def _generate_override_yaml(container_name: str, port: int) -> str:
    """Runs every satellite client on host networking, same as the primary
    — not the simpler-looking bridge+port-mapping this wizard used to
    generate. Reason (a real incident, not a hypothetical): PrimeConnect
    AI's launch turned on voice and the call "rang but never answered" —
    the WebRTC offer/answer signaling succeeded fine over HTTPS, but the
    actual audio never arrived. Root cause was plain bridge networking: the
    real-time audio media is UDP, negotiated ad hoc via ICE, and it never
    goes through Caddy's HTTP reverse proxy the way the rest of the site
    does — bridge-mode Docker NAT gave the browser no path to reach the
    container's UDP media port at all, so every voice call timed out
    identically regardless of which client it was. Host networking gives
    the container the VPS's real public IP directly, and the box's
    firewall already opens the necessary ephemeral UDP range for exactly
    this (`ufw status` shows 49152:65535/udp allowed — confirmed safe
    before relying on it: the app's own HTTP port is NOT in ufw's allow
    list, so it stays unreachable from the internet directly, same
    protection the primary already relies on today).

    This applies unconditionally, whether or not voice is even turned on
    for this client yet — it costs nothing to be voice-ready by default
    (site_config.yaml's `voice.enabled` still fully gates whether the
    feature does anything), and it means the next client that turns voice
    on doesn't need this whole investigation repeated. See
    docs/VOICE_NETWORKING.md for the full incident writeup and the
    "turning voice on for a client" runbook.

    Host networking shares the box's real network namespace, so uvicorn
    can't default to port 8000 the way the primary does — that's already
    bound by the primary's own container. `command:` pins this client's own
    uvicorn to its assigned port instead (an override, not a Dockerfile
    edit — the image stays byte-identical across every client); the
    healthcheck is overridden to match, since the Dockerfile's own
    HEALTHCHECK is baked in against the old hardcoded port 8000.

    The `# opsconsole_assigned_port` comment isn't read by Docker at all —
    it exists purely so next_free_port() can find this client's port by a
    stable marker, since host networking has no `ports:` mapping to scan
    for the way bridge mode did."""
    return (
        "# Generated by ops-console's New Client wizard — same pattern as every\n"
        "# other satellite checkout on this box. Runs on host networking, same as\n"
        "# the primary, so WebRTC voice call media (UDP) can actually reach this\n"
        "# container — plain bridge networking has no path for that traffic at all\n"
        "# (see docs/VOICE_NETWORKING.md for the incident this fixes: a voice call\n"
        "# that rings but never answers). Pinned to this client's own assigned port\n"
        "# via a command + healthcheck override so it doesn't collide with the\n"
        "# primary's port 8000. Names its container distinctly and deliberately\n"
        "# never starts its own `caddy` service — the primary's shared Caddy\n"
        "# handles TLS/routing for this instance too.\n"
        f"# opsconsole_assigned_port: {port}\n"
        "services:\n"
        "  app:\n"
        f"    container_name: {container_name}\n"
        "    network_mode: host\n"
        f'    command: ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "{port}", "--workers", "1"]\n'
        "    healthcheck:\n"
        f'      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:{port}/health"]\n'
        "      interval: 30s\n"
        "      timeout: 5s\n"
        "      retries: 3\n"
        "      start_period: 15s\n"
    )


def _generate_starter_site_config(display_name: str, medical: bool = False) -> str:
    """A minimal, valid site_config.yaml — passes deploy/validate_site_config.py's
    checks (required top-level keys, non-empty consultants/services, quoted
    hours) with obviously-placeholder content, meant to be replaced via the
    admin Configuracion tab (a live edit, no redeploy needed) rather than
    guessed at here. Leaves site.emergency_number unset entirely, so a brand
    new client defaults to the generic "information & booking assistant"
    framing (backend/agent/receptionist.py's config-driven behavior) unless
    it's a real clinic that sets one explicitly. demo_mode starts true so
    the instance is immediately testable with no SMTP/PIN friction — turn it
    off once real bookings/SMTP are wired up.

    The voice: block below is filled in with the same provider wiring
    proven working on both the primary and PrimeConnect AI (Mistral for
    llm/stt, Google for tts), not left as a bare `enabled: false` — turning
    voice on for any future client used to mean reconstructing this whole
    nested structure from memory/grepping a working instance's /data every
    single time. It's inert while `enabled: false` (backend/api.py's
    lifespan only ever evaluates it inside `if voice_enabled():`), so
    shipping it prefilled costs nothing for clients that never touch voice.

    IMPORTANT if you do flip enabled: true for a client — tts.provider
    'google' is NOT an EU-owned processor (backend/config.py's
    EU_VOICE_PROVIDERS is {'mistral', 'gladia', 'piper', 'local'}), and this
    codebase refuses to boot voice with a non-EU provider whenever that
    client's own .env has ENV=prod (a real incident: PrimeConnect AI's
    container crash-looped on exactly this — "RuntimeError: Refusing to
    start in production with voice.enabled and a non-EU voice provider").
    Two ways forward, both legitimate, and it's a business decision each
    time, not a default this wizard should make for you: (1) set that
    client's own .env to ENV=dev — same as the primary and PrimeConnect AI
    already run, fine for a non-regulated business that doesn't care which
    vendor processes voice audio — or (2) keep ENV=prod and swap
    tts.provider to an EU-owned one instead, if this client needs to stay
    strictly EU-compliant (e.g. an actual medical/dental clinic). See
    docs/VOICE_NETWORKING.md for the full runbook either way."""
    safe_name = display_name.replace('"', "'")
    return f"""consultants:
- name: "Owner"
  name_spoken_en: "Owner"
  name_spoken_es: "Owner"
  specialty: "General inquiries"
llm:
  model: mistral-large-latest
  provider: mistral
  temperature: 0.3
security:
  attempt_window_minutes: 15
  lockout_minutes: 15
  max_attempts: 3
services:
- name: "Consultation"
  description: "General consultation — edit or replace this in the admin panel."
  duration_min: 30
  price_cents: 0
site:
  name: "{safe_name}"
  phone: "+00 000 000 000"
  email: "placeholder@example.com"
  language: "en"
  # Asked at intake (2026-07-19): clients handling medical/patient data get
  # the backend's strict EU rules; everyone else explicitly opts out — the
  # backend treats an ABSENT key as strict, so this must always be written.
  eu_medical_data_protection: {str(medical).lower()}
  timezone: "Europe/Madrid"
  business_type: "general business"
  disclose_prices: false
  demo_mode: true
  hours:
    mon: ['09:00', '18:00']
    tue: ['09:00', '18:00']
    wed: ['09:00', '18:00']
    thu: ['09:00', '18:00']
    fri: ['09:00', '18:00']
  welcome_headline: "Ask us anything"
  welcome_paragraph: "This is a starter configuration — edit everything here in the admin Configuracion tab."
voice:
  enabled: false
  greeting_en: "You are speaking with our virtual assistant. How can I help?"
  greeting_es: "Le atiende nuestro asistente virtual. En que puedo ayudarle?"
  llm:
    provider: mistral
    model: mistral-small-2506
  stt:
    provider: mistral
    model: voxtral-mini-transcribe-realtime-2602
  tts:
    # piper: EU-approved, needs no credentials, sane built-in voices — the
    # shipped default 'google' is a documented dev-only stopgap that FAILS
    # the production boot guard (MULTI_CLINIC_ONBOARDING.md #1) and failed
    # the onboarding config check live on 2026-07-19. Never generate it.
    provider: piper
  max_session_minutes: 15
  max_turns_unverified: 6
  max_turns_verified: 25
"""


def _generate_placeholder_env(deploy_name: str, hostname: str) -> tuple[str, str, str]:
    """A bootable-but-not-yet-real .env: enough for the container to start
    and pass the prod ADMIN_PASSWORD guard, with the two per-instance
    secrets (admin password, backup passphrase) freshly randomized rather
    than left blank or defaulted — never reused from another client. LLM/
    SMTP/Twilio keys are deliberately left blank; that's the Credentials
    tool's job, not this wizard's, since which real key to use is a
    judgment call this wizard has no business making. Returns (content,
    admin_password, backup_passphrase) so the caller can surface the
    generated secrets back to the person running this."""
    admin_password = secrets.token_urlsafe(18)
    backup_passphrase = secrets.token_urlsafe(32)
    content = (
        f"# Generated by ops-console's New Client wizard for {deploy_name}.\n"
        f"# Bootable placeholder only — LLM/SMTP/Twilio keys are blank on purpose;\n"
        f"# use the Credentials tool to copy real values in from an existing client.\n"
        "MISTRAL_API_KEY=\n"
        f"ADMIN_PASSWORD={admin_password}\n"
        f"CORS_ORIGINS=https://{hostname}\n"
        "SMTP_HOST=\n"
        "SMTP_PORT=587\n"
        "SMTP_USERNAME=\n"
        "SMTP_PASSWORD=\n"
        "SMTP_USE_TLS=True\n"
        "SMTP_OVERRIDE_RECIPIENT=\n"
        "ENV=prod\n"
        "LOG_LEVEL=INFO\n"
        "DOMAIN=\n"
        "ACME_EMAIL=\n"
        f"BACKUP_PASSPHRASE={backup_passphrase}\n"
        f"APP_CONTAINER_NAME={deploy_name}\n"
        f"COMPOSE_PROJECT_NAME={deploy_name}\n"
        "TWILIO_ACCOUNT_SID=\n"
        "TWILIO_AUTH_TOKEN=\n"
        "TWILIO_FROM_NUMBER=\n"
    )
    return content, admin_password, backup_passphrase


def create_new_client_stream(
    ssh_target: str, deploy_name: str, hostname: str, display_name: str, template_remote_dir: str,
    medical: bool = False,
):
    """Generator variant of create_new_client — the actual implementation.
    Runs the exact same provisioning pipeline (clone -> write override/.env/
    site_config -> build -> up -> swap the real starter config into /data ->
    reseed -> restart), but yields progress events as it goes instead of
    blocking silently until everything finishes — the whole point being that
    a `docker compose build` can genuinely take minutes, and staring at a
    blank spinner that whole time is exactly the complaint this exists to
    fix. Two event shapes:
      {"type": "log", "line": "..."}   one line of real output — either a
                                        quick precheck's own status line, or
                                        streamed live from the combined
                                        clone/build/up/swap SSH command
      {"type": "result", ...}          exactly one, always last — same
                                        shape create_new_client() returns

    Does NOT touch Caddy or clients.json — see wire_caddy_stream() and the
    /api/new-client(/stream) routes for those. Refuses outright if the
    target directory already exists, rather than risking any overwrite of
    something already there.

    Every docker compose call below is pinned with `-p {deploy_name}` —
    added after a real incident where an unpinned `docker compose up -d`
    for one client ended up recreating/removing a DIFFERENT client's
    container on the same VPS (neither invocation `cd`s into remote_dir,
    so Compose's own project-name inference isn't reliable across separate
    SSH round trips). See core._project_name's docstring for the full
    story; deploy_name is used directly here since remote_dir is always
    exactly f"~/{deploy_name}", so it's already the correct project name
    with no extra computation needed."""
    empty = {"ok": False, "error": None, "stage": None, "output": "", "port": None,
              "remote_dir": None, "admin_password": None}

    def _result(**overrides: Any) -> dict[str, Any]:
        return {"type": "result", **empty, **overrides}

    name_error = validate_deploy_name(deploy_name)
    if name_error:
        yield _result(error=name_error, stage="validate")
        return
    if not ssh_target or not template_remote_dir:
        yield _result(error="no ssh_target/template client configured", stage="config")
        return

    remote_dir = f"~/{deploy_name}"
    shell_dir = _shell_remote_dir(remote_dir)

    yield {"type": "log", "line": f"$ checking whether {remote_dir} already exists on {ssh_target}…"}
    exists_ok, exists_output = run_ssh(
        ssh_target,
        f"if [ -e {shell_dir} ]; then "
        f"([ -f {shell_dir}/docker-compose.yml ] && [ -d {shell_dir}/.git ] "
        f"&& echo RESUMABLE || echo FOREIGN); else echo MISSING; fi")
    if not exists_ok:
        yield _result(error=exists_output.strip()[-300:] or "ssh failed", stage="precheck")
        return
    resume = "RESUMABLE" in exists_output
    if "FOREIGN" in exists_output:
        yield _result(error=f"{remote_dir} exists but is not a client checkout (no "
                             "docker-compose.yml/.git) — refusing to touch it. Remove it by "
                             "hand if you really want this name.",
                      stage="precheck")
        return
    if resume:
        yield {"type": "log", "line": f"  {remote_dir} is an existing client checkout — RESUMING: "
                                       "clone will be skipped, the existing .env kept, and the DB "
                                       "reseeded only if it never was."}

    yield {"type": "log", "line": "$ git remote get-url origin  (from the template client)"}
    origin_result = get_repo_origin(ssh_target, template_remote_dir)
    if not origin_result["ok"]:
        yield _result(error=f"couldn't determine the git origin to clone from: {origin_result['error']}",
                      stage="origin")
        return
    origin = origin_result["origin"]
    yield {"type": "log", "line": f"  origin = {origin}"}

    yield {"type": "log", "line": "$ picking a free loopback port…"}
    port_result = next_free_port(ssh_target)
    if not port_result["ok"]:
        yield _result(error=f"couldn't determine a free port: {port_result['error']}", stage="port")
        return
    port = port_result["port"]
    yield {"type": "log", "line": f"  port = {port}"}

    override_yaml = _generate_override_yaml(deploy_name, port)
    site_config_yaml = _generate_starter_site_config(display_name, medical)
    env_content, admin_password, backup_passphrase = _generate_placeholder_env(deploy_name, hostname)

    override_b64 = base64.b64encode(override_yaml.encode("utf-8")).decode("ascii")
    site_config_b64 = base64.b64encode(site_config_yaml.encode("utf-8")).decode("ascii")
    env_b64 = base64.b64encode(env_content.encode("utf-8")).decode("ascii")

    compose_file = f"{shell_dir}/docker-compose.yml"
    override_file = f"{shell_dir}/docker-compose.override.yml"

    # google_tts.json below is `touch`ed empty on purpose, same "judgment
    # call, not this wizard's to guess" reasoning as the blank LLM/SMTP/
    # Twilio keys in _generate_placeholder_env: it's a real Google Cloud
    # service-account credential, not a per-client value this wizard has
    # anything sensible to invent. The starter site_config.yaml's voice
    # block is prefilled and ready to go, but voice.enabled must stay
    # false until this file is replaced with real credentials — see
    # docs/VOICE_NETWORKING.md's "turning voice on for a client" runbook
    # for the one-line copy from the primary's own (real) google_tts.json.
    cmd = (
        f"echo '{_MARK_CLONE}'; "
        # Resume-safe: an existing valid checkout is pulled, not re-cloned.
        f"if [ -d {shell_dir}/.git ]; then git -C {shell_dir} pull --ff-only 2>&1; clone_status=$?; "
        f"else git clone {origin} {shell_dir} 2>&1; clone_status=$?; fi; "
        f"echo '{_MARK_FILES}'; "
        f"if [ $clone_status -eq 0 ]; then "
        f"printf '%s' '{override_b64}' | base64 -d > {override_file} && "
        f"printf '%s' '{site_config_b64}' | base64 -d > {shell_dir}/site_config.yaml && "
        # Never clobber an existing .env on resume — it holds the REAL admin
        # password/credentials; only a fresh checkout gets the placeholder.
        f"([ -f {shell_dir}/.env ] && echo '(.env exists — kept)' || "
        f"(printf '%s' '{env_b64}' | base64 -d > {shell_dir}/.env)) && "
        f"chmod 600 {shell_dir}/.env && touch {shell_dir}/google_tts.json 2>&1; files_status=$?; "
        f"else echo '(skipped — clone failed)'; files_status=1; fi; "
        f"echo '{_MARK_BUILD}'; "
        f"if [ $clone_status -eq 0 ] && [ $files_status -eq 0 ]; then "
        f"docker compose -p {deploy_name} -f {compose_file} -f {override_file} build app 2>&1; build_status=$?; "
        f"else echo '(skipped — clone or file setup failed)'; build_status=1; fi; "
        f"echo '{_MARK_UP}'; "
        f"if [ $build_status -eq 0 ]; then "
        f"docker compose -p {deploy_name} -f {compose_file} -f {override_file} up -d app 2>&1; up_status=$?; "
        f"else echo '(skipped — build failed)'; up_status=1; fi; "
        f"echo '{_MARK_SWAP}'; "
        f"if [ $up_status -eq 0 ]; then "
        f"sleep 3 && "
        f"docker cp {shell_dir}/site_config.yaml {deploy_name}:/data/site_config.yaml 2>&1 && "
        # docker cp preserves the HOST file's ownership (the deploy user's),
        # which the container's app user can READ but not WRITE — so every
        # config save (admin panel, ops-console plan push) 500s with
        # PermissionError. Bit acme on 2026-07-20, the first wizard-deployed
        # instance to take a config write. chown to whatever owns /data (the
        # app user — it creates site.sqlite there) right after the copy.
        f"docker exec -u root {deploy_name} sh -c "
        f"'chown --reference=/data /data/site_config.yaml && chmod 664 /data/site_config.yaml' 2>&1 && "
        # Seed-once marker: --reset wipes the DB, fine on a fresh instance,
        # destructive on a resumed one that may already hold real data.
        f"docker compose -p {deploy_name} -f {compose_file} -f {override_file} exec -T app "
        f"sh -c 'if [ -f /data/.opsconsole_seeded ]; then echo \"(already seeded — skipping reset)\"; "
        f"else python -m backend.db.seed --reset && touch /data/.opsconsole_seeded; fi' 2>&1 && "
        f"docker compose -p {deploy_name} -f {compose_file} -f {override_file} restart app 2>&1; swap_status=$?; "
        f"else echo '(skipped — up failed)'; swap_status=1; fi; "
        f"echo '{_MARK_STATUS}'; "
        f"echo \"clone=$clone_status files=$files_status build=$build_status up=$up_status swap=$swap_status\""
    )
    yield {"type": "log", "line": f"$ cloning, building, and booting {deploy_name} — this can take a couple of minutes…"}
    holder: dict[str, Any] = {}
    for line in stream_ssh(ssh_target, cmd, holder, timeout=DEPLOY_TIMEOUT):
        yield {"type": "log", "line": line}
    if not holder.get("ok"):
        yield _result(error=(holder.get("output") or "").strip()[-1000:] or "ssh failed", stage="ssh",
                      port=port, remote_dir=remote_dir)
        return
    output = holder.get("output", "")

    try:
        clone_text, after_clone = output.split(_MARK_FILES, 1)
        clone_text = clone_text.split(_MARK_CLONE, 1)[-1]
        files_text, after_files = after_clone.split(_MARK_BUILD, 1)
        build_text, after_build = after_files.split(_MARK_UP, 1)
        up_text, after_up = after_build.split(_MARK_SWAP, 1)
        swap_text, status_text = after_up.split(_MARK_STATUS, 1)
    except (IndexError, ValueError):
        yield _result(error="could not parse provisioning output from remote", stage="parse",
                      output=output.strip()[-_OUTPUT_CHAR_LIMIT:], port=port, remote_dir=remote_dir)
        return

    m = re.search(r"clone=(\d+)\s+files=(\d+)\s+build=(\d+)\s+up=(\d+)\s+swap=(\d+)", status_text)
    combined_output = (
        f"--- git clone ---\n{clone_text.strip()}\n\n"
        f"--- write override/.env/site_config ---\n{files_text.strip()}\n\n"
        f"--- docker compose build ---\n{build_text.strip()}\n\n"
        f"--- docker compose up -d ---\n{up_text.strip()}\n\n"
        f"--- swap in starter config + reseed + restart ---\n{swap_text.strip()}"
    )[-_OUTPUT_CHAR_LIMIT:]

    base_result = {"output": combined_output, "port": port, "remote_dir": remote_dir,
                    "admin_password": admin_password, "backup_passphrase": backup_passphrase}
    if not m:
        yield _result(**base_result, error="could not determine provisioning stage results", stage="parse")
        return

    stages = ["clone", "files", "build", "up", "swap"]
    codes = [int(g) for g in m.groups()]
    for stage, code in zip(stages, codes):
        if code != 0:
            yield {"type": "result", **base_result, "ok": False,
                   "error": f"'{stage}' step failed (see output) — nothing after it ran", "stage": stage}
            return

    if resume:
        # The generated placeholder password was NOT written (existing .env
        # kept) — surface the real one so the result stays truthful.
        pw_ok, pw_out = run_ssh(ssh_target,
                                f"grep '^ADMIN_PASSWORD=' {shell_dir}/.env | head -1 | cut -d= -f2-")
        if pw_ok and pw_out.strip():
            base_result["admin_password"] = pw_out.strip()
            base_result["backup_passphrase"] = "(kept from existing .env)"

    yield {"type": "result", **base_result, "ok": True, "error": None, "stage": "done"}


def create_new_client(
    ssh_target: str, deploy_name: str, hostname: str, display_name: str, template_remote_dir: str,
) -> dict[str, Any]:
    """Non-streaming wrapper around create_new_client_stream — drains the
    generator and returns only its final result dict, dropping the
    intermediate log lines. Kept for headless/test use and any caller that
    doesn't need a live console; written this way (rather than as a
    separate implementation) so the streaming and non-streaming paths can
    never drift apart."""
    result: dict[str, Any] = {"ok": False, "error": "create_new_client_stream produced no result", "stage": "internal"}
    for event in create_new_client_stream(ssh_target, deploy_name, hostname, display_name, template_remote_dir):
        if event.get("type") == "result":
            result = {k: v for k, v in event.items() if k != "type"}
    return result


def wire_caddy_stream(ssh_target: str, primary_remote_dir: str, hostname: str, port: int, label: str):
    """Generator variant of wire_caddy — the actual implementation. Runs
    deploy/add_clinic_site.sh on the primary checkout (the one that owns
    the shared Caddy) to append the new site block, validate it, and
    restart Caddy so HTTPS gets provisioned for the new hostname, streaming
    its output live (same reasoning as create_new_client_stream — this
    genuinely takes a few seconds of git/caddy-validate/restart work, worth
    seeing happen rather than just waiting). The script itself is already
    idempotent, backs up the Caddyfile first, and restores that backup
    automatically if validation fails — this just invokes it over SSH the
    same way a human would.

    Invoked as `bash deploy/add_clinic_site.sh`, not `./deploy/add_clinic_site.sh`
    — a real run hit `Permission denied` because that file's executable bit
    wasn't set on that particular checkout (git doesn't always preserve it
    across a fresh clone unless it was explicitly committed that way).
    Running it through bash directly only needs read access to the file, so
    this can never depend on a checkout's file-mode bit being right.

    Yields {"type": "log", ...} lines, then exactly one final
    {"type": "result", "ok":, "error":, "output":}."""
    shell_dir = _shell_remote_dir(primary_remote_dir.rstrip("/"))
    cmd = f"cd {shell_dir} && bash deploy/add_clinic_site.sh {hostname} {port} {label} 2>&1"
    yield {"type": "log", "line": f"$ bash deploy/add_clinic_site.sh {hostname} {port} {label}"}
    holder: dict[str, Any] = {}
    for line in stream_ssh(ssh_target, cmd, holder, timeout=DEPLOY_TIMEOUT):
        yield {"type": "log", "line": line}
    output = holder.get("output", "")
    if not holder.get("ok"):
        yield {"type": "result", "ok": False, "error": output.strip()[-1500:] or "ssh failed",
               "output": output.strip()[-_OUTPUT_CHAR_LIMIT:]}
        return
    if "[FAIL]" in output:
        yield {"type": "result", "ok": False, "error": "add_clinic_site.sh reported a failure (see output)",
               "output": output.strip()[-_OUTPUT_CHAR_LIMIT:]}
        return
    # Auto-commit the appended block so the Caddyfile can't drift from git
    # again (the PrimeConnect block sat uncommitted for two days; the manual
    # "please commit this" instruction was proven to be skipped). Best-effort:
    # a read-only deploy key just leaves a note, never fails the wiring.
    commit_cmd = (
        f"cd {shell_dir} && git add deploy/Caddyfile && "
        f"(git diff --cached --quiet || git commit -m 'deploy: add {label} Caddy site block (auto)') 2>&1; "
        "git push origin master 2>&1 || echo push-failed-commit-is-local-only"
    )
    ok2, out2 = run_ssh(ssh_target, commit_cmd, timeout=60)
    last = out2.strip().splitlines()[-1] if out2.strip() else "done"
    yield {"type": "log", "line": "auto-committing Caddyfile: " + last}
    yield {"type": "result", "ok": True, "error": None, "output": output.strip()[-_OUTPUT_CHAR_LIMIT:]}


def wire_caddy(ssh_target: str, primary_remote_dir: str, hostname: str, port: int, label: str) -> dict[str, Any]:
    """Non-streaming wrapper around wire_caddy_stream — see
    create_new_client's analogous wrapper for why it's written this way."""
    result: dict[str, Any] = {"ok": False, "error": "wire_caddy_stream produced no result", "output": ""}
    for event in wire_caddy_stream(ssh_target, primary_remote_dir, hostname, port, label):
        if event.get("type") == "result":
            result = {k: v for k, v in event.items() if k != "type"}
    return result


# ---------------------------------------------------------------------------
# Teardown + Caddy hygiene (Onboarding v2 — plan 1E / 1F)
# ---------------------------------------------------------------------------

def teardown_client_stream(ssh_target: str, deploy_name: str,
                            primary_remote_dir: str, hostname: str,
                            protected_dirs: list[str]):
    """Tears a client instance back down, in reverse order of provisioning:
    remove its Caddy site block (backup + validate-in-throwaway-container +
    FORCE-RECREATE caddy — never a reload, the single-file bind mount keeps
    the old inode otherwise, 2026-07-19 lesson), `docker compose down -v`
    the instance (containers + named volumes), and delete the checkout
    directory. clients.json/onboarding-record cleanup is the ROUTE's job
    (local state, no SSH).

    Refuses to touch the primary/template checkout or anything whose name
    fails the same validation provisioning uses. Yields log lines, then one
    {"type": "result"} — same contract as every other streamer here.
    """
    def _res(ok: bool, error: str | None = None):
        return {"type": "result", "ok": ok, "error": error}

    name_error = validate_deploy_name(deploy_name)
    if name_error:
        yield _res(False, name_error)
        return
    protected = {(d or "").rstrip("/").rsplit("/", 1)[-1] for d in protected_dirs}
    if deploy_name in protected:
        yield _res(False, f"{deploy_name!r} is a protected checkout (primary/template) — refusing.")
        return

    remote_dir = f"~/{deploy_name}"
    shell_dir = _shell_remote_dir(remote_dir)
    primary_shell = _shell_remote_dir(primary_remote_dir.rstrip("/"))

    # 1. Caddy block removal (only if the hostname actually has a block).
    if hostname and "/" not in hostname and " " not in hostname:
        yield {"type": "log", "line": f"$ removing Caddy site block for {hostname} (if present)…"}
        caddyfile = f"{primary_shell}/deploy/Caddyfile"
        awk = (
            "awk -v host='" + hostname + "' '"
            "$0 == host \" {\" {skip=1; next} "
            "skip && $0 == \"}\" {skip=0; next} "
            "!skip' "
        )
        cmd = (
            f"if grep -qF '{hostname} {{' {caddyfile}; then "
            f"cp {caddyfile} {caddyfile}.bak.teardown-$(date +%Y%m%d%H%M%S) && "
            f"{awk}{caddyfile} > {caddyfile}.tmp && mv {caddyfile}.tmp {caddyfile} && "
            f"cd {primary_shell} && "
            f"docker run --rm --env-file .env -v {primary_shell}/deploy/Caddyfile:/etc/caddy/Caddyfile:ro "
            f"caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 && "
            f"docker compose up -d --force-recreate caddy 2>&1 && echo CADDY_REMOVED; "
            f"else echo CADDY_ABSENT; fi"
        )
        holder: dict = {}
        for line in stream_ssh(ssh_target, cmd, holder, timeout=DEPLOY_TIMEOUT):
            yield {"type": "log", "line": line}
        out = holder.get("output", "")
        if "CADDY_REMOVED" not in out and "CADDY_ABSENT" not in out:
            yield _res(False, "Caddy block removal did not confirm — the Caddyfile backup is on "
                              "the server; fix by hand before retrying. Output: " + out.strip()[-400:])
            return

    # 2. Containers + volumes, then the directory.
    yield {"type": "log", "line": f"$ docker compose -p {deploy_name} down -v …"}
    holder = {}
    cmd = (
        f"docker compose -p {deploy_name} down -v --remove-orphans 2>&1; "
        f"rm -rf {shell_dir} 2>&1 && echo DIR_REMOVED"
    )
    for line in stream_ssh(ssh_target, cmd, holder, timeout=DEPLOY_TIMEOUT):
        yield {"type": "log", "line": line}
    if "DIR_REMOVED" not in holder.get("output", ""):
        yield _res(False, "compose down ran but the directory removal did not confirm: "
                          + holder.get("output", "").strip()[-400:])
        return
    yield {"type": "log", "line": "teardown complete."}
    yield _res(True)


def caddyfile_git_status(ssh_target: str, primary_remote_dir: str) -> dict:
    """The commit-back gate (plan 1F): after add_clinic_site.sh appends a
    block on the VPS, is deploy/Caddyfile committed and pushed, or drifting
    again? Returns {"ok", "clean", "pushed", "detail"} — clean means no
    uncommitted diff; pushed means no local-only commits touching it."""
    shell = _shell_remote_dir(primary_remote_dir.rstrip("/"))
    cmd = (
        f"cd {shell} && git fetch origin --quiet 2>/dev/null; "
        f"echo DIRTY=$(git status --porcelain deploy/Caddyfile | wc -l); "
        f"echo AHEAD=$(git log --oneline origin/master..HEAD -- deploy/Caddyfile 2>/dev/null | wc -l)"
    )
    ok, out = run_ssh(ssh_target, cmd, timeout=40)
    if not ok:
        return {"ok": False, "clean": False, "pushed": False,
                "detail": out.strip()[-300:] or "ssh failed"}
    import re as _re
    dirty = _re.search(r"DIRTY=(\d+)", out)
    ahead = _re.search(r"AHEAD=(\d+)", out)
    clean = bool(dirty and dirty.group(1) == "0")
    pushed = bool(ahead and ahead.group(1) == "0")
    detail = ("committed and pushed" if clean and pushed else
              "UNCOMMITTED changes on the VPS" if not clean else
              "committed on the VPS but NOT pushed")
    return {"ok": True, "clean": clean, "pushed": pushed, "detail": detail}
