---
name: enable-client-voice
description: Turn on voice calling for a dental-clinic-agent client deployment (new or existing) on Simon's VPS — networking, real TTS credentials, the EU-provider/ENV decision, and the reseed+restart cycle. Use whenever Simon asks to enable/add/turn on voice for a client, or a voice call "rings but doesn't answer" / the container crash-loops after enabling voice.
---

# Enable voice for a client

Full incident writeup and reasoning: `docs/VOICE_NETWORKING.md` in this
repo (ops-console). Read it if anything here doesn't match what you're
seeing — this file is the condensed, runnable version of that doc.

Simon does all execution himself over his own SSH access
(`deploy@chat.briers.eu`) — this skill's job is to figure out exactly which
commands he needs and hand them over ready to paste, not to execute
anything directly. Never invent a fix here that isn't already validated in
`docs/VOICE_NETWORKING.md`; if something new comes up, diagnose it the same
evidence-first way (read the actual logs/config before guessing) and add
what you learn back to that doc afterward.

## Step 0 — figure out which client, and its current state

Ask (or infer from context) the client's deploy directory name (e.g.
`primeconnect-chatbot`) and hostname. Then get the ground truth before
writing any fix — don't assume any of this from memory:

```bash
echo "=== override.yml (networking) ==="
cat ~/<client-dir>/docker-compose.override.yml

echo "=== current voice config ==="
docker exec <container-name> cat /data/site_config.yaml | grep -A 15 "^voice:"

echo "=== ENV ==="
grep -E "^ENV=" ~/<client-dir>/.env

echo "=== google_tts.json present + size (placeholder is 0 bytes) ==="
ls -la ~/<client-dir>/google_tts.json
```

## Step 1 — networking (skip if already provisioned after the wizard fix)

Check `docker-compose.override.yml` from step 0. It needs `network_mode:
host` plus a `# opsconsole_assigned_port: N` marker and a `command:`
override pinning uvicorn to that port. If it instead shows
`network_mode: bridge` + a `ports:` mapping (the old style, from before
this fix), it needs replacing — see `_generate_override_yaml()` in
`backend/new_client.py` for the current template, substituting this
client's own `container_name` and assigned port (the number from its old
`ports: - "127.0.0.1:PORT:8000"` line). Give Simon the full new file
content plus:
```bash
docker compose -p <client-dir> up -d --force-recreate app
```

New clients created by the wizard after this fix already have this right
— skip straight to Step 2.

## Step 2 — real Google TTS credentials

The wizard only ever creates an empty placeholder. Copy the real,
shared credential from the primary:
```bash
cp ~/dental-clinic-agent/google_tts.json ~/<client-dir>/google_tts.json
```

## Step 3 — the EU-provider / ENV decision (ask Simon, don't assume)

`tts.provider: google` is not EU-owned, and this codebase refuses to boot
voice with it whenever `ENV=prod` (real incident: PrimeConnect AI
crash-looped on exactly this). Ask which applies to this client:

- **This client doesn't need to stay EU-compliant** (e.g. Simon's own
  business, or any client he's explicitly said doesn't care) → set
  `ENV=dev`:
  ```bash
  sed -i 's/^ENV=.*/ENV=dev/' ~/<client-dir>/.env
  ```
- **This client must stay EU-compliant** (an actual medical/regulated
  business) → keep `ENV=prod`, and swap `tts.provider` in the config
  below from `google` to an EU-owned option (`mistral`, `gladia`, or
  `piper` — confirm which is actually implemented for TTS in
  `backend/voice/pipeline.py` before promising it works; only Google TTS
  has been proven end-to-end as of this writing).

## Step 4 — write the voice config and enable it

```bash
docker compose -p <client-dir> exec -T app python - <<'EOF'
import yaml
with open("/data/site_config.yaml") as f:
    cfg = yaml.safe_load(f) or {}
cfg["voice"] = {
    "enabled": True,
    "greeting_en": "You are speaking with our virtual assistant. How can I help?",
    "greeting_es": "Le atiende nuestro asistente virtual. En que puedo ayudarle?",
    "llm": {"provider": "mistral", "model": "mistral-small-2506"},
    "stt": {"provider": "mistral", "model": "voxtral-mini-transcribe-realtime-2602"},
    "tts": {"provider": "google", "credentials_path": "google_tts.json"},
    "max_session_minutes": 15,
    "max_turns_unverified": 6,
    "max_turns_verified": 25,
}
with open("/data/site_config.yaml", "w") as f:
    yaml.safe_dump(cfg, f, allow_unicode=True)
print("voice config written")
EOF
```

If this fails with `PermissionError: ... '/data/site_config.yaml'`:
```bash
docker compose -p <client-dir> exec -u root -T app chown -R appuser:appuser /data
```
then retry the write above.

## Step 5 — reseed + recreate (not just restart)

A raw `/data` edit isn't reliably picked up without a reseed, and an
`ENV` change needs a full recreate, not a restart:
```bash
docker compose -p <client-dir> exec -T app python -m backend.db.seed --reset
docker compose -p <client-dir> up -d --force-recreate app
```

## Step 6 — verify before declaring done

```bash
curl -s https://<client-hostname>/config | python3 -m json.tool | grep '"voice"'
docker logs <container-name> --tail 40
```

Confirm `"voice": true` and no `RuntimeError`/crash in the log tail. Then
have Simon actually test a call in the browser — signaling succeeding
(`POST /voice/offer` 200) is not sufficient confirmation by itself; the
ICE/media path is the part that silently failed before this fix, so a
real end-to-end call (it rings *and* answers) is the only real proof.

## If something here doesn't match reality

Diagnose from real evidence before proposing a fix — `docker logs -f
<container> --tail 0` while reproducing, browser devtools console/network
during the same test, and the actual current file contents (`cat`, not
memory) — the same way this whole runbook was originally built. Once
resolved, add the new failure mode to `docs/VOICE_NETWORKING.md` so it
doesn't need rediscovering for client #101.
