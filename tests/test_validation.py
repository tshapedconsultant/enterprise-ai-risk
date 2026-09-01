"""Input limits, malformed payloads, and isolation — no network."""

import uuid

import pytest
from pydantic import ValidationError

from app.models import ChatMessage, ChatRequest, VendorInput


def test_empty_vendor_name_rejected(client):
    response = client.post(
        "/api/v1/assess-vendor",
        json={
            "vendor_name": "",
            "service_description": "s",
            "intended_use": "u",
            "data_processed": "logs",
        },
    )
    assert response.status_code == 422


def test_vendor_name_newline_rejected(client):
    response = client.post(
        "/api/v1/assess-vendor",
        json={
            "vendor_name": "Evil\nVendor",
            "service_description": "s",
            "intended_use": "u",
            "data_processed": "logs",
        },
    )
    assert response.status_code == 422


def test_overlong_description_rejected(client):
    response = client.post(
        "/api/v1/assess-vendor",
        json={
            "vendor_name": "V",
            "service_description": "x" * 4001,
            "intended_use": "u",
            "data_processed": "logs",
        },
    )
    assert response.status_code == 422


def test_unicode_vendor_accepted(client):
    response = client.post(
        "/api/v1/assess-vendor",
        json={
            "vendor_name": "日本ベンダー",
            "service_description": "要約 API",
            "intended_use": "内部",
            "data_processed": "compiler logs",
            "has_dpa": True,
            "geographic_scope": "EU",
            "international_transfers": "none",
            "retention_period": "30 days",
            "model_provider": "local",
        },
    )
    assert response.status_code == 200
    assert response.json()["assessment_metadata"]["vendor"] == "日本ベンダー"


def test_html_is_treated_as_text(client):
    response = client.post(
        "/api/v1/assess-vendor",
        json={
            "vendor_name": "<b>Acme</b>",
            "service_description": "<script>alert(1)</script>",
            "intended_use": "use",
            "data_processed": "compiler logs",
        },
    )
    assert response.status_code == 200
    assert "<script>" in response.json()["assessment_metadata"]["vendor"] or response.json()["assessment_metadata"]["vendor"] == "<b>Acme</b>"


def test_wrong_types_rejected(client):
    response = client.post(
        "/api/v1/assess-vendor",
        json={
            "vendor_name": "V",
            "service_description": "s",
            "intended_use": "u",
            "data_processed": "logs",
            "has_dpa": "not-a-bool",
            "special_category_data": ["yes"],
        },
    )
    assert response.status_code == 422


def test_invalid_dpa_review_status_rejected():
    with pytest.raises(ValidationError):
        VendorInput(
            vendor_name="V",
            service_description="s",
            intended_use="u",
            data_processed="logs",
            dpa_review_status="signed-in-blood",
        )


def test_empty_chat_message_rejected(client):
    response = client.post("/api/v1/chat", json={"message": "", "history": []})
    assert response.status_code == 422


def test_chat_history_over_limit_rejected(client):
    history = [{"role": "user", "content": f"turn {i}"} for i in range(21)]
    response = client.post("/api/v1/chat", json={"message": "hello", "history": history})
    assert response.status_code == 422


def test_chat_invalid_assessment_id_rejected(client):
    response = client.post(
        "/api/v1/chat",
        json={"message": "hello", "history": [], "assessment_id": "not-a-uuid"},
    )
    assert response.status_code == 422


def test_chat_unknown_assessment_id(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    response = client.post(
        "/api/v1/chat",
        json={"message": "hello", "history": [], "assessment_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404


def test_assess_internal_error_hides_details(client, sample_intake, monkeypatch):
    def boom(_payload):
        raise RuntimeError("SELECT * FROM users WHERE api_key=sk-secret")

    monkeypatch.setattr("app.main.run_assessment", boom)
    response = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "users" not in detail
    assert "sk-secret" not in detail
    assert "request_id=" in detail
    assert response.headers.get("X-Request-Id")


def test_latest_isolates_webhook_to_named_assessment(client):
    a = client.post(
        "/api/v1/assess-vendor",
        json={
            "vendor_name": "VendorA",
            "service_description": "API",
            "intended_use": "test",
            "data_processed": "logs",
            "has_dpa": False,
        },
    )
    b = client.post(
        "/api/v1/assess-vendor",
        json={
            "vendor_name": "VendorB",
            "service_description": "API",
            "intended_use": "test",
            "data_processed": "logs",
            "has_dpa": False,
        },
    )
    assert a.status_code == 200 and b.status_code == 200
    id_a = a.json()["assessment_metadata"]["assessment_id"]
    id_b = b.json()["assessment_metadata"]["assessment_id"]
    from tests.conftest import LEGAL_APPROVER, fetch_assessment, jira_issue_updated, post_jira_webhook

    closed = post_jira_webhook(
        client,
        jira_issue_updated(email=LEGAL_APPROVER, assessment_id=id_a, changelog_id="iso-a"),
        "evt-iso-a",
    )
    assert closed.status_code == 200
    latest = fetch_assessment(client, id_b)
    assert latest.status_code == 200
    assert latest.json()["assessment"]["assessment_metadata"]["vendor"] == "VendorB"
    assert latest.json()["assessment"]["decision_record"]["legal_approver"] is None

    chat_a = client.post(
        "/api/v1/chat",
        json={"message": "What is the workflow?", "history": [], "assessment_id": id_a},
    )
    assert chat_a.status_code == 200
    assert chat_a.json()["vendor"] == "VendorA"
    assert chat_a.json()["decision"] == a.json()["assessment_metadata"]["decision"]
    chat_b = client.post(
        "/api/v1/chat",
        json={"message": "What is the workflow?", "history": [], "assessment_id": id_b},
    )
    assert chat_b.json()["vendor"] == "VendorB"


def test_assessment_token_required_when_enabled(client, sample_intake, monkeypatch):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    token = assess.headers["X-Assessment-Token"]
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    monkeypatch.setenv("REQUIRE_ASSESSMENT_AUTH", "true")
    denied = client.get(
        "/api/v1/assessment/latest",
        headers={"X-Assessment-Id": assessment_id},
    )
    assert denied.status_code == 401
    ok = client.get(
        "/api/v1/assessment/latest",
        headers={"X-Assessment-Token": token, "X-Assessment-Id": assessment_id},
    )
    assert ok.status_code == 200
    other = client.post(
        "/api/v1/chat",
        json={"message": "hello", "history": [], "assessment_id": assessment_id},
        headers={"X-Assessment-Token": "not-the-token"},
    )
    assert other.status_code == 401


def test_chat_message_max_length():
    with pytest.raises(ValidationError):
        ChatRequest(message="x" * 4001, history=[])
    ChatRequest(message="ok", history=[ChatMessage(role="user", content="hi") for _ in range(20)])
