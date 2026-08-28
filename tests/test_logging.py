"""Verify structured logging on critical paths."""

import json
import logging

from app.logging_config import JsonFormatter, configure_logging, get_logger


def test_json_formatter_includes_extra_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="enterprise_ai_risk.test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.event = "test.event"
    record.vendor = "Acme"
    line = formatter.format(record)
    payload = json.loads(line)
    assert payload["msg"] == "hello"
    assert payload["event"] == "test.event"
    assert payload["vendor"] == "Acme"


def test_configure_logging_json_mode(tmp_path):
    log_file = tmp_path / "app.log"
    configure_logging(level="DEBUG", fmt="json", log_file=str(log_file), force=True)
    logger = get_logger("app.test")
    logger.info("json line", extra={"event": "test.json"})
    text = log_file.read_text(encoding="utf-8").strip()
    payload = json.loads(text)
    assert payload["event"] == "test.json"


def test_log_file_uses_rotating_handler(tmp_path):
    from logging.handlers import RotatingFileHandler

    from app.logging_config import _ROOT_LOGGER

    log_file = tmp_path / "rotate.log"
    try:
        configure_logging(level="INFO", fmt="text", log_file=str(log_file), force=True)
        root = logging.getLogger(_ROOT_LOGGER)
        assert any(isinstance(handler, RotatingFileHandler) for handler in root.handlers)
    finally:
        configure_logging(force=True)


def test_assess_emits_scoring_and_store_logs(client, sample_intake, caplog_app):
    response = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert response.status_code == 200
    events = []
    for record in caplog_app.records:
        if hasattr(record, "event"):
            events.append(record.event)
    assert "scoring.evaluate" in events
    assert "store.save" in events
    assert "api.assess.done" in events


def test_webhook_logs_approval(client, sample_intake, caplog_app):
    from tests.conftest import signed_webhook

    assess = client.post("/api/v1/assess-vendor", json=sample_intake)
    assert assess.status_code == 200
    assessment_id = assess.json()["assessment_metadata"]["assessment_id"]
    body = {
        "status": "Done",
        "previous_status": "To Do",
        "approver_email": "dpo@adevinta.com",
        "labels": ["legal-review"],
        "assessment_id": assessment_id,
        "issue_type": "Task",
    }
    raw, headers = signed_webhook(body, "evt-log-legal")
    client.post("/api/v1/webhooks/jira", content=raw, headers=headers)
    events = [getattr(r, "event", None) for r in caplog_app.records]
    assert "jira.approval" in events
    assert "store.save_assessment" in events
