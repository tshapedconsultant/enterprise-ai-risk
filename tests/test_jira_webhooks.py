"""Jira webhook security, idempotency, and happy-path — in-process, no Jira Cloud."""

import json
import time
import uuid

import pytest

from app import store
from app.jira_workflow import apply_approval
from app.models import VendorInput
from app.scoring import evaluate
from tests.conftest import (
    AIGOV_APPROVER,
    LEGAL_APPROVER,
    SECOPS_APPROVER,
    TEST_APPROVER_DOMAIN,
    WEBHOOK_SECRET,
    fetch_assessment,
    jira_issue_updated,
    post_jira_webhook,
    signed_webhook,
)


def _evaluate_personal() -> object:
    return evaluate(
        VendorInput(
            vendor_name="OpenAI",
            service_description="API",
            intended_use="Copilot",
            data_processed="Prompts with customer names",
            has_dpa=False,
        )
    )


def test_gmail_approver_rejected():
    with pytest.raises(ValueError, match=TEST_APPROVER_DOMAIN):
        apply_approval(
            _evaluate_personal(),
            jira_issue_updated(email="j.perez@gmail.com", labels=["legal-review"]),
        )


def test_unauthorized_corporate_mailbox_rejected():
    with pytest.raises(ValueError, match="not authorized"):
        apply_approval(
            _evaluate_personal(),
            jira_issue_updated(email=f"j.perez@{TEST_APPROVER_DOMAIN}", labels=["legal-review"]),
        )


def test_three_gates_close_workflow(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assert assess.json()["assessment_metadata"]["decision"] != "APPROVE"
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]

    denied = post_jira_webhook(
        client,
        jira_issue_updated(
            email="j.perez@gmail.com",
            labels=["legal-review"],
            assessment_id=assessment_id,
            changelog_id="chg-gmail",
        ),
        "evt-gmail",
    )
    assert denied.status_code == 422

    raw, headers = signed_webhook(
        jira_issue_updated(email=LEGAL_APPROVER, labels=["legal-review"], assessment_id=assessment_id),
        "evt-bad",
        secret="wrong",
    )
    bad_secret = client.post("/api/v1/webhooks/jira", content=raw, headers=headers)
    assert bad_secret.status_code == 401

    gates = [
        (LEGAL_APPROVER, ["legal-review"], "AIGOV-11", "evt-legal"),
        (SECOPS_APPROVER, ["infosec"], "AIGOV-12", "evt-sec"),
        (AIGOV_APPROVER, ["ai-governance-review"], "AIGOV-13", "evt-gov"),
    ]
    last = None
    for email, labels, key, event_id in gates:
        last = post_jira_webhook(
            client,
            jira_issue_updated(
                key=key,
                email=email,
                labels=labels,
                assessment_id=assessment_id,
                changelog_id=event_id,
            ),
            event_id,
        )
        assert last.status_code == 200, last.text
    body = last.json()
    assert body["workflow_status"] == "DEPARTMENT_GATES_COMPLETED"
    assert body["human_decision"] is None
    assert body["legal_approver"] == LEGAL_APPROVER
    events = store.list_audit_events(assessment_id)
    assert [event["event_type"] for event in events] == [
        "assessment.created",
        "workflow.updated",
        "workflow.updated",
        "workflow.updated",
    ]
    assert store.verify_audit_chain(assessment_id)["valid"] is True

    replay = post_jira_webhook(
        client,
        jira_issue_updated(
            key="AIGOV-11",
            email=LEGAL_APPROVER,
            labels=["legal-review"],
            assessment_id=assessment_id,
            changelog_id="evt-legal",
        ),
        "evt-legal",
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert replay.json().get("legal_approver") is None


def test_webhook_without_assessment_is_404(client):
    response = post_jira_webhook(
        client,
        jira_issue_updated(email=LEGAL_APPROVER, labels=["legal-review"]),
        "evt-none",
    )
    assert response.status_code == 404


def test_webhook_hmac_invalid(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(email=LEGAL_APPROVER, assessment_id=assessment_id)
    raw, headers = signed_webhook(body, "evt-hmac-bad")
    headers["X-Hub-Signature-256"] = "sha256=" + ("ab" * 32)
    response = client.post("/api/v1/webhooks/jira", content=raw, headers=headers)
    assert response.status_code == 401


def test_webhook_hmac_required(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(email=LEGAL_APPROVER, assessment_id=assessment_id)
    raw = json.dumps(body).encode("utf-8")
    response = client.post(
        "/api/v1/webhooks/jira",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Jira-Timestamp": str(time.time()),
            "X-Jira-Event-Id": "evt-no-hmac",
        },
    )
    assert response.status_code == 401


def test_webhook_shared_secret_without_hmac_rejected(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(email=LEGAL_APPROVER, assessment_id=assessment_id)
    raw = json.dumps(body).encode("utf-8")
    response = client.post(
        "/api/v1/webhooks/jira",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Jira-Secret": WEBHOOK_SECRET,
            "X-Jira-Timestamp": str(time.time()),
            "X-Jira-Event-Id": "evt-secret-only",
        },
    )
    assert response.status_code == 401
    latest = fetch_assessment(client, assessment_id).json()
    assert latest["assessment"]["decision_record"]["legal_approver"] is None


def test_webhook_signed_hmac_accepted(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(email=LEGAL_APPROVER, assessment_id=assessment_id, changelog_id="chg-signed")
    raw, headers = signed_webhook(body, "evt-signed")
    headers.pop("X-Jira-Secret", None)
    response = client.post("/api/v1/webhooks/jira", content=raw, headers=headers)
    assert response.status_code == 200
    assert response.json()["legal_approver"] == LEGAL_APPROVER
    assert response.json()["duplicate"] is False


def test_webhook_stale_timestamp_rejected(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(email=LEGAL_APPROVER, assessment_id=assessment_id)
    raw, headers = signed_webhook(body, "evt-stale")
    headers["X-Jira-Timestamp"] = str(time.time() - 301)
    from app.security import hmac_hex

    headers["X-Hub-Signature-256"] = f"sha256={hmac_hex(raw, WEBHOOK_SECRET)}"
    response = client.post("/api/v1/webhooks/jira", content=raw, headers=headers)
    assert response.status_code == 401
    assert "timestamp" in response.json()["detail"].lower()


def test_webhook_replay_does_not_double_count(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(
        email=LEGAL_APPROVER,
        assessment_id=assessment_id,
        changelog_id="chg-replay",
    )
    first = post_jira_webhook(client, body, "evt-replay-legal")
    second = post_jira_webhook(client, body, "evt-replay-legal")
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    latest = fetch_assessment(client, assessment_id).json()
    record = latest["assessment"]["decision_record"]
    assert record["legal_approver"] == LEGAL_APPROVER
    assert record["secops_approver"] is None
    assert record["workflow_status"] == "HUMAN_REVIEW_REQUIRED"


def test_webhook_unknown_assessment_id(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    body = jira_issue_updated(email=LEGAL_APPROVER, assessment_id=str(uuid.uuid4()))
    response = post_jira_webhook(client, body, "evt-unknown-aid")
    assert response.status_code == 404


def test_webhook_unknown_id_does_not_consume_event(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    unknown = jira_issue_updated(email=LEGAL_APPROVER, assessment_id=str(uuid.uuid4()))
    missed = post_jira_webhook(client, unknown, "evt-retry-after-404")
    assert missed.status_code == 404
    valid = jira_issue_updated(email=LEGAL_APPROVER, assessment_id=assessment_id)
    retry = post_jira_webhook(client, valid, "evt-retry-after-404")
    assert retry.status_code == 200
    assert retry.json()["duplicate"] is False
    latest = fetch_assessment(client, assessment_id).json()
    assert latest["assessment"]["decision_record"]["legal_approver"] == LEGAL_APPROVER


def test_webhook_422_does_not_consume_event_id(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    denied = post_jira_webhook(
        client,
        jira_issue_updated(
            email="j.perez@gmail.com",
            assessment_id=assessment_id,
            changelog_id="chg-retry-422",
        ),
        "evt-retry-after-422",
    )
    assert denied.status_code == 422
    retry = post_jira_webhook(
        client,
        jira_issue_updated(
            email=LEGAL_APPROVER,
            assessment_id=assessment_id,
            changelog_id="chg-retry-422-ok",
        ),
        "evt-retry-after-422",
    )
    assert retry.status_code == 200
    assert retry.json()["duplicate"] is False
    latest = fetch_assessment(client, assessment_id).json()
    assert latest["assessment"]["decision_record"]["legal_approver"] == LEGAL_APPROVER


def test_webhook_malformed_assessment_id_rejected(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(email=LEGAL_APPROVER, assessment_id="not-a-uuid")
    response = post_jira_webhook(client, body, "evt-bad-aid")
    assert response.status_code == 400
    assert "UUID" in response.json()["detail"]
    latest = fetch_assessment(client, assessment_id).json()
    assert latest["assessment"]["decision_record"]["legal_approver"] is None


def test_webhook_wrong_project_rejected(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(
        key="OTHER-1",
        project="OTHER",
        email=LEGAL_APPROVER,
        assessment_id=assessment_id,
    )
    response = post_jira_webhook(client, body, "evt-other-proj")
    assert response.status_code == 422
    assert "project" in response.json()["detail"].lower()


def test_webhook_issue_not_on_assessment(client, sample_intake):
    from app import store

    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    assessment = store.get_assessment(assessment_id)
    for ticket, key in zip(assessment.jira_tickets, ["AIGOV-1", "AIGOV-2", "AIGOV-3", "AIGOV-4"]):
        ticket.fields.issue_key = key
    store.save_assessment(assessment)

    body = jira_issue_updated(
        key="AIGOV-99",
        email=LEGAL_APPROVER,
        assessment_id=assessment_id,
        labels=["legal-review"],
    )
    response = post_jira_webhook(client, body, "evt-foreign-issue")
    assert response.status_code == 422
    assert "not part of this assessment" in response.json()["detail"]


def test_webhook_done_to_done_rejected(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(
        email=LEGAL_APPROVER,
        assessment_id=assessment_id,
        previous="Done",
        status="Done",
    )
    response = post_jira_webhook(client, body, "evt-done-done")
    assert response.status_code == 422
    assert "open status" in response.json()["detail"].lower() or "Done" in response.json()["detail"]


def test_webhook_missing_department_label(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(
        email=LEGAL_APPROVER,
        assessment_id=assessment_id,
        labels=["vendor-risk"],
    )
    response = post_jira_webhook(client, body, "evt-no-dept")
    assert response.status_code == 422


def test_webhook_two_department_labels_rejected(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(
        email=LEGAL_APPROVER,
        assessment_id=assessment_id,
        labels=["legal-review", "infosec"],
    )
    response = post_jira_webhook(client, body, "evt-two-labels")
    assert response.status_code == 422
    assert "single department" in response.json()["detail"]


def test_webhook_actor_email_beats_spoofed_approver(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = jira_issue_updated(
        email=f"attacker@{TEST_APPROVER_DOMAIN}",
        assessment_id=assessment_id,
        labels=["legal-review"],
    )
    body["approver_email"] = LEGAL_APPROVER
    response = post_jira_webhook(client, body, "evt-spoof-email")
    assert response.status_code == 422
    assert "not authorized" in response.json()["detail"]


def test_webhook_malformed_json(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    raw = b"{not json"
    from app.security import hmac_hex

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={hmac_hex(raw, WEBHOOK_SECRET)}",
        "X-Jira-Timestamp": str(time.time()),
        "X-Jira-Event-Id": "evt-bad-json",
    }
    response = client.post("/api/v1/webhooks/jira", content=raw, headers=headers)
    assert response.status_code == 400


def test_legal_gate_twice_same_approver_is_idempotent():
    assessment = _evaluate_personal()
    body = jira_issue_updated(email=LEGAL_APPROVER, labels=["legal-review"], changelog_id="a")
    first = apply_approval(assessment, body)
    second = apply_approval(first, body)
    assert second.decision_record.legal_approver == LEGAL_APPROVER
    assert second.decision_record.workflow_status == "HUMAN_REVIEW_REQUIRED"


def test_legal_gate_twice_different_approver_rejected():
    assessment = _evaluate_personal()
    assessment = apply_approval(assessment, jira_issue_updated(email=LEGAL_APPROVER, labels=["legal-review"]))
    assessment.decision_record.legal_approver = "previous-dpo@example.com"
    with pytest.raises(ValueError, match="already closed"):
        apply_approval(
            assessment,
            jira_issue_updated(email=LEGAL_APPROVER, labels=["legal-review"], changelog_id="chg-3"),
        )


def test_webhook_assignee_without_actor_rejected(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = {
        "webhookEvent": "jira:issue_updated",
        "assessment_id": assessment_id,
        "changelog": {
            "id": "chg-assignee-only",
            "items": [{"field": "status", "fromString": "To Do", "toString": "Done"}],
        },
        "issue": {
            "key": "AIGOV-10",
            "fields": {
                "status": {"name": "Done"},
                "labels": ["legal-review"],
                "issuetype": {"name": "Task"},
                "project": {"key": "AIGOV"},
                "assignee": {"emailAddress": LEGAL_APPROVER},
            },
        },
    }
    response = post_jira_webhook(client, body, "evt-assignee-only")
    assert response.status_code == 422
    assert "assignee is not an approver" in response.json()["detail"]
    latest = fetch_assessment(client, assessment_id).json()
    assert latest["assessment"]["decision_record"]["legal_approver"] is None


def test_webhook_missing_previous_status(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = {
        "status": "Done",
        "approver_email": LEGAL_APPROVER,
        "labels": ["legal-review"],
        "assessment_id": assessment_id,
        "issue_type": "Task",
    }
    response = post_jira_webhook(client, body, "evt-no-prev")
    assert response.status_code == 422


def test_webhook_resolves_assessment_by_jira_issue_key(client, sample_intake):
    from app import store

    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    store.bind_jira_issue("AIGOV-77", assessment_id)
    body = jira_issue_updated(
        key="AIGOV-77",
        email=LEGAL_APPROVER,
        labels=["legal-review"],
        assessment_id=None,
        changelog_id="chg-by-key",
    )
    body.pop("assessment_id", None)
    body["issue"]["fields"]["description"] = "No id in description"
    response = post_jira_webhook(client, body, "evt-by-key")
    assert response.status_code == 200
    assert response.json()["assessment_id"] == assessment_id
    assert response.json()["legal_approver"] == LEGAL_APPROVER


def test_webhook_never_falls_back_to_only_assessment(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    body = jira_issue_updated(
        key="AIGOV-UNMAPPED",
        email=LEGAL_APPROVER,
        assessment_id=None,
        changelog_id="chg-no-global-fallback",
    )
    body.pop("assessment_id", None)
    body["issue"]["fields"]["description"] = "No id in description"
    response = post_jira_webhook(client, body, "evt-no-global-fallback")
    assert response.status_code == 400
    assert "assessment_id is required" in response.json()["detail"]


def test_webhook_rejects_issue_mapping_assessment_mismatch(client, sample_intake):
    from app import store

    first = client.post("/api/v1/assess-vendor", json=sample_intake)
    second_payload = dict(sample_intake, vendor_name="OtherCo")
    second = client.post("/api/v1/assess-vendor", json=second_payload)
    first_id = first.json()["assessment_metadata"]["assessment_id"]
    second_id = second.json()["assessment_metadata"]["assessment_id"]
    store.bind_jira_issue("AIGOV-88", first_id)
    body = jira_issue_updated(
        key="AIGOV-88",
        email=LEGAL_APPROVER,
        assessment_id=second_id,
        changelog_id="chg-map-conflict",
    )
    response = post_jira_webhook(client, body, "evt-map-conflict")
    assert response.status_code == 409
    assert "another assessment" in response.json()["detail"]
