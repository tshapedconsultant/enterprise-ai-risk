"""Shared pytest fixtures. Tests run in-process; no Jira or OpenAI network."""

import json
import logging
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["JIRA_WEBHOOK_SECRET"] = "test-webhook-secret"
os.environ["JIRA_APPROVER_DOMAIN"] = "adevinta.com"
os.environ["REQUIRE_ASSESSMENT_AUTH"] = "false"
os.environ["WEBHOOK_EVENT_STORE"] = str(Path(__file__).resolve().parent / "logs" / "webhook-events.sqlite")
os.environ["HEALTH_DETAILS_TOKEN"] = ""
os.environ["LOG_LEVEL"] = "DEBUG"
os.environ["LOG_FORMAT"] = "text"
os.environ["LOG_FILE"] = str(Path(__file__).resolve().parent / "logs" / "pytest-app.log")
os.environ["OPENAI_API_KEY"] = ""
os.environ["JIRA_BASE_URL"] = ""
os.environ["JIRA_API_TOKEN"] = ""

from app.logging_config import configure_logging

configure_logging(force=True)

TEST_APPROVER_DOMAIN = os.environ["JIRA_APPROVER_DOMAIN"]
LEGAL_APPROVER = f"dpo@{TEST_APPROVER_DOMAIN}"
SECOPS_APPROVER = f"secops@{TEST_APPROVER_DOMAIN}"
AIGOV_APPROVER = f"aigov@{TEST_APPROVER_DOMAIN}"
WEBHOOK_SECRET = os.environ["JIRA_WEBHOOK_SECRET"]


@pytest.fixture()
def client():
    from app import store
    from app.main import app

    store.clear()
    with TestClient(app) as test_client:
        yield test_client
    store.clear()


@pytest.fixture()
def caplog_app(caplog):
    """Capture enterprise_ai_risk log records (structured extra fields on record)."""
    caplog.set_level(logging.DEBUG, logger="enterprise_ai_risk")
    return caplog


@pytest.fixture()
def sample_intake() -> dict:
    return {
        "vendor_name": "OpenAI",
        "service_description": "GPT API",
        "intended_use": "Employee copilot",
        "data_processed": "Prompts with customer names",
        "has_dpa": False,
    }


def hmac_headers(raw: bytes, event_id: str, secret: str = WEBHOOK_SECRET, timestamp: str | None = None) -> dict:
    from app.security import hmac_hex

    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={hmac_hex(raw, secret)}",
        "X-Jira-Timestamp": timestamp if timestamp is not None else str(time.time()),
        "X-Jira-Event-Id": event_id,
    }


def signed_webhook(body: dict, event_id: str, secret: str = WEBHOOK_SECRET) -> tuple[bytes, dict]:
    """Return raw body bytes and HMAC/timestamp/event headers for /webhooks/jira."""
    raw = json.dumps(body).encode("utf-8")
    return raw, hmac_headers(raw, event_id, secret=secret)


def jira_issue_updated(
    *,
    key: str = "AIGOV-10",
    status: str = "Done",
    previous: str = "To Do",
    email: str = LEGAL_APPROVER,
    labels: list[str] | None = None,
    assessment_id: str | None = None,
    issue_type: str = "Task",
    project: str = "AIGOV",
    changelog_id: str = "chg-1",
) -> dict:
    """Realistic Jira Cloud issue.updated webhook body."""
    body = {
        "webhookEvent": "jira:issue_updated",
        "user": {"emailAddress": email},
        "issue": {
            "key": key,
            "fields": {
                "status": {"name": status},
                "labels": labels or ["legal-review"],
                "issuetype": {"name": issue_type},
                "project": {"key": project},
                "assignee": {"emailAddress": email},
                "description": f"Assessment-ID: {assessment_id}" if assessment_id else "No id",
            },
        },
        "changelog": {
            "id": changelog_id,
            "items": [{"field": "status", "fromString": previous, "toString": status}],
        },
    }
    if assessment_id:
        body["assessment_id"] = assessment_id
    return body


def post_jira_webhook(client: TestClient, body: dict, event_id: str, headers: dict | None = None):
    raw, signed_headers = signed_webhook(body, event_id)
    if headers:
        signed_headers.update(headers)
    return client.post("/api/v1/webhooks/jira", content=raw, headers=signed_headers)
