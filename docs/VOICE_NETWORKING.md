# Voice calls: networking, config, and the EU-provider guard

Why this document exists: PrimeConnect AI's launch turned voice on and hit
three separate, stacked failures before it actually worked — a container
crash, a permission error, and a silent "rings but never answers" bug that
took the longest to find. None of them were obvious from the symptom alone.
This is the record of what each one actually was, so the next client (there
will be ~100 more) doesn't repeat the same investigation from zero. See also
`.claude/skills/enable-client-voice/SKILL.md` for the runnable version of
this as a Claude Code skill.

## The three failure modes, in the order you'll actually hit them

### 1. "Rings but never answers" — bridge networking has no path for WebRTC media

**Symptom:** the mic button appears, clicking it "rings" (the call appears
to start), but no audio ever comes back in either direction, and it hangs
up after a few seconds.

**Root cause:** voice calls use WebRTC. The signaling handshake
(`POST /voice/offer`, exchanging an SDP offer/answer) is plain HTTPS and
goes through Caddy's reverse proxy exactly like every other request — that
part always works. The actual audio, though, is a separate real-time UDP
media stream negotiated via ICE, and it does **not** go through Caddy at
all; the browser tries to reach the container directly.

Every satellite client (anything that isn't the primary) runs Docker
**bridge** networking, published only as a loopback TCP port
(`127.0.0.1:PORT:8000` before this fix). Bridge mode's Docker NAT has no
mechanism for that UDP media path — the browser has nothing to reach, ICE
negotiation times out (~4s), and the call closes. This has nothing to do
with which STT/LLM/TTS providers are configured — it fails identically no
matter what's plugged into the pipeline.

**Fix:** every satellite client now runs `network_mode: host`, same as the
primary. This gives the container the VPS's real public IP directly, and
the box's firewall already opens the ephemeral UDP range this needs
(`sudo ufw status verbose` → `49152:65535/udp ALLOW IN Anywhere`). Checked
and confirmed before relying on this: the app's own HTTP port is **not** in
ufw's allow list, so — same as the primary already does today — it stays
unreachable directly from the internet; only Caddy's 80/443 and the WebRTC
UDP range are actually open. Host networking does not create a new public
exposure.

Because host networking shares the box's real network namespace, the app
can't default to port 8000 (the primary already owns that) — each
satellite's `docker-compose.override.yml` pins its own `uvicorn` to its
assigned port via a `command:` override instead, with a matching
`healthcheck:` override (the image's baked-in healthcheck still points at
8000). This is generated automatically by
`ops-console/backend/new_client.py`'s `_generate_override_yaml()` as of the
fix that came out of this incident — see that function's own docstring for
the code-level detail.

Diagnosing this live, if it happens again: `docker logs <container> -f`
while reproducing the call. A working signaling handshake followed by
`ICE connection state is checking, connection is connecting` that never
progresses, ending in `Timeout establishing the connection to the remote
peer`, is this exact failure.

### 2. Container crash-loops on boot: "Refusing to start in production with voice.enabled and a non-EU voice provider"

**Symptom:** after turning `voice.enabled: true` on and restarting, the
container doesn't come back up at all — `docker logs` shows a
`RuntimeError` and `Application startup failed. Exiting.`

**Root cause:** `backend/config.py` defines `EU_VOICE_PROVIDERS =
{"mistral", "gladia", "piper", "local"}` — deliberately EU-owned processors
only, no US-headquartered vendor regardless of "EU region" hosting claims.
`validate_voice_providers()` checks every configured `voice.{stt,tts,llm}
.provider` against that set. `backend/api.py`'s startup (`lifespan`)
treats a violation as **fatal** whenever that client's own `.env` has
`ENV=prod` — it refuses to boot at all, not just warn. On `ENV=dev` the
exact same violation is only ever logged as a warning.

Google Cloud TTS (the only TTS provider actually wired up in this codebase
today) is not in that EU-owned set. The primary (`chat.briers.eu`) and
PrimeConnect AI both use it successfully — because both run `ENV=dev`, not
because Google TTS is somehow exempt.

**This is a real compliance guard, not a bug** — it exists so a genuinely
EU-regulated client (an actual medical/dental clinic, say) can't
accidentally end up processing patient voice audio through a non-EU vendor.
Whether it's fine to bypass is a business decision **per client**, made
consciously each time, not a default to flip globally:

- **Bypass it** (`ENV=dev` in that client's own `.env`) when the business
  genuinely doesn't care which vendor processes voice audio — e.g.
  PrimeConnect AI, per Simon's explicit call: "this is for our own website,
  its not medical, noone cares who the voice provider is." `ENV=dev` also
  turns a couple of other prod-only guards into warnings (e.g. sessions
  become in-memory-only instead of persisted) — acceptable trade-offs for
  a client that's already made the same call about the voice provider.
- **Keep it enforced** (`ENV=prod`, unchanged) for anything that must stay
  strictly EU-compliant, and use an EU-owned provider for `tts` instead of
  `google`.

`ENV` is read once at process startup (`os.getenv` at import time in
`config.py`) — editing `.env` alone does nothing until the container is
recreated (`docker compose ... up -d --force-recreate app`), not just
restarted.

### 3. `PermissionError: [Errno 13] Permission denied: '/data/site_config.yaml'`

**Symptom:** an in-container script trying to write `/data/site_config.yaml`
(e.g. to flip `voice.enabled`) fails with this exact error, even though the
file itself shows normal-looking permissions.

**Root cause:** the named volume's on-disk ownership can end up owned by a
different uid than the container's non-root `appuser` (uid 10001) expects
— seen both locally on Windows Docker Desktop and on the VPS itself for a
freshly-provisioned client.

**Fix:**
```bash
docker compose -p <project> exec -u root -T app chown -R appuser:appuser /data
```
Then retry the write. Harmless to run this pre-emptively; it's a no-op if
ownership is already correct.

## The proven-working `voice:` config block

This is what's actually running on both the primary and PrimeConnect AI —
copy this exactly rather than reconstructing it from the `EU_VOICE_PROVIDERS`
set or guessing at model names. It's also now the default the wizard writes
into every new client's starter `site_config.yaml` (disabled, ready to flip
on) — see `_generate_starter_site_config()` in `new_client.py`.

```yaml
voice:
  enabled: false   # flip to true when actually turning voice on
  greeting_en: "You are speaking with our virtual assistant. How can I help?"
  greeting_es: "Le atiende nuestro asistente virtual. En que puedo ayudarle?"
  llm:
    provider: mistral
    model: mistral-small-2506
  stt:
    provider: mistral
    model: voxtral-mini-transcribe-realtime-2602
  tts:
    provider: google
    credentials_path: google_tts.json
  max_session_minutes: 15
  max_turns_unverified: 6
  max_turns_verified: 25
```

`google_tts.json` is a real Google Cloud service-account credential — it's
bind-mounted read-only (`./google_tts.json:/app/google_tts.json:ro` in
`docker-compose.yml`, present in every checkout already), not baked into
the image. The wizard only ever creates an **empty placeholder** file at
this path for a new client (same "not this wizard's judgment call to
guess" reasoning as blank LLM/SMTP/Twilio keys) — it must be replaced with
the real shared credential before voice can do anything with TTS.

## Runbook: turning voice on for a client

1. **Networking** — nothing to do for any client provisioned by the wizard
   after this fix; it already runs host networking on its own assigned
   port. For an older client provisioned before this fix (check: does its
   `docker-compose.override.yml` have `network_mode: host` and a
   `# opsconsole_assigned_port:` comment, or the old `network_mode: bridge`
   + `ports:` form?), replace its override file with the new-style block —
   see `_generate_override_yaml()`'s current output, or ask this to be
   regenerated for that client's assigned port.

2. **Real Google TTS credentials:**
   ```bash
   cp ~/<primary-checkout>/google_tts.json ~/<client-dir>/google_tts.json
   ```

3. **`.env` decision** — EU-compliance call, per client (see failure mode
   2 above). If bypassing:
   ```bash
   sed -i 's/^ENV=.*/ENV=dev/' ~/<client-dir>/.env
   ```

4. **Enable + write voice config:**
   ```bash
   docker compose -p <client-dir-name> exec -T app python - <<'EOF'
   import yaml
   with open("/data/site_config.yaml") as f:
       cfg = yaml.safe_load(f) or {}
   cfg["voice"]["enabled"] = True
   with open("/data/site_config.yaml", "w") as f:
       yaml.safe_dump(cfg, f, allow_unicode=True)
   EOF
   ```
   If this fails with the `PermissionError` from failure mode 3, run the
   `chown` fix above first, then retry.

5. **Reseed + recreate** (a raw `/data` edit alone isn't reliably picked
   up; `ENV` changes need a full container recreate, not just a restart):
   ```bash
   docker compose -p <client-dir-name> exec -T app python -m backend.db.seed --reset
   docker compose -p <client-dir-name> up -d --force-recreate app
   ```

6. **Verify:**
   ```bash
   curl -s https://<client-hostname>/config | python3 -m json.tool | grep '"voice"'
   docker logs <container-name> --tail 40
   ```
   Confirm `"voice": true` and no `RuntimeError` in the logs, then test an
   actual call in the browser.
