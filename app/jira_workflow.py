"""
Jira orchestration for decentralized human approval.

Workflow overview:
  1. OUTBOUND — After engine triage, build an Epic plus one department Task per gate.
     Sub-tasks are not used: in Jira they cannot be children of an Epic.
     Optionally POST to Jira Cloud REST when JIRA_BASE_URL and JIRA_API_TOKEN are set.
  2. INBOUND — Jira webhooks hit POST /api/v1/webhooks/jira when a human closes a gate Task.
  3. CLOSURE — When required Legal, SecOps, and AI Governance gates are closed by
     authorized humans, workflow_status becomes DEPARTMENT_GATES_COMPLETED.
     That is not a business approval. HUMAN_APPROVED_WITH_CONDITIONS is reserved
     for a later final decision-maker.

The scoring engine never issues a final APPROVE. Humans work in Jira.
The transitioning user's email (webhook `user`) is the only approver identity;
the issue assignee is never treated as the actor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from app.config import get_settings
from app.enums import Department, GateStatus, WorkflowStatus
from app.logging_config import get_logger
from app.models import (
    AssessmentResponse,
    JiraFields,
    JiraIssueType,
    JiraPriority,
    JiraProject,
    JiraTicket,
)
from app.timeutil import utc_now_iso

logger = get_logger("app.jira_workflow")

DONE_STATUSES = {"done", "cerrado", "closed", "approved", "aprobado", "resolved"}
ASSESSMENT_ID_PREFIX = "Assessment-ID:"
ASSESSMENT_ROOT_HASH_PREFIX = "Assessment-Root-Hash:"
AUDIT_ROOT_HASH_PREFIX = "Audit-Root-Hash:"
AUDIT_SEQ_PREFIX = "Audit-Seq:"
JIRA_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
REQUIRED_DEPARTMENTS = [Department.LEGAL.value, Department.INFOSEC.value, Department.AIGOV.value]
_HASH_LINE_PREFIXES = (
    ASSESSMENT_ROOT_HASH_PREFIX,
    AUDIT_ROOT_HASH_PREFIX,
    AUDIT_SEQ_PREFIX,
)


@dataclass
class ParsedWebhook:
    key: str
    status: str
    email: str
    labels: List[str]
    assessment_id: Optional[str]
    previous_status: str = ""
    issue_type: str = ""
    project_key: str = ""
    actor_email: str = ""
    root_hash: str = ""
    audit_seq: Optional[int] = None


def _assessment_id_footer(assessment_id: Optional[str]) -> str:
    if not assessment_id:
        return ""
    return f"\n{ASSESSMENT_ID_PREFIX} {assessment_id}"


def _prefixed_line(text: str, prefix: str) -> Optional[str]:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped.split(prefix, 1)[1].strip()
    return None


def strip_root_hash_lines(text: str) -> str:
    """Remove previously stamped root-hash lines so re-stamping stays idempotent."""
    kept = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if any(stripped.startswith(prefix) for prefix in _HASH_LINE_PREFIXES):
            continue
        kept.append(line)
    return "\n".join(kept).rstrip()


def root_hash_footer(root_hash: str, seq: int) -> str:
    return (
        f"\n{ASSESSMENT_ROOT_HASH_PREFIX} {root_hash}"
        f"\n{AUDIT_ROOT_HASH_PREFIX} {root_hash}"
        f"\n{AUDIT_SEQ_PREFIX} {seq}"
    )


def stamp_root_hash(tickets: List[JiraTicket], root_hash: str, seq: int) -> None:
    """Append Assessment-Root-Hash / Audit-Root-Hash to each ticket description."""
    if not root_hash:
        return
    block = root_hash_footer(root_hash, seq)
    for ticket in tickets:
        description = strip_root_hash_lines(ticket.fields.description or "")
        ticket.fields.description = description + block


def build_audit_anchor_payload(
    assessment_id: str,
    root_hash: str,
    seq: int,
    event_id: int,
) -> Dict[str, Any]:
    """Outbound Jira comment/ticket body that copies the chain root into Jira."""
    description = (
        "External audit chain anchor. This is not a department gate.\n"
        "Rewriting the local SQLite ledger without this Jira copy will diverge.\n"
        f"{ASSESSMENT_ID_PREFIX} {assessment_id}"
        f"{root_hash_footer(root_hash, seq)}\n"
        f"Audit-Event-ID: {event_id}"
    )
    return {
        "summary": f"[Audit-Root-Hash] {assessment_id} seq {seq}",
        "description": description,
        "labels": ["ai-governance", "audit-anchor"],
    }


def publish_audit_anchor(
    assessment_id: str,
    root_hash: str,
    seq: int,
    event_id: int,
    issue_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Copy the current audit root hash into Jira (live comment) or a dry-run payload.

    Dry-run still embeds the hash so demos can show the external-attester shape
    without credentials. That is not WORM; a Jira admin can still edit the issue.
    """
    payload = build_audit_anchor_payload(assessment_id, root_hash, seq, event_id)
    if not jira_configured():
        logger.info(
            "jira audit anchor dry-run",
            extra={
                "event": "jira.anchor",
                "published": False,
                "dry_run": True,
                "assessment_id": assessment_id,
                "seq": seq,
            },
        )
        return {
            "published": False,
            "dry_run": True,
            "reason": "Jira credentials not configured (dry-run payload only)",
            "destination": "jira",
            "issue_key": issue_key,
            "payload": payload,
        }
    if not issue_key:
        return {
            "published": False,
            "dry_run": False,
            "reason": "no_issue_key",
            "destination": "jira",
            "issue_key": None,
            "payload": payload,
        }

    settings = get_settings()
    base = settings.jira_base_url.rstrip("/")
    auth = (settings.jira_bot_email, settings.jira_api_token)
    with httpx.Client(timeout=JIRA_TIMEOUT) as client:
        response = client.post(
            f"{base}/rest/api/3/issue/{issue_key}/comment",
            json={"body": _adf(payload["description"])},
            auth=auth,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        comment_id = str((response.json() or {}).get("id") or "")
    logger.info(
        "jira audit anchor comment posted",
        extra={
            "event": "jira.anchor",
            "published": True,
            "assessment_id": assessment_id,
            "issue_key": issue_key,
            "seq": seq,
        },
    )
    return {
        "published": True,
        "dry_run": False,
        "reason": None,
        "destination": "jira",
        "issue_key": issue_key,
        "comment_id": comment_id,
        "payload": payload,
    }


def _assignee_account_ids_from_env() -> Dict[str, str]:
    return get_settings().parsed_assignee_account_ids()


def approver_domain() -> str:
    """Corporate email domain for human approvers (env: JIRA_APPROVER_DOMAIN). Required."""
    return get_settings().jira_approver_domain


def jira_project_key() -> str:
    return get_settings().jira_project_key


def gate_issue_type() -> str:
    """Issue type for department gates. Must be a standard type that can sit under an Epic (not Sub-task)."""
    return get_settings().jira_gate_issue_type


def departments() -> Dict[str, Dict[str, str]]:
    """Department queue metadata: Jira label, team name, default assignee, DecisionRecord field."""
    domain = approver_domain()
    child_type = gate_issue_type()
    return {
        "legal": {
            "label": "legal-review",
            "team": "DPO / Legal",
            "assignee_email": f"dpo@{domain}",
            "approver_field": "legal_approver",
            "issue_type": child_type,
        },
        "infosec": {
            "label": "infosec",
            "team": "SecOps",
            "assignee_email": f"secops@{domain}",
            "approver_field": "secops_approver",
            "issue_type": child_type,
        },
        "aigov": {
            "label": "ai-governance-review",
            "team": "AI Governance",
            "assignee_email": f"aigov@{domain}",
            "approver_field": "aigov_approver",
            "issue_type": child_type,
        },
    }


def allowed_approvers() -> Dict[str, Set[str]]:
    """Explicit mailbox allow-list per department. Domain suffix alone is not enough."""
    domain = approver_domain()
    defaults = {
        "legal": {f"dpo@{domain}".lower()},
        "infosec": {f"secops@{domain}".lower()},
        "aigov": {f"aigov@{domain}".lower()},
    }
    override = get_settings().parsed_allowed_approvers()
    if override is None:
        return defaults
    return override


def approver_domain_ok(email: str) -> bool:
    """Reject webhook approvals from non-corporate mail domains."""
    return email.lower().endswith("@" + approver_domain().lower())


def approver_authorized(email: str, department: str) -> bool:
    allow = allowed_approvers().get(department, set())
    return email.lower() in allow


def _ticket(
    summary: str,
    description: str,
    priority: str,
    labels: List[str],
    issue_type: str,
    assignee_email: Optional[str] = None,
    parent_key: Optional[str] = None,
    department: Optional[str] = None,
) -> JiraTicket:
    """Build one JiraTicket; department is stripped before real Jira POST."""
    fields = JiraFields(
        project=JiraProject(key=jira_project_key()),
        summary=summary,
        description=description,
        issuetype=JiraIssueType(name=issue_type),
        priority=JiraPriority(name=priority),
        labels=labels,
        assignee={"emailAddress": assignee_email} if assignee_email else None,
        parent={"key": parent_key} if parent_key else None,
        department=department,
    )
    return JiraTicket(fields=fields)


def build_epic_and_subtasks(
    vendor: str,
    triage_decision: str,
    residual: str,
    need_legal: bool,
    need_infosec: bool,
    need_aigov: bool,
    legal_reason: str,
    infosec_reason: str,
    aigov_reason: str,
    assessment_id: Optional[str] = None,
) -> List[JiraTicket]:
    """
    Parent Epic plus one Task per department that must review.

    Jira Sub-tasks cannot be children of an Epic. Gates are standard Tasks with
    parent = Epic key (team-managed / modern company-managed). Classic sites that
    still use Epic Link can set JIRA_EPIC_LINK_FIELD=customfield_XXXXX.

    parent.key is a placeholder until outbound create returns the real Epic key.
    """
    parent_key = f"{jira_project_key()}-PARENT"
    id_footer = _assessment_id_footer(assessment_id)
    tickets = [
        _ticket(
            summary=f"[Triage] {vendor} — decentralized review",
            description=(
                f"Parent governance ticket for {vendor}.\n"
                f"Engine triage: {triage_decision} (residual risk {residual}).\n"
                "This ticket is NOT an approval. Workflow becomes DEPARTMENT_GATES_COMPLETED when "
                "Legal, SecOps, and AI Governance close their department Tasks. That still awaits final approval.\n"
                "Orchestration: FastAPI console → Jira REST (outbound) → inbound webhook."
                f"{id_footer}"
            ),
            priority="High",
            labels=["ai-governance", "vendor-risk", "triage-parent"],
            issue_type="Epic",
            department="parent",
        )
    ]

    if need_legal:
        tickets.append(
            _ticket(
                summary=f"Review DPA and approve DPIA for {vendor}",
                description=(
                    f"Assigned to DPO team ({departments()['legal']['assignee_email']}).\n"
                    f"{legal_reason}\n"
                    "When this task is closed, Jira should notify POST /api/v1/webhooks/jira."
                    f"{id_footer}"
                ),
                priority="Highest",
                labels=["ai-governance", "vendor-risk", "legal-review"],
                issue_type=gate_issue_type(),
                assignee_email=departments()["legal"]["assignee_email"],
                parent_key=parent_key,
                department="legal",
            )
        )
    if need_infosec:
        tickets.append(
            _ticket(
                summary=f"Validate SOC 2 Type II report for {vendor}",
                description=(
                    f"Assigned to SecOps ({departments()['infosec']['assignee_email']}).\n"
                    f"{infosec_reason}\n"
                    "When this task is closed, Jira should notify POST /api/v1/webhooks/jira."
                    f"{id_footer}"
                ),
                priority="High",
                labels=["ai-governance", "vendor-risk", "infosec"],
                issue_type=gate_issue_type(),
                assignee_email=departments()["infosec"]["assignee_email"],
                parent_key=parent_key,
                department="infosec",
            )
        )
    if need_aigov:
        tickets.append(
            _ticket(
                summary=f"Review bias and explainability risk for {vendor} model",
                description=(
                    f"Assigned to AI Governance ({departments()['aigov']['assignee_email']}).\n"
                    f"{aigov_reason}\n"
                    "When this task is closed, Jira should notify POST /api/v1/webhooks/jira."
                    f"{id_footer}"
                ),
                priority="High",
                labels=["ai-governance", "vendor-risk", "ai-governance-review"],
                issue_type=gate_issue_type(),
                assignee_email=departments()["aigov"]["assignee_email"],
                parent_key=parent_key,
                department="aigov",
            )
        )
    return tickets


def jira_configured() -> bool:
    """True when outbound Jira POST is possible."""
    return get_settings().jira_configured


def _adf(text: str) -> Dict[str, Any]:
    """Jira Cloud v3 expects Atlassian Document Format, not a plain string."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text or " "}],
            }
        ],
    }


def _resolve_account_id(email: str, client: httpx.Client, auth: tuple[str, str], base: str) -> Optional[str]:
    """Map corporate email to Jira Cloud accountId. Exact email match only."""
    if not email:
        return None
    mapped = _assignee_account_ids_from_env().get(email.lower())
    if mapped:
        return mapped
    response = client.get(
        f"{base}/rest/api/3/user/search",
        params={"query": email, "maxResults": 5},
        auth=auth,
        headers={"Accept": "application/json"},
    )
    if response.status_code != 200:
        logger.warning(
            "jira user search failed",
            extra={"event": "jira.assignee", "email": email, "status": response.status_code},
        )
        return None
    users = response.json()
    email_lower = email.lower()
    for user in users:
        if (user.get("emailAddress") or "").lower() == email_lower:
            return user.get("accountId")
    logger.warning(
        "jira user search no exact email match",
        extra={"event": "jira.assignee", "email": email, "hits": len(users) if isinstance(users, list) else 0},
    )
    return None


def _jira_assignee_fields(
    assignee: Optional[Dict[str, str]],
    client: httpx.Client,
    auth: tuple[str, str],
    base: str,
) -> Optional[Dict[str, str]]:
    if not assignee:
        return None
    email = assignee.get("emailAddress")
    if not email:
        return assignee
    account_id = _resolve_account_id(email, client, auth, base)
    if account_id:
        return {"accountId": account_id}
    logger.warning(
        "jira assignee omitted — accountId not resolved",
        extra={"event": "jira.assignee", "email": email},
    )
    return None


def _delete_jira_issue(client: httpx.Client, base: str, auth: tuple[str, str], key: str) -> None:
    response = client.delete(
        f"{base}/rest/api/3/issue/{key}",
        auth=auth,
        headers={"Accept": "application/json"},
    )
    if response.status_code not in (200, 204):
        logger.warning(
            "jira rollback delete failed",
            extra={"event": "jira.rollback", "issue_key": key, "status": response.status_code},
        )


def publish_to_jira(tickets: List[JiraTicket]) -> Dict[str, Any]:
    """
    Outbound: create Epic then department Tasks in order. No-op unless credentials are set.

    Returns {"published": False, "reason": ...} for dry-run demos.
    """
    if not jira_configured():
        logger.info(
            "jira outbound skipped",
            extra={"event": "jira.publish", "published": False, "reason": "no_credentials"},
        )
        return {"published": False, "reason": "Jira credentials not configured (dry-run payload only)"}

    settings = get_settings()
    base = settings.jira_base_url.rstrip("/")
    email = settings.jira_bot_email
    token = settings.jira_api_token
    auth = (email, token)
    created: List[str] = []
    parent_key = None

    with httpx.Client(timeout=JIRA_TIMEOUT) as client:
        try:
            for ticket in tickets:
                payload = {"fields": ticket.fields.model_dump(exclude_none=True)}
                payload["fields"].pop("department", None)
                payload["fields"].pop("issue_key", None)
                payload["fields"]["description"] = _adf(str(payload["fields"].get("description") or ""))
                assignee = payload["fields"].get("assignee")
                if assignee:
                    payload["fields"]["assignee"] = _jira_assignee_fields(assignee, client, auth, base)
                    if payload["fields"]["assignee"] is None:
                        payload["fields"].pop("assignee", None)
                if payload["fields"].get("issuetype", {}).get("name") == "Epic":
                    payload["fields"].pop("parent", None)
                elif parent_key:
                    epic_link_field = settings.jira_epic_link_field
                    if epic_link_field:
                        payload["fields"].pop("parent", None)
                        payload["fields"][epic_link_field] = parent_key
                    else:
                        # Standard Task under Epic (not a Sub-task).
                        payload["fields"]["parent"] = {"key": parent_key}
                response = client.post(
                    f"{base}/rest/api/3/issue",
                    json=payload,
                    auth=auth,
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
                response.raise_for_status()
                key = response.json().get("key")
                created.append(key)
                ticket.fields.issue_key = key
                if ticket.fields.issuetype.name == "Epic":
                    parent_key = key
                    ticket.fields.parent = None
                else:
                    ticket.fields.parent = {"key": parent_key}
        except Exception as exc:
            for key in reversed(created):
                try:
                    _delete_jira_issue(client, base, auth, key)
                except Exception as rollback_exc:
                    logger.warning(
                        "jira rollback error",
                        extra={"event": "jira.rollback", "issue_key": key, "error": str(rollback_exc)},
                    )
            logger.error(
                "jira publish aborted — rolled back created issues",
                extra={"event": "jira.publish", "created_keys": created, "error": str(exc)},
            )
            raise

    logger.info(
        "jira issues created",
        extra={
            "event": "jira.publish",
            "published": True,
            "keys": created,
            "parent": parent_key,
        },
    )
    return {"published": True, "keys": created, "parent": parent_key}


def _changelog_status(body: Dict[str, Any]) -> Tuple[str, str]:
    changelog = body.get("changelog") or {}
    for item in changelog.get("items") or []:
        if str(item.get("field") or "").lower() == "status":
            return str(item.get("fromString") or ""), str(item.get("toString") or "")
    return str(body.get("previous_status") or ""), ""


def _is_jira_cloud_payload(body: Dict[str, Any]) -> bool:
    """True when the body is a Jira Cloud webhook (issue + fields), not a test stub."""
    if body.get("webhookEvent"):
        return True
    issue = body.get("issue")
    return isinstance(issue, dict) and isinstance(issue.get("fields"), dict)


def _description_text(fields: Dict[str, Any], body: Dict[str, Any]) -> str:
    description = fields.get("description") or body.get("description") or ""
    if isinstance(description, dict):
        texts: List[str] = []
        for block in description.get("content", []):
            for node in block.get("content", []):
                if node.get("type") == "text":
                    texts.append(node.get("text", ""))
        return "\n".join(texts)
    return str(description or "")


def parse_webhook_event(body: Dict[str, Any]) -> ParsedWebhook:
    issue = body.get("issue") or body
    fields = issue.get("fields") or {}
    key = issue.get("key") or body.get("issue_key") or ""
    status = (fields.get("status") or {}).get("name") or body.get("status") or ""
    prev, to_status = _changelog_status(body)
    if to_status:
        status = to_status
    user = body.get("user") or {}
    actor = (user.get("emailAddress") or "").strip().lower()
    # Assignee is who owns the ticket, not who transitioned it. Never use it as approver.
    if _is_jira_cloud_payload(body):
        email = actor
    else:
        email = actor or str(body.get("approver_email") or "")
    labels = fields.get("labels") or body.get("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    issue_type = (fields.get("issuetype") or {}).get("name") or body.get("issue_type") or ""
    project = (fields.get("project") or {}).get("key") or ""
    if not project and key and "-" in key:
        project = key.split("-", 1)[0]
    description = _description_text(fields, body)
    assessment_id = (
        body.get("assessment_id")
        or issue.get("assessment_id")
        or fields.get("customfield_assessment_id")
        or _prefixed_line(description, ASSESSMENT_ID_PREFIX)
    )
    root_hash = (
        body.get("root_hash")
        or body.get("audit_root_hash")
        or _prefixed_line(description, AUDIT_ROOT_HASH_PREFIX)
        or _prefixed_line(description, ASSESSMENT_ROOT_HASH_PREFIX)
        or ""
    )
    seq_raw = body.get("audit_seq") or _prefixed_line(description, AUDIT_SEQ_PREFIX)
    audit_seq: Optional[int] = None
    if seq_raw not in (None, ""):
        try:
            audit_seq = int(seq_raw)
        except (TypeError, ValueError):
            audit_seq = None
    return ParsedWebhook(
        key=str(key),
        status=str(status),
        email=email.strip().lower(),
        labels=list(labels),
        assessment_id=assessment_id or None,
        previous_status=prev,
        issue_type=str(issue_type),
        project_key=str(project),
        actor_email=actor,
        root_hash=str(root_hash or ""),
        audit_seq=audit_seq,
    )


def department_from_labels(labels: List[str]) -> Optional[str]:
    """Map Jira labels to internal department keys (legal | infosec | aigov)."""
    lowered = {str(x).lower() for x in labels}
    matches: List[str] = []
    if "legal-review" in lowered:
        matches.append("legal")
    if "infosec" in lowered:
        matches.append("infosec")
    if "ai-governance-review" in lowered:
        matches.append("aigov")
    if len(matches) > 1:
        raise ValueError("Webhook labels must map to a single department")
    return matches[0] if matches else None


def _known_issue_keys(assessment: AssessmentResponse) -> Set[str]:
    keys = set()
    for ticket in assessment.jira_tickets:
        if ticket.fields.issue_key:
            keys.add(ticket.fields.issue_key)
    return keys


def apply_approval(assessment: AssessmentResponse, body: Dict[str, Any]) -> AssessmentResponse:
    """
    Record a human gate closure from Jira. Required department tickets must exist
    and be closed by an allow-listed approver. Completing those gates is
    DEPARTMENT_GATES_COMPLETED, not a final business approval.
    """
    parsed = parse_webhook_event(body)
    if parsed.status.lower() not in DONE_STATUSES:
        logger.debug(
            "webhook ignored non-done status",
            extra={"event": "jira.webhook", "status": parsed.status, "issue_key": parsed.key},
        )
        return assessment

    previous = (parsed.previous_status or "").strip()
    if not previous:
        raise ValueError("Webhook must include changelog status transition or previous_status")
    if previous.lower() in DONE_STATUSES:
        raise ValueError("Transition must be from an open status to Done, not Done → Done")

    expected_project = jira_project_key()
    if parsed.project_key and parsed.project_key.upper() != expected_project.upper():
        raise ValueError(f"Issue project {parsed.project_key} does not match {expected_project}")
    if parsed.key:
        prefix = parsed.key.split("-", 1)[0]
        if prefix.upper() != expected_project.upper():
            raise ValueError(f"Issue {parsed.key} is not in project {expected_project}")
        known = _known_issue_keys(assessment)
        if known and parsed.key not in known:
            raise ValueError(f"Issue {parsed.key} is not part of this assessment")

    record = assessment.decision_record
    if record is None:
        raise ValueError("No DecisionRecord on the current assessment")

    dept = department_from_labels(parsed.labels) or body.get("department")
    if dept not in departments():
        raise ValueError("Webhook labels must include legal-review, infosec, or ai-governance-review")

    expected_type = departments()[dept]["issue_type"]
    allowed_types = {
        expected_type.lower(),
        "task",
        "sub-task",
        "subtask",
    }
    if parsed.issue_type and parsed.issue_type.lower() not in allowed_types:
        raise ValueError(f"Issue type {parsed.issue_type} is not a department gate Task")

    if _is_jira_cloud_payload(body) and not parsed.actor_email:
        raise ValueError(
            "Webhook must include user.emailAddress of the account that transitioned the issue; "
            "assignee is not an approver"
        )

    email = parsed.email
    if not email or not approver_domain_ok(email):
        raise ValueError(
            f"Approver must be a @{approver_domain()} account. Got: {email or 'EVIDENCE NOT FOUND'}"
        )
    if not approver_authorized(email, dept):
        raise ValueError(f"Approver {email} is not authorized for {dept} reviews")

    present_tickets = needed_departments(assessment)
    if dept not in present_tickets:
        raise ValueError(
            f"No {dept} department task exists on this assessment; absence of a ticket is not approval"
        )

    field_name = departments()[dept]["approver_field"]
    existing = getattr(record, field_name)
    if existing:
        if existing == email:
            return assessment
        raise ValueError(f"{dept} gate already closed by {existing}")

    updated = assessment.model_copy(deep=True)
    record = updated.decision_record
    required = list(record.required_departments or REQUIRED_DEPARTMENTS)
    record.gate_status = dict(record.gate_status or {})
    for required_dept in required:
        if required_dept not in present_tickets:
            record.gate_status[required_dept] = GateStatus.MISSING_TICKET
        elif getattr(record, departments()[required_dept]["approver_field"]):
            record.gate_status[required_dept] = GateStatus.REQUIRED_CLOSED
        else:
            record.gate_status[required_dept] = GateStatus.REQUIRED_OPEN

    now = utc_now_iso()
    if dept == "legal":
        record.legal_approver = email
        record.legal_approved_at = now
    elif dept == "infosec":
        record.secops_approver = email
        record.secops_approved_at = now
    elif dept == "aigov":
        record.aigov_approver = email
        record.aigov_approved_at = now
    record.gate_status[dept] = GateStatus.REQUIRED_CLOSED

    missing = [d for d in required if record.gate_status.get(d) == GateStatus.MISSING_TICKET]
    open_required = [
        d for d in required
        if d in present_tickets and not getattr(record, departments()[d]["approver_field"])
    ]
    if missing:
        record.workflow_status = WorkflowStatus.HUMAN_REVIEW_REQUIRED
        record.decision_basis = list(record.decision_basis) + ["RULE-GATE-TICKET-MISSING"]
        logger.warning(
            "required gate ticket missing",
            extra={"event": "jira.approval", "missing": missing},
        )
    elif not open_required:
        record.workflow_status = WorkflowStatus.DEPARTMENT_GATES_COMPLETED
        record.human_decision = None
        record.decision_basis = list(record.decision_basis) + ["RULE-DEPARTMENT-GATES-COMPLETED"]
        logger.info(
            "department gates completed — awaiting final approval",
            extra={
                "event": "jira.approval",
                "department": dept,
                "approver": email,
                "workflow_status": record.workflow_status,
            },
        )
    else:
        record.workflow_status = WorkflowStatus.HUMAN_REVIEW_REQUIRED
        logger.info(
            "department gate recorded",
            extra={
                "event": "jira.approval",
                "department": dept,
                "approver": email,
                "workflow_status": record.workflow_status,
            },
        )

    updated.decision_record = record
    if updated.evidence_pack:
        updated.evidence_pack.decision_audit_trail = record.model_dump()
    return updated


def needed_departments(assessment: AssessmentResponse) -> List[str]:
    """Department keys that have gate Tasks on this assessment."""
    depts = []
    for ticket in assessment.jira_tickets:
        d = ticket.fields.department
        if d and d != "parent":
            depts.append(d)
    return depts
