"""Table-driven secret redaction: JSON, query strings, headers, false positives."""

import pytest

from app.security import key_is_sensitive, redact_mapping, redact_secrets


@pytest.mark.parametrize(
    ("raw", "must_hide", "must_keep"),
    [
        ("password: hunter2", ["hunter2"], ["password"]),
        ('api_key="quoted secret"', ["quoted secret"], ["api_key"]),
        ("secret: 'value with spaces'", ["value with spaces"], ["secret"]),
        (
            '{"api_key": "json secret", "vendor": "OpenAI"}',
            ["json secret"],
            ["vendor", "OpenAI"],
        ),
        (
            '{"token": "abc", "nested": true}',
            ["abc"],
            ["nested"],
        ),
        (
            "https://api.example/v1?api_key=supersecret&q=vendor",
            ["supersecret"],
            ["q=vendor"],
        ),
        (
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9\nX-Request-Id: abc",
            ["eyJhbGciOiJIUzI1NiJ9"],
            ["X-Request-Id", "abc"],
        ),
        (
            "bearer sk-abcdefghijklmnopqrstuvwxyz",
            ["sk-abcdefghijklmnopqrstuvwxyz"],
            ["bearer"],
        ),
        (
            "client_secret=line1",
            ["line1"],
            ["client_secret"],
        ),
    ],
)
def test_redact_secrets_hides_common_formats(raw, must_hide, must_keep):
    redacted = redact_secrets(raw)
    assert "[REDACTED]" in redacted
    for secret in must_hide:
        assert secret not in redacted
    for kept in must_keep:
        assert kept in redacted


@pytest.mark.parametrize(
    "raw",
    [
        "token_count is 3",
        "the password policy requires 12 characters",
        '{"status": "ok", "vendor": "Tokenize Inc"}',
        "https://example.com/docs?q=password-policy",
        "assessment_id=11111111-1111-1111-1111-111111111111",
    ],
)
def test_redact_secrets_false_positives(raw):
    assert redact_secrets(raw) == raw


@pytest.mark.parametrize(
    ("key", "sensitive"),
    [
        ("dpa_document_id", True),
        ("dpia_reference", True),
        ("api_key", True),
        ("Authorization", True),
        ("session_token", True),
        ("client_secret", True),
        ("token_count", False),
        ("vendor_name", False),
        ("status", False),
        ("workflow_status", False),
    ],
)
def test_key_is_sensitive(key, sensitive):
    assert key_is_sensitive(key) is sensitive


def test_redact_mapping_field_based_and_nested():
    payload = {
        "dpa_document_id": "DPA-SECRET-99",
        "vendor_name": "OpenAI",
        "token_count": 3,
        "nested": {"access_token": "abc def", "label": "legal-review"},
        "note": "api_key=supersecret",
        "headers": ["Authorization: Bearer xyz", "Accept: application/json"],
    }
    dumped = redact_mapping(payload)
    assert dumped["dpa_document_id"] == "[REDACTED]"
    assert dumped["vendor_name"] == "OpenAI"
    assert dumped["token_count"] == 3
    assert dumped["nested"]["access_token"] == "[REDACTED]"
    assert dumped["nested"]["label"] == "legal-review"
    assert "supersecret" not in dumped["note"]
    assert "xyz" not in dumped["headers"][0]
    assert dumped["headers"][1] == "Accept: application/json"
