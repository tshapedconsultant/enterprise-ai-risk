"""
Pydantic schemas for the Enterprise AI Risk Assessment API.

These models serve three purposes:
  1. Validate incoming vendor intake (VendorInput)
  2. Enforce the LLM structured-output shape (AssessmentResponse) via OpenAI parse()
  3. Shape Jira Cloud create-issue payloads (JiraTicket → fields)

The AssessmentResponse schema mirrors the governance JSON contract described
in the assessment agent prompt — suitable for CI/CD posting to Jira and for
downstream audit trails.
"""

from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.enums import (
    Confidence,
    ControlStatus,
    DataCategory,
    DpaReviewStatus,
    DpiaStatus,
    EvidenceStatus,
    GateStatus,
    WorkflowStatus,
)

TEXT_SHORT = 200
TEXT_MED = 4000
TEXT_LONG = 8000


# ---------------------------------------------------------------------------
# Vendor intake — maps to Section 1 of the governance assessment prompt
# ---------------------------------------------------------------------------
class VendorInput(BaseModel):
    """Everything the assessor knows about the third party at intake time."""

    vendor_name: str = Field(..., min_length=1, max_length=TEXT_SHORT, description="Name of the third-party AI vendor")

    @field_validator("vendor_name")
    @classmethod
    def vendor_name_single_line(cls, value: str) -> str:
        if any(ch in value for ch in ("\n", "\r", "\x00")):
            raise ValueError("vendor_name must be a single line")
        return value.strip()
    service_description: str = Field(..., min_length=1, max_length=TEXT_MED, description="AI system or service description")
    intended_use: str = Field(..., min_length=1, max_length=TEXT_MED, description="How the organization intends to use the service")
    data_processed: str = Field(
        ...,
        min_length=1,
        max_length=TEXT_MED,
        description="Data processed, classification, and users affected",
    )
    has_dpa: bool = Field(
        default=False,
        description="Declared DPA checkbox. Does not prove a signed agreement exists.",
    )
    dpa_document_id: Optional[str] = Field(default=None, max_length=TEXT_SHORT, description="Internal document id if a DPA file exists")
    dpa_signed: Optional[bool] = Field(default=None, description="Whether a signed DPA was verified by a human")
    dpa_review_status: DpaReviewStatus = Field(
        default=DpaReviewStatus.NOT_PROVIDED,
        description="Lifecycle of the DPA artifact, independent from has_dpa",
    )
    data_categories: List[DataCategory] = Field(
        default_factory=list,
        description="Structured data classes declared at intake. Keyword inference is only a signal.",
    )
    business_owner: Optional[str] = Field(default=None, max_length=TEXT_SHORT, description="Internal business owner")
    geographic_scope: Optional[str] = Field(default=None, max_length=TEXT_MED, description="Countries / regions in scope")
    model_provider: Optional[str] = Field(default=None, max_length=TEXT_SHORT, description="Foundation model or API provider")
    integration_architecture: Optional[str] = Field(
        default=None,
        max_length=TEXT_MED,
        description="How the vendor is integrated (API, SaaS UI, plugin, on-prem)",
    )
    regulatory_context: Optional[str] = Field(
        default=None,
        max_length=TEXT_MED,
        description="Known regulatory context (GDPR, EU AI Act, NIS2, DORA, HIPAA, etc.)",
    )
    privacy_assessment_required: Optional[bool] = Field(
        default=None,
        description="Whether a DPIA/PIA is required. None = infer from facts.",
    )
    dpia_status: Optional[DpiaStatus] = Field(
        default=None,
        description="unknown | not_started | in_progress | completed | not_required",
    )
    dpia_reference: Optional[str] = Field(default=None, max_length=TEXT_SHORT, description="Internal DPIA identifier if completed")
    processing_purposes: Optional[str] = Field(default=None, max_length=TEXT_MED, description="Purposes of processing")
    data_subjects: Optional[str] = Field(default=None, max_length=TEXT_MED, description="Categories of data subjects")
    special_category_data: bool = Field(default=False, description="GDPR Article 9 special categories")
    international_transfers: Optional[str] = Field(
        default=None,
        max_length=TEXT_MED,
        description="none | unknown | description of destinations / SCCs",
    )
    retention_period: Optional[str] = Field(default=None, max_length=TEXT_SHORT, description="Declared retention")
    automated_decision_making: bool = Field(
        default=False,
        description="Whether the use includes automated decisions with legal or similar effect",
    )
    privacy_risk_level: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Business-declared privacy risk; does not override the engine",
    )


# ---------------------------------------------------------------------------
# Jira integration — fields map directly to Jira REST API v3 issue create
# ---------------------------------------------------------------------------
class JiraProject(BaseModel):
    """Target Jira project; default AIGOV = AI Governance board."""

    key: str = "AIGOV"


class JiraIssueType(BaseModel):
    name: str = "Task"


class JiraPriority(BaseModel):
    """Jira priority name — must match project configuration (Highest/High/Medium/Low)."""

    name: str


class JiraFields(BaseModel):
    """Top-level 'fields' object for POST /rest/api/3/issue, plus workflow metadata."""

    project: JiraProject
    summary: str = Field(..., max_length=255)
    description: str = Field(..., max_length=TEXT_LONG)
    issuetype: JiraIssueType
    priority: JiraPriority
    labels: List[str]
    assignee: Optional[Dict[str, str]] = None
    parent: Optional[Dict[str, str]] = None
    department: Optional[str] = None  # parent | legal | infosec | aigov — stripped before Jira POST
    issue_key: Optional[str] = None  # filled after outbound create


class JiraTicket(BaseModel):
    """One remediation ticket ready for CI/CD automation."""

    fields: JiraFields


# ---------------------------------------------------------------------------
# Assessment output — machine-readable governance record
# ---------------------------------------------------------------------------
class AssessmentMetadata(BaseModel):
    """High-level decision summary for dashboards and audit logs."""

    assessment_id: str = Field(..., description="Stable UUID for webhook and chat correlation")
    vendor: str
    assessment_date: str  # ISO date YYYY-MM-DD (UTC)
    decision: str  # Engine triage only — never a final APPROVE
    overall_residual_risk: str  # Very Low | Low | Moderate | High | Critical
    applicable_frameworks: List[str] = Field(
        default_factory=list,
        description="Enabled compliance frameworks (COMPLIANCE_FRAMEWORKS). GDPR is enforced; others are alignment.",
    )

    @field_validator("assessment_id")
    @classmethod
    def assessment_id_is_uuid(cls, value: str) -> str:
        UUID(value)
        return value


class FrameworkAlignment(BaseModel):
    """Control gaps mapped to NIST AI RMF and ISO/IEC 42001."""

    nist_ai_rmf_gaps: List[str]
    iso_42001_gaps: List[str]


class EvidenceItem(BaseModel):
    """One auditable evidence record. Distinct from a finding (which is a conclusion)."""

    control_id: str
    source: str
    document: str
    page: Optional[str] = None
    excerpt: str
    evidence_status: EvidenceStatus
    collected_at: str
    confidence: Confidence


class ControlAssessment(BaseModel):
    """Result of evaluating one control against evidence."""

    control_id: str
    name: str
    status: ControlStatus
    nist_ai_rmf: str
    iso_42001: str
    rationale: str


class DecisionRecord(BaseModel):
    """Trail of the engine triage plus human gates closed in Jira."""

    decision: str
    decision_basis: List[str]
    controls_triggered: List[str]
    evidence_used: List[str]
    risk_score: int = Field(..., ge=1, le=5)
    residual_risk: str
    reviewer: str
    timestamp: str
    model_version: str
    scoring_method: str = "deterministic"
    llm_used_for_decision: bool = False
    workflow_status: WorkflowStatus = WorkflowStatus.HUMAN_REVIEW_REQUIRED
    human_decision: Optional[str] = None
    legal_approver: Optional[str] = None
    legal_approved_at: Optional[str] = None
    secops_approver: Optional[str] = None
    secops_approved_at: Optional[str] = None
    aigov_approver: Optional[str] = None
    aigov_approved_at: Optional[str] = None
    required_departments: List[str] = Field(default_factory=lambda: ["legal", "infosec", "aigov"])
    gate_status: Dict[str, GateStatus] = Field(default_factory=dict)


class PrivacyTriage(BaseModel):
    """GDPR/DPIA snapshot. Complements has_dpa; does not replace Legal."""

    personal_data_inferred: bool
    privacy_assessment_required: bool
    inferred_required: bool = False
    declared_required: Optional[bool] = None
    legal_review_required: bool = True
    dpia_decision_basis: str = "DPIA review indicated — not a legal determination"
    dpia_status: DpiaStatus
    dpia_reference: Optional[str] = None
    processing_purposes: Optional[str] = None
    data_subjects: Optional[str] = None
    special_category_data: bool = False
    international_transfers: str = "unknown"
    retention_period: Optional[str] = None
    automated_decision_making: bool = False
    privacy_risk_level: str
    gdpr_notes: str
    inferred_data_categories: List[str] = Field(default_factory=list)
    declared_data_categories: List[str] = Field(default_factory=list)


class GovernanceEvidencePack(BaseModel):
    """Machine-readable pack for audit, CI/CD, and (later) PDF export."""

    risk_decision: Dict[str, Any]
    evidence_matrix: List[Dict[str, Any]]
    gdpr_dpia: Dict[str, Any]
    eu_ai_act_applicability: str
    nist_ai_rmf_mapping: List[str]
    iso_42001_mapping: List[str]
    critical_findings: List[str]
    remediation_plan: List[str]
    jira_tickets: List[Dict[str, Any]]
    decision_audit_trail: Dict[str, Any]


class AssessmentResponse(BaseModel):
    """
    Full assessment payload returned by POST /api/v1/assess-vendor.

    Decision and residual risk always come from the deterministic engine.
    evidence_gaps remains a string list for the UI; evidence_items is the audit object.
    The static console renders evidence_items in the Missing evidence tab with
    progressive disclosure (control id + status chip, excerpt on expand).
    """

    assessment_metadata: AssessmentMetadata
    framework_alignment: FrameworkAlignment
    critical_findings: List[str]
    evidence_gaps: List[str]
    jira_tickets: List[JiraTicket]
    evidence_items: List[EvidenceItem] = Field(default_factory=list)
    control_assessments: List[ControlAssessment] = Field(default_factory=list)
    decision_record: Optional[DecisionRecord] = None
    privacy_triage: Optional[PrivacyTriage] = None
    evidence_pack: Optional[GovernanceEvidencePack] = None
    risk_factors: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Chat — follow-up Q&A about the current assessment
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    """One turn in the conversation history sent to the chat endpoint."""

    role: Literal["user", "assistant"] = Field(..., description="user or assistant")
    content: str = Field(..., min_length=1, max_length=TEXT_MED)


class ChatRequest(BaseModel):
    """User question plus prior turns so the LLM can maintain context."""

    message: str = Field(..., min_length=1, max_length=TEXT_MED)
    history: List[ChatMessage] = Field(default_factory=list, max_length=20)
    assessment_id: Optional[str] = Field(
        default=None,
        description="Target assessment UUID. Required to load a report; there is no global latest assessment.",
    )

    @field_validator("assessment_id")
    @classmethod
    def chat_assessment_id_is_uuid(cls, value: Optional[str]) -> Optional[str]:
        if value:
            UUID(value)
        return value


class ChatResponse(BaseModel):
    """
    Assistant reply plus echoed metadata for the client.

    The browser renders structured cards and chips from AssessmentResponse directly.
    These optional fields let lightweight clients show summary chips without re-parsing
    the full assessment JSON.

    used_llm indicates whether OpenAI was called or the keyword mock answered.
    """

    reply: str
    vendor: Optional[str] = None
    decision: Optional[str] = None
    overall_residual_risk: Optional[str] = None
    used_llm: bool = False
    request_id: Optional[str] = None
