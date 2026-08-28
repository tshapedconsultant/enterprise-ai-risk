# Deployment

**Status:** Demo / PoC image. Assessments live in an in-memory store (TTL-capped); lost on restart. Do **not** run multiple replicas expecting a shared session.

Container layout: FastAPI (`uvicorn`) serves `/` and `/static/*` from the same image. No nginx sidecar required for a first deploy.

## Build and run locally

```bash
docker compose up --build
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Without Compose:

```bash
docker build -t enterprise-ai-risk .
docker run --rm -p 8000:8000 --env-file .env enterprise-ai-risk
```

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
| `WEBHOOK_EVENT_STORE` | Recommended | SQLite file for Jira event IDs. Unset or `:memory:` is process-local. |
| `OPENAI_API_KEY` | No | Chat LLM only |
| `JIRA_BASE_URL` / `JIRA_API_TOKEN` | No | Dry-run tickets if unset |
| `REQUIRE_ASSESSMENT_AUTH` | Defaults **on** | Set `false` only for local demos. Console already sends `X-Assessment-Token`. |
| `TRUSTED_PROXIES` | If behind a load balancer | Comma-separated IPs/CIDRs of **the proxies**. `X-Forwarded-For` is ignored unless the TCP peer is in this list. |

## Scale and state

| Constraint | Implication |
|------------|-------------|
| `store.py` assessments are process RAM | Restart or a second replica loses / splits assessments |
| Webhook event IDs can be SQLite (`WEBHOOK_EVENT_STORE`) | Shared file/volume stops duplicate Jira events across workers; assessments still need PostgreSQL |
| Webhooks still bind to in-memory sessions | Sticky sessions do not fully fix this; use one replica until PostgreSQL (roadmap A) |
| No built-in TLS | Terminate TLS at the load balancer / Cloud Run / ingress |

**Rule:** `replicas: 1` (or Cloud Run `max-instances=1`) until the durable PostgreSQL store exists (see [FUTURE_IMPROVEMENTS.md](FUTURE_IMPROVEMENTS.md) item 5). Do not ship production on the in-memory store.

## Google Cloud Run

```bash
gcloud run deploy enterprise-ai-risk \
  --source . \
  --region europe-west1 \
  --allow-unauthenticated \
  --max-instances 1 \
  --cpu 1 \
  --memory 512Mi \
  --set-env-vars "JIRA_APPROVER_DOMAIN=adevinta.com,WEBHOOK_EVENT_STORE=/tmp/webhook-events.sqlite" \
  --set-secrets "JIRA_WEBHOOK_SECRET=jira-webhook-secret:latest"
```

`--allow-unauthenticated` matches today’s lack of SSO. For an internal tool, put Cloud Run behind IAP or `--no-allow-unauthenticated` plus a load balancer.

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

## What this image is not

- Not multi-tenant
- Not SSO
- Not a substitute for a DPO or SecOps review
- Not production-durable (see [FUTURE_IMPROVEMENTS.md](FUTURE_IMPROVEMENTS.md) step A)
