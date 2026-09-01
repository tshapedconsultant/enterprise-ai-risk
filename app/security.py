"""HMAC, replay protection, rate limits, request IDs, and LLM redaction."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional

from fastapi import HTTPException, Request

from app.config import ConfigurationError, get_settings
from app.logging_config import get_logger
from app.netutil import ip_in_networks
from app.timeutil import utc_now

logger = get_logger("app.security")

RATE_LIMIT_WINDOW = 60.0

# Value-based heuristics (chat strings, URLs, JSON blobs). Keys are handled in redact_mapping.
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:api[_-]?key|secret|password|token|authorization|access_token|'
    r'refresh_token|client_secret|credential|credentials|private_key)"\s*:\s*)'
    r'"(?:\\.|[^"\\])*"'
)
_QUERY_SECRET_RE = re.compile(
    r'(?i)((?:api[_-]?key|secret|password|token|access_token|refresh_token|'
    r'client_secret|authorization)=)([^&\s#]+)'
)
_HEADER_SECRET_RE = re.compile(
    r"(?im)^(authorization\s*[:=]\s*)(.+)$"
)
_KV_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|password|token|bearer)\s*[:=]\s*"
    r"(\"[^\"]*\"|'[^']*'|[^\s&]+)"
)
_OPENAI_KEY_RE = re.compile(r"sk-[a-zA-Z0-9]{10,}")

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "access_token",
        "refresh_token",
        "client_secret",
        "dpa_document_id",
        "dpia_reference",
        "private_key",
        "credential",
        "credentials",
        "webhook_secret",
        "jira_api_token",
        "openai_api_key",
        "x-api-key",
        "x_api_key",
    }
)
_SENSITIVE_SUFFIXES = (
    "_password",
    "_secret",
    "_token",
    "_api_key",
    "_credential",
    "_credentials",
)

_lock = threading.Lock()
_rate_hits: Dict[str, Deque[float]] = defaultdict(deque)


def new_request_id() -> str:
    return str(uuid.uuid4())


def webhook_secret() -> str:
    try:
        return get_settings().jira_webhook_secret
    except ConfigurationError:
        return ""


def validate_assessment_id(value: Optional[str]) -> Optional[str]:
    """Return a UUID string or None if empty. Reject malformed IDs before store lookup."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        uuid.UUID(text)
    except ValueError:
        raise HTTPException(status_code=400, detail="assessment_id must be a UUID") from None
    return text


def hmac_hex(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def parse_signature_header(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = value.strip()
    if raw.lower().startswith("sha256="):
        return raw.split("=", 1)[1].strip()
    return raw


def verify_webhook_hmac(body: bytes, signature_header: Optional[str], secret: str) -> None:
    expected = hmac_hex(body, secret)
    provided = parse_signature_header(signature_header)
    if not provided or not hmac.compare_digest(provided, expected):
        logger.warning("jira webhook hmac mismatch", extra={"event": "api.webhook.hmac_fail"})
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


def parse_timestamp(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        from datetime import datetime

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def verify_timestamp(raw: Optional[str], skew: Optional[int] = None) -> None:
    ts = parse_timestamp(raw)
    if ts is None:
        raise HTTPException(status_code=401, detail="Missing or invalid webhook timestamp")
    now = utc_now().timestamp()
    allowed = get_settings().jira_webhook_skew_seconds if skew is None else skew
    if abs(now - ts) > allowed:
        raise HTTPException(status_code=401, detail="Webhook timestamp outside allowed window")


def event_id_from(
    header_id: Optional[str],
    nonce: Optional[str],
    body: dict,
    issue_key: str,
) -> str:
    if header_id:
        return header_id.strip()
    if nonce:
        return f"nonce:{nonce.strip()}"
    changelog = body.get("changelog") or {}
    changelog_id = str(changelog.get("id") or "")
    webhook_event = str(body.get("webhookEvent") or "")
    if changelog_id or webhook_event or issue_key:
        material = f"{webhook_event}|{issue_key}|{changelog_id}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
    raise HTTPException(status_code=400, detail="Webhook event_id or nonce is required")


def _expire_rate_hits(now: float) -> None:
    empty: List[str] = []
    for key, hits in _rate_hits.items():
        while hits and now - hits[0] > RATE_LIMIT_WINDOW:
            hits.popleft()
        if not hits:
            empty.append(key)
    for key in empty:
        del _rate_hits[key]


def client_ip(request: Request) -> str:
    """
    Peer address, unless the peer is in TRUSTED_PROXIES.

    X-Forwarded-For / X-Real-IP are ignored unless the connecting IP is a
    configured proxy. The client is the rightmost untrusted hop (or the
    leftmost hop if the whole chain is trusted).
    """
    peer = request.client.host if request.client else ""
    networks = get_settings().trusted_proxy_networks()
    if not networks or not peer or not ip_in_networks(peer, networks):
        return peer or "unknown"

    forwarded = (request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or "").strip()
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if not hops:
        return peer
    for hop in reversed(hops):
        if not ip_in_networks(hop, networks):
            return hop
    return hops[0]


def enforce_rate_limit(request: Request, bucket: str = "api") -> None:
    ip = client_ip(request)
    key = f"{bucket}:{ip}"
    now = time.monotonic()
    with _lock:
        _expire_rate_hits(now)
        hits = _rate_hits[key]
        if len(hits) >= get_settings().api_rate_limit_per_minute:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        hits.append(now)


def _normalize_key(key: str) -> str:
    return key.lower().replace("-", "_")


def key_is_sensitive(key: str) -> bool:
    """True for known secret field names (exact or suffix)."""
    normalized = _normalize_key(key)
    if normalized in {_normalize_key(k) for k in _SENSITIVE_KEYS}:
        return True
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def redact_secrets(text: str) -> str:
    if not text:
        return text
    redacted = _JSON_SECRET_RE.sub(r'\1"[REDACTED]"', text)
    redacted = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _HEADER_SECRET_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _KV_SECRET_RE.sub(r"\1=[REDACTED]", redacted)
    return _OPENAI_KEY_RE.sub("[REDACTED]", redacted)


def redact_mapping(payload: dict) -> dict:
    out = {}
    for key, value in payload.items():
        if key_is_sensitive(str(key)):
            out[key] = "[REDACTED]" if value not in (None, "") else value
        elif isinstance(value, str):
            out[key] = redact_secrets(value)
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        elif isinstance(value, list):
            out[key] = [
                redact_mapping(v)
                if isinstance(v, dict)
                else redact_secrets(v)
                if isinstance(v, str)
                else v
                for v in value
            ]
        else:
            out[key] = value
    return out


def public_error_detail(request_id: str) -> str:
    return f"Request failed. Check server logs using request_id={request_id}"


def _header_bearer(request: Request) -> Optional[str]:
    raw = (request.headers.get("Authorization") or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return None


def tokens_match(provided: Optional[str], expected: str) -> bool:
    """Constant-time compare; False when either side is missing."""
    if not expected or not provided:
        return False
    if len(provided) != len(expected):
        secrets.compare_digest(expected, expected)
        return False
    return secrets.compare_digest(provided, expected)


def require_api_token(request: Request) -> None:
    """
    Gate mutating console APIs when API_ACCESS_TOKEN is set.

    Unset token = open assess-vendor (local demo). Set it for any networked deploy.
    """
    expected = get_settings().api_access_token
    if not expected:
        return
    provided = request.headers.get("X-API-Token") or _header_bearer(request)
    if not tokens_match(provided, expected):
        logger.warning("api token rejected", extra={"event": "api.auth.denied"})
        raise HTTPException(status_code=401, detail="API token required")
