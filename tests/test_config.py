"""Startup configuration must fail fast, not mid-request."""

import pytest

from app.config import ConfigurationError, Settings, get_settings, validate_startup


def test_validate_startup_ok():
    result = validate_startup()
    assert result["approver_domain"] == "adevinta.com"
    assert result["webhook_event_store"] == "sqlite"
    assert result["settings"].jira_project_key == "AIGOV"
    assert result["settings"].api_rate_limit_per_minute == 60


def test_settings_exposes_defaults(monkeypatch):
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("API_RATE_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.delenv("SESSION_TTL_SECONDS", raising=False)
    settings = get_settings()
    assert settings.jira_project_key == "AIGOV"
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.api_rate_limit_per_minute == 60
    assert settings.session_ttl_seconds == 24 * 3600
    assert isinstance(settings, Settings)


def test_validate_startup_requires_domain(monkeypatch):
    monkeypatch.delenv("JIRA_APPROVER_DOMAIN", raising=False)
    with pytest.raises(ConfigurationError, match="JIRA_APPROVER_DOMAIN"):
        validate_startup()


def test_validate_startup_requires_webhook_secret(monkeypatch):
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "")
    with pytest.raises(ConfigurationError, match="JIRA_WEBHOOK_SECRET"):
        validate_startup()


def test_invalid_trusted_proxies_fail_settings(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "not-a-cidr")
    with pytest.raises(ConfigurationError, match="TRUSTED_PROXIES"):
        get_settings()
