"""E5 workspace fixture builders (Stage E / E5).

E5 must measure what the ANALYST WORKSPACE actually presents, so this module does
not invent a presentation: it reads the REAL workspace read model produced by
``application.services.workspace_service`` and the REAL authority classes produced by
the frontend's own ``web/src/utils/copilot.js`` (executed with Node), and compares
both against the persisted rows.

Test-support only: nothing here is production code, and nothing in ``src`` imports it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hisiem_soc_copilot.evaluation_harness.cross_plane_workspace import (
    AGENT_VERDICT,
    EXECUTION_RESULT,
    HUMAN_DECISION,
    KNOWLEDGE_CONTEXT,
    MODEL_FINDING,
    PLATFORM_FACT,
    POLICY_DECISION,
    PersistedWorkspaceFacts,
    PresentedWorkspace,
)

HISIEM_WEB = Path("D:/Project/SIEM/web")
COPILOT_JS = HISIEM_WEB / "src" / "utils" / "copilot.js"


def frontend_evidence_authority(source_types: Sequence[str]) -> dict[str, str]:
    """Run the REAL frontend authority derivation for each persisted source type.

    ``utils/copilot.js`` owns the mapping from a persisted evidence source type to the
    authority class the workspace renders. E5 executes that module rather than
    restating the mapping, so evaluation and the UI cannot drift apart.
    """
    unique = sorted(set(source_types))
    if not unique:
        return {}
    module_url = COPILOT_JS.as_posix()
    script = (
        f"import('file:///{module_url}').then(m=>{{"
        "const t=JSON.parse(process.argv[1]);const o={};"
        "for(const x of t){o[x]=m.evidenceAuthority(x)}"
        "console.log(JSON.stringify(o))})"
    )
    completed = subprocess.run(
        ["node", "-e", script, json.dumps(unique)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-800:]
    return {str(key): str(value) for key, value in json.loads(completed.stdout).items()}


def frontend_authority_labels() -> dict[str, str]:
    """The rendered authority labels the frontend uses (its own vocabulary)."""
    module_url = COPILOT_JS.as_posix()
    script = (
        f"import('file:///{module_url}').then(m=>{{"
        "console.log(JSON.stringify(m.AUTHORITY_LABELS))})"
    )
    completed = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-800:]
    return {str(key): str(value) for key, value in json.loads(completed.stdout).items()}


def presented_from_read_model(
    model: Any, *, evidence_authority: dict[str, str] | None = None
) -> PresentedWorkspace:
    """Project the REAL workspace read model into bounded presentation facts.

    ``evidence_authority`` maps evidence id → authority class as derived by the REAL
    frontend; when it is omitted the mapping is derived here from each evidence row's
    persisted source type through that same frontend function.
    """
    source_types = {
        str(item.evidence_id): str(item.source.type) for item in model.evidence
    }
    if evidence_authority is None:
        by_type = frontend_evidence_authority(list(source_types.values()))
        evidence_authority = {
            evidence_id: by_type.get(source_type, "UNKNOWN")
            for evidence_id, source_type in source_types.items()
        }

    proposal = model.response.proposals[0] if model.response.proposals else None
    approval = proposal.approval if proposal is not None else None
    decision = approval.decision if approval is not None and approval.decision else None
    submission = proposal.submission if proposal is not None else None
    execution = proposal.execution if proposal is not None else None

    return PresentedWorkspace(
        evidence_authority=dict(evidence_authority),
        finding_ids=tuple(str(item.finding_id) for item in model.findings),
        agent_verdict=(model.result.verdict.disposition if model.result is not None else ""),
        policy_decision=(proposal.policy_decision if proposal is not None else None),
        human_decision=(decision.decision if decision is not None else None),
        submission_status=(submission.status if submission is not None else None),
        execution_status=(execution.status if execution is not None else None),
        proposal_status=(proposal.status if proposal is not None else ""),
        timeline_statuses=tuple(
            str(entry.status) for entry in model.timeline if entry.status
        ),
    )


def persisted_from_read_model(model: Any) -> PersistedWorkspaceFacts:
    """The persisted truth the presentation is measured against.

    Read from the rows the projection was built from (evidence source types, findings,
    result verdict, proposal policy, approval decision, submission, execution) — never
    from the presentation itself.
    """
    proposal = model.response.proposals[0] if model.response.proposals else None
    approval = proposal.approval if proposal is not None else None
    decision = approval.decision if approval is not None and approval.decision else None
    submission = proposal.submission if proposal is not None else None
    execution = proposal.execution if proposal is not None else None

    return PersistedWorkspaceFacts(
        evidence_source_types={
            str(item.evidence_id): str(item.source.type) for item in model.evidence
        },
        evidence_ids=tuple(str(item.evidence_id) for item in model.evidence),
        finding_ids=tuple(str(item.finding_id) for item in model.findings),
        agent_verdict=(model.result.verdict.disposition if model.result is not None else ""),
        policy_decision=(proposal.policy_decision if proposal is not None else None),
        human_decision=(decision.decision if decision is not None else None),
        submission_status=(submission.status if submission is not None else None),
        execution_status=(execution.status if execution is not None else None),
        proposal_status=(proposal.status if proposal is not None else ""),
    )


def authority_classes_for_source_types(source_types: Sequence[str]) -> dict[str, str]:
    """Convenience: source type → frontend authority class (no evidence ids needed)."""
    return frontend_evidence_authority(source_types)


__all__ = [
    "AGENT_VERDICT",
    "EXECUTION_RESULT",
    "HUMAN_DECISION",
    "KNOWLEDGE_CONTEXT",
    "MODEL_FINDING",
    "PLATFORM_FACT",
    "POLICY_DECISION",
    "authority_classes_for_source_types",
    "frontend_authority_labels",
    "frontend_evidence_authority",
    "persisted_from_read_model",
    "presented_from_read_model",
]
