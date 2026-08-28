"""
Enterprise AI Risk Assessment package.

Governance console for third-party AI vendor risk:
  - Structured vendor intake (Pydantic)
  - Deterministic scoring engine (the LLM never chooses triage)
  - Web UI: tabbed report, audit chips, disclosure rows (static/)
  - Chat Q&A grounded in a redacted assessment (optional OpenAI)
  - Jira Epic + departmental Tasks, with HMAC webhooks
  - In-memory store keyed by assessment_id (replace before production)
"""
