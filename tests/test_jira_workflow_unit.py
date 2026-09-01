"""Unit tests for jira_workflow helpers (no network)."""

import json
from unittest.mock import patch

import httpx
import pytest

from app.jira_workflow import (
    apply_approval,
    approver_domain,
    approver_domain_ok,
    build_epic_and_subtasks,
    department_from_labels,
    jira_configured,
    parse_webhook_event,
    publish_to_jira,
)
from app.models import VendorInput
from app.scoring import evaluate


def test_parse_webhook_minimal_payload():
    parsed = parse_webhook_event(
        {
            "status": "Done",
            "approver_email": "j.perez@example.com",
            "labels": ["legal-review"],
            "assessment_id": "uuid-123",
        }
    )
    assert parsed.status == "Done"
    assert parsed.email == "j.perez@example.com"
    assert parsed.labels == ["legal-review"]
    assert parsed.key == ""
    assert parsed.assessment_id == "uuid-123"


def test_parse_webhook_jira_issue_shape():
    parsed = parse_webhook_event(
        {
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": "AIGOV-42",
                "fields": {
                    "status": {"name": "Done"},
                    "labels": ["infosec"],
                    "assignee": {"emailAddress": "secops@example.com"},
                    "description": "Line one\nAssessment-ID: abc-def\n",
                },
            }
        }
    )
    assert parsed.key == "AIGOV-42"
    assert parsed.status == "Done"
    assert parsed.email == ""
    assert parsed.labels == ["infosec"]
    assert parsed.assessment_id == "abc-def"


def test_parse_webhook_uses_actor_not_assignee():
    from app.jira_workflow import parse_webhook_event

    parsed = parse_webhook_event(
        {
            "webhookEvent": "jira:issue_updated",
            "user": {"emailAddress": "dpo@example.com"},
            "issue": {
                "key": "AIGOV-42",
                "fields": {
                    "status": {"name": "Done"},
                    "labels": ["legal-review"],
                    "assignee": {"emailAddress": "secops@example.com"},
                },
            },
        }
    )
    assert parsed.email == "dpo@example.com"
    assert parsed.actor_email == "dpo@example.com"


def test_department_from_labels():
    assert department_from_labels(["legal-review", "vendor-risk"]) == "legal"
    assert department_from_labels(["infosec"]) == "infosec"
    assert department_from_labels(["ai-governance-review"]) == "aigov"
    assert department_from_labels(["other"]) is None


def test_department_from_labels_rejects_multiple_departments():
    with pytest.raises(ValueError, match="single department"):
        department_from_labels(["legal-review", "infosec"])


def test_approver_domain_ok():
    assert approver_domain_ok("dpo@example.com")
    assert not approver_domain_ok("dpo@gmail.com")
    assert not approver_domain_ok("")


def test_approver_domain_required(monkeypatch):
    from app.config import ConfigurationError

    monkeypatch.delenv("JIRA_APPROVER_DOMAIN", raising=False)
    with pytest.raises(ConfigurationError, match="valid email domain"):
        approver_domain()


def test_approver_domain_rejects_malformed(monkeypatch):
    from app.config import ConfigurationError

    monkeypatch.setenv("JIRA_APPROVER_DOMAIN", "notadomain")
    with pytest.raises(ConfigurationError, match="valid email domain"):
        approver_domain()
    monkeypatch.setenv("JIRA_APPROVER_DOMAIN", "@example.com")
    with pytest.raises(ConfigurationError, match="valid email domain"):
        approver_domain()
    monkeypatch.setenv("JIRA_APPROVER_DOMAIN", "Example.com")
    assert approver_domain() == "example.com"


def test_resolve_account_id_requires_exact_match():
    from app.jira_workflow import _resolve_account_id

    class FakeClient:
        def get(self, url, params=None, auth=None, headers=None):
            class Resp:
                status_code = 200

                def json(self):
                    return [{"accountId": "wrong-person", "emailAddress": "other@example.com"}]

            return Resp()

    assert _resolve_account_id("dpo@example.com", FakeClient(), ("u", "p"), "https://ex.atlassian.net") is None


def test_delete_jira_issue_sends_auth():
    from app.jira_workflow import _delete_jira_issue

    calls = {}

    class FakeClient:
        def delete(self, url, auth=None, headers=None):
            calls["url"] = url
            calls["auth"] = auth
            calls["headers"] = headers

            class Resp:
                status_code = 204

            return Resp()

    _delete_jira_issue(FakeClient(), "https://ex.atlassian.net", ("bot@x", "token"), "AIGOV-1")
    assert calls["auth"] == ("bot@x", "token")
    assert calls["headers"]["Accept"] == "application/json"
    assert calls["url"].endswith("/issue/AIGOV-1")


def test_missing_department_ticket_is_not_approval():
    assessment = evaluate(
        VendorInput(
            vendor_name="T",
            service_description="s",
            intended_use="u",
            data_processed="email addresses",
            has_dpa=False,
        )
    )
    assessment.jira_tickets = [t for t in assessment.jira_tickets if t.fields.department != "legal"]
    before_gates = dict(assessment.decision_record.gate_status)
    with pytest.raises(ValueError, match="not approval"):
        apply_approval(
            assessment,
            {
                "status": "Done",
                "previous_status": "To Do",
                "approver_email": "dpo@example.com",
                "labels": ["legal-review"],
            },
        )
    assert assessment.decision_record.legal_approver is None
    assert assessment.decision_record.gate_status == before_gates


def test_publish_dry_run_without_credentials():
    tickets = build_epic_and_subtasks(
        vendor="V",
        triage_decision="PENDING REVIEW",
        residual="Moderate",
        need_legal=True,
        need_infosec=True,
        need_aigov=True,
        legal_reason="L",
        infosec_reason="S",
        aigov_reason="A",
    )
    result = publish_to_jira(tickets)
    assert result["published"] is False
    assert not jira_configured()


def test_apply_approval_ignores_open_status():
    assessment = evaluate(
        VendorInput(
            vendor_name="T",
            service_description="s",
            intended_use="u",
            data_processed="email",
            has_dpa=False,
        )
    )
    before = assessment.decision_record.workflow_status
    updated = apply_approval(assessment, {"status": "In Progress", "previous_status": "To Do", "labels": ["legal-review"]})
    assert updated.decision_record.workflow_status == before
    assert updated.decision_record.legal_approver is None


def test_single_gate_does_not_approve():
    assessment = evaluate(
        VendorInput(
            vendor_name="T",
            service_description="s",
            intended_use="u",
            data_processed="email",
            has_dpa=False,
        )
    )
    updated = apply_approval(
        assessment,
        {"status": "Done", "previous_status": "To Do", "approver_email": "dpo@example.com", "labels": ["legal-review"]},
    )
    assert updated.decision_record.legal_approver == "dpo@example.com"
    assert updated.decision_record.workflow_status == "HUMAN_REVIEW_REQUIRED"
    assert updated.decision_record.human_decision is None


def _sample_tickets():
    return build_epic_and_subtasks(
        vendor="V",
        triage_decision="PENDING REVIEW",
        residual="Moderate",
        need_legal=True,
        need_infosec=True,
        need_aigov=True,
        legal_reason="L",
        infosec_reason="S",
        aigov_reason="A",
        assessment_id="11111111-1111-1111-1111-111111111111",
    )


def _jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("JIRA_USER_EMAIL", "bot@example.com")


class _JiraMock:
    def __init__(self, fail_on_create: int | None = None, fail_status: int = 500, timeout: bool = False):
        self.creates = 0
        self.deleted: list[str] = []
        self.payloads: list[dict] = []
        self.fail_on_create = fail_on_create
        self.fail_status = fail_status
        self.timeout = timeout

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.timeout:
            raise httpx.TimeoutException("jira timeout", request=request)
        path = request.url.path
        if path.endswith("/user/search"):
            email = request.url.params.get("query") or ""
            return httpx.Response(
                200,
                json=[{"accountId": f"acc-{email}", "emailAddress": email}],
            )
        if path.endswith("/issue") and request.method == "POST":
            self.creates += 1
            if self.fail_on_create is not None and self.creates == self.fail_on_create:
                return httpx.Response(self.fail_status, json={"error": "failed"})
            payload = json.loads(request.content.decode("utf-8"))
            self.payloads.append(payload)
            key = f"AIGOV-{self.creates}"
            return httpx.Response(201, json={"key": key, "id": str(self.creates)})
        if "/issue/" in path and request.method == "DELETE":
            key = path.rsplit("/", 1)[-1]
            self.deleted.append(key)
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "unexpected"})


def _run_publish(monkeypatch, mock: _JiraMock):
    _jira_env(monkeypatch)
    transport = httpx.MockTransport(mock.handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs.setdefault("timeout", httpx.Timeout(10.0, connect=3.0))
        return real_client(*args, **kwargs)

    tickets = _sample_tickets()
    with patch("app.jira_workflow.httpx.Client", fake_client):
        return publish_to_jira(tickets), tickets, mock


def test_publish_creates_epic_then_subtasks_with_real_parent(monkeypatch):
    result, tickets, mock = _run_publish(monkeypatch, _JiraMock())
    assert result["published"] is True
    assert result["keys"] == ["AIGOV-1", "AIGOV-2", "AIGOV-3", "AIGOV-4"]
    assert result["parent"] == "AIGOV-1"
    assert tickets[0].fields.issue_key == "AIGOV-1"
    assert tickets[0].fields.parent is None
    assert "parent" not in mock.payloads[0]["fields"]
    assert mock.payloads[0]["fields"]["issuetype"]["name"] == "Epic"
    for payload in mock.payloads[1:]:
        assert payload["fields"]["parent"] == {"key": "AIGOV-1"}
        assert payload["fields"]["issuetype"]["name"] == "Task"
        desc = payload["fields"]["description"]
        assert desc["type"] == "doc"
        assert desc["version"] == 1
        assert desc["content"][0]["type"] == "paragraph"


def test_publish_rollback_when_second_subtask_fails(monkeypatch):
    mock = _JiraMock(fail_on_create=3, fail_status=500)
    with pytest.raises(httpx.HTTPStatusError):
        _run_publish(monkeypatch, mock)
    assert mock.creates == 3
    assert mock.deleted == ["AIGOV-2", "AIGOV-1"]


def test_publish_401_rolls_back_epic(monkeypatch):
    mock = _JiraMock(fail_on_create=1, fail_status=401)
    with pytest.raises(httpx.HTTPStatusError):
        _run_publish(monkeypatch, mock)
    assert mock.deleted == []


def test_publish_429_rolls_back(monkeypatch):
    mock = _JiraMock(fail_on_create=2, fail_status=429)
    with pytest.raises(httpx.HTTPStatusError):
        _run_publish(monkeypatch, mock)
    assert mock.deleted == ["AIGOV-1"]


def test_publish_timeout(monkeypatch):
    mock = _JiraMock(timeout=True)
    with pytest.raises(httpx.TimeoutException):
        _run_publish(monkeypatch, mock)


def test_publish_user_search_no_match_omits_assignee(monkeypatch):
    class NoUser(_JiraMock):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/user/search"):
                return httpx.Response(200, json=[{"accountId": "other", "emailAddress": "other@example.com"}])
            return super().handler(request)

    result, tickets, mock = _run_publish(monkeypatch, NoUser())
    assert result["published"] is True
    for payload in mock.payloads:
        assert "assignee" not in payload["fields"] or payload["fields"].get("assignee") is None


def test_department_tickets_are_tasks_not_subtasks():
    tickets = _sample_tickets()
    assert tickets[0].fields.issuetype.name == "Epic"
    assert tickets[0].fields.parent is None
    for ticket in tickets[1:]:
        assert ticket.fields.issuetype.name == "Task"
        assert ticket.fields.parent == {"key": "AIGOV-PARENT"}


def test_assignee_fallback_does_not_approve():
    assessment = evaluate(
        VendorInput(
            vendor_name="T",
            service_description="s",
            intended_use="u",
            data_processed="email",
            has_dpa=False,
        )
    )
    with pytest.raises(ValueError, match="assignee is not an approver"):
        apply_approval(
            assessment,
            {
                "webhookEvent": "jira:issue_updated",
                "changelog": {"items": [{"field": "status", "fromString": "To Do", "toString": "Done"}]},
                "issue": {
                    "key": "AIGOV-10",
                    "fields": {
                        "status": {"name": "Done"},
                        "labels": ["legal-review"],
                        "issuetype": {"name": "Task"},
                        "project": {"key": "AIGOV"},
                        "assignee": {"emailAddress": "dpo@example.com"},
                    },
                },
            },
        )
    assert assessment.decision_record.legal_approver is None

