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


def _format_env_value(value: str) -> str:
    """Re-quotes a value for writing when it needs it. parse_env STRIPS
    surrounding quotes on read, so writing values back raw silently
    unquoted them — which is exactly how a Gmail app password with spaces
    (quoted on the source instance) landed unquoted on a destination and
    broke everything that reads .env as shell (Acme's backups,
    2026-07-20). Values containing whitespace or '#' get double quotes
    (docker compose strips them back off); a value that ALSO contains a
    double quote gets single quotes instead; everything else is written
    as-is."""
    if re.search(r"[\s#]", value or ""):
        if '"' not in value:
            return f'"{value}"'
        if "'" not in value:
            return f"'{value}'"
    return value


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
    lines = [f"{key}={_format_env_value(value)}" for key, value in env.items()]
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


def test_zenmux(key: str) -> dict[str, Any]:
    if not key:
        return {"ok": False, "message": "no key provided"}
    # ZENMUX_API_KEY may hold several comma-separated keys (same rotation
    # convention as the others) — test the first. ZenMux is OpenAI-compatible,
    # so GET /v1/models with the bearer is the minimal read-only check.
    key = key.split(",")[0].strip()
    try:
        resp = requests.get(
            "https://zenmux.ai/api/v1/models",
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


# ---------------------------------------------------------------------------
# Live model listing — the config picker's "Browse live…" model browser.
# Every supported provider speaks the OpenAI-compatible GET /v1/models, so one
# call lists whatever that provider currently offers (no hardcoded catalog).
# Base URLs mirror the product's backend/config.py LLM_PROVIDERS — kept here,
# not imported, because ops-console is deliberately standalone from the product.
# ---------------------------------------------------------------------------
PROVIDER_MODELS_BASE = {
    "mistral": "https://api.mistral.ai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "zenmux": "https://zenmux.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
}


def list_provider_models(provider: str, key: str) -> dict[str, Any]:
    """List a provider's currently-offered models via its OpenAI-compatible
    GET /v1/models, normalized to a small uniform shape for the picker's live
    browser: {id, label, context_length, input_modalities, output_modalities}.
    Providers differ in richness — Zenmux/OpenRouter return display_name,
    modalities and context_length; Mistral/NVIDIA return the bare id — so every
    field past `id` is best-effort. Read-only and defensive, like the testers."""
    base = PROVIDER_MODELS_BASE.get((provider or "").strip())
    if not base:
        return {"ok": False, "message": f"unknown provider {provider!r}", "models": []}
    headers = {}
    k = (key or "").split(",")[0].strip()  # first of a comma-rotation list
    if k:
        headers["Authorization"] = f"Bearer {k}"
    try:
        resp = requests.get(f"{base}/models", headers=headers, timeout=HTTP_TIMEOUT)
    except Exception as e:
        return {"ok": False, "message": str(e), "models": []}
    if resp.status_code != 200:
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}", "models": []}
    try:
        data = resp.json().get("data", []) or []
    except Exception as e:
        return {"ok": False, "message": f"bad JSON: {e}", "models": []}
    models = []
    for m in data:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        models.append({
            "id": m["id"],
            "label": m.get("display_name") or m["id"],
            "context_length": m.get("context_length"),
            "input_modalities": m.get("input_modalities") or [],
            "output_modalities": m.get("output_modalities") or [],
        })
    models.sort(key=lambda x: x["id"].lower())
    return {"ok": True, "message": f"{len(models)} model(s)", "models": models}


# ---------------------------------------------------------------------------
# Pipeline lane tests — run a clinic's actual (provider, model, voice) combo and
# return what comes back: a reply for LLM, a playable WAV for TTS. Keys come
# from the vault (passed in). Console-side TTS covers ZenMux (REST /audio/speech
# → PCM wrapped as WAV); other TTS providers are heard via the on-instance test.
# ---------------------------------------------------------------------------

def pipeline_test_llm(provider: str, model: str, key: str) -> dict[str, Any]:
    """One tiny chat round-trip against the OpenAI-compatible endpoint, to prove
    the provider+model actually answers. Returns {ok, reply, ms}."""
    import time
    base = PROVIDER_MODELS_BASE.get((provider or "").strip())
    if not base:
        return {"ok": False, "message": f"unknown provider {provider!r}"}
    k = (key or "").split(",")[0].strip()
    headers = {"Content-Type": "application/json"}
    if k:
        headers["Authorization"] = f"Bearer {k}"
    t0 = time.monotonic()
    try:
        resp = requests.post(
            f"{base}/chat/completions", headers=headers,
            json={"model": model, "temperature": 0,
                  "messages": [{"role": "user", "content": "Reply with exactly the word: OK"}]},
            timeout=HTTP_TIMEOUT)
    except Exception as e:
        return {"ok": False, "message": str(e)}
    ms = int((time.monotonic() - t0) * 1000)
    if resp.status_code != 200:
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}", "ms": ms}
    try:
        reply = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        return {"ok": False, "message": f"bad response: {e}", "ms": ms}
    return {"ok": True, "reply": reply or "(empty reply)", "ms": ms}


def tts_sample_zenmux(key: str, model: str, voice: str, text: str) -> dict[str, Any]:
    """Synthesize a short clip via ZenMux /audio/speech (PCM) and return it as a
    data: WAV URL the browser can play. Returns {ok, audio}."""
    import struct
    k = (key or "").split(",")[0].strip()
    try:
        resp = requests.post(
            "https://zenmux.ai/api/v1/audio/speech",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {k}"},
            json={"model": model or "google/gemini-3.1-flash-tts-preview",
                  "input": text or "Hello.", "voice": voice, "response_format": "pcm"},
            timeout=30)
    except Exception as e:
        return {"ok": False, "message": str(e)}
    if resp.status_code != 200:
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    try:
        data = resp.json()
    except Exception as e:
        return {"ok": False, "message": f"bad JSON: {e}"}
    if isinstance(data, dict) and data.get("error"):
        return {"ok": False, "message": str(data["error"])}
    b64 = data.get("audio") if isinstance(data, dict) else None
    if not b64:
        return {"ok": False, "message": "no audio in response"}
    pcm = base64.b64decode(b64)
    rate = 24000  # ZenMux pcm default: 16-bit LE mono 24 kHz
    n = len(pcm)
    header = (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE" + b"fmt "
              + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
              + b"data" + struct.pack("<I", n))
    wav = header + pcm
    return {"ok": True, "audio": "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")}


def tts_sample_mistral(key: str, model: str, voice_id: str, text: str) -> dict[str, Any]:
    """Synthesize a short clip via Mistral (Voxtral) TTS and return it as a
    playable MP3 data URL. Uses the same REST call the SDK's
    audio.speech.complete wraps: POST /v1/audio/speech with a preset (or cloned)
    voice_id. Mistral is EU, so this is testable straight from the console like
    ZenMux/Google. Returns {ok, audio}. `voice_id` is a Mistral preset/clone id
    (discover them with the agent's mistral_voice_tool.py)."""
    k = (key or "").split(",")[0].strip()
    if not voice_id:
        return {"ok": False, "message": "no Mistral voice_id — set one on the ES/EN "
                "field first (list ids with mistral_voice_tool.py)"}
    # Force a TTS model: the voice_tts_model slot can carry a non-TTS id (e.g.
    # the STT 'voxtral-mini-latest', which Mistral rejects as invalid_model on a
    # speech call). Only honour a model that is actually a TTS model.
    tts_model = model if "tts" in (model or "").lower() else "voxtral-mini-tts-2603"
    try:
        resp = requests.post(
            "https://api.mistral.ai/v1/audio/speech",
            headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
            json={"model": tts_model, "input": text or "Hola.",
                  "voice_id": voice_id, "response_format": "mp3"},
            timeout=30)
    except Exception as e:
        return {"ok": False, "message": str(e)}
    if resp.status_code != 200:
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    # Response is JSON with base64 audio_data (the shape the SDK decodes); fall
    # back to raw audio bytes if a future version streams them directly.
    ctype = resp.headers.get("Content-Type", "")
    if "application/json" in ctype:
        try:
            d = resp.json()
        except Exception as e:
            return {"ok": False, "message": f"bad JSON: {e}"}
        if isinstance(d, dict) and d.get("error"):
            return {"ok": False, "message": str(d["error"])}
        b64 = (d.get("audio_data") or d.get("audio")) if isinstance(d, dict) else None
        if not b64:
            return {"ok": False, "message": "no audio_data in Mistral response"}
        return {"ok": True, "audio": "data:audio/mp3;base64," + b64}
    return {"ok": True, "audio": "data:audio/mp3;base64,"
            + base64.b64encode(resp.content).decode("ascii")}


def list_voices_mistral(key: str) -> dict[str, Any]:
    """List the Mistral (Voxtral) preset/cloned voices on this account — the
    live source for the Catalog's Mistral voice browse (GET /v1/audio/voices,
    the REST call the SDK's audio.voices.list wraps). Voxtral voices are
    multilingual (one voice speaks ES and EN), so `languages` is a list.
    Returns {ok, voices:[{id, name, gender, languages}]}."""
    k = (key or "").split(",")[0].strip()
    try:
        # type=all → built-in PRESET voices + any custom clones (the default may
        # otherwise omit the presets). languages/gender aren't in the list
        # response (only on the per-voice detail endpoint), so voices come back
        # id+name only — that's fine, Voxtral voices are multilingual anyway.
        resp = requests.get(
            "https://api.mistral.ai/v1/audio/voices",
            headers={"Authorization": f"Bearer {k}"},
            params={"limit": 100, "type": "all"}, timeout=30)
    except Exception as e:
        return {"ok": False, "message": str(e), "voices": []}
    if resp.status_code != 200:
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}", "voices": []}
    try:
        d = resp.json()
    except Exception as e:
        return {"ok": False, "message": f"bad JSON: {e}", "voices": []}
    # tolerate the various envelope shapes the SDK also probes for
    items = (d.get("items") or d.get("data") or d.get("voices")) if isinstance(d, dict) else d
    if not isinstance(items, list):
        items = []
    voices = []
    for v in items:
        if not isinstance(v, dict):
            continue
        vid = v.get("id") or v.get("voice_id")
        if not vid:
            continue
        langs = v.get("languages") or v.get("language") or []
        if isinstance(langs, str):
            langs = [langs]
        voices.append({"id": vid, "name": v.get("name") or vid,
                       "gender": (v.get("gender") or "").lower(),
                       "languages": list(langs)})
    return {"ok": True, "voices": voices}


def tts_sample_google(sa_info: dict[str, Any], voice: str, text: str) -> dict[str, Any]:
    """Synthesize a short clip via Google Cloud TTS using the service-account
    JSON (the same google_tts.json the live bot uses) — mint an access token
    from it, then POST to the text:synthesize REST endpoint. Returns {ok, audio}
    as a playable MP3 data URL. Needs `google-auth` installed on ops-console
    (added to requirements.txt); requests is already present."""
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GoogleRequest
    except Exception as e:
        return {"ok": False, "message": f"google-auth not installed on ops-console "
                f"(pip install google-auth): {e}"}
    try:
        creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(GoogleRequest())
        token = creds.token
    except Exception as e:
        return {"ok": False, "message": f"google auth failed: {e}"}
    # language_code from the voice id prefix, e.g. "es-ES-Neural2-A" -> "es-ES"
    parts = (voice or "").split("-")
    lang = "-".join(parts[:2]) if len(parts) >= 2 else "en-US"
    try:
        # EU multi-region endpoint: keeps synthesis data within the EU (GDPR
        # residency), matching the live bot (backend/voice/google_tts.py).
        resp = requests.post(
            "https://eu-texttospeech.googleapis.com/v1/text:synthesize",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"input": {"text": text or "Hello."},
                  "voice": {"languageCode": lang, "name": voice},
                  "audioConfig": {"audioEncoding": "MP3"}},
            timeout=30)
    except Exception as e:
        return {"ok": False, "message": str(e)}
    if resp.status_code != 200:
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    b64 = resp.json().get("audioContent")
    if not b64:
        return {"ok": False, "message": "no audioContent in response"}
    return {"ok": True, "audio": "data:audio/mp3;base64," + b64}


def pipeline_stt_connection(provider: str, key: str) -> dict[str, Any]:
    """Cheap 'is the STT provider reachable + key valid' check — a models-list
    call. Proves auth/connectivity, not transcription."""
    res = list_provider_models(provider, key)
    if res.get("ok"):
        return {"ok": True, "message": f"connected — {res.get('message', '')}".strip()}
    return {"ok": False, "message": res.get("message", "connection failed")}


def stt_transcribe_mistral(key: str, audio_data_url: str, language: str | None = None) -> dict[str, Any]:
    """Transcribe a recorded WAV clip via Mistral Voxtral's batch transcription
    REST endpoint (the realtime model is websocket-only, so the console test uses
    voxtral-mini-latest). `audio_data_url` may be a bare base64 string or a
    data:audio/wav;base64,… URL. Returns {ok, transcript, ms}."""
    import time
    k = (key or "").split(",")[0].strip()
    raw = audio_data_url or ""
    if raw.strip().startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        audio = base64.b64decode(raw)
    except Exception as e:
        return {"ok": False, "message": f"bad audio: {e}"}
    if not audio:
        return {"ok": False, "message": "empty audio"}
    files = {"file": ("sample.wav", audio, "audio/wav")}
    data = {"model": "voxtral-mini-latest"}
    if language:
        data["language"] = language
    t0 = time.monotonic()
    try:
        resp = requests.post(
            "https://api.mistral.ai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {k}"}, files=files, data=data, timeout=60)
    except Exception as e:
        return {"ok": False, "message": str(e)}
    ms = int((time.monotonic() - t0) * 1000)
    if resp.status_code != 200:
        return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}", "ms": ms}
    try:
        text = (resp.json().get("text") or "").strip()
    except Exception as e:
        return {"ok": False, "message": f"bad response: {e}", "ms": ms}
    return {"ok": True, "transcript": text or "(empty transcript)", "ms": ms}


def run_credential_test(kind: str, values: dict[str, str]) -> dict[str, Any]:
    if kind == "mistral":
        return test_mistral(values.get("key", ""))
    if kind == "openrouter":
        return test_openrouter(values.get("key", ""))
    if kind == "nvidia":
        return test_nvidia(values.get("key", ""))
    if kind == "zenmux":
        return test_zenmux(values.get("key", ""))
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
