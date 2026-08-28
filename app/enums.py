"""Shared enumerations so workflow and DPA states are not magic strings."""

from __future__ import annotations

from enum import Enum


class WorkflowStatus(str, Enum):
    ENGINE_TRIAGE = "ENGINE_TRIAGE"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"
    DEPARTMENT_GATES_COMPLETED = "DEPARTMENT_GATES_COMPLETED"
    HUMAN_APPROVED_WITH_CONDITIONS = "HUMAN_APPROVED_WITH_CONDITIONS"
    HUMAN_REJECTED = "HUMAN_REJECTED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class HumanDecision(str, Enum):
    APPROVE_WITH_CONDITIONS = "APPROVE WITH CONDITIONS"
    REJECT = "REJECT"


class EngineDecision(str, Enum):
    PENDING_REVIEW = "PENDING REVIEW"
    REQUIRES_REMEDIATION = "REQUIRES REMEDIATION"
    ESCALATE = "ESCALATE TO AI GOVERNANCE / LEGAL / SECURITY"


class Department(str, Enum):
    LEGAL = "legal"
    INFOSEC = "infosec"
    AIGOV = "aigov"


class DpaReviewStatus(str, Enum):
    NOT_PROVIDED = "not_provided"
    DECLARED = "declared"
    RECEIVED = "received"
    REVIEWED = "reviewed"
    ACCEPTED = "accepted"


class DataCategory(str, Enum):
    PUBLIC = "public"
    PERSONAL = "personal"
    SPECIAL_CATEGORY = "special_category"
    FINANCIAL = "financial"
    HEALTH = "health"
    CREDENTIALS = "credentials"
    CONFIDENTIAL = "confidential"
    ANONYMOUS = "anonymous"


class GateStatus(str, Enum):
    REQUIRED_OPEN = "required_open"
    REQUIRED_CLOSED = "required_closed"
    NOT_APPLICABLE = "not_applicable"
    MISSING_TICKET = "missing_ticket"


class DpiaStatus(str, Enum):
    UNKNOWN = "unknown"
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NOT_REQUIRED = "not_required"


class EvidenceStatus(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    INSUFFICIENT = "insufficient"
    NOT_APPLICABLE = "not_applicable"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ControlStatus(str, Enum):
    PASS = "pass"
    PASS_DECLARED = "pass_declared"
    FAIL = "fail"
    DECLARED_UNVERIFIED = "declared_unverified"
    NOT_APPLICABLE = "not_applicable"
    DECLARED = "declared"
