"""
LLM integration — chat Q&A only.

Separation of concerns:
  - Assessments always call app.scoring.evaluate() (deterministic triage + score).
  - The LLM may explain findings in chat but must not change decision or residual risk.
  - The browser renders a structured assistant card from AssessmentResponse; chat replies
    are conversational bubbles. Neither replaces the pinned decision bar or audit chips.
  - Without OPENAI_API_KEY, mock_chat() answers via keyword routing (demo mode).
"""

import json
from typing import List, Optional, Tuple

from openai import OpenAI
from pydantic import BaseModel

from app.config import get_settings
from app.logging_config import get_logger
from app.models import AssessmentResponse, ChatMessage, VendorInput
from app.prompts import CHAT_SYSTEM_PROMPT, build_chat_user_message
from app.scoring import evaluate as deterministic_evaluate
from app.security import redact_mapping, redact_secrets

logger = get_logger("app.llm")

_client: Optional[OpenAI] = None
LLM_UNAVAILABLE = (
    "Chat is temporarily unavailable. The deterministic assessment is unchanged. "
    "Retry later or use the structured report on the right."
)


def llm_enabled() -> bool:
    """True when OPENAI_API_KEY is set; exposed via /api/v1/health/details."""
    return get_settings().llm_enabled


def get_client() -> OpenAI:
    """Lazy singleton OpenAI client with timeout and bounded retries."""
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=get_settings().openai_api_key,
            timeout=20.0,
            max_retries=2,
        )
    return _client


def model_name() -> str:
    """Chat model name (env: OPENAI_MODEL). Never used for scoring."""
    return get_settings().openai_model


def run_assessment(payload: VendorInput) -> Tuple[AssessmentResponse, bool]:
    """
    Produce AssessmentResponse from vendor intake.

    Returns (assessment, used_llm_for_decision). The second value is always False:
    scoring is deterministic; the tuple shape is kept for API stability.
    """
    return deterministic_evaluate(payload), False


def answer_question(
    message: str,
    history: List[ChatMessage],
    assessment: Optional[AssessmentResponse],
    intake: Optional[VendorInput],
) -> Tuple[str, bool]:
    """Answer a follow-up. Chat may use an LLM; it cannot change the triage decision."""
    if assessment is None:
        logger.debug("chat without assessment", extra={"event": "llm.chat.no_session"})
    if llm_enabled():
        logger.debug("chat via llm", extra={"event": "llm.chat.openai"})
        try:
            return _llm_chat(message, history, assessment, intake), True
        except Exception:
            logger.exception("openai chat failed", extra={"event": "llm.chat.error"})
            return LLM_UNAVAILABLE, False
    logger.debug("chat via mock", extra={"event": "llm.chat.mock"})
    return mock_chat(message, assessment, intake), False


def _safe_json(model: Optional[BaseModel]) -> Optional[str]:
    if model is None:
        return None
    dumped = model.model_dump()
    return json.dumps(redact_mapping(dumped), indent=2)


def _llm_chat(
    message: str,
    history: List[ChatMessage],
    assessment: Optional[AssessmentResponse],
    intake: Optional[VendorInput],
) -> str:
    """OpenAI chat completion grounded in redacted assessment JSON + intake."""
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    for item in history[-12:]:
        if item.role in ("user", "assistant") and item.content.strip():
            messages.append({"role": item.role, "content": redact_secrets(item.content)[:4000]})
    messages.append(
        {
            "role": "user",
            "content": build_chat_user_message(
                redact_secrets(message)[:4000],
                _safe_json(assessment),
                _safe_json(intake),
            ),
        }
    )
    create_kwargs: dict = {
        "model": model_name(),
        "messages": messages,
    }
    if model_name().startswith("gpt-5"):
        # GPT-5.6 rejects legacy max_tokens; the 3.0 SDK still serializes it if we
        # pass max_completion_tokens as a typed kwarg. extra_body avoids that.
        create_kwargs["extra_body"] = {"max_completion_tokens": 1200}
    else:
        create_kwargs["temperature"] = 0.2
        create_kwargs["max_completion_tokens"] = 1200
    completion = get_client().chat.completions.create(**create_kwargs)
    choices = getattr(completion, "choices", None) or []
    if not choices:
        raise ValueError("LLM returned no choices")
    message_obj = getattr(choices[0], "message", None)
    content = getattr(message_obj, "content", None) if message_obj is not None else None
    text = (content or "").strip()
    if not text:
        raise ValueError("LLM returned empty content")
    return text


def reset_client() -> None:
    """Drop the cached OpenAI client (tests)."""
    global _client
    _client = None


def mock_chat(
    message: str,
    assessment: Optional[AssessmentResponse],
    intake: Optional[VendorInput],
) -> str:
    """
    Keyword router for demo mode without OpenAI.

    Explains engine output, Jira workflow, and evidence — does not rescore or approve.
    """
    if assessment is None:
        return "No assessment loaded. Use the form on the left and click Assess vendor."

    meta = assessment.assessment_metadata
    record = assessment.decision_record
    privacy = assessment.privacy_triage
    q = message.lower()

    if any(k in q for k in ("decision", "approve", "reject", "escalate", "workflow", "approver")):
        basis = ", ".join((record.decision_basis[:8] if record else []) or ["EVIDENCE NOT FOUND"])
        workflow = record.workflow_status if record else "EVIDENCE NOT FOUND"
        human = record.human_decision if record and record.human_decision else "none (engine does not approve)"
        return (
            f"**Engine triage:** {meta.decision}\n"
            f"**Jira workflow:** {workflow}\n"
            f"**Human decision:** {human}\n"
            f"**Legal:** {(record.legal_approver if record else None) or 'pending'} "
            f"**SecOps:** {(record.secops_approver if record else None) or 'pending'} "
            f"**AI Gov:** {(record.aigov_approver if record else None) or 'pending'}\n\n"
            f"**Residual risk:** {meta.overall_residual_risk}\n"
            f"**Score (1–5):** {record.risk_score if record else 'EVIDENCE NOT FOUND'}\n"
            f"**Engine:** {record.model_version if record else 'deterministic'}\n"
            f"**Rules triggered:** {basis}\n\n"
            "The engine only triages (REQUIRES REMEDIATION, PENDING REVIEW, or ESCALATE). "
            "Department gates in Jira (Legal, SecOps, AI Governance) produce DEPARTMENT_GATES_COMPLETED, "
            "which is not a business approval. Chat cannot change that. Not legal advice."
        )

    if any(k in q for k in ("residual", "score", "risk rating", "how risky")):
        return (
            f"**Residual risk** for {meta.vendor}: **{meta.overall_residual_risk}** "
            f"(score {record.risk_score if record else 'n/a'}/5).\n\n"
            "Residual does not drop because the vendor claims a control. "
            f"Findings: {len(assessment.critical_findings)}. "
            f"Gaps: {len(assessment.evidence_gaps)}."
        )

    if "dpia" in q or "privacy" in q:
        if not privacy:
            return "EVIDENCE NOT FOUND — no DPIA triage object in this session."
        return (
            f"**DPIA review indicated:** {privacy.privacy_assessment_required}\n"
            f"**Basis:** {privacy.dpia_decision_basis}\n"
            f"**DPIA status:** {privacy.dpia_status}\n"
            f"**Reference:** {privacy.dpia_reference or 'EVIDENCE NOT FOUND'}\n"
            f"**Special categories:** {privacy.special_category_data}\n"
            f"**Automated decisions:** {privacy.automated_decision_making}\n"
            f"**Transfers:** {privacy.international_transfers}\n"
            f"**Retention:** {privacy.retention_period or 'EVIDENCE NOT FOUND'}\n\n"
            f"{privacy.gdpr_notes}"
        )

    if "dpa" in q or "processing agreement" in q:
        has_dpa = bool(intake and intake.has_dpa)
        return (
            f"Intake `has_dpa` is **{has_dpa}**. GDPR is not equivalent to that checkbox.\n\n"
            + (
                "DPA is declared. Signed PDF is still not attached in this console."
                if has_dpa
                else "No DPA. If personal data is involved, that is a red flag blocking approval."
            )
        )

    if "audit" in q or "trace" in q or "rule" in q or "engine" in q:
        if not record:
            return "EVIDENCE NOT FOUND — no DecisionRecord."
        return (
            f"**Reviewer (engine):** {record.reviewer}\n"
            f"**Workflow:** {record.workflow_status}\n"
            f"**Legal:** {record.legal_approver or 'pending'} · **SecOps:** {record.secops_approver or 'pending'} · "
            f"**AI Gov:** {record.aigov_approver or 'pending'}\n"
            f"**Method:** {record.scoring_method} (LLM on decision: {record.llm_used_for_decision})\n"
            f"**Timestamp:** {record.timestamp}\n"
            f"**Controls triggered:** {', '.join(record.controls_triggered) or 'none'}\n"
            f"**Basis:** {', '.join(record.decision_basis)}"
        )

    if "jira" in q or "ticket" in q or "remediation" in q:
        if not assessment.jira_tickets:
            return "EVIDENCE NOT FOUND — this assessment generated no tickets."
        lines = [
            f"{meta.vendor} has **{len(assessment.jira_tickets)}** ticket(s) (parent Epic + department Tasks):\n"
        ]
        for ticket in assessment.jira_tickets:
            fields = ticket.fields
            dept = fields.department or fields.issuetype.name
            assignee = (fields.assignee or {}).get("emailAddress") or "unassigned"
            lines.append(f"- **{fields.summary}** [{dept}] ({fields.priority.name}) → {assignee}")
        return "\n".join(lines)

    if "nist" in q or "iso" in q or "framework" in q or "42001" in q:
        nist = ", ".join(assessment.framework_alignment.nist_ai_rmf_gaps) or "EVIDENCE NOT FOUND"
        iso = ", ".join(assessment.framework_alignment.iso_42001_gaps) or "EVIDENCE NOT FOUND"
        return f"**NIST AI RMF:** {nist}\n\n**ISO/IEC 42001:** {iso}"

    if "gap" in q or "missing" in q or "evidence" in q:
        items = assessment.evidence_items or []
        if items:
            lines = ["**Evidence matrix:**"]
            for item in items:
                lines.append(f"- {item.control_id} [{item.evidence_status}] {item.document}")
            return "\n".join(lines)
        if not assessment.evidence_gaps:
            return "No evidence gaps recorded."
        return "**Missing evidence:**\n" + "\n".join(f"- {item}" for item in assessment.evidence_gaps)

    if "finding" in q or "red flag" in q or "critical" in q:
        findings = assessment.critical_findings
        if not findings:
            return "No critical findings."
        return "**Findings:**\n" + "\n".join(f"- {item}" for item in findings)

    if "train" in q or "training" in q:
        return (
            "EVIDENCE NOT FOUND in this session for a contractual training clause. "
            "Request the DPA/ToS clause from the vendor and reassess."
        )

    summary_findings = "\n".join(f"- {item}" for item in assessment.critical_findings[:3])
    return (
        f"{meta.vendor} assessed on {meta.assessment_date} by engine {record.model_version if record else 'deterministic'}.\n\n"
        f"**Decision:** {meta.decision}\n"
        f"**Residual risk:** {meta.overall_residual_risk}\n\n"
        f"**Findings:**\n{summary_findings}\n\n"
        "Ask about decision, DPIA, DPA, evidence, NIST/ISO, or Jira."
    )
