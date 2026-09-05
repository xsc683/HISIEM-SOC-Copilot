"""Deterministic GP-01 runtime identity derivation (E1-B.3 §5).

Every runtime entity is a pure SHA-256 function of ``run_id``: re-running the
same ``run_id`` reproduces the exact same detection identity while a fresh
``run_id`` normally gets a fresh attack source, so prior detection-suppression
state cannot contaminate a new run. No network, no host timezone, no randomness.
"""

from __future__ import annotations

import hashlib
from ipaddress import IPv4Address

from .contracts import RunIdentity

# RFC 2544 benchmarking space 198.18.0.0/15 — non-routable, deterministic and
# disjoint from any real customer address space. The /15 holds 2**17 addresses.
_BENCHMARK_BASE = 0xC6120000  # 198.18.0.0
_BENCHMARK_MASK = 0x1FFFF  # 17 low bits within the /15

_DOMAIN = b"hisiem-soc-copilot/evaluation/gp01-identity"


def _label_digest(run_id: str, label: str) -> bytes:
    """SHA-256 digest over a scoped ``label`` + ``run_id`` (32 bytes)."""
    material = _DOMAIN + b":" + label.encode("utf-8") + b":" + run_id.encode("utf-8")
    return hashlib.sha256(material).digest()


def _benchmark_ipv4(run_id: str, label: str) -> IPv4Address:
    """Deterministic address in 198.18.0.0/15 derived from ``run_id``."""
    offset = int.from_bytes(_label_digest(run_id, label)[0:4], "big") & _BENCHMARK_MASK
    return IPv4Address(_BENCHMARK_BASE + offset)


def _bump_watermark(attack: IPv4Address, watermark: IPv4Address) -> IPv4Address:
    """Return a watermark address that differs from ``attack`` (walk the space)."""
    offset = (int(watermark) - _BENCHMARK_BASE + 1) & _BENCHMARK_MASK
    candidate = IPv4Address(_BENCHMARK_BASE + offset)
    if candidate == attack:
        return _bump_watermark(attack, candidate)
    return candidate


def derive_run_identity(run_id: str, *, attack_source_ip: str | None = None) -> RunIdentity:
    """Derive the full runtime identity for ``run_id``.

    Deterministic; invariant ``attack_source_ip != watermark_source_ip`` always
    holds. Pass ``attack_source_ip`` to pin a specific attacker source; the
    watermark source is still guaranteed distinct.
    """
    if not run_id:
        raise ValueError("run_id must be a non-empty string")

    attack = _benchmark_ipv4(run_id, "attack")
    watermark = _bump_watermark(attack, _benchmark_ipv4(run_id, "watermark"))
    if attack_source_ip is not None:
        attack = IPv4Address(attack_source_ip)  # raises ValueError on bad IPv4
        if attack == watermark:
            watermark = _bump_watermark(attack, watermark)

    user_name = "svc" + _label_digest(run_id, "user").hex()[0:8]
    host_name = "app-" + _label_digest(run_id, "host").hex()[0:8]
    run_tag = "gp01-" + _label_digest(run_id, "tag").hex()[0:10]
    return RunIdentity(
        run_id=run_id,
        run_tag=run_tag,
        attack_source_ip=str(attack),
        watermark_source_ip=str(watermark),
        user_name=user_name,
        host_name=host_name,
    )


def derive_watermark_user_name(run_id: str) -> str:
    """Deterministic account used by the W1 watermark control event.

    Guaranteed distinct from the attack account (different prefix/domain), so W1
    can never satisfy the GP-01 same-account evidence requirement.
    """
    return "mkr" + _label_digest(run_id, "watermark-user").hex()[0:8]


def derive_event_process_id(run_id: str, role: str) -> int:
    """Plausible per-role sshd process id, deterministic for (run_id, role).

    The ssh-auth parser does not capture ``process.pid``, so any plausible value
    is acceptable; keeping it deterministic makes rendered lines reproducible.
    """
    raw = int.from_bytes(_label_digest(run_id, "pid:" + role)[0:2], "big")
    return 1000 + (raw % 59000)  # 1000..59999


__all__ = [
    "derive_event_process_id",
    "derive_run_identity",
    "derive_watermark_user_name",
]
