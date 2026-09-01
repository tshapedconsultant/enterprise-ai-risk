"""
SQLite-backed assessment store keyed by assessment_id (UUID).

Intake, AssessmentResponse, access token, and Jira issue.key mappings survive
process restart when DATA_STORE (or WEBHOOK_EVENT_STORE) is a file path.
TTL and MAX_SESSIONS are retention policy, not the storage model.

Chat and GET /assessments/{id} never fall back to a global "latest" row.
Callers must pass assessment_id. Webhooks resolve by explicit id or a persisted
issue.key mapping. The deprecated /assessment/latest route also requires an ID.

The SQLite hash chain provides tamper evidence, not tenant RLS or external
immutable/WORM guarantees.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from app.config import get_settings
from app.logging_config import get_logger
from app.models import AssessmentResponse, VendorInput
from app.timeutil import utc_now_iso

logger = get_logger("app.store")

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_store_mode: str = "memory"


def _now() -> float:
    return time.time()


def _init_locked() -> str:
    global _conn, _store_mode
    if _conn is not None:
        return _store_mode
    path = get_settings().effective_data_store
    if path == ":memory:":
        _conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=5.0)
        _store_mode = "memory"
    else:
        parent = Path(path).parent
        if parent and str(parent) not in {".", ""}:
            parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        _store_mode = "sqlite"
        _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS assessments ("
        "assessment_id TEXT PRIMARY KEY NOT NULL, "
        "intake_json TEXT NOT NULL, "
        "assessment_json TEXT NOT NULL, "
        "access_token TEXT NOT NULL, "
        "updated_at REAL NOT NULL)"
    )
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS jira_issues ("
        "issue_key TEXT PRIMARY KEY NOT NULL, "
        "assessment_id TEXT NOT NULL, "
        "FOREIGN KEY (assessment_id) REFERENCES assessments(assessment_id) ON DELETE CASCADE)"
    )
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS webhook_events ("
        "event_id TEXT PRIMARY KEY NOT NULL, "
        "seen_at REAL NOT NULL)"
    )
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "assessment_id TEXT NOT NULL, "
        "event_type TEXT NOT NULL, "
        "timestamp TEXT NOT NULL, "
        "payload_json TEXT NOT NULL, "
        "previous_hash TEXT NOT NULL, "
        "event_hash TEXT NOT NULL UNIQUE, "
        "FOREIGN KEY (assessment_id) REFERENCES assessments(assessment_id) ON DELETE CASCADE)"
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_events_assessment "
        "ON audit_events(assessment_id, id)"
    )
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_heads ("
        "assessment_id TEXT PRIMARY KEY NOT NULL, "
        "event_count INTEGER NOT NULL, "
        "head_hash TEXT NOT NULL, "
        "FOREIGN KEY (assessment_id) REFERENCES assessments(assessment_id) ON DELETE CASCADE)"
    )
    _conn.commit()
    return _store_mode


def init_store() -> str:
    """Open the SQLite database (file or in-memory). Idempotent."""
    with _lock:
        return _init_locked()


def init_event_store() -> str:
    """Alias kept for startup/config call sites."""
    return init_store()


def close_store() -> None:
    """Drop the connection so the next init can pick up a new path (tests)."""
    global _conn, _store_mode
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
            _store_mode = "memory"


def close_event_store() -> None:
    close_store()


def store_mode() -> str:
    with _lock:
        return _store_mode


def event_store_mode() -> str:
    return store_mode()


def _purge_locked() -> None:
    assert _conn is not None
    settings = get_settings()
    cutoff = _now() - settings.session_ttl_seconds
    _conn.execute("DELETE FROM assessments WHERE updated_at < ?", (cutoff,))
    _conn.execute(
        "DELETE FROM webhook_events WHERE seen_at < ?",
        (_now() - settings.webhook_event_ttl_seconds,),
    )
    while True:
        count = _conn.execute("SELECT COUNT(*) FROM assessments").fetchone()[0]
        if count <= settings.max_sessions:
            break
        oldest = _conn.execute(
            "SELECT assessment_id FROM assessments ORDER BY updated_at ASC LIMIT 1"
        ).fetchone()
        if not oldest:
            break
        _conn.execute("DELETE FROM assessments WHERE assessment_id = ?", (oldest[0],))
    _conn.commit()


def new_access_token() -> str:
    return secrets.token_urlsafe(32)


def _token_digest(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _assessment_audit_payload(assessment: AssessmentResponse) -> dict:
    serialized = assessment.model_dump_json()
    return {
        "assessment_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "decision_record": (
            assessment.decision_record.model_dump(mode="json")
            if assessment.decision_record
            else None
        ),
        "engine_decision": assessment.assessment_metadata.decision,
        "engine_version": (
            assessment.decision_record.model_version
            if assessment.decision_record
            else None
        ),
    }


def _event_hash(
    assessment_id: str,
    event_type: str,
    timestamp: str,
    payload_json: str,
    previous_hash: str,
) -> str:
    envelope = _canonical_json(
        {
            "assessment_id": assessment_id,
            "event_type": event_type,
            "timestamp": timestamp,
            "payload": json.loads(payload_json),
            "previous_hash": previous_hash,
        }
    )
    return hashlib.sha256(envelope.encode("utf-8")).hexdigest()


def _append_audit_event_locked(
    assessment: AssessmentResponse,
    event_type: str,
) -> str:
    """Append one hash-linked event inside the caller's SQLite transaction."""
    assert _conn is not None
    assessment_id = assessment.assessment_metadata.assessment_id
    head = _conn.execute(
        "SELECT event_count, head_hash FROM audit_heads WHERE assessment_id = ?",
        (assessment_id,),
    ).fetchone()
    previous_hash = head[1] if head else ""
    timestamp = utc_now_iso()
    payload_json = _canonical_json(_assessment_audit_payload(assessment))
    current_hash = _event_hash(
        assessment_id,
        event_type,
        timestamp,
        payload_json,
        previous_hash,
    )
    _conn.execute(
        "INSERT INTO audit_events"
        "(assessment_id, event_type, timestamp, payload_json, previous_hash, event_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            assessment_id,
            event_type,
            timestamp,
            payload_json,
            previous_hash,
            current_hash,
        ),
    )
    _conn.execute(
        "INSERT INTO audit_heads(assessment_id, event_count, head_hash) VALUES (?, 1, ?) "
        "ON CONFLICT(assessment_id) DO UPDATE SET "
        "event_count=audit_heads.event_count + 1, head_hash=excluded.head_hash",
        (assessment_id, current_hash),
    )
    return current_hash


def save(intake: VendorInput, assessment: AssessmentResponse, access_token: Optional[str] = None) -> str:
    """Persist intake + assessment. Returns the access token for this assessment."""
    assessment_id = assessment.assessment_metadata.assessment_id
    token = access_token or new_access_token()
    with _lock:
        _init_locked()
        assert _conn is not None
        _purge_locked()
        with _conn:
            existing = _conn.execute(
                "SELECT 1 FROM assessments WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchone()
            _conn.execute(
                "INSERT INTO assessments"
                "(assessment_id, intake_json, assessment_json, access_token, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(assessment_id) DO UPDATE SET "
                "intake_json=excluded.intake_json, "
                "assessment_json=excluded.assessment_json, "
                "access_token=excluded.access_token, "
                "updated_at=excluded.updated_at",
                (
                    assessment_id,
                    intake.model_dump_json(),
                    assessment.model_dump_json(),
                    _token_digest(token),
                    _now(),
                ),
            )
            _append_audit_event_locked(
                assessment,
                "assessment.updated" if existing else "assessment.created",
            )
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
            "store": store_mode(),
        },
    )
    return token


def save_assessment(assessment: AssessmentResponse) -> None:
    """Replace the assessment after a Jira webhook without changing intake or token."""
    assessment_id = assessment.assessment_metadata.assessment_id
    with _lock:
        _init_locked()
        assert _conn is not None
        row = _conn.execute(
            "SELECT intake_json, access_token FROM assessments WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"No session for assessment_id={assessment_id}")
        with _conn:
            _conn.execute(
                "UPDATE assessments SET assessment_json = ?, updated_at = ? WHERE assessment_id = ?",
                (assessment.model_dump_json(), _now(), assessment_id),
            )
            _append_audit_event_locked(assessment, "assessment.updated")
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


def update_assessment(
    assessment_id: str,
    updater: Callable[[AssessmentResponse], AssessmentResponse],
    event_type: str = "assessment.updated",
) -> AssessmentResponse:
    """Serialize read-modify-write updates so concurrent webhooks do not lose a gate."""
    with _lock:
        _init_locked()
        assert _conn is not None
        row = _conn.execute(
            "SELECT assessment_json FROM assessments WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"No session for assessment_id={assessment_id}")
        current = AssessmentResponse.model_validate_json(row[0])
        updated = updater(current)
        if updated.assessment_metadata.assessment_id != assessment_id:
            raise ValueError("Assessment updater cannot change assessment_id")
        with _conn:
            _conn.execute(
                "UPDATE assessments SET assessment_json = ?, updated_at = ? "
                "WHERE assessment_id = ?",
                (updated.model_dump_json(), _now(), assessment_id),
            )
            _append_audit_event_locked(updated, event_type)
    record = updated.decision_record
    logger.info(
        "assessment updated",
        extra={
            "event": "store.update_assessment",
            "assessment_id": assessment_id,
            "vendor": updated.assessment_metadata.vendor,
            "workflow_status": record.workflow_status if record else None,
            "human_decision": record.human_decision if record else None,
        },
    )
    return updated


def get_assessment(assessment_id: Optional[str] = None) -> Optional[AssessmentResponse]:
    """Return one assessment by id. Does not fall back to a global latest row."""
    if not assessment_id:
        return None
    with _lock:
        _init_locked()
        assert _conn is not None
        _purge_locked()
        row = _conn.execute(
            "SELECT assessment_json FROM assessments WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()
        if row is None:
            return None
        return AssessmentResponse.model_validate_json(row[0])


def get_intake(assessment_id: Optional[str] = None) -> Optional[VendorInput]:
    """Return intake paired with the given assessment."""
    if not assessment_id:
        return None
    with _lock:
        _init_locked()
        assert _conn is not None
        _purge_locked()
        row = _conn.execute(
            "SELECT intake_json FROM assessments WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()
        if row is None:
            return None
        return VendorInput.model_validate_json(row[0])


def _get_access_token_digest(assessment_id: Optional[str] = None) -> Optional[str]:
    if not assessment_id:
        return None
    with _lock:
        _init_locked()
        assert _conn is not None
        row = _conn.execute(
            "SELECT access_token FROM assessments WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()
        return row[0] if row else None


def bind_jira_issue(issue_key: str, assessment_id: str) -> None:
    """Map a Jira issue.key to an assessment so webhooks do not need a global latest."""
    key = (issue_key or "").strip()
    if not key or not assessment_id:
        return
    with _lock:
        _init_locked()
        assert _conn is not None
        existing = _conn.execute(
            "SELECT assessment_id FROM jira_issues WHERE issue_key = ?",
            (key,),
        ).fetchone()
        if existing and existing[0] != assessment_id:
            raise ValueError(
                f"Jira issue {key} is already bound to another assessment"
            )
        _conn.execute(
            "INSERT OR IGNORE INTO jira_issues(issue_key, assessment_id) VALUES (?, ?)",
            (key, assessment_id),
        )
        _conn.commit()


def bind_jira_issues(issue_keys: List[str], assessment_id: str) -> None:
    for key in issue_keys:
        bind_jira_issue(key, assessment_id)


def resolve_jira_issue(issue_key: str) -> Optional[str]:
    key = (issue_key or "").strip()
    if not key:
        return None
    with _lock:
        _init_locked()
        assert _conn is not None
        row = _conn.execute(
            "SELECT assessment_id FROM jira_issues WHERE issue_key = ?",
            (key,),
        ).fetchone()
        return row[0] if row else None


def list_audit_events(assessment_id: str) -> List[dict]:
    """Return ordered event envelopes for audit export and tests (never tokens)."""
    with _lock:
        _init_locked()
        assert _conn is not None
        rows = _conn.execute(
            "SELECT id, event_type, timestamp, payload_json, previous_hash, event_hash "
            "FROM audit_events WHERE assessment_id = ? ORDER BY id",
            (assessment_id,),
        ).fetchall()
    events = []
    for row in rows:
        try:
            payload = json.loads(row[3])
            payload_parse_error = False
        except json.JSONDecodeError:
            payload = None
            payload_parse_error = True
        events.append(
            {
                "id": row[0],
                "assessment_id": assessment_id,
                "event_type": row[1],
                "timestamp": row[2],
                "payload": payload,
                "payload_parse_error": payload_parse_error,
                "previous_hash": row[4],
                "event_hash": row[5],
            }
        )
    return events


def verify_audit_chain(assessment_id: str) -> dict:
    """Verify event hashes, links, and the same-database head/count checkpoint."""
    with _lock:
        _init_locked()
        assert _conn is not None
        rows = _conn.execute(
            "SELECT id, event_type, timestamp, payload_json, previous_hash, event_hash "
            "FROM audit_events WHERE assessment_id = ? ORDER BY id",
            (assessment_id,),
        ).fetchall()
        head = _conn.execute(
            "SELECT event_count, head_hash FROM audit_heads WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchone()
    previous_hash = ""
    for row in rows:
        event_id, event_type, timestamp, payload_json, stored_previous, stored_hash = row
        try:
            expected_hash = _event_hash(
                assessment_id,
                event_type,
                timestamp,
                payload_json,
                previous_hash,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return {
                "valid": False,
                "event_count": len(rows),
                "head_hash": previous_hash,
                "failed_event_id": event_id,
            }
        if stored_previous != previous_hash or not secrets.compare_digest(
            stored_hash, expected_hash
        ):
            return {
                "valid": False,
                "event_count": len(rows),
                "head_hash": previous_hash,
                "failed_event_id": event_id,
            }
        previous_hash = stored_hash
    checkpoint_valid = bool(
        head
        and head[0] == len(rows)
        and secrets.compare_digest(head[1], previous_hash)
    )
    return {
        "valid": checkpoint_valid,
        "event_count": len(rows),
        "head_hash": previous_hash,
        "failed_event_id": None if checkpoint_valid else (rows[-1][0] if rows else None),
    }


def assessment_auth_required() -> bool:
    """Default on. Set REQUIRE_ASSESSMENT_AUTH=false only for local demos."""
    return get_settings().require_assessment_auth


def token_matches(assessment_id: Optional[str], provided: Optional[str]) -> bool:
    if not assessment_auth_required():
        return True
    expected = _get_access_token_digest(assessment_id)
    if not expected or not provided:
        return False
    candidate = _token_digest(provided) if expected.startswith("sha256:") else provided
    return secrets.compare_digest(expected, candidate)


def remember_event(event_id: str) -> bool:
    """Return True if this event is new; False if it was already processed."""
    now = _now()
    with _lock:
        _init_locked()
        assert _conn is not None
        _purge_locked()
        cursor = _conn.execute(
            "INSERT OR IGNORE INTO webhook_events(event_id, seen_at) VALUES (?, ?)",
            (event_id, now),
        )
        _conn.commit()
        return cursor.rowcount == 1


def forget_event(event_id: str) -> None:
    """Drop a recorded event id so a later retry can be processed (failed apply)."""
    with _lock:
        if _conn is None:
            return
        _conn.execute("DELETE FROM webhook_events WHERE event_id = ?", (event_id,))
        _conn.commit()


def count() -> int:
    """Number of assessments currently retained (tests / webhook disambiguation)."""
    with _lock:
        _init_locked()
        assert _conn is not None
        _purge_locked()
        return int(_conn.execute("SELECT COUNT(*) FROM assessments").fetchone()[0])


def clear() -> None:
    """Drop all session state. Used by tests; not exposed as an API."""
    with _lock:
        _init_locked()
        assert _conn is not None
        _conn.execute("DELETE FROM jira_issues")
        _conn.execute("DELETE FROM audit_events")
        _conn.execute("DELETE FROM audit_heads")
        _conn.execute("DELETE FROM assessments")
        _conn.execute("DELETE FROM webhook_events")
        _conn.commit()
    logger.debug("session cleared", extra={"event": "store.clear"})
