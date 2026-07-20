# One-shot: stamp the Mistral free-tier facts (2026-07-20) onto every
# mistral set in the running ops-console (http://127.0.0.1:8101).
# - tier -> free, owner stays as-is, values untouched (edit keeps them)
# - notes -> rate-limit fact sheet from admin.mistral.ai/plateforme/limits
# - ledger source rates -> buy €0/0/0, NO monthly cap (rate-limited pipe,
#   not a finite tank)
$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8101"

$notes = "Mistral FREE tier (facts 2026-07-20): no monthly volume cap - rate-limited pipe. " +
  "small-2506: 2.25M tok/min, 5 req/s (fleet workhorse). " +
  "large-2512: 250k tok/min, 0.07 req/s (~1 req/14s - needs key_2 fallback). " +
  "voxtral (STT/TTS): 50k tok/min, 1 req/s; audio 3600 s/min (concurrent-calls ceiling). " +
  "One key serves LLM+STT+TTS. Jul-2026 provider meter: 36.9M tok (fleet + personal mixed; " +
  "key split planned). Privacy/ZDR terms for medical clients: TO CHECK on plateforme/privacy."

$sets = Invoke-RestMethod "$base/api/vault/sets"
$mistral = @($sets | Where-Object { $_.kind -eq "mistral" })
if (-not $mistral) { Write-Host "No mistral sets found." -ForegroundColor Yellow; exit 1 }

foreach ($s in $mistral) {
    $body = @{ id = $s.id; name = $s.name; kind = $s.kind
               tier = "free"; notes = $notes } | ConvertTo-Json
    Invoke-RestMethod -Method Post -Uri "$base/api/vault/sets" `
        -ContentType "application/json" -Body $body | Out-Null
    Write-Host "vault set updated : $($s.name) -> tier=free, notes stamped" -ForegroundColor Green

    $rates = @{ set_id = $s.id; buy_in = 0; buy_cached = 0; buy_out = 0
                cap_tokens = $null } | ConvertTo-Json
    Invoke-RestMethod -Method Post -Uri "$base/api/ledger/source-rates" `
        -ContentType "application/json" -Body $rates | Out-Null
    Write-Host "ledger rates set  : $($s.name) -> buy 0/0/0, no cap (infinite pipe)" -ForegroundColor Green
}
Write-Host "`nDone - check the Credentials and Tokens tabs (reload the page)." -ForegroundColor Cyan
