"""
External root-hash anchors for the SQLite audit chain.

The local hash chain remains the in-process evidence. These sinks copy the
current head hash to a destination the SQLite file does not control:

- jira: dry-run payload or live Epic/issue comment (org system of record)
- rekor: Sigstore Rekor transparency log (when explicitly enabled)
- s3: object PUT, with Object Lock headers when configured

Jira dry-run and the local `audit_anchors` table are not WORM. A Jira admin
can still edit tickets. Rekor (public log) and S3 Object Lock Compliance are
the stronger external attesters when those sinks are actually used.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx

from app.config import get_settings
from app.logging_config import get_logger
from app.timeutil import utc_now

logger = get_logger("app.audit_anchor")

REKOR_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
S3_TIMEOUT = httpx.Timeout(15.0, connect=3.0)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _http_client(timeout: httpx.Timeout) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def parse_rekor_response(data: dict) -> dict:
    """Extract uuid / logIndex / integratedTime from a Rekor create-entry body."""
    if not data:
        raise ValueError("empty Rekor response")
    uuid = ""
    entry: dict[str, Any] = data
    if "uuid" in data and ("logIndex" in data or "body" in data):
        uuid = str(data.get("uuid") or "")
        entry = data
    else:
        uuid = str(next(iter(data.keys())))
        nested = data[uuid]
        if isinstance(nested, dict):
            entry = nested
    if not uuid:
        raise ValueError("Rekor response missing uuid")
    verification = entry.get("verification") or {}
    return {
        "uuid": uuid,
        "log_index": entry.get("logIndex"),
        "integrated_time": entry.get("integratedTime"),
        "log_id": entry.get("logID"),
        "signed_entry_timestamp": verification.get("signedEntryTimestamp"),
    }


def build_rekor_entry(root_hash: str) -> dict:
    """
    hashedrekord-shaped Rekor entry for the chain head.

    Ephemeral ECDSA P-256 keys are used when the cryptography package is
    installed. Public Rekor rejects unsigned bodies; tests mock HTTP.
    """
    spec: dict[str, Any] = {
        "data": {"hash": {"algorithm": "sha256", "value": root_hash}},
    }
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        signature = key.sign(bytes.fromhex(root_hash), ec.ECDSA(hashes.SHA256()))
        pub_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        spec["signature"] = {
            "content": base64.b64encode(signature).decode("ascii"),
            "publicKey": {"content": base64.b64encode(pub_pem).decode("ascii")},
        }
    except ImportError:
        spec["signature"] = {
            "content": base64.b64encode(bytes.fromhex(root_hash)).decode("ascii"),
            "publicKey": {
                "content": base64.b64encode(
                    b"enterprise-ai-risk-audit-anchor (install cryptography for live Rekor)"
                ).decode("ascii")
            },
        }
    return {
        "apiVersion": "0.0.1",
        "kind": "hashedrekord",
        "spec": spec,
    }


def submit_rekor(
    root_hash: str,
    *,
    url: str,
    client: Optional[httpx.Client] = None,
) -> dict:
    """POST the root hash to Rekor. Caller must not invoke this in tests without a mock."""
    payload = build_rekor_entry(root_hash)
    close = False
    if client is None:
        client = _http_client(REKOR_TIMEOUT)
        close = True
    try:
        response = client.post(
            f"{url.rstrip('/')}/api/v1/log/entries",
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        parsed = parse_rekor_response(response.json())
        parsed["destination"] = url.rstrip("/")
        parsed["entry"] = payload
        return parsed
    finally:
        if close:
            client.close()


def _sign_s3_headers(
    method: str,
    url: str,
    body: bytes,
    extra_headers: dict[str, str],
    access_key: str,
    secret_key: str,
    region: str,
) -> dict[str, str]:
    parsed = urlparse(url)
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "host": parsed.netloc,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    for key, value in extra_headers.items():
        headers[key.lower()] = value
    signed_header_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_uri = quote(parsed.path or "/", safe="/-_.~")
    canonical_query = parsed.query
    canonical_request = (
        f"{method}\n{canonical_uri}\n{canonical_query}\n"
        f"{canonical_headers}\n{signed_header_names}\n{payload_hash}"
    )
    credential_scope = f"{datestamp}/{region}/s3/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    def keyed(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = keyed(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )
    return headers


def build_s3_object_url(bucket: str, key: str, endpoint_url: str, region: str) -> str:
    object_key = key.lstrip("/")
    if endpoint_url:
        base = endpoint_url.rstrip("/")
        return f"{base}/{bucket}/{object_key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{object_key}"


def object_lock_headers(mode: str, retain_days: int) -> dict[str, str]:
    if not mode:
        return {}
    until = utc_now() + timedelta(days=max(1, int(retain_days or 1)))
    retain_until = until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "x-amz-object-lock-mode": mode,
        "x-amz-object-lock-retain-until-date": retain_until,
        "Content-MD5": "",  # filled after body is known
    }


def submit_s3(
    body: bytes,
    *,
    bucket: str,
    key: str,
    region: str,
    access_key: str,
    secret_key: str,
    endpoint_url: str = "",
    object_lock_mode: str = "",
    object_lock_retain_days: int = 365,
    client: Optional[httpx.Client] = None,
) -> dict:
    url = build_s3_object_url(bucket, key, endpoint_url, region)
    extra = {
        "content-type": "application/json",
    }
    lock = object_lock_headers(object_lock_mode, object_lock_retain_days)
    lock.pop("Content-MD5", None)
    if object_lock_mode:
        extra["content-md5"] = base64.b64encode(
            hashlib.md5(body, usedforsecurity=False).digest()
        ).decode("ascii")
        extra["x-amz-object-lock-mode"] = lock["x-amz-object-lock-mode"]
        extra["x-amz-object-lock-retain-until-date"] = lock["x-amz-object-lock-retain-until-date"]
    headers = _sign_s3_headers(
        "PUT",
        url,
        body,
        extra,
        access_key,
        secret_key,
        region,
    )
    close = False
    if client is None:
        client = _http_client(S3_TIMEOUT)
        close = True
    try:
        response = client.put(url, content=body, headers=headers)
        response.raise_for_status()
        return {
            "destination": url,
            "etag": response.headers.get("etag") or response.headers.get("ETag"),
            "status_code": response.status_code,
            "object_lock_mode": object_lock_mode or None,
        }
    finally:
        if close:
            client.close()


def _jira_issue_key(assessment_id: str) -> Optional[str]:
    from app import store

    keys = store.list_jira_issue_keys(assessment_id)
    return keys[0] if keys else None


def run_jira_sink(
    assessment_id: str,
    root_hash: str,
    seq: int,
    event_id: int,
    issue_key: Optional[str] = None,
) -> dict:
    from app.jira_workflow import publish_audit_anchor

    key = issue_key or _jira_issue_key(assessment_id)
    result = publish_audit_anchor(assessment_id, root_hash, seq, event_id, issue_key=key)
    dry_run = bool(result.get("dry_run"))
    if result.get("reason") == "no_issue_key":
        return {
            "status": "pending",
            "external_ref": None,
            "verification": result,
        }
    if dry_run:
        return {
            "status": "anchored",
            "external_ref": f"jira:dry-run:seq={seq}",
            "verification": result,
        }
    if result.get("published"):
        comment_id = result.get("comment_id") or ""
        ref = f"jira:{key}"
        if comment_id:
            ref = f"{ref}#comment-{comment_id}"
        return {
            "status": "anchored",
            "external_ref": ref,
            "verification": result,
        }
    return {
        "status": "failed",
        "external_ref": None,
        "verification": result,
        "error": result.get("reason") or "jira_anchor_failed",
    }


def run_rekor_sink(root_hash: str) -> dict:
    settings = get_settings()
    url = settings.effective_rekor_url()
    if not url:
        return {
            "status": "failed",
            "external_ref": None,
            "verification": {"destination": None},
            "error": "rekor_not_configured",
        }
    parsed = submit_rekor(root_hash, url=url)
    uuid = parsed.get("uuid") or ""
    return {
        "status": "anchored",
        "external_ref": f"rekor:{uuid}" if uuid else f"rekor:{url}",
        "verification": parsed,
    }


def run_s3_sink(
    assessment_id: str,
    root_hash: str,
    seq: int,
    event_id: int,
    prev_root: str,
    created_at: str,
) -> dict:
    settings = get_settings()
    bucket = settings.audit_anchor_s3_bucket
    if not bucket:
        return {
            "status": "failed",
            "external_ref": None,
            "verification": {"destination": None},
            "error": "s3_not_configured",
        }
    if not settings.aws_access_key_id or not settings.aws_secret_access_key:
        return {
            "status": "failed",
            "external_ref": None,
            "verification": {"bucket": bucket},
            "error": "s3_credentials_missing",
        }
    prefix = settings.audit_anchor_s3_prefix.rstrip("/")
    key = f"{prefix}/{assessment_id}/{seq}-{root_hash}.json"
    body = _canonical_json(
        {
            "assessment_id": assessment_id,
            "seq": seq,
            "event_id": event_id,
            "root_hash": root_hash,
            "prev_root": prev_root,
            "created_at": created_at,
            "sink": "s3",
        }
    ).encode("utf-8")
    put = submit_s3(
        body,
        bucket=bucket,
        key=key,
        region=settings.aws_region or "us-east-1",
        access_key=settings.aws_access_key_id,
        secret_key=settings.aws_secret_access_key,
        endpoint_url=settings.aws_endpoint_url,
        object_lock_mode=settings.audit_anchor_s3_object_lock_mode,
        object_lock_retain_days=settings.audit_anchor_s3_object_lock_retain_days,
    )
    return {
        "status": "anchored",
        "external_ref": put["destination"],
        "verification": put,
    }


def dispatch_pending_anchors(assessment_id: str, issue_key: Optional[str] = None) -> None:
    """Call configured sinks for pending rows. Never raises into the assessment save path."""
    from app import store

    rows = store.list_pending_anchors(assessment_id)
    for row in rows:
        sink = row["sink"]
        try:
            if sink == "jira":
                result = run_jira_sink(
                    assessment_id,
                    row["root_hash"],
                    row["seq"],
                    row["event_id"],
                    issue_key=issue_key,
                )
            elif sink == "rekor":
                result = run_rekor_sink(row["root_hash"])
            elif sink == "s3":
                result = run_s3_sink(
                    assessment_id,
                    row["root_hash"],
                    row["seq"],
                    row["event_id"],
                    row["prev_root"],
                    row["created_at"],
                )
            else:
                result = {
                    "status": "failed",
                    "external_ref": None,
                    "verification": {},
                    "error": f"unknown_sink:{sink}",
                }
        except Exception as exc:
            logger.warning(
                "audit anchor sink failed",
                extra={
                    "event": "audit.anchor.failed",
                    "assessment_id": assessment_id,
                    "sink": sink,
                    "seq": row["seq"],
                    "error": str(exc),
                },
            )
            result = {
                "status": "failed",
                "external_ref": None,
                "verification": {"error": str(exc)},
                "error": str(exc),
            }
        store.update_anchor_result(
            row["id"],
            status=result["status"],
            external_ref=result.get("external_ref"),
            verification=result.get("verification") or {},
        )
        logger.info(
            "audit anchor updated",
            extra={
                "event": "audit.anchor",
                "assessment_id": assessment_id,
                "sink": sink,
                "seq": row["seq"],
                "status": result["status"],
            },
        )


def sinks_for_pending_rows() -> list[str]:
    """
    Sinks that get a pending row on each chain append.

    Rekor/S3 rows are only inserted when those sinks are actually enabled so
    tests stay offline. Jira always records a row when selected (dry-run ok).
    """
    settings = get_settings()
    sinks = settings.parsed_audit_anchor_sinks()
    pending: list[str] = []
    for sink in sinks:
        if sink == "jira":
            pending.append(sink)
        elif sink == "rekor" and settings.rekor_sink_enabled():
            pending.append(sink)
        elif sink == "s3" and settings.s3_sink_enabled():
            pending.append(sink)
    return pending
