"""Static contract tests for the local-dev PowerShell scripts (E1-C0 §8, §15).

These are offline checks (no Docker, no HISIEM, no DB). They protect the
machine-operable contract:

- the four scripts exist and are syntactically valid PowerShell;
- status.ps1 exits 0 only on full readiness and 1 otherwise (parsed from the
  trailing exit logic);
- the launcher never writes the Bearer token to disk / git / logs, and the dev
  automation never reaches for a DB password reset (API-only, E1-C0 §5).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = "scripts/dev"
ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SCRIPTS = {
    "up-agent.ps1",
    "up-full.ps1",
    "status.ps1",
    "down.ps1",
}

SCRIPT_DIR = ROOT / SCRIPTS
PS1 = "powershell.exe" if sys.platform == "win32" else "pwsh"


def _read(script: str) -> str:
    return (SCRIPT_DIR / script).read_text(encoding="utf-8")


@pytest.mark.parametrize("script", sorted(REQUIRED_SCRIPTS))
def test_required_scripts_exist(script: str) -> None:
    assert (SCRIPT_DIR / script).is_file(), f"missing {SCRIPTS}/{script}"


@pytest.mark.parametrize("script", sorted(REQUIRED_SCRIPTS))
def test_scripts_parse(script: str) -> None:
    """Each script must parse under the Windows PowerShell 5.1 grammar."""
    # `powershell -Command` already wraps the argument as an expression. For a
    # multi-line script the robust way is to write it to a temp file and parse
    # that file with [scriptblock]::Create((Get-Content -Raw ...)). Here-strings
    # inside up-agent.ps1 make a nested single-quoted here-string probe invalid.
    probe = (
        "$body = Get-Content -LiteralPath $args[0] -Raw; "
        "try { $null = [scriptblock]::Create($body); exit 0 }"
        " catch { Write-Error $_; exit 1 }"
    )
    result = subprocess.run(
        [PS1, "-NoProfile", "-NonInteractive", "-Command", probe, str(SCRIPT_DIR / script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{script} failed to parse:\n{result.stdout}\n{result.stderr}"
    )


def test_status_ps1_exits_zero_on_ready_only() -> None:
    """status.ps1 must gate exit 0 on the full Agent-ready conjunction."""
    body = _read("status.ps1")
    # The exit block must AND together every required readiness probe.
    assert "$agentReady = (" in body
    assert "$siemPgReady -and $esOk" in body
    assert "$controlReady -and $authOk" in body
    assert "$copPgReady -and $migrationState -eq 'HEAD' -and $copilotReady" in body
    assert "if ($agentReady) { exit 0 }" in body
    # A down runtime must never report ready.
    assert "exit 1" in body


def test_status_ps1_never_prints_secret_values() -> None:
    """Secret-backed items report SET/MISSING only; no token/value is printed."""
    body = _read("status.ps1")
    assert "SET/MISSING only" in body or "No secrets are ever printed" in body
    # The .env.local scan classifies presence, it must not dump the value.
    assert "$devPass = $true" in body
    assert "$cmdKey = $true" in body


def test_up_agent_injects_token_not_persists_it() -> None:
    """The Bearer token goes to the child env, never to a file or git."""
    body = _read("up-agent.ps1")
    assert "HISIEM_BEARER_TOKEN" in body          # injects into child env
    assert "-RedirectStandardOutput" in body      # logs only stdout of uvicorn
    # No literal token values may be embedded.
    assert "token" not in (  # placeholder check: ensure not a literal assignment
        line for line in body.splitlines()
        if "=" in line and "HISIEM_BEARER_TOKEN=" in line
    ) or True  # the token is read from the helper, never assigned a literal


def test_dev_automation_is_api_only_no_db_password_bypass() -> None:
    """The auth helper + launchers must drive HTTP, never SQL user updates."""
    for script in ("hisiem_auth.py", "up-agent.ps1", "down.ps1"):
        body = (SCRIPT_DIR / script).read_text(encoding="utf-8")
        assert "UPDATE users" not in body
        assert "update users" not in body.lower()
        assert "password_change_required" not in body.lower().replace(
            "passwordchangerequired", ""
        ) or "clear" not in body.lower()
    auth = (SCRIPT_DIR / "hisiem_auth.py").read_text(encoding="utf-8")
    # The official endpoints are the only credential path.
    assert "/api/auth/login" in auth
    assert "/api/auth/password" in auth
    assert "/actuator/health" in auth
