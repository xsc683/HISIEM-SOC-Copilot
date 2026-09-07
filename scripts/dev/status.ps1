<#
.SYNOPSIS
    Local Integrated Runtime status (E1-C0 §15). Machine-readable + human-readable.

.DESCRIPTION
    Reports READY/DOWN/FAILED for each service in the local Agent Evaluation
    profile. Uses HTTP readiness endpoints (never just "port listening"). The
    langgraph_checkpoint schema is LangGraph-owned and is not checked here.

    Exit code: 0 = Agent profile fully ready; non-zero = not ready.

    No secrets are ever printed. Secret-backed items report SET/MISSING only.

.EXAMPLE
    .\scripts\dev\status.ps1
#>
[CmdletBinding()]
param(
    [string]$EnvFile = "$PSScriptRoot\..\..\.env.local"
)

$ErrorActionPreference = 'SilentlyContinue'

# Load .env.local (gitignored dev config) so the readout reflects the configured
# runtime — DB URL, LLM provider, secrets presence — not just ambient process env.
$envFile = (Resolve-Path "$PSScriptRoot\..\..\.env.local" -ErrorAction SilentlyContinue).Path
if (-not $envFile) { $envFile = "$PSScriptRoot\..\..\.env.local" }
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#')) {
            $idx = $line.IndexOf('=')
            if ($idx -gt 0) {
                $k = $line.Substring(0, $idx).Trim()
                $v = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
                if (-not [Environment]::GetEnvironmentVariable($k)) {
                    [Environment]::SetEnvironmentVariable($k, $v, 'Process')
                }
            }
        }
    }
}

# ---- tiny helpers ----------------------------------------------------------
function Test-ActuatorUp {
    param([string]$Uri)
    try {
        $r = Invoke-RestMethod -Uri $Uri -TimeoutSec 5 -ErrorAction Stop
        return ($r.status -eq 'UP')
    } catch { return $false }
}

function Test-HttpOk {
    param([string]$Uri)
    try {
        $resp = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        return ($resp.StatusCode -eq 200)
    } catch { return $false }
}

function Test-ContainerRunning {
    param([string]$Name)
    $hits = docker ps --filter "name=^/$Name$" --filter "status=running" --format '{{.Names}}' 2>$null
    return ($hits -match [regex]::Escape($Name))
}

# ---- service probes ---------------------------------------------------------
$controlHealth = 'http://127.0.0.1:8080/actuator/health'
$copilotHealth = 'http://127.0.0.1:8000/healthz'
$esHealth      = 'http://127.0.0.1:9200/_cluster/health'

$controlReady = Test-ActuatorUp $controlHealth
$copilotReady = Test-HttpOk $copilotHealth
$esOk         = Test-HttpOk $esHealth
$siemPgReady  = Test-ContainerRunning 'siem-postgres'
$copPgReady   = Test-ContainerRunning 'copilot-postgres'

# HISIEM auth liveness: /api/auth/login is permitAll. An empty POST yields
# 400 (validation) or 401 (auth) — either proves the endpoint is live. No
# credentials are sent and none are printed.
$authOk = $false
if ($controlReady) {
    try {
        $resp = Invoke-WebRequest -Uri 'http://127.0.0.1:8080/api/auth/login' `
            -Method POST -ContentType 'application/json' -Body '{}' `
            -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        $authOk = ($resp.StatusCode -in 400, 401)
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        $authOk = ($code -in 400, 401)
    }
}

# ---- Copilot migration state (alembic current must equal head) ------------
$migrationState = 'FAILED'
$py  = "$PSScriptRoot\..\..\.venv\Scripts\python.exe"
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
if ($env:COPILOT_DATABASE_URL -and $copPgReady) {
    Push-Location $root
    $cur = (& $py -m alembic current 2>$null | Out-String)
    $head = (& $py -m alembic heads 2>$null | Out-String)
    Pop-Location
    if ($LASTEXITCODE -eq 0 -and $cur.Trim() -and $head.Trim() -and
        $cur.Contains($head.Trim())) {
        $migrationState = 'HEAD'
    } elseif ($LASTEXITCODE -eq 0) {
        $migrationState = 'DRIFT'
    }
} elseif (-not $env:COPILOT_DATABASE_URL) {
    $migrationState = 'MISSING_URL'
}

# ---- Model provider config readiness ---------------------------------------
$modelState = 'MISSING'
$provider = $env:LLM_PROVIDER
if ($provider -eq 'scripted') {
    $modelState = 'READY'          # deterministic provider, no key needed
} elseif ($provider -eq 'openai_compatible') {
    $keyEnv = if ($env:LLM_API_KEY_ENV) { $env:LLM_API_KEY_ENV } else { 'CMD_API_KEY' }
    $modelState = if ([Environment]::GetEnvironmentVariable($keyEnv)) { 'READY' } else { 'MISSING' }
}

# ---- .env.local secret presence (SET/MISSING only) -------------------------
$devUser = $false; $devPass = $false; $bootstrap = $false; $cmdKey = $false
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*HISIEM_DEV_USERNAME\s*=\s*\S+')       { $devUser = $true }
        if ($_ -match '^\s*HISIEM_DEV_PASSWORD\s*=\s*\S+')       { $devPass = $true }
        if ($_ -match '^\s*HISIEM_BOOTSTRAP_PASSWORD\s*=\s*\S+') { $bootstrap = $true }
        # CMD_API_KEY is only SET if it has a real (non-empty, non-placeholder)
        # value; an empty assignment is MISSING.
        if ($_ -match '^\s*CMD_API_KEY\s*=\s*(\S+)') {
            if ($matches[1] -notin @('', 'your-api-key', 'sk-placeholder')) { $cmdKey = $true }
        }
    }
}

# ---- Report ----------------------------------------------------------------
function Out-Status {
    param([string]$Name, [string]$State)
    '{0,-22} {1}' -f $Name, $State
}
Out-Status 'Profile' 'AGENT'
Out-Status 'HISIEM PostgreSQL' $(if ($siemPgReady) { 'READY' } else { 'DOWN' })
Out-Status 'Elasticsearch' $(if ($esOk) { 'READY' } else { 'DOWN' })
Out-Status 'HISIEM Control API' $(if ($controlReady) { 'READY' } else { 'DOWN' })
Out-Status 'HISIEM Auth' $(if ($authOk) { 'READY' } else { 'FAILED' })
Out-Status 'Copilot PostgreSQL' $(if ($copPgReady) { 'READY' } else { 'DOWN' })
Out-Status 'Copilot Migration' $migrationState
Out-Status 'Copilot API' $(if ($copilotReady) { 'READY' } else { 'DOWN' })
Out-Status 'Model Configuration' $modelState
Out-Status 'HISIEM_DEV_USERNAME' $(if ($devUser) { 'SET' } else { 'MISSING' })
Out-Status 'HISIEM_DEV_PASSWORD' $(if ($devPass) { 'SET' } else { 'MISSING' })
Out-Status 'HISIEM_BOOTSTRAP_PASSWORD' $(if ($bootstrap) { 'SET' } else { 'MISSING' })
Out-Status 'CMD_API_KEY' $(if ($cmdKey) { 'SET' } else { 'MISSING' })

# ---- Exit code -------------------------------------------------------------
$agentReady = ($siemPgReady -and $esOk -and $controlReady -and $authOk -and
    $copPgReady -and $migrationState -eq 'HEAD' -and $copilotReady)
if ($agentReady) { exit 0 }
exit 1
