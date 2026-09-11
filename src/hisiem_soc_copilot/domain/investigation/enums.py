"""Investigation domain enums.

Values match the V1 domain model / persistence schema exactly (VARCHAR storage).
"""

from __future__ import annotations

import enum


class InvestigationStatus(enum.StrEnum):
    """Business lifecycle status of the INVESTIGATION-ANALYSIS lifecycle only.

    The response workflow (proposal -> approval -> SOAR execution) is an
    INDEPENDENT post-completion aggregate lifecycle owned by ``ResponseProposal``
    and ``ResponseExecutionRef``. An Investigation therefore never enters an
    approval/execution status: approval, rejection, and provider execution state
    never change ``Investigation.status`` (domain-model.md §33-§38).
    """

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.FAILED,
            InvestigationStatus.CANCELLED,
        }

    @property
    def is_active(self) -> bool:
        return self in {
            InvestigationStatus.CREATED,
            InvestigationStatus.RUNNING,
        }


class InvestigationPhase(enum.StrEnum):
    """RUNNING-internal phase; may loop while status stays stable."""

    HYDRATING = "HYDRATING"
    PLANNING = "PLANNING"
    INVESTIGATING = "INVESTIGATING"
    VERIFYING = "VERIFYING"
    FINALIZING = "FINALIZING"


class TerminationReason(enum.StrEnum):
    """Optional reason recorded when an investigation reaches a terminal status."""

    COMPLETED_WITHOUT_RESPONSE = "COMPLETED_WITHOUT_RESPONSE"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"
    FAILED_START = "FAILED_START"
    FAILED_FATAL = "FAILED_FATAL"


class PlanStepStatus(enum.StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"


class HypothesisStatus(enum.StrEnum):
    OPEN = "OPEN"
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    UNRESOLVED = "UNRESOLVED"


class EvidenceRelation(enum.StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    CONTEXT = "CONTEXT"


class VerdictDisposition(enum.StrEnum):
    MALICIOUS = "MALICIOUS"
    BENIGN = "BENIGN"
    INCONCLUSIVE = "INCONCLUSIVE"


class EvidenceSourceType(enum.StrEnum):
    """Evidence source type per domain-model.md §9."""

    HISIEM_ALERT = "HISIEM_ALERT"
    HISIEM_EVENT = "HISIEM_EVENT"
    HISIEM_LOG_SEARCH = "HISIEM_LOG_SEARCH"
    HISIEM_ENTITY = "HISIEM_ENTITY"
    THREAT_INTEL = "THREAT_INTEL"
    KNOWLEDGE = "KNOWLEDGE"
    SYSTEM = "SYSTEM"


class ProvenanceAuthority(enum.StrEnum):
    """Data trust provenance authority (domain-model.md §42)."""

    PLATFORM_AUTHORITY = "PLATFORM_AUTHORITY"
    SYSTEM_AUTHORITY = "SYSTEM_AUTHORITY"
    HUMAN_AUTHORITY = "HUMAN_AUTHORITY"
    EXTERNAL_EVIDENCE = "EXTERNAL_EVIDENCE"
    MODEL_DERIVED = "MODEL_DERIVED"


class ModelOutputKind(enum.StrEnum):
    """Instruction-trust classification for model-produced content."""

    DATA_ONLY = "DATA_ONLY"
    CANDIDATE = "CANDIDATE"
    CONTROL_COMMAND = "CONTROL_COMMAND"
