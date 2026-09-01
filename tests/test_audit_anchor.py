"""External audit-root anchors: Jira payload, mocked Rekor/S3, verification."""

from pathlib import Path
from unittest.mock import patch

import httpx

from app import store
from app.audit_anchor import (
    build_s3_object_url,
    parse_rekor_response,
    submit_rekor,
    submit_s3,
)
from app.jira_workflow import (
    AUDIT_ROOT_HASH_PREFIX,
    build_epic_and_subtasks,
    parse_webhook_event,
    publish_audit_anchor,
    stamp_root_hash,
)
from app.models import VendorInput
from app.scoring import evaluate


def _intake(name: str = "AnchorCo") -> VendorInput:
    return VendorInput(
        vendor_name=name,
        service_description="API",
        intended_use="test",
        data_processed="none",
        has_dpa=False,
    )


def test_rekor_must_not_be_called_by_default(monkeypatch):
    store.clear()

    def boom(*_args, **_kwargs):
        raise AssertionError("Rekor must not be called unless enabled")

    monkeypatch.setattr("app.audit_anchor.submit_rekor", boom)
    monkeypatch.setattr("app.audit_anchor.submit_s3", boom)
    payload = _intake()
    assessment = evaluate(payload)
    store.save(payload, assessment)
    aid = assessment.assessment_metadata.assessment_id
    verification = store.verify_audit_chain(aid)
    assert verification["valid"] is True
    assert verification["anchors"]["configured_sinks"] == ["jira"]
    assert verification["anchors"]["missing_external_anchor"] is False
    anchors = store.list_audit_anchors(aid)
    assert len(anchors) == 1
    assert anchors[0]["sink"] == "jira"
    assert anchors[0]["status"] == "anchored"
    assert anchors[0]["verification"]["dry_run"] is True
    assert AUDIT_ROOT_HASH_PREFIX in anchors[0]["verification"]["payload"]["description"]
    assert anchors[0]["root_hash"] == verification["head_hash"]


def test_missing_external_anchor_is_visible():
    store.clear()
    payload = _intake("MissingAnchor")
    assessment = evaluate(payload)
    aid = assessment.assessment_metadata.assessment_id
    store.save(payload, assessment)
    assert store.verify_audit_chain(aid)["anchors"]["missing_external_anchor"] is False
    store.delete_audit_anchors(aid)
    verification = store.verify_audit_chain(aid)
    assert verification["valid"] is True
    assert verification["anchors"]["missing_external_anchor"] is True
    assert verification["anchors"]["last_successful"] is None


def test_jira_dry_run_payload_contains_root_hash():
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
        assessment_id="11111111-1111-1111-1111-111111111111",
    )
    root = "a" * 64
    stamp_root_hash(tickets, root, seq=1)
    assert f"{AUDIT_ROOT_HASH_PREFIX} {root}" in tickets[0].fields.description
    result = publish_audit_anchor(
        "11111111-1111-1111-1111-111111111111",
        root,
        1,
        1,
    )
    assert result["dry_run"] is True
    assert root in result["payload"]["description"]
    assert "Assessment-Root-Hash:" in result["payload"]["description"]


def test_parse_webhook_extracts_root_hash():
    parsed = parse_webhook_event(
        {
            "issue": {
                "key": "AIGOV-1",
                "fields": {
                    "description": (
                        "Assessment-ID: abc-def\n"
                        "Assessment-Root-Hash: deadbeef\n"
                        "Audit-Root-Hash: deadbeef\n"
                        "Audit-Seq: 3\n"
                    )
                },
            }
        }
    )
    assert parsed.assessment_id == "abc-def"
    assert parsed.root_hash == "deadbeef"
    assert parsed.audit_seq == 3


def test_parse_rekor_response_uuid_map():
    parsed = parse_rekor_response(
        {
            "entry-uuid-1": {
                "logIndex": 9,
                "integratedTime": 1700000000,
                "logID": "log-id",
                "verification": {"signedEntryTimestamp": "c2V0"},
            }
        }
    )
    assert parsed["uuid"] == "entry-uuid-1"
    assert parsed["log_index"] == 9
    assert parsed["integrated_time"] == 1700000000


def test_rekor_client_is_mocked(tmp_path, monkeypatch):
    path = tmp_path / "rekor.sqlite"
    monkeypatch.setenv("DATA_STORE", str(path))
    monkeypatch.setenv("AUDIT_ANCHOR_SINKS", "rekor")
    monkeypatch.setenv("REKOR_URL", "https://rekor.example.test")
    store.close_store()

    class FakeClient:
        def __init__(self):
            self.urls = []

        def post(self, url, json=None, headers=None):
            self.urls.append(url)
            assert json["spec"]["data"]["hash"]["algorithm"] == "sha256"
            assert len(json["spec"]["data"]["hash"]["value"]) == 64

            class Resp:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "rekor-uuid": {
                            "logIndex": 42,
                            "integratedTime": 1700000000,
                            "logID": "log-id",
                            "verification": {"signedEntryTimestamp": "c2V0"},
                        }
                    }

            return Resp()

        def close(self):
            return None

    fake = FakeClient()
    monkeypatch.setattr("app.audit_anchor._http_client", lambda timeout: fake)
    try:
        payload = _intake("RekorCo")
        assessment = evaluate(payload)
        aid = assessment.assessment_metadata.assessment_id
        store.save(payload, assessment)
        anchors = store.list_audit_anchors(aid)
        assert len(anchors) == 1
        assert anchors[0]["sink"] == "rekor"
        assert anchors[0]["status"] == "anchored"
        assert anchors[0]["external_ref"] == "rekor:rekor-uuid"
        assert anchors[0]["verification"]["log_index"] == 42
        assert fake.urls == ["https://rekor.example.test/api/v1/log/entries"]
        assert store.verify_audit_chain(aid)["anchors"]["head_matches_last_anchor"] is True
    finally:
        store.close_store()
        monkeypatch.setenv(
            "DATA_STORE",
            str(Path(__file__).resolve().parent / "logs" / "app.sqlite"),
        )
        monkeypatch.setenv("AUDIT_ANCHOR_SINKS", "jira")
        monkeypatch.delenv("REKOR_URL", raising=False)
        store.init_store()


def test_submit_rekor_parses_http_body():
    class FakeClient:
        def post(self, url, json=None, headers=None):
            class Resp:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {
                        "uuid-x": {
                            "logIndex": 1,
                            "integratedTime": 2,
                            "logID": "abc",
                        }
                    }

            return Resp()

        def close(self):
            return None

    parsed = submit_rekor("ab" * 32, url="https://rekor.example.test", client=FakeClient())
    assert parsed["uuid"] == "uuid-x"
    assert parsed["log_index"] == 1


def test_s3_object_lock_headers_are_sent():
    captured = {}

    class FakeClient:
        def put(self, url, content=None, headers=None):
            captured["url"] = url
            captured["headers"] = {k.lower(): v for k, v in (headers or {}).items()}
            captured["body"] = content

            class Resp:
                status_code = 200
                headers = {"etag": '"abc"'}

                def raise_for_status(self):
                    return None

            return Resp()

        def close(self):
            return None

    result = submit_s3(
        b'{"root_hash":"abc"}',
        bucket="gov-audit",
        key="audit-anchors/id/1-abc.json",
        region="us-east-1",
        access_key="AKIAFAKE",
        secret_key="secret",
        object_lock_mode="COMPLIANCE",
        object_lock_retain_days=30,
        client=FakeClient(),
    )
    assert result["object_lock_mode"] == "COMPLIANCE"
    assert captured["headers"]["x-amz-object-lock-mode"] == "COMPLIANCE"
    assert "x-amz-object-lock-retain-until-date" in captured["headers"]
    assert "content-md5" in captured["headers"]
    assert captured["url"].startswith("https://gov-audit.s3.us-east-1.amazonaws.com/")


def test_s3_sink_mocked_via_store(tmp_path, monkeypatch):
    path = tmp_path / "s3.sqlite"
    monkeypatch.setenv("DATA_STORE", str(path))
    monkeypatch.setenv("AUDIT_ANCHOR_SINKS", "s3")
    monkeypatch.setenv("AUDIT_ANCHOR_S3_BUCKET", "gov-audit")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AUDIT_ANCHOR_S3_OBJECT_LOCK_MODE", "GOVERNANCE")
    store.close_store()

    def fake_submit(body, **kwargs):
        assert kwargs["bucket"] == "gov-audit"
        assert kwargs["object_lock_mode"] == "GOVERNANCE"
        return {
            "destination": "https://gov-audit.s3.us-east-1.amazonaws.com/obj",
            "etag": '"abc"',
            "status_code": 200,
            "object_lock_mode": "GOVERNANCE",
        }

    monkeypatch.setattr("app.audit_anchor.submit_s3", fake_submit)
    try:
        payload = _intake("S3Co")
        assessment = evaluate(payload)
        aid = assessment.assessment_metadata.assessment_id
        store.save(payload, assessment)
        anchors = store.list_audit_anchors(aid)
        assert anchors[0]["sink"] == "s3"
        assert anchors[0]["status"] == "anchored"
        assert "gov-audit" in anchors[0]["external_ref"]
    finally:
        store.close_store()
        monkeypatch.setenv(
            "DATA_STORE",
            str(Path(__file__).resolve().parent / "logs" / "app.sqlite"),
        )
        monkeypatch.setenv("AUDIT_ANCHOR_SINKS", "jira")
        monkeypatch.delenv("AUDIT_ANCHOR_S3_BUCKET", raising=False)
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.delenv("AUDIT_ANCHOR_S3_OBJECT_LOCK_MODE", raising=False)
        store.init_store()


def test_s3_virtual_hosted_url():
    assert build_s3_object_url("b", "k.json", "", "eu-west-1") == (
        "https://b.s3.eu-west-1.amazonaws.com/k.json"
    )
    assert build_s3_object_url("b", "k.json", "http://127.0.0.1:9000", "us-east-1") == (
        "http://127.0.0.1:9000/b/k.json"
    )


def test_jira_live_anchor_comment_is_posted(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("JIRA_USER_EMAIL", "bot@example.com")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(201, json={"id": "10001"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    with patch("app.jira_workflow.httpx.Client", fake_client):
        result = publish_audit_anchor("aid", "ab" * 32, 1, 1, issue_key="AIGOV-1")
    assert result["published"] is True
    assert result["comment_id"] == "10001"
    assert "/issue/AIGOV-1/comment" in captured["url"]
    assert "Audit-Root-Hash" in captured["body"]


def test_inbound_webhook_records_observed_hash(client, sample_intake):
    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    head = store.verify_audit_chain(assessment_id)["head_hash"]
    from tests.conftest import LEGAL_APPROVER, jira_issue_updated, post_jira_webhook

    body = jira_issue_updated(
        email=LEGAL_APPROVER,
        labels=["legal-review"],
        assessment_id=assessment_id,
        key="AIGOV-11",
    )
    body["issue"]["fields"]["description"] = (
        f"Assessment-ID: {assessment_id}\n"
        f"Assessment-Root-Hash: {head}\n"
        f"Audit-Root-Hash: {head}\n"
    )
    response = post_jira_webhook(client, body, "evt-anchor-hash")
    assert response.status_code == 200, response.text
    assert response.json()["observed_root_hash"] == head
    anchors = store.list_audit_anchors(assessment_id)
    jira_rows = [row for row in anchors if row["sink"] == "jira"]
    assert jira_rows[-1]["verification"]["observed_from_webhook"]["root_hash"] == head
