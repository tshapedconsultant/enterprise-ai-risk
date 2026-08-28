"""HTTP API tests — health, assess, session, chat, validation."""

import pytest


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "approver_domain" not in body
    assert "llm_enabled" not in body


def test_health_details(client):
    response = client.get("/api/v1/health/details")
    assert response.status_code == 200
    body = response.json()
    assert body["scoring_engine"] == "deterministic-rules-v1"
    assert body["llm_enabled"] is False
    assert body["jira_outbound"] is False
    assert body["approver_domain"] == "adevinta.com"
    assert body["webhook_event_store"] == "sqlite"


def test_index_serves_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Enterprise AI Vendor Risk" in response.text or "Vendor assessment" in response.text


def test_assess_vendor_returns_full_payload(client, sample_intake, caplog_app):
    response = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert response.status_code == 200
    body = response.json()
    assert body["assessment_metadata"]["vendor"] == "OpenAI"
    assert body["assessment_metadata"]["decision"] != "APPROVE"
    assert "JIRA_PUBLISH:False" in body["decision_record"]["decision_basis"]
    assert len(body["jira_tickets"]) == 4
    assert any("assess-vendor complete" in r.message for r in caplog_app.records)


def test_assess_vendor_validation_error(client):
    response = client.post("/api/v1/assess-vendor", json={"vendor_name": "Only name"})
    assert response.status_code == 422


def test_latest_after_assess(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    latest = client.get("/api/v1/assessment/latest")
    assert latest.status_code == 200
    data = latest.json()
    assert data["assessment"] is not None
    assert data["intake"]["vendor_name"] == "OpenAI"
    assert data["llm_enabled"] is False
    assert "access_token" not in data
    token = assess.headers.get("X-Assessment-Token")
    assert token
    assert token not in str(data)


def test_chat_after_simulated_reload_requires_persisted_token(client, sample_intake, monkeypatch):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    token = assess.headers["X-Assessment-Token"]
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    monkeypatch.setenv("REQUIRE_ASSESSMENT_AUTH", "true")
    latest = client.get(
        "/api/v1/assessment/latest",
        headers={"X-Assessment-Token": token, "X-Assessment-Id": assessment_id},
    )
    assert latest.status_code == 200
    assert latest.json()["assessment"]["assessment_metadata"]["assessment_id"] == assessment_id
    denied = client.post(
        "/api/v1/chat",
        json={"message": "What is the workflow?", "history": [], "assessment_id": assessment_id},
    )
    assert denied.status_code == 401
    ok = client.post(
        "/api/v1/chat",
        json={"message": "What is the workflow?", "history": [], "assessment_id": assessment_id},
        headers={"X-Assessment-Token": token, "X-Assessment-Id": assessment_id},
    )
    assert ok.status_code == 200
    assert ok.json()["vendor"] == "OpenAI"


def test_latest_empty_session(client):
    latest = client.get("/api/v1/assessment/latest")
    assert latest.json()["assessment"] is None


def test_latest_rejects_malformed_assessment_id(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    response = client.get(
        "/api/v1/assessment/latest",
        headers={"X-Assessment-Id": "not-a-uuid"},
    )
    assert response.status_code == 400
    assert "UUID" in response.json()["detail"]


def test_chat_without_assessment(client):
    response = client.post("/api/v1/chat", json={"message": "Why is risk high?", "history": []})
    assert response.status_code == 200
    assert "No assessment loaded" in response.json()["reply"]
    assert response.json()["used_llm"] is False


def test_chat_after_assess(client, sample_intake, caplog_app):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    decision = assess.json()["assessment_metadata"]["decision"]
    response = client.post(
        "/api/v1/chat",
        json={"message": "What is the decision?", "history": []},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["vendor"] == "OpenAI"
    assert body["used_llm"] is False
    assert body["decision"] == decision
    assert decision in body["reply"]
    assert any(r.name == "enterprise_ai_risk.app.llm" for r in caplog_app.records)
