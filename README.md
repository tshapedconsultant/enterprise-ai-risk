# Enterprise AI Risk Console

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/framework-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Status](https://img.shields.io/badge/status-Demo%20%2F%20PoC-orange)](docs/ARCHITECTURE_DECISIONS.md)
[![Engine](https://img.shields.io/badge/scoring-deterministic--rules--v1-1B2118)](docs/ARCHITECTURE.md)

FastAPI service and single-page console for **third-party AI vendor governance**. This is an **Enterprise AI Governance & Risk Control Layer** — not a generic compliance chatbot.

- **Evidence-first:** missing evidence never reduces residual risk.
- **Deterministic triage:** the rules engine sets score and triage decision; the LLM only explains in chat (if configured).
- **Jira orchestration:** Epic + department Tasks; humans approve in Jira; inbound webhooks close gates.
- **Audit trail:** `DecisionRecord` captures rules, approvers, and workflow status.
- **Readable console:** tabbed report, audit chips, expandable rows, structured assistant card (vanilla `static/` — no SPA build).

## Share a demo link (no coding for guests)

You cannot share `http://127.0.0.1:8000` — that only works on your PC. Host the console once, then send people an **https://** URL. They open it in a browser and use the form. They do not install Python or Git.

1. Open **[Deploy to Render](https://render.com/deploy?repo=https://github.com/tshapedconsultant/enterprise-ai-risk)** (Render account and GitHub login). The blueprint uses a paid Starter instance because persistent disks are not available on the free plan.
2. Click **Apply**. Wait until the service is live (a few minutes).
3. Copy the URL Render shows (looks like `https://enterprise-ai-risk.onrender.com`) and send that.

The blueprint uses a paid Starter instance (persistent disk), not the free web plan, so it should not spin down the way Render's free services do. Assessments use SQLite and survive an application restart when `DATA_STORE` is on persistent storage. This remains a Demo / PoC rather than a production tenant: there is no SSO/RBAC or tenant database isolation. Set `API_ACCESS_TOKEN` for any networked demo. The generated `API_ACCESS_TOKEN` is shown once in the Render dashboard; guests need that token if the console prompts for it.

Chat stays on the mock assistant unless you add `OPENAI_API_KEY` in Render → Environment.

## Quick start

### Windows (PowerShell)

```powershell
cd enterprise-ai-risk
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --port 8000
```

### Linux / macOS (bash)

```bash
cd enterprise-ai-risk
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Without `OPENAI_API_KEY`, assessments and chat use deterministic rules (demo mode). Add a key to `.env` for LLM-grounded chat answers.

### Docker

Requires Docker Engine. Copy `.env.example` to `.env` first if you want optional OpenAI/Jira variables.

```bash
docker compose up --build
```

Equivalent without Compose:

```bash
docker build -t enterprise-ai-risk .
docker run --rm -p 8000:8000 --env-file .env -v enterprise-ai-risk-data:/data \
  -e DATA_STORE=/data/app.sqlite enterprise-ai-risk
```

The console is still a **Demo / PoC**: SQLite is restart-safe with a
hash-chained audit ledger. External root-hash anchors (Jira dry-run by default;
optional Rekor or S3 Object Lock) are the WORM/attester path. Jira dry-run is
not legally WORM. There is still no SSO/RBAC, tenant ownership, or PostgreSQL
row-level security.

## Architecture (summary)

```
Intake form → POST /assess-vendor
    → scoring.evaluate()     # controls + score 1–5; triage from rules/gdpr.yaml
    → frameworks.stamp()     # GDPR enforced; EU AI Act / ISO 42001 / NIST alignment-only
    → jira_workflow          # Epic + Legal / SecOps / AI Gov Tasks
    → store (SQLite)         # assessment, token, workflow, Jira map, hash-chained audit
    → optional Jira POST     # if JIRA_BASE_URL + token set
    → audit_anchor sinks     # Jira hash copy; optional Rekor / S3 Object Lock

Jira department Task closed → POST /webhooks/jira
    → HMAC (active or previous secret) + assessment_id or issue.key map
    → apply_approval()       # record allow-listed @{JIRA_APPROVER_DOMAIN} approver
    → DEPARTMENT_GATES_COMPLETED when all gates close (not a business APPROVE)
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (how it is built), [docs/ARCHITECTURE_DECISIONS.md](docs/ARCHITECTURE_DECISIONS.md) (why, trade-offs, limitations), and [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Web console (`static/index.html`) |
| `GET` | `/api/v1/health` | Public liveness (`{ "status": "ok" }` only) |
| `GET` | `/api/v1/health/details` | Diagnostics; gated by `HEALTH_DETAILS_TOKEN` when set |
| `GET` | `/api/v1/config` | Non-secret UI flags and enabled frameworks |
| `GET` | `/api/v1/assessments/{assessment_id}` | Restore one assessment by UUID + token |
| `GET` | `/api/v1/assessments/{assessment_id}/audit` | Verify the hash chain and external root-hash anchors |
| `GET` | `/api/v1/assessment/latest` | **Deprecated.** Requires `X-Assessment-Id`; never returns a global latest row |
| `POST` | `/api/v1/assess-vendor` | Run triage; return full governance JSON |
| `POST` | `/api/v1/chat` | Q&A for one `assessment_id` (required with default assessment auth) |
| `POST` | `/api/v1/webhooks/jira` | Inbound human approval from Jira |

### Engine triage decisions (never final APPROVE)

| Decision | Meaning |
|----------|---------|
| `PENDING REVIEW` | No critical red flag; departments must still close Jira gates |
| `REQUIRES REMEDIATION` | Material gaps (DPA, DPIA, score, etc.) |
| `ESCALATE TO AI GOVERNANCE / LEGAL / SECURITY` | Personal data without DPA, Art. 9, or score 5 |
| `APPROVE WITH CONDITIONS` | **Human only** — reserved for a final decision-maker. Closing department gates is `DEPARTMENT_GATES_COMPLETED`, not approval |

### Jira webhook (test)

After an assessment, simulate Legal approval (HMAC of the raw body is required; see `scripts/simulate_jira_webhook.py`):

```http
POST /api/v1/webhooks/jira
Content-Type: application/json

{
  "status": "Done",
  "approver_email": "j.perez@example.com",
  "labels": ["legal-review"],
  "assessment_id": "ASSESSMENT_UUID"
}
```

Repeat for `infosec` and `ai-governance-review`. Non-`@example.com` emails are rejected.

## Configuration

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Optional; enables LLM chat |
| `OPENAI_MODEL` | Chat model (default `gpt-4o-mini`) |
| `JIRA_BASE_URL` | Jira Cloud base URL for outbound create |
| `JIRA_USER_EMAIL` | Bot user for Jira REST auth |
| `JIRA_API_TOKEN` | Jira API token |
| `JIRA_WEBHOOK_SECRET` | Required at startup; HMAC-SHA256 of the raw body (`X-Hub-Signature-256`) |
| `JIRA_WEBHOOK_SECRET_PREVIOUS` | Optional previous HMAC secret during a short rotation window |
| `JIRA_APPROVER_DOMAIN` | Required at startup; corporate mailbox suffix for human approvers |
| `DATA_STORE` | SQLite path for assessments, hash-chained audit events, local `audit_anchors` copies, tokens, Jira mappings, and webhook replay IDs (default `data/app.sqlite`) |
| `AUDIT_ANCHOR_SINKS` | Comma-separated `jira`, `rekor`, `s3` (default `jira`). Jira dry-run embeds the root hash without credentials; Rekor/S3 stay off until configured |
| `API_ACCESS_TOKEN` | Shared deployment-wide bearer / `X-API-Token` gate; not identity, OIDC/AD/SSO, roles, tenant ownership, or authorization |
| `REQUIRE_ASSESSMENT_AUTH` | Per-assessment token protection (default `true`). Restore, audit, and chat need `assessment_id` plus `X-Assessment-Token` |
| `HEALTH_DETAILS_TOKEN` | Optional; when set, `GET /api/v1/health/details` requires `X-Health-Token` |
| `COMPLIANCE_FRAMEWORKS` | GDPR decision profile plus alignment-only EU AI Act, ISO 42001, and NIST AI RMF metadata |
| `FRAMEWORK_RULES_DIR` | Optional override for validated YAML profiles (default `rules/`) |
| `TRUSTED_PROXIES` | Comma-separated proxy IPs/CIDRs; required before `X-Forwarded-For` is trusted |
| `LOG_LEVEL` | Log verbosity (`INFO` default) |
| `LOG_FORMAT` | `text` or `json` (default `text`) |
| `LOG_FILE` | Optional log file path (e.g. `logs/app.log`) |

## Project layout

| Path | Role |
|------|------|
| `app/main.py` | FastAPI routes |
| `app/scoring.py` | Deterministic governance engine |
| `rules/*.yaml` | Validated GDPR decision and alignment-only framework profiles |
| `app/jira_workflow.py` | Epic + department Task payloads + webhook logic |
| `app/models.py` | Pydantic schemas (intake, assessment, Jira) |
| `app/llm.py` | Chat only; never scores |
| `app/store.py` | SQLite assessment, workflow, token, Jira mapping, webhook IDs, hash-chained audit ledger, and `audit_anchors` |
| `app/audit_anchor.py` | External root-hash sinks (Jira / Rekor / S3 Object Lock) |
| `app/frameworks.py` | Framework catalog and evidence-pack scoping |
| `app/logging_config.py` | Central logging (`LOG_LEVEL`, `LOG_FORMAT`, `LOG_FILE`) |
| `static/` | Web console (`index.html`, `styles.css`, `app.js`) — intake form, tabbed report, audit chips, chat |
| `Dockerfile` / `docker-compose.yml` | Container image (see [DEPLOYMENT.md](docs/DEPLOYMENT.md)) |
| `.github/workflows/ci.yml` | Test gate, evidence-pack artifact upload, production deploy gate |
| `tests/` | pytest: API, engine, webhooks, LLM mock, store, logging, golden regression |

## Documentation

- [User guide](docs/USER_GUIDE.md) — for business, legal, and compliance users
- [Architecture](docs/ARCHITECTURE.md) — technical design and Jira integration
- [Architecture decisions](docs/ARCHITECTURE_DECISIONS.md) — why, trade-offs, and limitations
- [Testing](docs/TESTING.md) — pytest, webhook simulation, rule regression
- [Security / threat model](docs/SECURITY.md) — prompt injection and webhook spoofing
- [Deployment](docs/DEPLOYMENT.md) — Docker, Cloud Run, ECS, Kubernetes
- [Future improvements](docs/FUTURE_IMPROVEMENTS.md) — production roadmap
- [V2 improvements](docs/V2_IMPROVEMENTS.md) — persistence, DPIA Workspace, evidence repository
- Optional Word export — Markdown is canonical; do not hand-edit `.docx`: `python scripts/export_docx.py docs/USER_GUIDE.md --out-dir artifacts/docs`
- [Contributing](CONTRIBUTING.md), [changelog](CHANGELOG.md), and [MIT license](LICENSE)

## Production notes

- Replace SQLite with PostgreSQL + tenant row-level security before multi-tenant production. The local hash chain is tamper-evident, not WORM. Jira/Rekor/S3 are the external attesters; Jira dry-run is not legally WORM, and a Jira admin can still edit tickets. Rekor is the stronger public log when enabled.
- Map Jira `assignee.emailAddress` to `accountId` before outbound create.
- Configure Jira automation to POST to `/api/v1/webhooks/jira` on department Task Done.
- Outbound Jira errors do **not** fail the assessment (see [resilience](docs/ARCHITECTURE.md#outbound-resilience)).
