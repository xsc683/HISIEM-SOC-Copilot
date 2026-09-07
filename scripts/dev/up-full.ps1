<#
.SYNOPSIS
    Bring up the FULL profile (Agent profile + HISIEM full data stack).

.DESCRIPTION
    Runs the Agent Evaluation core (up-agent) and additionally starts the HISIEM
    full data pipeline services (logstash + kafka + flink-jobmanager +
    flink-taskmanager + kibana) from the SIEM infra compose. This is the profile
    used for GP-01 dataset generation (E1-B.x) and end-to-end evaluation.

    The full stack is NOT part of the default Agent Evaluation profile; see
    docs/local-integrated-runtime.md for the profile boundary. Services that are
    already running are left untouched (idempotent).

    control-api (host JVM) and the Copilot API (host python) are handled exactly
    as in up-agent.ps1: detected, never auto-spawned for the JVM, and the
    Copilot API is started with a runtime-only HISIEM Bearer token.

.EXAMPLE
    .\scripts\dev\up-full.ps1
#>
[CmdletBinding()]
param(
    [string]$EnvFile = "$PSScriptRoot\..\..\.env.local"
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$hisiemCompose = 'D:\Project\SIEM\infra\docker-compose.yml'

# 1. Bring up the Agent core (containers + control-api check + token + Copilot).
& "$PSScriptRoot\up-agent.ps1" -EnvFile $EnvFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "up-full: agent core failed (exit $LASTEXITCODE); not starting full data stack." -ForegroundColor Red
    exit $LASTEXITCODE
}

# 2. Full data pipeline services (Kafka, Logstash, Flink, Kibana). These belong
#    to the FULL profile only — never the Agent profile (E1-C0 §4).
if (-not (Test-Path $hisiemCompose)) {
    Write-Host "HISIEM compose not found at $hisiemCompose" -ForegroundColor Red
    exit 5
}
Write-Host 'Starting HISIEM full data stack (kafka + logstash + flink + kibana) ...' -ForegroundColor Cyan
docker compose -p infra -f $hisiemCompose up -d kafka logstash flink-jobmanager flink-taskmanager kibana
if ($LASTEXITCODE -ne 0) {
    Write-Host 'up-full: HISIEM full data stack failed to start.' -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ''
Write-Host 'FULL profile is up. Run status.ps1 for a full status readout.' -ForegroundColor Green
Write-Host 'NOTE: GP-01 dataset generation additionally requires the Flink detection'
Write-Host '      job jar submission and Kafka topics (see docs/local-integrated-runtime.md).' -ForegroundColor Yellow
exit 0
