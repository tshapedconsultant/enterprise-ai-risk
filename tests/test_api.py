"""HTTP API tests — health, assess, session, chat, validation."""

from tests.conftest import fetch_assessment


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
    assert body["approver_domain"] == "example.com"
    assert body["data_store"] == "sqlite"
    assert body["api_auth_required"] is False
    assert "gdpr" in body["frameworks"]["enabled"]
    assert "gdpr" in body["frameworks"]["enforced"]


def test_public_config(client):
    response = client.get("/api/v1/config")
    assert response.status_code == 200
    body = response.json()
    assert body["approver_domain"] == "example.com"
    assert body["api_auth_required"] is False
    assert "nist_ai_rmf" in body["frameworks"]["enabled"]


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
    assert "gdpr" in body["assessment_metadata"]["applicable_frameworks"]
    assert "JIRA_PUBLISH:False" in body["decision_record"]["decision_basis"]
    assert len(body["jira_tickets"]) == 4
    assert any("assess-vendor complete" in r.message for r in caplog_app.records)


def test_assess_vendor_validation_error(client):
    response = client.post("/api/v1/assess-vendor", json={"vendor_name": "Only name"})
    assert response.status_code == 422


def test_latest_without_id_does_not_leak_another_session(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    leaked = client.get("/api/v1/assessment/latest")
    assert leaked.status_code == 200
    assert leaked.json()["assessment"] is None


def test_latest_after_assess(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    latest = fetch_assessment(client, assessment_id)
    assert latest.status_code == 200
    data = latest.json()
    assert data["assessment"] is not None
    assert data["intake"]["vendor_name"] == "OpenAI"
    assert data["llm_enabled"] is False
    assert "access_token" not in data
    token = assess.headers.get("X-Assessment-Token")
    assert token
    assert token not in str(data)


def test_assessment_audit_endpoint_verifies_chain(client, sample_intake, monkeypatch):
    monkeypatch.setenv("REQUIRE_ASSESSMENT_AUTH", "true")
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    token = assess.headers["X-Assessment-Token"]
    denied = client.get(f"/api/v1/assessments/{assessment_id}/audit")
    assert denied.status_code == 401
    audit = client.get(
        f"/api/v1/assessments/{assessment_id}/audit",
        headers={"X-Assessment-Token": token},
    )
    assert audit.status_code == 200
    assert audit.json()["verification"]["valid"] is True
    assert audit.json()["events"][0]["event_type"] == "assessment.created"
    assert "access_token" not in str(audit.json())


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
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    response = client.post(
        "/api/v1/chat",
        json={"message": "What is the decision?", "history": [], "assessment_id": assessment_id},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["vendor"] == "OpenAI"
    assert body["used_llm"] is False
    assert body["decision"] == decision
    assert decision in body["reply"]
    assert any(r.name == "enterprise_ai_risk.app.llm" for r in caplog_app.records)


def test_chat_does_not_fall_back_to_another_vendor(client, sample_intake):
    first = client.post("/api/v1/assess-vendor", json=sample_intake)
    second = client.post(
        "/api/v1/assess-vendor",
        json={
            "vendor_name": "OtherCo",
            "service_description": "API",
            "intended_use": "test",
            "data_processed": "none",
            "has_dpa": False,
        },
    )
    assert first.status_code == 200 and second.status_code == 200
    orphan = client.post("/api/v1/chat", json={"message": "What is the decision?", "history": []})
    assert orphan.status_code == 200
    assert orphan.json()["vendor"] is None
    assert "No assessment loaded" in orphan.json()["reply"]


def test_api_token_protects_assess_vendor(client, sample_intake, monkeypatch):
    monkeypatch.setenv("API_ACCESS_TOKEN", "console-secret")
    denied = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert denied.status_code == 401
    ok = client.post(
        "/api/v1/assess-vendor",
        json=sample_intake,
        headers={"X-API-Token": "console-secret"},
    )
    assert ok.status_code == 200
    bearer = client.post(
        "/api/v1/assess-vendor",
        json=sample_intake,
        headers={"Authorization": "Bearer console-secret"},
    )
    assert bearer.status_code == 200


def test_api_token_protects_chat(client, sample_intake, monkeypatch):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    monkeypatch.setenv("API_ACCESS_TOKEN", "console-secret")
    denied = client.post(
        "/api/v1/chat",
        json={"message": "status?", "assessment_id": assessment_id},
    )
    assert denied.status_code == 401
    allowed = client.post(
        "/api/v1/chat",
        json={"message": "status?", "assessment_id": assessment_id},
        headers={"X-API-Token": "console-secret"},
    )
    assert allowed.status_code == 200
