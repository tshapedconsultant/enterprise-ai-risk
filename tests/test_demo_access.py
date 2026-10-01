"""Demo email gate: store the address, issue a token, do not publish the list."""

import pytest

from app import store


def test_demo_access_hidden_when_gate_off(client):
    response = client.post("/api/v1/demo-access", json={"email": "ada@example.com"})
    assert response.status_code == 404
    assert store.demo_lead_recorded("ada@example.com") is False
    assert client.get("/api/v1/config").json()["demo_email_gate"] is False


def test_demo_access_stores_email_and_opens_console(client, sample_intake, monkeypatch):
    monkeypatch.setenv("DEMO_EMAIL_GATE", "true")
    monkeypatch.setenv("API_ACCESS_TOKEN", "")

    denied = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert denied.status_code == 401

    invalid = client.post("/api/v1/demo-access", json={"email": "not-an-email"})
    assert invalid.status_code == 422
    assert store.demo_lead_recorded("not-an-email") is False

    granted = client.post("/api/v1/demo-access", json={"email": "Ada@Example.com"})
    assert granted.status_code == 200
    body = granted.json()
    assert body["ok"] is True
    assert "email" not in body
    token = body["access_token"]
    assert token
    assert store.demo_lead_recorded("ada@example.com") is True
    assert store.demo_access_token_valid(token) is True

    opened = client.post(
        "/api/v1/assess-vendor",
        json=sample_intake,
        headers={"X-API-Token": token},
    )
    assert opened.status_code == 200

    again = client.post("/api/v1/demo-access", json={"email": "ada@example.com"})
    assert again.status_code == 200
    assert again.json()["access_token"] != token
    assert store.demo_access_token_valid(token) is False
    assert store.demo_access_token_valid(again.json()["access_token"]) is True

    listing = client.get("/api/v1/demo-access")
    assert listing.status_code == 405


def test_operator_token_still_works_with_email_gate(client, sample_intake, monkeypatch):
    monkeypatch.setenv("DEMO_EMAIL_GATE", "true")
    monkeypatch.setenv("API_ACCESS_TOKEN", "operator-secret")
    response = client.post(
        "/api/v1/assess-vendor",
        json=sample_intake,
        headers={"X-API-Token": "operator-secret"},
    )
    assert response.status_code == 200


def test_index_includes_email_gate(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="demo-gate"' in response.text
