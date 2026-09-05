"""Deterministic launch projection of a sealed manifest (E1-B.4 §14).

The production Copilot investigation may see ONLY the real provider launch
reference — provider/resource_type/address_id/business_id — never the oracle,
events, or the full sealed object. `EvaluationLaunchRef` maps 1:1 onto the
production ExternalResourceRef used to start an investigation.
"""

from __future__ import annotations

from .contracts import EvaluationLaunchRef, SealedManifest

__all__ = ["launch_ref"]


def launch_ref(manifest: SealedManifest) -> EvaluationLaunchRef:
    """Return the ONLY production-safe projection of ``manifest``.

    Guarantees: provider = source alert provider (hisiem), resource_type = "alert",
    address_id = the REAL HISIEM alert ES _id copied verbatim from the resolved
    source alert (never derived), business_id = optional display metadata. No
    oracle/events/code/integrity content can reach the launcher through this type.
    """
    return manifest.launch_projection
