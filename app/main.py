"""
FastAPI application — Enterprise AI Risk Assessment API + web console.

Endpoints:
  GET  /                              Single-page console (static/index.html)
  GET  /api/v1/health                 Public liveness
  GET  /api/v1/health/details         Diagnostic (token-protected when configured)
  GET  /api/v1/assessment/latest      Restore in-memory session after reload
  POST /api/v1/assess-vendor          Deterministic triage + Jira ticket payloads
  POST /api/v1/webhooks/jira          Inbound human approvals from Jira
  POST /api/v1/chat                   Follow-up Q&A on the current assessment

Web console (static/):
  Left  — vendor intake form (high-contrast fields, expandable privacy sections)
  Right — pinned decision bar, audit trail grid + gate chips, tabbed report with
          expandable rows, structured assistant card, chat composer (sticky)
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import store
from app.config import ConfigurationError, get_settings, validate_startup
from app.jira_workflow import (
    apply_approval,
    jira_configured,
    parse_webhook_event,
    publish_to_jira,
)
from app.llm import answer_question, llm_enabled, run_assessment
from app.logging_config import configure_logging, get_logger
from app.models import AssessmentResponse, ChatRequest, ChatResponse, VendorInput
from app.security import (
    enforce_rate_limit,
    event_id_from,
    new_request_id,
    public_error_detail,
    verify_timestamp,
    verify_webhook_hmac,
    validate_assessment_id,
    webhook_secret,
)

configure_logging()
logger = get_logger("app.main")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        config = validate_startup()
    except ConfigurationError:
        logger.exception("startup configuration invalid", extra={"event": "app.start.config_error"})
        raise
    _app.state.settings = config["settings"]
    logger.info(
        "application starting",
        extra={
            "event": "app.start",
            "llm_enabled": llm_enabled(),
            "jira_outbound": jira_configured(),
            "approver_domain": config["approver_domain"],
            "webhook_event_store": config["webhook_event_store"],
        },
    )
    yield
    logger.info("application stopped", extra={"event": "app.stop"})


app = FastAPI(
    title="Enterprise AI Risk Assessment API",
    description="API for automated third-party AI vendor governance, chat Q&A, and Jira integration.",
    version="1.3.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or new_request_id()
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.get("/")
async def index() -> FileResponse:
    """Serve the governance console (intake + tabbed report + chat)."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/v1/health")
async def health() -> dict:
    """Public liveness. Does not disclose configuration."""
    return {"status": "ok"}


@app.get("/api/v1/health/details")
async def health_details(
    x_health_token: Optional[str] = Header(default=None, alias="X-Health-Token"),
) -> dict:
    settings = get_settings()
    if settings.health_details_token and x_health_token != settings.health_details_token:
        raise HTTPException(status_code=401, detail="Invalid health token")
    payload = {
        "status": "ok",
        "llm_enabled": llm_enabled(),
        "scoring_engine": "deterministic-rules-v1",
        "jira_outbound": jira_configured(),
        "approver_domain": settings.jira_approver_domain,
        "webhook_event_store": store.event_store_mode(),
    }
    return payload


@app.get("/api/v1/assessment/latest")
async def latest_assessment(
    x_assessment_token: Optional[str] = Header(default=None, alias="X-Assessment-Token"),
    x_assessment_id: Optional[str] = Header(default=None, alias="X-Assessment-Id"),
) -> dict:
    """Return the in-memory assessment so the UI can restore state on page reload."""
    assessment_id = validate_assessment_id(x_assessment_id)
    if not store.token_matches(assessment_id, x_assessment_token):
        raise HTTPException(status_code=401, detail="Assessment token required")
    assessment = store.get_assessment(assessment_id)
    intake = store.get_intake(assessment_id)
    if assessment is None:
        return {"assessment": None, "intake": None, "llm_enabled": llm_enabled()}
    return {
        "assessment": assessment.model_dump(),
        "intake": intake.model_dump() if intake else None,
        "llm_enabled": llm_enabled(),
    }


@app.post("/api/v1/assess-vendor")
async def assess_vendor_risk(payload: VendorInput, request: Request):
    enforce_rate_limit(request, "assess")
    request_id = getattr(request.state, "request_id", new_request_id())
    logger.info(
        "assess-vendor request",
        extra={"event": "api.assess", "vendor": payload.vendor_name, "request_id": request_id},
    )
    try:
        assessment, _used_llm = run_assessment(payload)
        try:
            jira_result = publish_to_jira(assessment.jira_tickets)
        except Exception as jira_exc:
            jira_result = {"published": False, "reason": "jira_unavailable"}
            logger.warning(
                "jira publish failed",
                extra={
                    "event": "api.assess.jira_error",
                    "vendor": payload.vendor_name,
                    "error": str(jira_exc),
                    "request_id": request_id,
                },
            )
        if assessment.decision_record:
            assessment.decision_record.decision_basis = list(assessment.decision_record.decision_basis) + [
                f"JIRA_PUBLISH:{jira_result.get('published')}"
            ]
        token = store.save(payload, assessment)
        meta = assessment.assessment_metadata
        logger.info(
            "assess-vendor complete",
            extra={
                "event": "api.assess.done",
                "vendor": meta.vendor,
                "decision": meta.decision,
                "jira_published": jira_result.get("published"),
                "request_id": request_id,
            },
        )
        response = JSONResponse(content=assessment.model_dump(mode="json"))
        response.headers["X-Assessment-Token"] = token
        response.headers["X-Request-Id"] = request_id
        return response
    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "assess-vendor failed",
            extra={"event": "api.assess.error", "vendor": payload.vendor_name, "request_id": request_id},
        )
        raise HTTPException(status_code=500, detail=public_error_detail(request_id)) from None


@app.post("/api/v1/webhooks/jira")
async def jira_webhook(
    request: Request,
    x_jira_signature: Optional[str] = Header(default=None, alias="X-Hub-Signature-256"),
    x_jira_signature_alt: Optional[str] = Header(default=None, alias="X-Jira-Signature"),
    x_jira_timestamp: Optional[str] = Header(default=None, alias="X-Jira-Timestamp"),
    x_jira_event_id: Optional[str] = Header(default=None, alias="X-Jira-Event-Id"),
    x_jira_nonce: Optional[str] = Header(default=None, alias="X-Jira-Nonce"),
    x_assessment_id: Optional[str] = Header(default=None, alias="X-Assessment-Id"),
) -> dict:
    """Inbound from Jira when a human moves a department Task to Done."""
    expected = webhook_secret()
    if not expected:
        logger.error("jira webhook secret missing", extra={"event": "api.webhook.misconfigured"})
        raise HTTPException(
            status_code=503,
            detail="JIRA_WEBHOOK_SECRET is not configured on the server",
        )
    enforce_rate_limit(request, "webhook")

    raw = await request.body()
    signature = x_jira_signature or x_jira_signature_alt
    if not signature:
        logger.warning("jira webhook hmac missing", extra={"event": "api.webhook.hmac_missing"})
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    verify_webhook_hmac(raw, signature, expected)

    verify_timestamp(x_jira_timestamp)

    try:
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("JSON root must be an object")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    if store.count() == 0:
        logger.warning("jira webhook no session", extra={"event": "api.webhook.no_session"})
        raise HTTPException(status_code=404, detail="No assessment in session to attach the approval to")

    parsed = parse_webhook_event(body)
    assessment_id = validate_assessment_id(
        x_assessment_id or body.get("assessment_id") or parsed.assessment_id
    )
    if not assessment_id and store.count() == 1:
        lone = store.get_assessment()
        if lone:
            assessment_id = lone.assessment_metadata.assessment_id
    if not assessment_id:
        logger.warning("jira webhook missing assessment_id", extra={"event": "api.webhook.no_id"})
        raise HTTPException(
            status_code=400,
            detail="assessment_id is required (body, X-Assessment-Id header, or Assessment-ID in issue description)",
        )

    assessment = store.get_assessment(assessment_id)
    if assessment is None:
        logger.warning(
            "jira webhook unknown assessment",
            extra={"event": "api.webhook.no_session", "assessment_id": assessment_id},
        )
        raise HTTPException(
            status_code=404,
            detail=f"No assessment found for assessment_id={assessment_id}",
        )

    event_id = event_id_from(x_jira_event_id, x_jira_nonce, body, parsed.key)
    if not store.remember_event(event_id):
        return {"ok": True, "duplicate": True, "event_id": event_id}

    try:
        updated = apply_approval(assessment, body)
    except ValueError as exc:
        store.forget_event(event_id)
        logger.warning(
            "jira webhook validation failed",
            extra={"event": "api.webhook.invalid", "detail": str(exc)},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        store.forget_event(event_id)
        request_id = getattr(request.state, "request_id", new_request_id())
        logger.exception("jira webhook error", extra={"event": "api.webhook.error", "request_id": request_id})
        raise HTTPException(status_code=400, detail=public_error_detail(request_id)) from None

    store.save_assessment(updated)
    record = updated.decision_record
    return {
        "ok": True,
        "duplicate": False,
        "event_id": event_id,
        "assessment_id": assessment_id,
        "workflow_status": record.workflow_status if record else None,
        "legal_approver": record.legal_approver if record else None,
        "secops_approver": record.secops_approver if record else None,
        "aigov_approver": record.aigov_approver if record else None,
        "human_decision": record.human_decision if record else None,
    }


@app.post("/api/v1/chat", response_model=ChatResponse)
async def chat_about_risk(payload: ChatRequest, request: Request) -> ChatResponse:
    enforce_rate_limit(request, "chat")
    request_id = getattr(request.state, "request_id", new_request_id())
    token = request.headers.get("X-Assessment-Token")
    if not store.token_matches(payload.assessment_id, token):
        raise HTTPException(status_code=401, detail="Assessment token required")
    assessment = store.get_assessment(payload.assessment_id)
    intake = store.get_intake(payload.assessment_id)
    if assessment is None and payload.assessment_id:
        raise HTTPException(
            status_code=404,
            detail=f"No assessment found for assessment_id={payload.assessment_id}",
        )
    if assessment is None:
        assessment = store.get_assessment()
        intake = store.get_intake()
    logger.info(
        "chat request",
        extra={
            "event": "api.chat",
            "has_assessment": assessment is not None,
            "assessment_id": payload.assessment_id,
            "message_len": len(payload.message),
            "request_id": request_id,
        },
    )
    try:
        reply, used_llm = answer_question(payload.message, payload.history, assessment, intake)
    except Exception:
        logger.exception("chat failed", extra={"event": "api.chat.error", "request_id": request_id})
        raise HTTPException(status_code=500, detail=public_error_detail(request_id)) from None

    meta = assessment.assessment_metadata if assessment else None
    logger.debug(
        "chat response",
        extra={"event": "api.chat.done", "used_llm": used_llm, "vendor": meta.vendor if meta else None},
    )
    return ChatResponse(
        reply=reply,
        vendor=meta.vendor if meta else None,
        decision=meta.decision if meta else None,
        overall_residual_risk=meta.overall_residual_risk if meta else None,
        used_llm=used_llm,
        request_id=request_id,
    )
