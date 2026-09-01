# Testing strategy

Automated tests protect the **control layer**: the engine must stay deterministic, webhooks must not accept spoofed approvals, and rule edits must not silently change past triage.

No test in this repo calls OpenAI or Jira Cloud.

## Setup

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
```

```bash
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

## Run the suite

```bash
pytest
pytest --cov=app --cov-report=term-missing --cov-fail-under=80
```

With live log output on the console (`log_cli=true` in `pytest.ini`). A full DEBUG trace is also written to `tests/logs/pytest.log`; the app logger file during tests is `tests/logs/pytest-app.log` (gitignored).

| File | What it covers |
|------|----------------|
| `tests/test_scoring.py` | Deterministic engine, parametrized business cases, score cap, stability |
| `tests/test_jira_webhooks.py` | HMAC, replay, project/issue binding, allow-list, spoofed actor, three gates |
| `tests/test_jira_workflow_unit.py` | Parse webhook, labels, dry-run, MockTransport publish/rollback/timeout |
| `tests/test_llm.py` | Mock chat plus OpenAI client mocks (timeout, empty choices, redaction, truncation) |
| `tests/test_validation.py` | Input limits, isolation, token auth, generic 500 |
| `tests/test_regression.py` | Golden snapshots under `tests/golden/` |
| `tests/test_api.py` | Health/config, static console, assess, ID-scoped restore, API/assessment tokens, chat |
| `tests/test_store.py` | SQLite round-trip/reopen, token retention, workflow update, Jira map, event replay, hash-chain verify/tamper |
| `tests/test_audit_anchor.py` | Jira root-hash payload, missing external anchor, mocked Rekor/S3, webhook correlation |
| `tests/test_frameworks.py` | Mandatory GDPR YAML engine plus configurable optional alignment sections |
| `tests/test_config.py` | Startup `ConfigurationError` for domain, webhook secret, trusted proxies |
| `tests/test_redaction.py` | Table-driven `redact_secrets` / `redact_mapping` (JSON, query, headers, false positives) |
| `tests/test_logging.py` | JSON formatter, `configure_logging`, structured `event` fields on assess/webhook paths |
| `tests/test_security_fixes.py` | Dual HMAC secrets, personal-data heuristics, DPIA override, concurrent assessments |

Single file:

```bash
pytest tests/test_scoring.py
pytest tests/test_jira_webhooks.py -q
pytest tests/test_logging.py -q
```

The static console (`static/`) is not covered by pytest. Verify UI changes manually in the browser or by fetching `/static/app.js` and `/static/styles.css` after deploy.

## Webhook simulation without internet

**Preferred (CI):** `pytest tests/test_jira_webhooks.py` uses `TestClient`. Nothing listens on a port and nothing reaches Atlassian.

**Against a running console** (optional, still local):

```bash
# PowerShell / bash — server on :8000
python scripts/simulate_jira_webhook.py
```

The script POSTs `/api/v1/assess-vendor`, reads `assessment_id` from the response, and sends it in every webhook body (plus `X-Assessment-Id` on gate closures). Set `CONSOLE_URL` and `JIRA_WEBHOOK_SECRET` to match the server. To target an existing assessment without re-assessing, set `ASSESSMENT_ID`.

Equivalent HMAC-signed request (Legal gate only — replace `YOUR_ASSESSMENT_ID` and the signature). `X-Jira-Secret` alone is rejected; use `scripts/simulate_jira_webhook.py` or:

```bash
BODY='{"status":"Done","previous_status":"To Do","approver_email":"j.perez@example.com","labels":["legal-review"],"assessment_id":"YOUR_ASSESSMENT_ID"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$JIRA_WEBHOOK_SECRET" | awk '{print $2}')
curl -s -X POST http://127.0.0.1:8000/api/v1/webhooks/jira \
  -H "Content-Type: application/json" \
  -H "X-Hub-Signature-256: sha256=$SIG" \
  -H "X-Jira-Timestamp: $(date +%s)" \
  -H "X-Jira-Event-Id: evt-legal-manual" \
  -H "X-Assessment-Id: YOUR_ASSESSMENT_ID" \
  -d "$BODY"
```

If no assessment is persisted, expect **404**. An event can resolve by explicit
`assessment_id` or persisted Jira `issue.key`; missing/unmapped correlation returns **400**.
Malformed UUIDs return **400** and unauthorized approvers return **422**.

## Regression (rule changes vs past cases)

Goldens store *triage contract* fields only (decision, score, rules, ticket departments) — not timestamps.

```bash
pytest tests/test_regression.py
```

If you **intentionally** change `app/scoring.py` rules:

1. Review the diff of decisions/scores.
2. Regenerate: `python tests/test_regression.py --write`
3. Commit both `*.intake.json` and `*.expected.json`.

Do not regenerate to make a failing test green without explaining the governance impact. A change from `ESCALATE` to `PENDING REVIEW` on `personal_no_dpa` is a product incident, not a flake.

Add a new case: drop `tests/golden/<name>.intake.json`, run `--write`, commit the new `expected.json`.

## Application logging

Configured in `app/logging_config.py` and initialized on import in `app/main.py`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOG_LEVEL` | `INFO` | `DEBUG` … `ERROR` |
| `LOG_FORMAT` | `text` | `text` (human) or `json` (one object per line) |
| `LOG_FILE` | unset | Optional append-only file (e.g. `logs/app.log`) |

Loggers are named under `enterprise_ai_risk` (e.g. `enterprise_ai_risk.app.scoring`). Structured fields use the `extra={"event": "…"}` pattern for grep-friendly audit trails.

Tests set `LOG_LEVEL=DEBUG` and `LOG_FILE=tests/logs/pytest-app.log` in `tests/conftest.py`. Use the `caplog_app` fixture to assert on `record.event` in `tests/test_logging.py`.

## What we do not test yet

| Gap | Why |
|-----|-----|
| Live Jira REST | Outbound is mocked with `httpx.MockTransport`; no call to Atlassian |
| Live OpenAI | Chat client is mocked; fallback and redaction (including JSON/query false positives) are covered |
| Multi-worker database behavior | SQLite restart and hash-chain checks are tested; PostgreSQL concurrency/RLS remains future work |

## CI/CD

GitHub Actions: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

| Job | When | Gate |
|-----|------|------|
| `test` | every push and PR | pytest with coverage ≥ 80% |
| `evidence-pack` | after `test` | Export `evidence_pack` JSON + SHA-256; upload as a GitHub Actions artifact (`evidence-pack`, 30 days) |
| `deploy` | `main` push only | GitHub Environment **production** (add required reviewers under Settings → Environments). Re-checks the pack hash, then `docker build`. |

Local pack export (same command CI uses):

```bash
python scripts/export_evidence_pack.py --intake tests/golden/personal_no_dpa.intake.json --out-dir artifacts
sha256sum -c artifacts/evidence-pack.sha256
```
