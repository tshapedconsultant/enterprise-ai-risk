"""
Simulate Jira inbound webhooks against a local console. No Jira Cloud, no internet.

Usage (server already running on :8000):

  python scripts/simulate_jira_webhook.py

The script POSTs /api/v1/assess-vendor, reads assessment_id from the response, and
includes it in every webhook payload (and X-Assessment-Id on gate closures).

Environment:
  CONSOLE_URL           — default http://127.0.0.1:8000
  JIRA_WEBHOOK_SECRET   — must match the server (default test-webhook-secret)
  ASSESSMENT_ID         — optional; skip assess and target an existing assessment
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("CONSOLE_URL", "http://127.0.0.1:8000")
SECRET = os.getenv("JIRA_WEBHOOK_SECRET", "test-webhook-secret")
DOMAIN = os.getenv("JIRA_APPROVER_DOMAIN", "example.com").lstrip("@")


def post(
    path: str,
    body: dict,
    secret: str | None = SECRET,
    assessment_id: str | None = None,
    event_id: str | None = None,
) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        digest = hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
        headers["X-Jira-Timestamp"] = str(time.time())
        headers["X-Jira-Event-Id"] = event_id or f"sim-{time.time_ns()}"
    if assessment_id:
        headers["X-Assessment-Id"] = assessment_id
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"detail": payload}
        return exc.code, parsed


def webhook_body(
    email: str,
    labels: list[str],
    assessment_id: str,
    status: str = "Done",
) -> dict:
    return {
        "status": status,
        "previous_status": "To Do",
        "approver_email": email,
        "labels": labels,
        "assessment_id": assessment_id,
        "issue_type": "Task",
    }


def main() -> int:
    assessment_id = os.getenv("ASSESSMENT_ID", "").strip()

    if not assessment_id:
        print("1) Assess vendor (creates session + Epic/department Tasks payload)")
        status, assessment = post(
            "/api/v1/assess-vendor",
            {
                "vendor_name": "OpenAI",
                "service_description": "API",
                "intended_use": "Copilot",
                "data_processed": "Prompts with customer names",
                "has_dpa": False,
            },
            secret=None,
        )
        meta = assessment.get("assessment_metadata", {})
        print(status, meta.get("decision"))
        assessment_id = meta.get("assessment_id") or ""
        if not assessment_id:
            print("No assessment_id in response — is the server running the latest API?")
            return 1
        print(f"   assessment_id={assessment_id}")
    else:
        print(f"1) Using ASSESSMENT_ID from environment: {assessment_id}")

    print("2) Spoof with Gmail — expect 422")
    print(
        post(
            "/api/v1/webhooks/jira",
            webhook_body("j.perez@gmail.com", ["legal-review"], assessment_id),
            event_id="sim-gmail",
        )
    )

    print("3) Wrong secret — expect 401 (JIRA_WEBHOOK_SECRET is required on the server)")
    print(
        post(
            "/api/v1/webhooks/jira",
            webhook_body(f"dpo@{DOMAIN}", ["legal-review"], assessment_id),
            secret="wrong",
            event_id="sim-wrong",
        )
    )

    print("4) Close three authorized department mailboxes")
    for email, label, event_id in (
        (f"dpo@{DOMAIN}", "legal-review", "sim-legal"),
        (f"secops@{DOMAIN}", "infosec", "sim-sec"),
        (f"aigov@{DOMAIN}", "ai-governance-review", "sim-gov"),
    ):
        code, body = post(
            "/api/v1/webhooks/jira",
            webhook_body(email, [label], assessment_id),
            assessment_id=assessment_id,
            event_id=event_id,
        )
        print(
            code,
            body.get("assessment_id"),
            body.get("workflow_status"),
            body.get("human_decision"),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
