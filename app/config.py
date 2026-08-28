"""Process configuration: one frozen Settings snapshot, validated at startup."""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.logging_config import get_logger

logger = get_logger("app.config")


class ConfigurationError(Exception):
    """Invalid or missing process configuration. Raised at startup, not mid-request."""


class Settings(BaseSettings):
    """
    Immutable process settings. Environment names are the uppercase field names
    (JIRA_APPROVER_DOMAIN, WEBHOOK_EVENT_STORE, …).

    Logging (LOG_LEVEL / LOG_FORMAT / LOG_FILE*) is applied in logging_config
    before this object is built, so a bad JIRA_* value can still be logged.
    """

    model_config = SettingsConfigDict(
        env_file=None if os.getenv("PYTEST_VERSION") else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        frozen=True,
    )

    jira_approver_domain: str
    jira_webhook_secret: str
    jira_project_key: str = "AIGOV"
    jira_gate_issue_type: str = "Task"
    jira_allowed_approvers: str = ""
    jira_assignee_account_ids: str = ""
    jira_base_url: str = ""
    jira_api_token: str = ""
    jira_user_email: str = ""
    jira_epic_link_field: str = ""
    jira_webhook_skew_seconds: int = 300

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    require_assessment_auth: bool = True
    webhook_event_store: str = ":memory:"
    webhook_event_ttl_seconds: int = 86400
    session_ttl_seconds: int = Field(default=24 * 3600)
    max_sessions: int = 200

    api_rate_limit_per_minute: int = 60
    trusted_proxies: str = ""
    health_details_token: str = ""

    @field_validator("jira_approver_domain")
    @classmethod
    def _approver_domain(cls, value: str) -> str:
        domain = value.strip().lower()
        if not domain or "@" in domain or "." not in domain:
            raise ValueError("JIRA_APPROVER_DOMAIN must be a valid email domain")
        return domain

    @field_validator("jira_webhook_secret")
    @classmethod
    def _webhook_secret(cls, value: str) -> str:
        secret = value.strip()
        if not secret:
            raise ValueError("JIRA_WEBHOOK_SECRET is required")
        return secret

    @field_validator(
        "jira_project_key",
        "jira_gate_issue_type",
        "jira_base_url",
        "jira_api_token",
        "jira_user_email",
        "jira_epic_link_field",
        "jira_allowed_approvers",
        "jira_assignee_account_ids",
        "openai_api_key",
        "openai_model",
        "webhook_event_store",
        "health_details_token",
        "trusted_proxies",
    )
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    @field_validator("jira_project_key")
    @classmethod
    def _project_key(cls, value: str) -> str:
        return value or "AIGOV"

    @field_validator("jira_gate_issue_type")
    @classmethod
    def _gate_type(cls, value: str) -> str:
        return value or "Task"

    @field_validator("webhook_event_store")
    @classmethod
    def _event_store_path(cls, value: str) -> str:
        return value or ":memory:"

    @field_validator("trusted_proxies")
    @classmethod
    def _trusted_proxies(cls, value: str) -> str:
        from app.netutil import parse_trusted_proxy_networks

        parse_trusted_proxy_networks(value)
        return value

    @property
    def jira_configured(self) -> bool:
        return bool(self.jira_base_url and self.jira_api_token)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def jira_bot_email(self) -> str:
        return self.jira_user_email or f"jira-bot@{self.jira_approver_domain}"

    @property
    def webhook_event_store_is_memory(self) -> bool:
        return self.webhook_event_store == ":memory:"

    def trusted_proxy_networks(self):
        from app.netutil import parse_trusted_proxy_networks

        return parse_trusted_proxy_networks(self.trusted_proxies)

    def parsed_assignee_account_ids(self) -> dict[str, str]:
        raw = self.jira_assignee_account_ids
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k).lower(): str(v) for k, v in data.items()}
        except json.JSONDecodeError:
            logger.warning("invalid JIRA_ASSIGNEE_ACCOUNT_IDS json", extra={"event": "jira.config"})
        return {}

    def parsed_allowed_approvers(self) -> dict[str, set[str]] | None:
        raw = self.jira_allowed_approvers
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k).lower(): {str(e).lower() for e in v} for k, v in data.items()}
        except json.JSONDecodeError:
            logger.warning("invalid JIRA_ALLOWED_APPROVERS json", extra={"event": "jira.config"})
        return None


def _to_configuration_error(exc: ValidationError) -> ConfigurationError:
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        if loc == "jira_approver_domain":
            return ConfigurationError("JIRA_APPROVER_DOMAIN must be a valid email domain")
        if loc == "jira_webhook_secret":
            return ConfigurationError("JIRA_WEBHOOK_SECRET is required")
        if loc == "trusted_proxies":
            return ConfigurationError(err.get("msg", "TRUSTED_PROXIES is invalid"))
    msg = exc.errors()[0].get("msg", "invalid configuration") if exc.errors() else "invalid configuration"
    return ConfigurationError(msg)


def get_settings() -> Settings:
    """Build a frozen Settings snapshot from the current environment."""
    try:
        return Settings()
    except ValidationError as exc:
        raise _to_configuration_error(exc) from exc


def validate_startup() -> dict[str, Any]:
    """
    Fail fast if required env is missing or malformed.

    Call from the FastAPI lifespan so uvicorn exits instead of serving a
    half-configured process.
    """
    from app import store

    settings = get_settings()
    event_mode = store.init_event_store()
    if event_mode == "memory":
        logger.warning(
            "WEBHOOK_EVENT_STORE is unset; webhook replay protection is process-local",
            extra={"event": "config.webhook_events.memory"},
        )
    logger.info(
        "configuration validated",
        extra={
            "event": "config.validated",
            "approver_domain": settings.jira_approver_domain,
            "webhook_event_store": event_mode,
        },
    )
    return {
        "approver_domain": settings.jira_approver_domain,
        "webhook_event_store": event_mode,
        "settings": settings,
    }
