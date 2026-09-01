"""
Deterministic AI governance scoring engine.

The LLM may explain an assessment in chat. It must not choose residual risk or
governance triage. Pipeline:

  Intake (facts) → per-control eval_* → risk factors → score (1–5) → triage decision
  → Jira Epic + department Tasks → DecisionRecord (HUMAN_REVIEW_REQUIRED)

Missing evidence never reduces residual risk. The engine never issues APPROVE;
humans close department gates in Jira (see app/jira_workflow.py). The static console
renders evidence_items in the Missing evidence tab; chat and the assistant card explain
the same JSON but do not change triage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple
from uuid import uuid4

from app.enums import (
    Confidence,
    ControlStatus,
    DataCategory,
    DpaReviewStatus,
    DpiaStatus,
    EngineDecision,
    EvidenceStatus,
    GateStatus,
    WorkflowStatus,
)
from app.jira_workflow import REQUIRED_DEPARTMENTS, build_epic_and_subtasks
from app.logging_config import get_logger
from app.models import (
    AssessmentMetadata,
    AssessmentResponse,
    ControlAssessment,
    DecisionRecord,
    EvidenceItem,
    FrameworkAlignment,
    GovernanceEvidencePack,
    PrivacyTriage,
    VendorInput,
)
from app.timeutil import utc_now

ENGINE_VERSION = "deterministic-rules-v1"

logger = get_logger("app.scoring")

PERSONAL_KEYWORD_TERMS = (
    "personal",
    "pii",
    "email",
    "emails",
    "name",
    "names",
    "employee",
    "patient",
    "phi",
    "health",
    "gdpr",
    "hr",
    "resume",
    "cv",
    "empleado",
    "cliente",
    "salud",
    "paciente",
    "telefono",
    "dirección",
    "direccion",
)

PERSONAL_PHRASES = (
    "customer data",
    "customer identifiers",
    "customer names",
    "user content",
    "chat history",
    "email address",
    "email addresses",
    "phone number",
    "home address",
    "ip address",
    "online identifier",
    "geolocation",
    "employee records",
    "hr data",
    "health data",
    "datos personales",
)

NEGATIVE_PHRASES = (
    "email delivery",
    "smtp",
    "public employee statistics",
    "aggregated statistics",
    "anonymous",
    "anonymised",
    "anonymized",
)

CATEGORY_HINTS = (
    (DataCategory.HEALTH, ("health", "phi", "patient", "salud", "paciente")),
    (DataCategory.CREDENTIALS, ("password", "credential", "secret", "api key")),
    (DataCategory.FINANCIAL, ("iban", "credit card", "payment card", "bank account")),
    (DataCategory.PERSONAL, ("email address", "phone", "customer identifiers", "employee records")),
)


def _intake_blob(payload: VendorInput) -> str:
    return " ".join(
        part
        for part in (
            payload.data_processed,
            payload.data_subjects or "",
            payload.processing_purposes or "",
        )
        if part
    ).lower()


def infer_data_categories(payload: VendorInput) -> List[str]:
    declared = [c.value if hasattr(c, "value") else str(c) for c in payload.data_categories]
    if declared:
        return declared
    blob = _intake_blob(payload)
    found: List[str] = []
    if payload.special_category_data:
        found.append(DataCategory.SPECIAL_CATEGORY.value)
        found.append(DataCategory.HEALTH.value)
    for category, hints in CATEGORY_HINTS:
        if any(re.search(rf"\b{re.escape(hint)}\b", blob) or hint in blob for hint in hints):
            found.append(category.value)
    if looks_like_personal_data(payload) and DataCategory.PERSONAL.value not in found:
        found.append(DataCategory.PERSONAL.value)
    return list(dict.fromkeys(found))


def looks_like_personal_data(payload: VendorInput) -> bool:
    """Auxiliary keyword signal. Structured data_categories always win when provided."""
    declared = {c.value if hasattr(c, "value") else str(c) for c in payload.data_categories}
    personal_declared = declared.intersection(
        {
            DataCategory.PERSONAL.value,
            DataCategory.SPECIAL_CATEGORY.value,
            DataCategory.HEALTH.value,
            DataCategory.FINANCIAL.value,
            DataCategory.CREDENTIALS.value,
        }
    )
    if personal_declared:
        return True
    if DataCategory.PUBLIC.value in declared or DataCategory.ANONYMOUS.value in declared:
        return payload.special_category_data
    if payload.special_category_data:
        return True
    blob = _intake_blob(payload)
    if any(phrase in blob for phrase in NEGATIVE_PHRASES) and not any(p in blob for p in PERSONAL_PHRASES):
        return False
    for phrase in PERSONAL_PHRASES:
        if phrase in blob:
            return True
    for term in PERSONAL_KEYWORD_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", blob):
            return True
    for category, hints in CATEGORY_HINTS:
        if category in {DataCategory.PUBLIC, DataCategory.ANONYMOUS}:
            continue
        if any(hint in blob for hint in hints):
            return True
    return False


def dpia_is_complete(payload: VendorInput) -> bool:
    return payload.dpia_status == DpiaStatus.COMPLETED and bool((payload.dpia_reference or "").strip())


def infer_privacy_signals(payload: VendorInput) -> Tuple[bool, bool, Optional[bool], str, List[str]]:
    personal = looks_like_personal_data(payload)
    categories = infer_data_categories(payload)
    inferred_required = bool(
        personal or payload.special_category_data or payload.automated_decision_making
    )
    declared = payload.privacy_assessment_required
    if payload.special_category_data or payload.automated_decision_making:
        indicated = True
        basis = "DPIA review indicated (Art. 9 / automated decisions). Not a legal determination."
    elif declared is True:
        indicated = True
        basis = "DPIA review indicated because the assessor declared it required."
    elif declared is False and personal:
        indicated = True
        basis = "DPIA review indicated: form said not required but personal data was inferred."
    elif declared is False:
        indicated = False
        basis = "No DPIA review indicated from declared facts; Legal still reviews the record."
    else:
        indicated = inferred_required
        basis = (
            "DPIA review indicated from inferred personal data."
            if indicated
            else "No personal-data signal; DPIA not inferred. Not a legal determination."
        )
    return personal, indicated, declared, basis, categories


def derive_triage_decision(
    *,
    personal: bool,
    has_dpa: bool,
    special_category: bool,
    dpia_ok: bool,
    need_dpia: bool,
    score: int,
) -> Tuple[str, str]:
    from app.frameworks import evaluate_gdpr_decision

    return evaluate_gdpr_decision(
        {
            "personal": personal,
            "has_dpa": has_dpa,
            "special_category": special_category,
            "dpia_ok": dpia_ok,
            "need_dpia": need_dpia,
            "score": score,
        }
    )


@dataclass
class ScoringState:
    """Mutable accumulator shared by eval_* control functions, then scoring and assembly."""

    payload: VendorInput
    now: datetime
    personal: bool
    need_dpia: bool
    declared_dpia: Optional[bool]
    dpia_basis: str
    inferred_categories: List[str]
    dpia_ok: bool
    dpa_declared: bool
    dpa_verified: bool
    dpa_status: DpaReviewStatus
    evidence: List[EvidenceItem] = field(default_factory=list)
    controls: List[ControlAssessment] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    factors: List[str] = field(default_factory=list)
    rules: List[str] = field(default_factory=list)
    nist: List[str] = field(default_factory=lambda: ["MEASURE-2.7", "MANAGE-4.1"])
    iso: List[str] = field(default_factory=lambda: ["8.2", "8.4"])
    geo: str = ""
    transfers: str = ""

    def add_evidence(
        self,
        control_id: str,
        status: EvidenceStatus,
        document: str,
        excerpt: str,
        confidence: Confidence = Confidence.HIGH,
        source: str = "intake",
    ) -> EvidenceItem:
        item = EvidenceItem(
            control_id=control_id,
            source=source,
            document=document,
            page=None,
            excerpt=excerpt,
            evidence_status=status,
            collected_at=self.now.isoformat(),
            confidence=confidence,
        )
        self.evidence.append(item)
        return item

    def add_control(
        self,
        control_id: str,
        name: str,
        status: ControlStatus,
        nist_id: str,
        iso_id: str,
        note: str,
    ) -> None:
        self.controls.append(
            ControlAssessment(
                control_id=control_id,
                name=name,
                status=status,
                nist_ai_rmf=nist_id,
                iso_42001=iso_id,
                rationale=note,
            )
        )


def _new_state(payload: VendorInput, now: datetime) -> ScoringState:
    personal, need_dpia, declared_dpia, dpia_basis, inferred_categories = infer_privacy_signals(payload)
    dpa_declared = bool(payload.has_dpa)
    if payload.dpa_review_status == DpaReviewStatus.NOT_PROVIDED and dpa_declared:
        dpa_status = DpaReviewStatus.DECLARED
    else:
        dpa_status = payload.dpa_review_status
    dpa_verified = bool(payload.dpa_signed) and dpa_status in {DpaReviewStatus.REVIEWED, DpaReviewStatus.ACCEPTED}
    return ScoringState(
        payload=payload,
        now=now,
        personal=personal,
        need_dpia=need_dpia,
        declared_dpia=declared_dpia,
        dpia_basis=dpia_basis,
        inferred_categories=inferred_categories,
        dpia_ok=dpia_is_complete(payload),
        dpa_declared=dpa_declared,
        dpa_verified=dpa_verified,
        dpa_status=dpa_status,
    )


def eval_dpa(state: ScoringState) -> None:
    payload = state.payload
    if state.dpa_verified:
        state.add_evidence(
            "CTRL-DPA",
            EvidenceStatus.PRESENT,
            payload.dpa_document_id or "DPA document",
            "Signed DPA recorded with review status accepted/reviewed.",
            confidence=Confidence.MEDIUM,
        )
        state.add_control(
            "CTRL-DPA",
            "Data processing agreement (DPA)",
            ControlStatus.PASS_DECLARED,
            "GOVERN-1.2",
            "5.4",
            f"dpa_review_status={state.dpa_status.value}; signed flag set. File not parsed in this console.",
        )
        state.rules.append("RULE-DPA-REVIEWED")
    elif state.dpa_declared:
        state.add_evidence(
            "CTRL-DPA",
            EvidenceStatus.INSUFFICIENT,
            "Intake checkbox has_dpa=true",
            "DPA declared. Signed PDF not attached / dpa_signed not verified — residual risk is not reduced to Low.",
            confidence=Confidence.MEDIUM,
        )
        state.add_control(
            "CTRL-DPA",
            "Data processing agreement (DPA)",
            ControlStatus.DECLARED_UNVERIFIED,
            "GOVERN-1.2",
            "5.4",
            "Checkbox checked; document not archived in this console.",
        )
        state.factors.append("DPA declared but file not attached")
        state.rules.append("RULE-DPA-DECLARED-UNVERIFIED")
    else:
        state.add_evidence(
            "CTRL-DPA",
            EvidenceStatus.MISSING,
            "Intake has_dpa=false",
            f"No executed DPA. data_processed={payload.data_processed!r}",
        )
        state.add_control(
            "CTRL-DPA",
            "Data processing agreement (DPA)",
            ControlStatus.FAIL,
            "GOVERN-1.1",
            "5.4",
            "has_dpa is false.",
        )
        state.nist.insert(0, "GOVERN-1.1")
        state.iso.insert(0, "6.1.4")
        state.findings.append(
            "No executed DPA. GDPR is not satisfied by an unchecked box: without a processor agreement there is no demonstrable controller–processor basis."
            if state.personal
            else "No executed DPA. Confirm whether personal data will be processed before production."
        )
        state.factors.append("No executed DPA")
        state.rules.append("RULE-DPA-MISSING")


def eval_dpia(state: ScoringState) -> None:
    payload = state.payload
    if payload.privacy_assessment_required is False and state.personal:
        state.findings.append(
            "Form marked DPIA not required but personal data was inferred; statutory inference overrides the checkbox."
        )
        state.rules.append("RULE-DPIA-EXPLICIT-FALSE-OVERRIDDEN")
    if state.need_dpia:
        if state.dpia_ok:
            state.add_evidence(
                "CTRL-DPIA",
                EvidenceStatus.PRESENT,
                payload.dpia_reference or "DPIA reference",
                f"DPIA marked completed. Reference: {payload.dpia_reference}",
                confidence=Confidence.MEDIUM,
            )
            state.add_control(
                "CTRL-DPIA",
                "Privacy impact assessment (DPIA)",
                ControlStatus.PASS_DECLARED,
                "MAP-1.5",
                "6.1.2",
                "Status completed with reference. Document not read in this session.",
            )
            state.rules.append("RULE-DPIA-DECLARED-COMPLETE")
        else:
            state.add_evidence(
                "CTRL-DPIA",
                EvidenceStatus.MISSING,
                "dpia_status / dpia_reference",
                f"DPIA required. status={payload.dpia_status!r} reference={payload.dpia_reference!r}",
            )
            state.add_control(
                "CTRL-DPIA",
                "Privacy impact assessment (DPIA)",
                ControlStatus.FAIL,
                "MAP-1.5",
                "6.1.2",
                "Processing risk to individuals without a completed DPIA.",
            )
            state.nist.append("MAP-1.5")
            state.iso.append("6.1.2")
            state.findings.append(
                "A DPIA is required: personal data, special categories, or automated decisions are indicated, "
                "and no completed DPIA with reference was provided."
            )
            state.factors.append("DPIA required and not completed")
            state.rules.append("RULE-DPIA-REQUIRED-MISSING")
    else:
        state.add_evidence(
            "CTRL-DPIA",
            EvidenceStatus.NOT_APPLICABLE,
            "Intake",
            "DPIA obligation not inferred from current facts. Reassess if data or use changes.",
            confidence=Confidence.MEDIUM,
        )
        state.add_control(
            "CTRL-DPIA",
            "Privacy impact assessment (DPIA)",
            ControlStatus.NOT_APPLICABLE,
            "MAP-1.5",
            "6.1.2",
            "Mandatory DPIA not inferred from current intake.",
        )
        state.rules.append("RULE-DPIA-NOT-INFERRED")


def eval_transfers(state: ScoringState) -> None:
    payload = state.payload
    state.transfers = (payload.international_transfers or "").strip()
    state.geo = (payload.geographic_scope or "").strip()
    if not state.geo or not state.transfers:
        state.add_evidence(
            "CTRL-TRANSFERS",
            EvidenceStatus.MISSING,
            "geographic_scope / international_transfers",
            f"geo={state.geo!r} transfers={state.transfers!r}",
        )
        state.add_control(
            "CTRL-TRANSFERS",
            "International transfers and residency",
            ControlStatus.FAIL,
            "MAP-1.5",
            "8.4",
            "Location or transfers not declared.",
        )
        state.findings.append(
            "Geographic scope or international transfers not declared. GDPR Chapter V cannot be assessed."
        )
        state.factors.append("Unknown international transfers")
        state.rules.append("RULE-TRANSFERS-UNKNOWN")
        state.nist.append("GOVERN-1.2")
    else:
        state.add_evidence(
            "CTRL-TRANSFERS",
            EvidenceStatus.PRESENT,
            "Intake geographic_scope + international_transfers",
            f"{state.geo} / {state.transfers}",
            confidence=Confidence.MEDIUM,
        )
        state.add_control(
            "CTRL-TRANSFERS",
            "International transfers and residency",
            ControlStatus.DECLARED,
            "MAP-1.5",
            "8.4",
            "Declared in intake; SCCs or TIA not in evidence.",
        )
        state.rules.append("RULE-TRANSFERS-DECLARED")


def eval_retention(state: ScoringState) -> None:
    payload = state.payload
    if not (payload.retention_period or "").strip():
        state.add_evidence("CTRL-RETENTION", EvidenceStatus.MISSING, "retention_period", "Retention period not stated.")
        state.add_control(
            "CTRL-RETENTION",
            "Retention period",
            ControlStatus.FAIL,
            "MAP-1.5",
            "8.2",
            "Without retention, minimization is not demonstrable.",
        )
        state.factors.append("Retention unspecified")
        state.rules.append("RULE-RETENTION-MISSING")
    else:
        state.add_evidence(
            "CTRL-RETENTION",
            EvidenceStatus.PRESENT,
            "retention_period",
            payload.retention_period or "",
            confidence=Confidence.MEDIUM,
        )
        state.add_control(
            "CTRL-RETENTION",
            "Retention period",
            ControlStatus.DECLARED,
            "MAP-1.5",
            "8.2",
            "Period declared; not cross-checked against DPA.",
        )
        state.rules.append("RULE-RETENTION-DECLARED")


def eval_art9(state: ScoringState) -> None:
    payload = state.payload
    if not payload.special_category_data:
        return
    state.findings.append(
        "Special category data declared (GDPR Art. 9). Specific legal basis, DPA, and DPIA are required. "
        "This layer does not approve without that evidence."
    )
    state.factors.append("Special category data")
    state.rules.append("RULE-ART9")
    state.add_evidence("CTRL-ART9", EvidenceStatus.PRESENT, "special_category_data=true", "Intake declares special categories.")
    state.add_control(
        "CTRL-ART9",
        "Special categories (Art. 9)",
        ControlStatus.FAIL if not payload.has_dpa or not state.dpia_ok else ControlStatus.DECLARED,
        "MAP-1.1",
        "6.1.2",
        "Art. 9 declared.",
    )


def eval_adm(state: ScoringState) -> None:
    payload = state.payload
    if not payload.automated_decision_making:
        return
    state.findings.append(
        "Automated decision-making is declared. Assess GDPR Art. 22 and EU AI Act if high-risk use. "
        "No model evaluation was provided in intake."
    )
    state.factors.append("Automated decision-making")
    state.rules.append("RULE-ADM")
    state.add_evidence("CTRL-ADM", EvidenceStatus.PRESENT, "automated_decision_making=true", "Declared in intake.")
    state.add_control(
        "CTRL-ADM",
        "Automated decisions",
        ControlStatus.DECLARED,
        "MAP-1.1",
        "8.5",
        "ADM declared; safeguards not evidenced.",
    )


def eval_security(state: ScoringState) -> None:
    state.add_evidence(
        "CTRL-SOC2",
        EvidenceStatus.MISSING,
        "Intake (no attachment)",
        "SOC 2 Type II / ISO 27001 / 42001 not supplied. Vendor claims do not reduce residual risk.",
    )
    state.add_control(
        "CTRL-SOC2",
        "Independent security pack",
        ControlStatus.FAIL,
        "MEASURE-2.7",
        "8.2",
        "No audit report in this session.",
    )
    state.findings.append(
        "No independent security evidence (SOC 2, pentest, certificates). Residual risk does not drop because of marketing."
    )
    state.factors.append("No independent security evidence")
    state.rules.append("RULE-SECURITY-PACK-MISSING")


def eval_model(state: ScoringState) -> None:
    payload = state.payload
    if not (payload.model_provider or "").strip():
        state.add_evidence("CTRL-MODEL", EvidenceStatus.MISSING, "model_provider", "Foundation model not named.")
        state.add_control(
            "CTRL-MODEL",
            "Model transparency",
            ControlStatus.FAIL,
            "MAP-2.2",
            "A.6.2.4",
            "Without model name there is no model card or evals.",
        )
        state.factors.append("Foundation model unnamed")
        state.rules.append("RULE-MODEL-UNNAMED")
        state.nist.append("MAP-2.2")
        state.iso.append("A.6.2.4")
    else:
        state.add_evidence(
            "CTRL-MODEL",
            EvidenceStatus.INSUFFICIENT,
            "model_provider",
            payload.model_provider or "",
            confidence=Confidence.MEDIUM,
        )
        state.add_control(
            "CTRL-MODEL",
            "Model transparency",
            ControlStatus.DECLARED,
            "MAP-2.2",
            "A.6.2.4",
            "Name declared; no model card or evals in evidence.",
        )
        state.rules.append("RULE-MODEL-NAMED-NO-EVAL")


def _risk_score(state: ScoringState) -> int:
    score = 2
    if state.personal:
        score += 1
        state.factors.append("Personal or likely personal data")
        state.rules.append("RULE-PERSONAL-DATA")
    if state.personal and not state.dpa_declared:
        score += 2
    if state.payload.special_category_data:
        score += 1
    if state.payload.automated_decision_making:
        score += 1
    if state.need_dpia and not state.dpia_ok:
        score += 1
    if not state.geo or not state.transfers:
        score += 1
    return min(5, score)


def _assemble(state: ScoringState, assessment_id: str, score: int, decision: str) -> AssessmentResponse:
    payload = state.payload
    vendor = payload.vendor_name
    residual_map = {1: "Very Low", 2: "Low", 3: "Moderate", 4: "High", 5: "Critical"}
    residual = residual_map[score]
    tickets = build_epic_and_subtasks(
        vendor=vendor,
        triage_decision=decision,
        residual=residual,
        need_legal=True,
        need_infosec=True,
        need_aigov=True,
        legal_reason="Review executed DPA, DPIA, transfers, and special categories. GDPR is not a checkbox.",
        infosec_reason="Validate current SOC 2 Type II, encryption, IR, and subprocessors. Do not accept marketing.",
        aigov_reason="Bias, hallucination, explainability, prompt injection, and intended use. The engine does not replace this review.",
        assessment_id=assessment_id,
    )
    state.rules.append("RULE-WORKFLOW-JIRA-GATES")
    triggered = [
        c.control_id
        for c in state.controls
        if c.status in (ControlStatus.FAIL, ControlStatus.DECLARED_UNVERIFIED)
    ]
    used = [e.control_id for e in state.evidence]
    gaps = [
        e.document + " — " + (e.excerpt[:160] if e.excerpt else e.evidence_status)
        for e in state.evidence
        if e.evidence_status in (EvidenceStatus.MISSING, EvidenceStatus.INSUFFICIENT)
    ]
    privacy = PrivacyTriage(
        personal_data_inferred=state.personal,
        privacy_assessment_required=state.need_dpia,
        inferred_required=bool(state.personal or payload.special_category_data or payload.automated_decision_making),
        declared_required=state.declared_dpia,
        legal_review_required=True,
        dpia_decision_basis=state.dpia_basis,
        dpia_status=payload.dpia_status or DpiaStatus.UNKNOWN,
        dpia_reference=payload.dpia_reference,
        processing_purposes=payload.processing_purposes,
        data_subjects=payload.data_subjects,
        special_category_data=payload.special_category_data,
        international_transfers=payload.international_transfers or "unknown",
        retention_period=payload.retention_period,
        automated_decision_making=payload.automated_decision_making,
        privacy_risk_level=payload.privacy_risk_level or residual,
        inferred_data_categories=state.inferred_categories,
        declared_data_categories=[c.value if hasattr(c, "value") else str(c) for c in payload.data_categories],
        gdpr_notes=(
            "DPIA review indicated is an engine triage signal, not a DPO legal opinion. "
            "GDPR is not reduced to the existence of a DPA. "
            f"{state.dpia_basis}"
        ),
    )
    eu_ai_act = (
        "Deployer / downstream provider applicability not determined without use-case classification. "
        "If automated decisions affect individuals, review Annex III. "
        "GPAI obligations on the model provider (if any) do not replace our obligations."
    )
    record = DecisionRecord(
        decision=decision,
        decision_basis=sorted(set(state.rules)),
        controls_triggered=triggered,
        evidence_used=used,
        risk_score=score,
        residual_risk=residual,
        reviewer="deterministic-engine",
        timestamp=state.now.isoformat(),
        model_version=ENGINE_VERSION,
        scoring_method="deterministic",
        llm_used_for_decision=False,
        workflow_status=WorkflowStatus.HUMAN_REVIEW_REQUIRED,
        required_departments=list(REQUIRED_DEPARTMENTS),
        gate_status={dept: GateStatus.REQUIRED_OPEN for dept in REQUIRED_DEPARTMENTS},
    )
    pack = GovernanceEvidencePack(
        risk_decision={"decision": decision, "residual_risk": residual, "risk_score": score},
        evidence_matrix=[e.model_dump() for e in state.evidence],
        gdpr_dpia=privacy.model_dump(),
        eu_ai_act_applicability=eu_ai_act,
        nist_ai_rmf_mapping=sorted(set(state.nist)),
        iso_42001_mapping=sorted(set(state.iso)),
        critical_findings=state.findings,
        remediation_plan=[t.fields.summary for t in tickets],
        jira_tickets=[t.model_dump() for t in tickets],
        decision_audit_trail=record.model_dump(),
    )
    return AssessmentResponse(
        assessment_metadata=AssessmentMetadata(
            assessment_id=assessment_id,
            vendor=vendor,
            assessment_date=state.now.date().isoformat(),
            decision=decision,
            overall_residual_risk=residual,
        ),
        framework_alignment=FrameworkAlignment(
            nist_ai_rmf_gaps=sorted(set(state.nist)),
            iso_42001_gaps=sorted(set(state.iso)),
        ),
        critical_findings=state.findings,
        evidence_gaps=gaps,
        evidence_items=state.evidence,
        control_assessments=state.controls,
        decision_record=record,
        privacy_triage=privacy,
        evidence_pack=pack,
        jira_tickets=tickets,
        risk_factors=sorted(set(state.factors)),
    )


def evaluate(payload: VendorInput) -> AssessmentResponse:
    """
    Run the full governance pipeline and return a structured AssessmentResponse.

    Side effects: none (pure function). Caller persists via store.save() in main.py.
    """
    now = utc_now()
    assessment_id = str(uuid4())
    state = _new_state(payload, now)
    eval_dpa(state)
    eval_dpia(state)
    eval_transfers(state)
    eval_retention(state)
    eval_art9(state)
    eval_adm(state)
    eval_security(state)
    eval_model(state)
    score = _risk_score(state)
    decision, decision_rule = derive_triage_decision(
        personal=state.personal,
        has_dpa=state.dpa_declared,
        special_category=payload.special_category_data,
        dpia_ok=state.dpia_ok,
        need_dpia=state.need_dpia,
        score=score,
    )
    state.rules.append(decision_rule)
    if decision == EngineDecision.PENDING_REVIEW.value:
        state.findings.append(
            "Triage: no personal-data-without-DPA red flag, but the engine does not approve. "
            "Legal, SecOps, and AI Governance must close their Jira department Tasks."
        )
    response = _assemble(state, assessment_id, score, decision)
    meta = response.assessment_metadata
    record = response.decision_record
    logger.info(
        "assessment complete",
        extra={
            "event": "scoring.evaluate",
            "vendor": meta.vendor,
            "decision": meta.decision,
            "residual_risk": meta.overall_residual_risk,
            "risk_score": record.risk_score if record else score,
            "rules_count": len(record.decision_basis) if record else 0,
            "findings_count": len(state.findings),
        },
    )
    return response
