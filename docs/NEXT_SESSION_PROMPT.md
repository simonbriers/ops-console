# Kickoff prompt — token-economy overhaul, day 2

Copy-paste everything below the line into a fresh session with the
`C:\AI\ops-console` and `C:\AI\dental-clinic-agent` folders connected.

---

We are mid-way through the token-economy / managed-service overhaul of my
ops-console + dental-clinic-agent. **Read
`ops-console/docs/TOKEN_ECONOMY_PLAN.md` first — it has the full design,
all settled decisions (D1–D8), the phase plan, and the day-1 status
block.** Short version of where we stand:

**DONE and verified (Phases 0–5):** acme is the dev guinea-pig instance
(port 8003; chat.* demo and primeconnectai.* are FROZEN — never apply
credentials, push plans, or write config to them; rehearse everything on
acme). The vault is v2 (roles llm/stt/tts/email/sms, assignments,
reconcile with active-provider detection, rotation re-apply). The ledger
meters LLM tokens per client per key alias (sqlite + background
collector), prices both sides in € (buy rates per source, sell rates +
base fee + token allowance + overage per plan), fires 80/100% alerts,
renders statements, and shows the animated tanks-and-pipes Flow tab.
Phase 5 works end to end: "Push plan" writes the allowance into an
instance's config via its validated admin API, and the clinic's own
dashboard shows the %-left gauge, red over-state banner, and sends
once-per-cycle warning emails. Verified live on acme yesterday.

**Key facts:** the mistral key is FREE tier (no monthly cap, only rate
limits — details in the vault set's notes), serves llm+stt+tts on one
key; google TTS = service-account file, free tier ~1M WaveNet-SKU
chars/month; paid-price reference table + fleet cost simulation (~$4.6/
month at paid rates!) is in the plan doc. The admin panel is a PWA — any
change to admin shell files (dashboard.html etc.) REQUIRES bumping
SHELL_CACHE in frontend/admin-sw.js or staff see stale pages. Instances
deploy by git pull from origin — local commits must be PUSHED before the
Updates tab can deliver them. Never run pytest in-session; never drive
the live app; docker compose RECREATE (not restart) for .env changes.

**Today's mission — Phase 6 (managed mode) + Phase 7 (config manager),
per the plan doc:**

1. Phase 6, product-side: `managed: true` flag in site_config; partition
   the six cfg-* admin tabs into business fields (clinic keeps) vs infra
   fields (LLM/voice providers+models, SMTP, security, demo_mode, embed
   domains, backups → console-only); server-side rejection of infra
   fields in SiteConfigPatch unless the request carries the new
   OPERATOR_TOKEN env var; tab-hiding via the existing role-gating
   pattern. Default false — unmanaged installs unchanged.
2. Phase 7, console-side: config manager module + per-client Config page
   mirroring the six tabs (each field tagged business/managed);
   API-first read/write via _admin_get_json/_admin_put_json (already
   built); SSH file read as fallback + drift detection (diff live vs
   last-written vs shipped defaults — closes the DEPLOYMENT.md §10
   stale-config gap); model switching UI with the rate-limit facts from
   the source notes shown next to the picker.
3. Rehearse both on acme end to end before touching anything else.

**Small leftovers to sweep first (~15 min):** verify all day-1 commits
are pushed in BOTH repos (git status); reset acme's test plan (it's on
1000 tokens/demo — keep demo type, set a sane allowance like 500k, push
plan again so the gauge shows green); upload google_tts.json to the
vault (kind "TTS — google file", tier free, notes with project id +
free-tier reading) and "Record only" it onto Primary Demo + PrimeConnect
if not done yet; check the Mistral privacy/ZDR terms for medical clients
(open item 10) when there's a moment.

**Parked ideas (don't start unless asked):** plan presets (demo/trial/
S/M/L dropdown), per-model buy pricing, Voxtral Spanish voice test for
Clínica Valor (EU replacement for its bad piper voice), mistral key
split (fleet vs personal), Phase 8 voice metering, clinic-branded cloned
voices as a premium feature.

Work the same way as day 1: build in the connected folders, syntax-check
everything, commit files to disk, give me the git commands, and use acme
as the crash-test dummy.
