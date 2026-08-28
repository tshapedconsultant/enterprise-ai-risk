"""
In-memory assessment store keyed by assessment_id (UUID).

Each assessment is isolated so concurrent users and overlapping Jira webhooks do not
overwrite each other. A threading lock guards dict updates within one process.
Sessions expire after SESSION_TTL_SECONDS and are capped at MAX_SESSIONS.

This is still one process for assessment sessions (no PostgreSQL). Webhook event
IDs are stored in SQLite when WEBHOOK_EVENT_STORE is a file path so multiple
workers on a shared volume do not replay the same Jira event. Production still
needs PostgreSQL for assessments (Future Improvements item 5).

The web console restores the latest session via GET /api/v1/assessment/latest and
persists the access token in sessionStorage. Assessment token auth is on by default.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from app.config import get_settings
from app.logging_config import get_logger
from app.models import AssessmentResponse, VendorInput

logger = get_logger("app.store")

_lock = threading.Lock()
_sessions: Dict[str, Tuple[VendorInput, AssessmentResponse, str, float]] = {}
_latest_id: Optional[str] = None
_event_conn: Optional[sqlite3.Connection] = None
_event_mode: str = "memory"


def _init_event_store_locked() -> str:
    global _event_conn, _event_mode
    if _event_conn is not None:
        return _event_mode
    path = get_settings().webhook_event_store
    if path == ":memory:":
        _event_conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=5.0)
        _event_mode = "memory"
    else:
        parent = Path(path).parent
        if parent and str(parent) not in {".", ""}:
            parent.mkdir(parents=True, exist_ok=True)
        _event_conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        _event_mode = "sqlite"
    _event_conn.execute(
        "CREATE TABLE IF NOT EXISTS webhook_events ("
        "event_id TEXT PRIMARY KEY NOT NULL, "
        "seen_at REAL NOT NULL)"
    )
    _event_conn.commit()
    return _event_mode


def init_event_store() -> str:
    """Open the webhook event database (SQLite file or in-memory). Idempotent."""
    with _lock:
        return _init_event_store_locked()


def close_event_store() -> None:
    """Drop the event-store connection so the next init can pick up a new path (tests)."""
    global _event_conn, _event_mode
    with _lock:
        if _event_conn is not None:
            _event_conn.close()
            _event_conn = None
            _event_mode = "memory"


def event_store_mode() -> str:
    with _lock:
        return _event_mode


def _now() -> float:
    return time.time()


def _purge_locked() -> None:
    global _latest_id
    settings = get_settings()
    cutoff = _now() - settings.session_ttl_seconds
    expired = [aid for aid, row in _sessions.items() if row[3] < cutoff]
    for aid in expired:
        _sessions.pop(aid, None)
        if _latest_id == aid:
            _latest_id = None
    if _event_conn is not None:
        _event_conn.execute(
            "DELETE FROM webhook_events WHERE seen_at < ?",
            (_now() - settings.webhook_event_ttl_seconds,),
        )
        _event_conn.commit()
    while len(_sessions) > settings.max_sessions:
        oldest = min(_sessions.items(), key=lambda item: item[1][3])[0]
        _sessions.pop(oldest, None)
        if _latest_id == oldest:
            _latest_id = None


def _resolve_id(assessment_id: Optional[str]) -> Optional[str]:
    if assessment_id:
        return assessment_id
    return _latest_id


def new_access_token() -> str:
    return secrets.token_urlsafe(32)


def save(intake: VendorInput, assessment: AssessmentResponse, access_token: Optional[str] = None) -> str:
    """Persist intake + assessment. Returns the access token for this assessment."""
    global _latest_id
    assessment_id = assessment.assessment_metadata.assessment_id
    token = access_token or new_access_token()
    with _lock:
        _purge_locked()
        _sessions[assessment_id] = (intake, assessment, token, _now())
        _latest_id = assessment_id
    meta = assessment.assessment_metadata
    record = assessment.decision_record
    logger.info(
        "session saved",
        extra={
            "event": "store.save",
            "assessment_id": assessment_id,
            "vendor": meta.vendor,
            "decision": meta.decision,
            "risk_score": record.risk_score if record else None,
            "workflow_status": record.workflow_status if record else None,
        },
    )
    return token


def save_assessment(assessment: AssessmentResponse) -> None:
    """Replace the assessment after a Jira webhook without changing intake or token."""
    assessment_id = assessment.assessment_metadata.assessment_id
    with _lock:
        if assessment_id not in _sessions:
            raise KeyError(f"No session for assessment_id={assessment_id}")
        intake, _, token, _created = _sessions[assessment_id]
        _sessions[assessment_id] = (intake, assessment, token, _now())
    record = assessment.decision_record
    logger.info(
        "assessment updated",
        extra={
            "event": "store.save_assessment",
            "assessment_id": assessment_id,
            "vendor": assessment.assessment_metadata.vendor,
            "workflow_status": record.workflow_status if record else None,
            "human_decision": record.human_decision if record else None,
        },
    )


def get_assessment(assessment_id: Optional[str] = None) -> Optional[AssessmentResponse]:
    """Return one assessment by id, or the most recently saved when id is omitted."""
    with _lock:
        _purge_locked()
        resolved = _resolve_id(assessment_id)
        if resolved and resolved in _sessions:
            return _sessions[resolved][1]
        return None


def get_intake(assessment_id: Optional[str] = None) -> Optional[VendorInput]:
    """Return intake paired with the given assessment (or latest)."""
    with _lock:
        _purge_locked()
        resolved = _resolve_id(assessment_id)
        if resolved and resolved in _sessions:
            return _sessions[resolved][0]
        return None


def get_access_token(assessment_id: Optional[str] = None) -> Optional[str]:
    with _lock:
        resolved = _resolve_id(assessment_id)
        if resolved and resolved in _sessions:
            return _sessions[resolved][2]
        return None


def assessment_auth_required() -> bool:
    """Default on. Set REQUIRE_ASSESSMENT_AUTH=false only for local demos."""
    return get_settings().require_assessment_auth


def token_matches(assessment_id: Optional[str], provided: Optional[str]) -> bool:
    if not assessment_auth_required():
        return True
    expected = get_access_token(assessment_id)
    if not expected or not provided:
        return False
    return secrets.compare_digest(expected, provided)


def remember_event(event_id: str) -> bool:
    """Return True if this event is new; False if it was already processed."""
    now = _now()
    with _lock:
        _init_event_store_locked()
        _purge_locked()
        assert _event_conn is not None
        cursor = _event_conn.execute(
            "INSERT OR IGNORE INTO webhook_events(event_id, seen_at) VALUES (?, ?)",
            (event_id, now),
        )
        _event_conn.commit()
        return cursor.rowcount == 1


def forget_event(event_id: str) -> None:
    """Drop a recorded event id so a later retry can be processed (failed apply)."""
    with _lock:
        if _event_conn is None:
            return
        _event_conn.execute("DELETE FROM webhook_events WHERE event_id = ?", (event_id,))
        _event_conn.commit()


def count() -> int:
    """Number of assessments currently in memory (tests / webhook disambiguation)."""
    with _lock:
        _purge_locked()
        return len(_sessions)


def clear() -> None:
    """Drop all session state. Used by tests; not exposed as an API."""
    global _latest_id
    with _lock:
        _sessions.clear()
        _latest_id = None
        if _event_conn is not None:
            _event_conn.execute("DELETE FROM webhook_events")
            _event_conn.commit()
    logger.debug("session cleared", extra={"event": "store.clear"})
