# Deployment

**Status:** Demo / PoC image. SQLite persists assessments and workflow state
when `DATA_STORE` is mounted on durable storage. Keep one replica; this is not
tenant-isolated production storage.

Container layout: FastAPI (`uvicorn`) serves `/` and `/static/*` from the same image. No nginx sidecar required for a first deploy.

## Build and run locally

```bash
docker compose up --build
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Without Compose:

```bash
docker build -t enterprise-ai-risk .
docker run --rm -p 8000:8000 --env-file .env \
  -v enterprise-ai-risk-data:/data -e DATA_STORE=/data/app.sqlite \
  enterprise-ai-risk
```

## Share a public URL (no coding for guests)

Localhost is not reachable by other people. Deploy once with [Deploy to Render](https://render.com/deploy?repo=https://github.com/tshapedconsultant/enterprise-ai-risk) (`render.yaml` at the repo root), then send the `https://…onrender.com` URL. They only use the browser.

The container listens on `$PORT` (Render, Cloud Run, Railway). Default remains `8000`.

Health check path: `GET /api/v1/health` → `{ "status": "ok" }`. Diagnostics (including `approver_domain` and `webhook_event_store`) are on `GET /api/v1/health/details`.

### Image contents

| In the image | Not in the image |
|--------------|------------------|
| `app/`, `static/`, `requirements.txt` | `.env`, `.venv`, `docs/`, tests |

Pass secrets as environment variables. Never `COPY .env`.

| Variable | Required to run | Notes |
|----------|-----------------|--------|
| `JIRA_APPROVER_DOMAIN` | **Yes (startup)** | Valid email domain (contains `.`, no `@`). Process exits if missing or malformed. |
| `JIRA_WEBHOOK_SECRET` | **Yes (startup)** | Process exits if empty. Webhook POSTs also return **503** if it is cleared later. |
| `JIRA_WEBHOOK_SECRET_PREVIOUS` | Rotation only | Optional old secret accepted during a short no-downtime rotation window; remove after callers move to active. |
| `DATA_STORE` | Recommended | SQLite file for assessments, hash-chained audit events, access-token digests, Jira maps, and webhook IDs. Default `data/app.sqlite`; mount it durably. |
| `API_ACCESS_TOKEN` | For networked demos | Shared bearer or `X-API-Token` gate for assess/chat. It is not identity, OIDC/AD/SSO, roles, tenant ownership, or authorization. |
| `OPENAI_API_KEY` | No | Chat LLM only |
| `JIRA_BASE_URL` / `JIRA_API_TOKEN` | No | Dry-run tickets if unset |
| `REQUIRE_ASSESSMENT_AUTH` | Defaults **on** | Set `false` only for local demos. Console already sends `X-Assessment-Token`. |
| `COMPLIANCE_FRAMEWORKS` | No | Enabled profile IDs; GDPR remains mandatory and decision-capable, others alignment-only. |
| `FRAMEWORK_RULES_DIR` | No | Override `rules/`; startup fails if an enabled profile is missing or invalid. |
| `TRUSTED_PROXIES` | If behind a load balancer | Comma-separated IPs/CIDRs of **the proxies**. `X-Forwarded-For` is ignored unless the TCP peer is in this list. |

## Scale and state

| Constraint | Implication |
|------------|-------------|
| `DATA_STORE` is a local SQLite file | Restart-safe on a durable volume; do not share a local volume across autoscaled replicas |
| Assessment and Jira mappings share one database | Webhooks resolve explicit UUIDs or persisted `issue.key`; no process-global latest row |
| No `tenant_id` or RLS; audit chain/checkpoint share one DB | A process token is not production multi-tenancy; the ledger is not external immutable/WORM storage |
| No built-in TLS | Terminate TLS at the load balancer / Cloud Run / ingress |

**Rule:** keep `replicas: 1` until PostgreSQL tenant isolation exists. SQLite
solves restart loss and provides tamper evidence for ordinary event
modification, not horizontal scaling or externally anchored immutability.

### Jira webhook secret rotation

1. Generate a new secret.
2. Deploy it as `JIRA_WEBHOOK_SECRET` and keep the old active value temporarily
   in `JIRA_WEBHOOK_SECRET_PREVIOUS`.
3. Update Jira automation/callers to sign with the new value.
4. Remove `JIRA_WEBHOOK_SECRET_PREVIOUS` after the bounded rollout window.

The verifier performs both comparisons and returns only a generic 401 on
failure; the active secret is always required.

## Google Cloud Run

```bash
gcloud run deploy enterprise-ai-risk \
  --source . \
  --region europe-west1 \
  --allow-unauthenticated \
  --max-instances 1 \
  --cpu 1 \
  --memory 512Mi \
  --set-env-vars "JIRA_APPROVER_DOMAIN=example.com,DATA_STORE=/tmp/app.sqlite" \
  --set-secrets "JIRA_WEBHOOK_SECRET=jira-webhook-secret:latest,API_ACCESS_TOKEN=console-api-token:latest"
```

Cloud Run's writable filesystem is ephemeral, so `/tmp/app.sqlite` does not
survive instance replacement. Use Cloud SQL/PostgreSQL for a durable Cloud Run
deployment. `--allow-unauthenticated` only makes sense with
`API_ACCESS_TOKEN`; for an internal tool, prefer IAP or authenticated ingress.

Map a custom domain and force HTTPS at Cloud Run. Point Jira automation at `https://<service>/api/v1/webhooks/jira`.

## AWS ECS (Fargate) sketch

1. Push the image to ECR.
2. Task definition: 256–512 CPU, 512–1024 MiB memory, port 8000.
3. Environment / Secrets Manager: same vars as Compose.
4. Service: **desired count 1**, circuit breaker optional.
5. ALB: HTTPS listener → target group health check `GET /api/v1/health` (matcher 200).
6. Security group: ALB → task :8000 only. Do not publish 8000 on a public IP if the ALB exists.

## Kubernetes sketch

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: enterprise-ai-risk
spec:
  replicas: 1
  selector:
    matchLabels:
      app: enterprise-ai-risk
  template:
    metadata:
      labels:
        app: enterprise-ai-risk
    spec:
      containers:
        - name: console
          image: REGION-docker.pkg.dev/PROJECT/enterprise-ai-risk:TAG
          ports:
            - containerPort: 8000
          envFrom:
            - secretRef:
                name: enterprise-ai-risk-env
          readinessProbe:
            httpGet:
              path: /api/v1/health
              port: 8000
            initialDelaySeconds: 5
          livenessProbe:
            httpGet:
              path: /api/v1/health
              port: 8000
            initialDelaySeconds: 15
---
apiVersion: v1
kind: Service
metadata:
  name: enterprise-ai-risk
spec:
  selector:
    app: enterprise-ai-risk
  ports:
    - port: 80
      targetPort: 8000
```

Put an Ingress (or Gateway) with TLS in front. Do not HPA this Deployment until the store is external.

## Jira from a public URL

Outbound: container needs egress HTTPS to `JIRA_BASE_URL`.

Inbound: Jira Cloud must reach `/api/v1/webhooks/jira`. Local `docker compose` on a laptop is not reachable unless you use a tunnel; Cloud Run / ECS / GKE with a public HTTPS URL is.

## CI/CD

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) is the pipeline example:

1. **Test gate** — pytest must pass (coverage ≥ 80%) or nothing else runs.
2. **Evidence pack artifact** — CI exports `GovernanceEvidencePack` JSON from a golden intake (`scripts/export_evidence_pack.py`) and uploads `artifacts/evidence-pack.json` plus `evidence-pack.sha256` to GitHub Actions artifact storage.
3. **Production environment gate** — on `main` only, the `deploy` job uses the GitHub Environment named `production`. Add **required reviewers** in the repo (Settings → Environments) so a human must approve before the job runs. The job re-verifies the SHA-256 and `docker build`s the image. It does not push to a registry until you add those credentials.

This is artifact storage + a deploy gate, not a substitute for PostgreSQL evidence persistence.

## What this image is not

- Not multi-tenant
- Not SSO
- Not a substitute for a DPO or SecOps review
- Not production-durable (see [FUTURE_IMPROVEMENTS.md](FUTURE_IMPROVEMENTS.md) step A)
