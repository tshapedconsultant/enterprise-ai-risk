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

## Quick start

### Windows (PowerShell)

```powershell
cd C:\Users\anlaf\enterprise-ai-risk
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
docker run --rm -p 8000:8000 --env-file .env enterprise-ai-risk
```

The console is still a **Demo / PoC**: in-memory assessments with TTL, not a multi-user production deployment.

## Architecture (summary)

```
Intake form → POST /assess-vendor
    → scoring.evaluate()     # deterministic controls, score 1–5, triage decision
    → jira_workflow          # Epic + Legal / SecOps / AI Gov Tasks
    → store (in-memory)      # session for chat + page reload
    → optional Jira POST     # if JIRA_BASE_URL + token set

Jira department Task closed → POST /webhooks/jira
    → apply_approval()       # record allow-listed @{JIRA_APPROVER_DOMAIN} approver
    → workflow APPROVED when all gates close
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (how it is built), [docs/ARCHITECTURE_DECISIONS.md](docs/ARCHITECTURE_DECISIONS.md) (why, trade-offs, limitations), and [docs/USER_GUIDE.md](docs/USER_GUIDE.md).

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Web console (`static/index.html`) |
| `GET` | `/api/v1/health` | Liveness, LLM mode, Jira outbound status |
| `GET` | `/api/v1/assessment/latest` | Restore session after page reload |
| `POST` | `/api/v1/assess-vendor` | Run triage; return full governance JSON |
| `POST` | `/api/v1/chat` | Q&A about the latest assessment |
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
  "approver_email": "j.perez@adevinta.com",
  "labels": ["legal-review"]
}
```

Repeat for `infosec` and `ai-governance-review`. Non-`@adevinta.com` emails are rejected.

## Configuration

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | Optional; enables LLM chat |
| `OPENAI_MODEL` | Chat model (default `gpt-4o-mini`) |
| `JIRA_BASE_URL` | Jira Cloud base URL for outbound create |
| `JIRA_USER_EMAIL` | Bot user for Jira REST auth |
| `JIRA_API_TOKEN` | Jira API token |
| `JIRA_WEBHOOK_SECRET` | Required at startup; HMAC-SHA256 of the raw body (`X-Hub-Signature-256`) |
| `JIRA_APPROVER_DOMAIN` | Required at startup; corporate mailbox suffix for human approvers |
| `WEBHOOK_EVENT_STORE` | Optional SQLite path for webhook event IDs (replay protection) |
| `TRUSTED_PROXIES` | Comma-separated proxy IPs/CIDRs; required before `X-Forwarded-For` is trusted |
| `LOG_LEVEL` | Log verbosity (`INFO` default) |
| `LOG_FORMAT` | `text` or `json` (default `text`) |
| `LOG_FILE` | Optional log file path (e.g. `logs/app.log`) |

## Project layout

| Path | Role |
|------|------|
| `app/main.py` | FastAPI routes |
| `app/scoring.py` | Deterministic governance engine |
| `app/jira_workflow.py` | Epic + department Task payloads + webhook logic |
| `app/models.py` | Pydantic schemas (intake, assessment, Jira) |
| `app/llm.py` | Chat only; never scores |
| `app/store.py` | In-memory session (replace for production) |
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
- [User guide (Word)](docs/User_Guide_AI_Vendor_Risk.docx) — `python docs/build_user_guide.py`
- [Future improvements (Word)](docs/Future_Improvements_AI_Governance.docx) — `python docs/build_future_improvements.py`
- [V2 improvements (Word)](docs/V2_Improvements_AI_Governance.docx) — `python docs/build_v2_improvements.py`

## Production notes

- Replace `app/store.py` with PostgreSQL + immutable audit log for multi-user and restart safety.
- Map Jira `assignee.emailAddress` to `accountId` before outbound create.
- Configure Jira automation to POST to `/api/v1/webhooks/jira` on department Task Done.
- Outbound Jira errors do **not** fail the assessment (see [resilience](docs/ARCHITECTURE.md#outbound-resilience)).
