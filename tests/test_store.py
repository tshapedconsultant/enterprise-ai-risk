"""In-memory store behaviour."""

from pathlib import Path

from app import store
from app.enums import WorkflowStatus
from app.models import VendorInput
from app.scoring import evaluate


def test_store_round_trip():
    store.clear()
    assert store.get_assessment() is None
    assert store.get_intake() is None

    payload = VendorInput(
        vendor_name="StoreCo",
        service_description="API",
        intended_use="test",
        data_processed="none",
        has_dpa=False,
    )
    assessment = evaluate(payload)
    store.save(payload, assessment)

    assert store.get_intake().vendor_name == "StoreCo"
    assert store.get_assessment().assessment_metadata.vendor == "StoreCo"

    store.clear()
    assert store.get_assessment() is None


def test_save_assessment_preserves_intake():
    store.clear()
    payload = VendorInput(
        vendor_name="KeepIntake",
        service_description="API",
        intended_use="test",
        data_processed="none",
        has_dpa=False,
    )
    assessment = evaluate(payload)
    store.save(payload, assessment)

    assessment.decision_record.workflow_status = WorkflowStatus.SUPERSEDED
    store.save_assessment(assessment)

    assert store.get_intake().vendor_name == "KeepIntake"
    assert store.get_assessment().decision_record.workflow_status == WorkflowStatus.SUPERSEDED


def test_assessment_auth_defaults_on(monkeypatch):
    monkeypatch.delenv("REQUIRE_ASSESSMENT_AUTH", raising=False)
    assert store.assessment_auth_required() is True
    assert store.token_matches(None, None) is False
    monkeypatch.setenv("REQUIRE_ASSESSMENT_AUTH", "false")
    assert store.assessment_auth_required() is False
    assert store.token_matches(None, None) is True


def test_webhook_events_shared_via_sqlite_file(tmp_path, monkeypatch):
    path = tmp_path / "events.sqlite"
    monkeypatch.setenv("WEBHOOK_EVENT_STORE", str(path))
    store.close_event_store()
    try:
        assert store.init_event_store() == "sqlite"
        assert store.remember_event("evt-shared") is True
        store.close_event_store()
        assert store.init_event_store() == "sqlite"
        assert store.remember_event("evt-shared") is False
    finally:
        store.close_event_store()
        monkeypatch.setenv(
            "WEBHOOK_EVENT_STORE",
            str(Path(__file__).resolve().parent / "logs" / "webhook-events.sqlite"),
        )
        store.init_event_store()
