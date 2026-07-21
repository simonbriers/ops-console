# Kickoff prompt — token-economy overhaul, day 3

Copy-paste everything below the line into a fresh session with the
`C:\AI\ops-console` and `C:\AI\dental-clinic-agent` folders connected.

---

We are mid-way through the token-economy / managed-service overhaul of my
ops-console + dental-clinic-agent. **Read
`ops-console/docs/TOKEN_ECONOMY_PLAN.md` first — design, decisions D1–D8,
phase plan, and TWO status blocks (day 1: Phases 0–5 verified; day 2:
Phases 6–7 built, NOT yet rehearsed).** Short version:

**DONE and verified (day 1, Phases 0–5):** acme dev instance (port 8003;
chat.* demo and primeconnectai.* are FROZEN — never apply credentials,
push plans, or write config to them). Vault v2, metering ledger (€ both
sides, 80/100% alerts, statements), Flow tab, clinic %-gauge + warning
emails — all verified live on acme.

**BUILT day 2, NOT yet rehearsed (Phases 6–7):** *Product* (dental repo,
Sprint 37): `site.managed` flag (default false, SSH-file-only by design)
partitions config into business vs infra; PUT /admin/config atomically
403s infra fields (`MANAGED_CONFIG_FIELDS`) without `X-Operator-Token` ==
`OPERATOR_TOKEN` env; SMTP password + Twilio token redacted from
non-operator reads while managed; admin panel hides `[data-managed]`
sections, drops the cfg-llm nav button, strips managed keys from saves
(SHELL_CACHE v18); tests in `backend/tests/test_managed_mode.py`.
*Console*: `backend/config_manager.py` + Config tab (`frontend/config.js`)
— field catalog tagged business/managed, API-first read/write with
echo-verify + last-written state (`config_state.json`), SSH fallback read,
three-way drift check (closes DEPLOYMENT.md §10 stale-config gap),
model picker showing the vault LLM source's notes, and confirm-name-gated
enable/disable managed mode (.env OPERATOR_TOKEN + in-container YAML flip
+ recreate + verify). Operator token rides on both `_admin_get_json`/
`_admin_put_json` transports; `operator_token` lives on the client record
(preserved across client edits; `/fetch-operator-token` recovery route).

**Suite status: GREEN (153 tests), both repos committed+pushed end of day
2.** Running the full suite surfaced two pre-existing problems, both fixed
same day (dental CHANGELOG Sprint 37 follow-ups): (1) a REAL regression —
the admin live-chat tray transcript rendered EMPTY on every instance since
the Sprint 34 watch-period work (`len()` of a JSON *string* used as the
message-count baseline); (2) test drift — the live config ships
`demo_mode: true` since Sprint 34, so `test_api`/`test_tools` now pin
`demo_mode: False` into their isolated copies (same pattern as the
`disclose_prices` pin). The live-tray fix means the fleet deserves a code
deploy, not just acme (the chat.*/primeconnectai.* freeze covers
config/credential/plan writes, NOT code updates via the Updates tab).

**Today's mission — rehearse Phases 6+7 end to end on acme:**

0. Preconditions: verify both repos pushed (`git status` — suite already
   green end of day 2); deploy the dental update to acme via the Updates
   tab (instances pull from origin), then fleet-wide when comfortable —
   the live-tray transcript fix should reach the demo + PrimeConnect too;
   reload the console (restart it so the new config_manager module +
   Config tab load).
1. Config tab, acme, BEFORE managed mode: Load (source should be "api"),
   check drift (expect some missing shipped defaults — new fields like
   site.managed won't be in acme's volume file until written), save a
   business field (site_name tweak), save an infra field (llm_temperature)
   — both should work since acme is unmanaged.
2. Enable managed mode on acme (type the name to confirm; watch the steps:
   .env token → yaml flag → recreate → verify). Then: business save still
   works; infra save WITHOUT token would 403 (the console has the token, so
   test that path from acme's own admin panel instead).
3. acme's clinic admin panel (browser, YOU drive it — agents must never):
   hard-reload so SW picks up v18; cfg-llm button gone; Reservas hides
   demo-mode/embed/max-chars and still saves; Email/SMS/Voz show the
   "managed by your provider" note, SMTP password field blank (redaction);
   Seguridad still changes a password. While you're in there: send a chat
   message to acme's widget and open the live-chat tray window — the
   transcript must now SHOW the messages (this verifies the day-2
   live-tray baseline fix live).
4. Model switching via the console picker (rate-limit notes from the
   mistral source set should render); flip acme llm_model small↔large and
   back, verify a chat round-trip after each.
5. Drift: vim one console-written field in acme's /data/site_config.yaml
   over SSH, run Check drift → it must appear under out-of-band; fix it
   back through the Config tab.
6. Disable managed mode, confirm acme returns to normal. Re-enable if you
   want acme to stay the managed-mode guinea pig (recommended).

**Then, if time remains:** convert PrimeConnect (unfreeze → enable managed
→ refreeze) as the first real managed instance; or start Phase 8 (voice
metering, plan doc Part 4).

**Leftovers from day 2's sweep, still open:**
- **Mistral privacy — ACTION REQUIRED (open item 10, researched 2026-07-20):
  the free API plan is opted IN to model training by default** (Mistral
  help center). Fix now: Mistral Admin Console → Privacy → disable
  "Anonymous improvement data" for the fleet key's workspace. Even opted
  out, retention is ~30 days for abuse monitoring; ZDR exists but only on
  the Scale plan (stateless calls only). For medical clients (Valor runs
  LLM+STT on this key) plan a paid/ZDR second mistral source
  ("compliance tiers as separate sources") before real patient volume.
- Reset acme's test plan (still 1000 tokens/demo → set ~500k, push plan,
  gauge green) — Tokens tab, 2 minutes.
- Upload google_tts.json to the vault (kind "TTS — google file", tier
  free, notes: project id + ~1M Neural2 chars/month free, resets on
  Google's billing calendar) and "Record only" onto Primary Demo +
  PrimeConnect, if not done.

**Parked (don't start unless asked):** plan presets, per-model buy
pricing, Voxtral Spanish voice for Valor, mistral key split (fleet vs
personal — related to the ZDR item above), Phase 8 voice metering,
clinic-branded cloned voices.

Work the same way: build in the connected folders, syntax-check, commit
files to disk, give me the git commands, acme is the crash-test dummy,
never run pytest yourself, never drive the live app.
