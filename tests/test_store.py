"""SQLite assessment store: round-trip, TTL retention, restart, Jira key map."""

from pathlib import Path
import sqlite3

import pytest

from app import store
from app.enums import WorkflowStatus
from app.models import VendorInput
from app.scoring import evaluate


def _intake(name: str = "StoreCo") -> VendorInput:
    return VendorInput(
        vendor_name=name,
        service_description="API",
        intended_use="test",
        data_processed="none",
        has_dpa=False,
    )


def test_store_round_trip():
    store.clear()
    payload = _intake()
    assessment = evaluate(payload)
    aid = assessment.assessment_metadata.assessment_id
    store.save(payload, assessment)

    assert store.get_assessment() is None
    assert store.get_intake() is None
    assert store.get_assessment(aid).assessment_metadata.vendor == "StoreCo"
    assert store.get_intake(aid).vendor_name == "StoreCo"

    store.clear()
    assert store.get_assessment(aid) is None


def test_save_assessment_preserves_intake():
    store.clear()
    payload = _intake("KeepIntake")
    assessment = evaluate(payload)
    aid = assessment.assessment_metadata.assessment_id
    store.save(payload, assessment)

    assessment.decision_record.workflow_status = WorkflowStatus.SUPERSEDED
    store.save_assessment(assessment)

    assert store.get_intake(aid).vendor_name == "KeepIntake"
    assert store.get_assessment(aid).decision_record.workflow_status == WorkflowStatus.SUPERSEDED


def test_assessment_auth_defaults_on(monkeypatch):
    monkeypatch.delenv("REQUIRE_ASSESSMENT_AUTH", raising=False)
    assert store.assessment_auth_required() is True
    assert store.token_matches(None, None) is False
    monkeypatch.setenv("REQUIRE_ASSESSMENT_AUTH", "false")
    assert store.assessment_auth_required() is False
    assert store.token_matches(None, None) is True


def test_assessments_survive_store_reopen(tmp_path, monkeypatch):
    path = tmp_path / "app.sqlite"
    monkeypatch.setenv("DATA_STORE", str(path))
    monkeypatch.setenv("REQUIRE_ASSESSMENT_AUTH", "true")
    store.close_store()
    try:
        store.init_store()
        store.clear()
        payload = _intake("DurableCo")
        assessment = evaluate(payload)
        aid = assessment.assessment_metadata.assessment_id
        token = store.save(payload, assessment)
        store.bind_jira_issue("AIGOV-99", aid)
        store.close_store()
        assert store.init_store() == "sqlite"
        restored = store.get_assessment(aid)
        assert restored is not None
        assert restored.assessment_metadata.vendor == "DurableCo"
        assert store.token_matches(aid, token) is True
        assert store.token_matches(aid, "wrong") is False
        assert store.resolve_jira_issue("AIGOV-99") == aid
    finally:
        store.close_store()
        monkeypatch.setenv(
            "DATA_STORE",
            str(Path(__file__).resolve().parent / "logs" / "app.sqlite"),
        )
        store.init_store()


def test_webhook_events_shared_via_sqlite_file(tmp_path, monkeypatch):
    path = tmp_path / "events.sqlite"
    monkeypatch.setenv("DATA_STORE", str(path))
    store.close_store()
    try:
        assert store.init_store() == "sqlite"
        assert store.remember_event("evt-shared") is True
        store.close_store()
        assert store.init_store() == "sqlite"
        assert store.remember_event("evt-shared") is False
    finally:
        store.close_store()
        monkeypatch.setenv(
            "DATA_STORE",
            str(Path(__file__).resolve().parent / "logs" / "app.sqlite"),
        )
        store.init_store()


def test_jira_issue_map_does_not_use_global_latest():
    store.clear()
    first = evaluate(_intake("One"))
    second = evaluate(_intake("Two"))
    store.save(_intake("One"), first)
    store.save(_intake("Two"), second)
    store.bind_jira_issue("AIGOV-1", first.assessment_metadata.assessment_id)
    assert store.resolve_jira_issue("AIGOV-1") == first.assessment_metadata.assessment_id
    with pytest.raises(ValueError, match="already bound"):
        store.bind_jira_issue("AIGOV-1", second.assessment_metadata.assessment_id)


def test_atomic_assessment_updater_preserves_token(monkeypatch):
    store.clear()
    monkeypatch.setenv("REQUIRE_ASSESSMENT_AUTH", "true")
    payload = _intake("AtomicCo")
    assessment = evaluate(payload)
    aid = assessment.assessment_metadata.assessment_id
    token = store.save(payload, assessment)

    def supersede(current):
        current.decision_record.workflow_status = WorkflowStatus.SUPERSEDED
        return current

    updated = store.update_assessment(aid, supersede)
    assert updated.decision_record.workflow_status == WorkflowStatus.SUPERSEDED
    assert store.get_assessment(aid).decision_record.workflow_status == WorkflowStatus.SUPERSEDED
    assert store.token_matches(aid, token) is True
    assert store.token_matches(aid, "wrong") is False
    events = store.list_audit_events(aid)
    assert [event["event_type"] for event in events] == [
        "assessment.created",
        "assessment.updated",
    ]
    assert events[0]["previous_hash"] == ""
    assert events[1]["previous_hash"] == events[0]["event_hash"]
    verification = store.verify_audit_chain(aid)
    assert verification["valid"] is True
    assert verification["event_count"] == 2
    assert verification["head_hash"] == events[1]["event_hash"]
    assert verification["failed_event_id"] is None
    assert verification["anchors"]["missing_external_anchor"] is False
    assert verification["anchors"]["head_matches_last_anchor"] is True


def test_audit_chain_detects_payload_tampering(tmp_path, monkeypatch):
    path = tmp_path / "tamper.sqlite"
    monkeypatch.setenv("DATA_STORE", str(path))
    store.close_store()
    try:
        payload = _intake("TamperCo")
        assessment = evaluate(payload)
        aid = assessment.assessment_metadata.assessment_id
        store.save(payload, assessment)
        assert store.verify_audit_chain(aid)["valid"] is True
        store.close_store()
        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE audit_events SET payload_json = ? WHERE assessment_id = ?",
                ('{"tampered":true}', aid),
            )
        store.init_store()
        verification = store.verify_audit_chain(aid)
        assert verification["valid"] is False
        assert verification["failed_event_id"] is not None
    finally:
        store.close_store()
        monkeypatch.setenv(
            "DATA_STORE",
            str(Path(__file__).resolve().parent / "logs" / "app.sqlite"),
        )
        store.init_store()
