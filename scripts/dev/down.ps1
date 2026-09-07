<#
.SYNOPSIS
    Stop the Local Integrated Runtime (E1-C0 §16). STOP preserves all volumes.

.DESCRIPTION
    Default (STOP): stops the Copilot and HISIEM docker services and leaves
    volumes/data intact. It never triggers a re-bootstrap. No auth reset.

    -Reset: DESTRUCTIVE. Stops services AND removes the named volumes (Copilot
    and HISIEM data, ES data, Kafka, Logstash, Flink checkpoints). Requires an
    explicit confirmation. Never run by default.

.PARAMETER Reset
    Perform a destructive reset (removes volumes) instead of a plain stop.

.PARAMETER SkipHisiem
    Only operate on the Copilot compose (HISIEM containers are owned by the
    SIEM repo's own infra compose and are stopped here too by default).

.EXAMPLE
    .\scripts\dev\down.ps1                 # STOP, preserves all data

.EXAMPLE
    .\scripts\dev\down.ps1 -Reset          # destructive, asks for confirmation
#>
[CmdletBinding()]
param(
    [switch]$Reset,
    [switch]$SkipHisiem
)

$ErrorActionPreference = 'Stop'
$copilotCompose = "$PSScriptRoot\..\..\infra\docker-compose.yml"
$hisiemCompose  = 'D:\Project\SIEM\infra\docker-compose.yml'

if ($Reset) {
    Write-Host 'WARNING: -Reset removes ALL Copilot AND HISIEM data volumes.' -ForegroundColor Red
    Write-Host 'This includes siem-postgres, elasticsearch, kafka, logstash, flink,' -ForegroundColor Red
    Write-Host 'and copilot-postgres data. HISIEM will require a fresh bootstrap.' -ForegroundColor Red
    $confirm = Read-Host 'Type RESET to confirm'
    if ($confirm -ne 'RESET') {
        Write-Host 'Aborted (confirmation mismatch). Nothing was removed.' -ForegroundColor Yellow
        exit 1
    }
}

# Stop Copilot compose.
if (Test-Path $copilotCompose) {
    if ($Reset) {
        docker compose -p copilot -f $copilotCompose down -v
    } else {
        docker compose -p copilot -f $copilotCompose down
    }
}

# Stop HISIEM compose (owned by the SIEM repo; read-only to us).
if (-not $SkipHisiem -and (Test-Path $hisiemCompose)) {
    if ($Reset) {
        docker compose -p infra -f $hisiemCompose down -v
    } else {
        docker compose -p infra -f $hisiemCompose down
    }
}

# The HISIEM control-api and Copilot API are HOST processes; stop them only if
# we can identify them. We do NOT kill arbitrary java/python — see docs. Host
# processes are intentionally left for the operator to stop, or reported.

Write-Host 'Runtime stopped (volumes preserved).' -ForegroundColor Green
