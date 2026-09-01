"""Security and concurrency fixes from security audit."""

import os

import pytest
from fastapi import HTTPException

from app.models import VendorInput
from app.scoring import looks_like_personal_data, infer_privacy_signals, evaluate
from app.security import hmac_hex, verify_webhook_hmac
from tests.conftest import fetch_assessment, signed_webhook


def test_webhook_hmac_accepts_active_and_previous_during_rotation():
    body = b'{"webhookEvent":"jira:issue_updated"}'
    active = hmac_hex(body, "active-secret")
    previous = hmac_hex(body, "previous-secret")
    verify_webhook_hmac(body, active, "active-secret", "previous-secret")
    verify_webhook_hmac(body, previous, "active-secret", "previous-secret")
    with pytest.raises(HTTPException) as wrong:
        verify_webhook_hmac(body, hmac_hex(body, "wrong"), "active-secret", "previous-secret")
    assert wrong.value.status_code == 401
    with pytest.raises(HTTPException) as removed:
        verify_webhook_hmac(body, previous, "active-secret")
    assert removed.value.status_code == 401


def test_email_delivery_is_not_personal_data():
    payload = VendorInput(
        vendor_name="Mailgun",
        service_description="SMTP relay",
        intended_use="Transactional mail",
        data_processed="email delivery system logs",
        has_dpa=False,
    )
    assert not looks_like_personal_data(payload)


def test_customer_identifiers_are_personal_data():
    payload = VendorInput(
        vendor_name="CRM",
        service_description="CRM",
        intended_use="sales",
        data_processed="customer identifiers stored in EU",
        has_dpa=False,
    )
    assert looks_like_personal_data(payload)
    assert looks_like_personal_data(
        VendorInput(
            vendor_name="T",
            service_description="s",
            intended_use="u",
            data_processed="Processing HR employee records",
            has_dpa=False,
        )
    )
    assert looks_like_personal_data(
        VendorInput(
            vendor_name="T",
            service_description="s",
            intended_use="u",
            data_processed="HR data for payroll",
            has_dpa=False,
        )
    )


def test_dpia_explicit_false_without_personal():
    payload = VendorInput(
        vendor_name="T",
        service_description="s",
        intended_use="u",
        data_processed="compiler logs only",
        has_dpa=False,
        privacy_assessment_required=False,
    )
    assert not looks_like_personal_data(payload)
    _, indicated, _, _, _ = infer_privacy_signals(payload)
    assert not indicated


def test_dpia_explicit_false_with_personal_still_required():
    payload = VendorInput(
        vendor_name="T",
        service_description="s",
        intended_use="u",
        data_processed="employee email addresses",
        has_dpa=False,
        privacy_assessment_required=False,
    )
    personal = looks_like_personal_data(payload)
    assert personal
    _, indicated, _, _, _ = infer_privacy_signals(payload)
    assert indicated
    result = evaluate(payload)
    assert "RULE-DPIA-EXPLICIT-FALSE-OVERRIDDEN" in result.decision_record.decision_basis


def test_assessment_id_is_stable_uuid():
    result = evaluate(
        VendorInput(
            vendor_name="T",
            service_description="s",
            intended_use="u",
            data_processed="none",
            has_dpa=False,
        )
    )
    aid = result.assessment_metadata.assessment_id
    assert len(aid) == 36
    assert result.jira_tickets[0].fields.description.endswith(f"Assessment-ID: {aid}")


def test_webhook_requires_secret(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    old = os.environ.pop("JIRA_WEBHOOK_SECRET", None)
    try:
        from tests.conftest import signed_webhook

        body = {
            "status": "Done",
            "previous_status": "To Do",
            "approver_email": "dpo@example.com",
            "labels": ["legal-review"],
            "assessment_id": assessment_id,
        }
        raw, headers = signed_webhook(body, "evt-nosecret")
        response = client.post("/api/v1/webhooks/jira", content=raw, headers=headers)
        assert response.status_code == 503
    finally:
        if old:
            os.environ["JIRA_WEBHOOK_SECRET"] = old


def test_concurrent_assessments_do_not_collide(client):
    payload_a = {
        "vendor_name": "VendorA",
        "service_description": "API",
        "intended_use": "test",
        "data_processed": "logs",
        "has_dpa": False,
    }
    payload_b = {
        "vendor_name": "VendorB",
        "service_description": "API",
        "intended_use": "test",
        "data_processed": "logs",
        "has_dpa": False,
    }
    a = client.post("/api/v1/assess-vendor", json=payload_a).json()
    b = client.post("/api/v1/assess-vendor", json=payload_b).json()
    id_a = a["assessment_metadata"]["assessment_id"]
    id_b = b["assessment_metadata"]["assessment_id"]
    assert id_a != id_b

    from tests.conftest import signed_webhook

    body = {
        "status": "Done",
        "previous_status": "To Do",
        "approver_email": "dpo@example.com",
        "labels": ["legal-review"],
        "assessment_id": id_a,
        "issue_type": "Task",
    }
    raw, headers = signed_webhook(body, "evt-a-legal")
    client.post("/api/v1/webhooks/jira", content=raw, headers=headers)
    latest_b = fetch_assessment(client, id_b).json()
    assert latest_b["assessment"]["assessment_metadata"]["vendor"] == "VendorB"
    assert latest_b["assessment"]["decision_record"]["legal_approver"] is None

    got_a = client.post(
        "/api/v1/chat",
        json={"message": "vendor name?", "history": [], "assessment_id": id_a},
    ).json()
    assert got_a["vendor"] == "VendorA"


def test_main_does_not_load_plaintext_key_file():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8-sig")
    assert "LUNA.txt" not in src
    assert "read_text" not in src


def test_rate_limiter_deletes_empty_keys(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app import security

    security._rate_hits.clear()
    security._rate_hits["assess:1.1.1.1"].append(0.0)
    monkeypatch.setattr(security.time, "monotonic", lambda: security.RATE_LIMIT_WINDOW + 5)
    request = MagicMock()
    request.client = SimpleNamespace(host="2.2.2.2")
    try:
        security.enforce_rate_limit(request, "assess")
        assert "assess:1.1.1.1" not in security._rate_hits
        assert "assess:2.2.2.2" in security._rate_hits
    finally:
        security._rate_hits.clear()


def test_rate_limit_uses_forwarded_for_when_peer_is_trusted_proxy(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app import security

    security._rate_hits.clear()
    monkeypatch.setenv("TRUSTED_PROXIES", "1.1.1.1")
    request = MagicMock()
    request.client = SimpleNamespace(host="1.1.1.1")
    request.headers.get.side_effect = lambda key, default=None: (
        "203.0.113.9" if str(key).lower() == "x-forwarded-for" else default
    )
    try:
        security.enforce_rate_limit(request, "assess")
        assert "assess:203.0.113.9" in security._rate_hits
        assert "assess:1.1.1.1" not in security._rate_hits
    finally:
        security._rate_hits.clear()


def test_forwarded_for_uses_rightmost_untrusted_hop(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app import security

    security._rate_hits.clear()
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8")
    request = MagicMock()
    request.client = SimpleNamespace(host="10.0.0.1")
    request.headers.get.side_effect = lambda key, default=None: (
        "203.0.113.9, 198.51.100.10" if str(key).lower() == "x-forwarded-for" else default
    )
    try:
        security.enforce_rate_limit(request, "assess")
        assert "assess:198.51.100.10" in security._rate_hits
        assert "assess:203.0.113.9" not in security._rate_hits
    finally:
        security._rate_hits.clear()


def test_forwarded_for_ignored_when_peer_is_not_a_trusted_proxy(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from app import security

    security._rate_hits.clear()
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8")
    request = MagicMock()
    request.client = SimpleNamespace(host="8.8.8.8")
    request.headers.get.side_effect = lambda key, default=None: (
        "203.0.113.9" if str(key).lower() == "x-forwarded-for" else default
    )
    try:
        security.enforce_rate_limit(request, "assess")
        assert "assess:8.8.8.8" in security._rate_hits
        assert "assess:203.0.113.9" not in security._rate_hits
    finally:
        security._rate_hits.clear()
