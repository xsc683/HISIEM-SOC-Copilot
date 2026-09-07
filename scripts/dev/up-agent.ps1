<#
.SYNOPSIS
    Bring up the Agent Evaluation profile (E1-C0 §13). Core launcher.

.DESCRIPTION
    Prepares the minimal local runtime for Agent Evaluation:
      HISIEM siem-postgres (5432) + Elasticsearch (9200) + control-api (8080),
      Copilot copilot-postgres (5433), Copilot DB migrated, Copilot API (8000)
      with a runtime HISIEM Bearer token injected into the child env.

    HISIEM control-api is a HOST JVM process, not in any compose file. This
    launcher DETECTS it; if it is not running it prints a clear BLOCKED status
    and the exact start command rather than trying to spawn a JVM.

    The HISIEM Bearer token is obtained via the official HTTP auth flow
    (scripts/dev/hisiem_auth.py) and is passed ONLY to the Copilot child
    process environment. It is never written to disk or printed.

    Idempotent: safe to re-run. If a service is already up it is left running.

.EXAMPLE
    .\scripts\dev\up-agent.ps1
#>
[CmdletBinding()]
param(
    # Alternative .env.local path (default is the repo-local one).
    [string]$EnvFile = "$PSScriptRoot\..\..\.env.local"
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$py   = "$root\.venv\Scripts\python.exe"
$copilotCompose = "$root\infra\docker-compose.yml"
$hisiemCompose  = 'D:\Project\SIEM\infra\docker-compose.yml'
$authHelper = "$PSScriptRoot\hisiem_auth.py"
$dbHelper   = "$PSScriptRoot\copilot_db.py"

function Load-EnvLocal {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        Write-Host "NOTE: $Path not found. Set HISIEM_DEV_USERNAME / HISIEM_DEV_PASSWORD /" -ForegroundColor Yellow
        Write-Host "      CMD_API_KEY etc. there (gitignored) for a fully automatic run." -ForegroundColor Yellow
        return
    }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#')) {
            $idx = $line.IndexOf('=')
            if ($idx -gt 0) {
                $k = $line.Substring(0, $idx).Trim()
                $v = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
                [Environment]::SetEnvironmentVariable($k, $v, 'Process')
            }
        }
    }
}

function Test-ContainerRunning {
    param([string]$Name)
    $hits = docker ps --filter "name=^/$Name$" --filter "status=running" --format '{{.Names}}' 2>$null
    return ($hits -match [regex]::Escape($Name))
}

function Wait-Health {
    param([string]$Uri, [int]$Seconds = 60)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-RestMethod -Uri $Uri -TimeoutSec 3 -ErrorAction Stop
            if ($r.status -eq 'UP' -or $r.status -eq 'ok') { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Wait-PostgresReady {
    param([string]$Name, [int]$Seconds = 45)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        docker exec $Name pg_isready -U copilot -d copilot 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

Load-EnvLocal $EnvFile

# ---------------------------------------------------------------------------
# 1. Ensure the HISIEM minimal docker services (siem-postgres + elasticsearch)
#    are running. control-api is a host process handled separately.
# ---------------------------------------------------------------------------
if (-not (Test-ContainerRunning 'siem-postgres') -or -not (Test-ContainerRunning 'siem-elasticsearch')) {
    if (-not (Test-Path $hisiemCompose)) {
        Write-Error "HISIEM compose not found at $hisiemCompose"
    }
    Write-Host 'Starting HISIEM infrastructure (siem-postgres + elasticsearch) ...' -ForegroundColor Cyan
    docker compose -p infra -f $hisiemCompose up -d postgres elasticsearch
    if ($LASTEXITCODE -ne 0) { Write-Error 'docker compose up (HISIEM) failed' }
}

# 2. Ensure Copilot PostgreSQL is running (host 5433).
if (-not (Test-ContainerRunning 'copilot-postgres')) {
    Write-Host 'Starting Copilot PostgreSQL (host 5433) ...' -ForegroundColor Cyan
    docker compose -p copilot -f $copilotCompose up -d
    if ($LASTEXITCODE -ne 0) { Write-Error 'docker compose up (Copilot) failed' }
}
if (-not (Wait-PostgresReady 'copilot-postgres')) {
    Write-Error 'copilot-postgres did not become ready (pg_isready)'
}

# 3. Validate HISIEM control-api is up.
$controlReady = $false
try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/actuator/health' -TimeoutSec 5 -ErrorAction Stop
    $controlReady = ($h.status -eq 'UP')
} catch { $controlReady = $false }

if (-not $controlReady) {
    Write-Host @'

BLOCKED: HISIEM control-api is not running on 127.0.0.1:8080.

Start it from the HISIEM repo root (D:\Project\SIEM):

    cd D:\Project\SIEM
    java -jar applications/control-api/target/hsiem-platform.jar

(on first run, also export SIEM_BOOTSTRAP_PASSWORD=<12+ char temp password>)

Re-run this launcher once control-api reports UP.
'@ -ForegroundColor Red
    exit 2
}
Write-Host 'HISIEM control-api: READY' -ForegroundColor Green

# 4. Ensure the Copilot DB schemas exist (copilot + langgraph_checkpoint) and
#    migrate the copilot schema via the guarded helper.
Write-Host 'Ensuring Copilot DB schemas + migration ...' -ForegroundColor Cyan
& docker exec copilot-postgres psql -U copilot -d copilot -c 'CREATE SCHEMA IF NOT EXISTS copilot;' -c 'CREATE SCHEMA IF NOT EXISTS langgraph_checkpoint;'
$dbUrl = $env:COPILOT_DATABASE_URL
if (-not $dbUrl) {
    $dbUrl = 'postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot'
    $env:COPILOT_DATABASE_URL = $dbUrl
}
Write-Host "Copilot DB target: $dbUrl (guard active)" -ForegroundColor DarkGray
& $py $dbHelper
if ($LASTEXITCODE -ne 0) { Write-Error 'Copilot DB migration guard/apply failed' }
Write-Host 'Copilot DB migration: HEAD' -ForegroundColor Green

# 5. Obtain a runtime HISIEM Bearer token via the official HTTP auth flow.
#    hisiem_auth.py reads HISIEM_DEV_USERNAME / HISIEM_DEV_PASSWORD /
#    HISIEM_BOOTSTRAP_PASSWORD from the (already-loaded) process environment.
Write-Host 'Obtaining HISIEM runtime token (official HTTP auth) ...' -ForegroundColor Cyan
$token = (& $py $authHelper 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $token) {
    Write-Host 'BLOCKED: could not obtain a HISIEM Bearer token automatically.' -ForegroundColor Red
    Write-Host 'Check HISIEM_DEV_USERNAME / HISIEM_DEV_PASSWORD (and HISIEM_BOOTSTRAP_PASSWORD on first run) in .env.local.' -ForegroundColor Yellow
    exit 3
}
Write-Host 'HISIEM auth: READY (token obtained, not printed)' -ForegroundColor Green

# 6. Start the Copilot API/runtime (host python process) if not already up.
$copilotUp = $false
try {
    $ch = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/healthz' -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
    $copilotUp = ($ch.StatusCode -eq 200)
} catch { $copilotUp = $false }

$copilotPid = $null
if (-not $copilotUp) {
    Write-Host 'Starting Copilot API (uvicorn on 8000) ...' -ForegroundColor Cyan
    $env:HISIEM_BEARER_TOKEN = $token     # inject token into CHILD env only
    # Trusted context: dev/test header adapter (local Agent Evaluation only).
    if (-not $env:COPILOT_AUTH_TRUSTED_CONTEXT_PROVIDER) {
        $env:COPILOT_AUTH_TRUSTED_CONTEXT_PROVIDER = 'header'
    }
    $logFile = "$root\.runtime\copilot-api.log"
    New-Item -ItemType Directory -Force -Path (Split-Path $logFile) | Out-Null
    $proc = Start-Process -FilePath $py -ArgumentList '-m', 'hisiem_soc_copilot.main' `
        -WorkingDirectory $root -RedirectStandardOutput $logFile `
        -RedirectStandardError "$logFile.err" -PassThru -WindowStyle Hidden
    $copilotPid = $proc.Id
    Write-Host "Copilot API starting (PID $copilotPid) ..." -ForegroundColor Cyan
} else {
    Write-Host 'Copilot API already running: READY' -ForegroundColor Green
}

# 7. Readiness wait.
$apiReady = Wait-Health 'http://127.0.0.1:8000/healthz' 60
if (-not $apiReady) {
    Write-Host 'BLOCKED: Copilot API did not become ready on /healthz' -ForegroundColor Red
    if ($copilotPid) { Write-Host "check log: $logFile" }
    exit 4
}
Write-Host 'Copilot API /healthz: READY' -ForegroundColor Green

# 8. Sanitized final status (delegate to status.ps1 for the exit code).
Write-Host ''
& "$PSScriptRoot\status.ps1" -EnvFile $EnvFile
exit $LASTEXITCODE
