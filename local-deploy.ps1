# Windows PowerShell local deploy script for ops-console (Docker path).
# Rebuilds the image from the current working tree and restarts the
# container — the Docker path bakes code into the image at build time, so
# code changes are invisible until this runs. Your data (clients.json,
# history.jsonl, deploy_log.jsonl, the vault) lives in the app_data_local
# named volume and survives every rebuild — this only replaces the code.
#
# Not needed for the plain-venv path: `uvicorn backend.main:app --reload`
# picks up backend changes by itself, and the frontend's ?v= cache-buster
# handles the rest on a plain browser reload.
$ErrorActionPreference = "Stop"

function Log ($msg, $color = "Cyan") {
    $time = Get-Date -Format "HH:mm:ss"
    Write-Host "[$time] $msg" -ForegroundColor $color
}

Log "Step 1/3: Stopping the running ops-console container..." "Yellow"
docker compose -f docker-compose.local.yml down --remove-orphans

Log "Step 2/3: Building and starting the new one..." "Yellow"
docker compose -f docker-compose.local.yml up -d --build

Log "Step 3/3: Waiting for /health on http://127.0.0.1:8101 ..." "Yellow"
$healthy = $false
for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 2
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 http://127.0.0.1:8101/health
        if ($resp.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
        Log "  not answering yet (attempt $i/10)..." "DarkGray"
    }
}

if ($healthy) {
    Log "Success! ops-console is up." "Green"
    Log "Open in browser: http://127.0.0.1:8101" "Green"
} else {
    Write-Error ("ops-console did not answer on http://127.0.0.1:8101/health after ~20s - " +
                 "check: docker compose -f docker-compose.local.yml logs app")
    exit 1
}
