# Future improvements — AI governance control layer

**Date:** 2026-08-14  
**Scope:** What is intentionally not in this Demo / PoC, and why  
**Current product:** DPIA object, EvidenceItem, deterministic engine, DecisionRecord, Jira gates, `evidence_pack` JSON  
**Out of scope here:** Durable session store (item 5) and PDF export of the pack

Companion Word file: generate with `python docs/build_future_improvements.py`.  
**v2 (persistence, DPIA Workspace, evidence repository):** [V2_IMPROVEMENTS.md](V2_IMPROVEMENTS.md) — Word: `python docs/build_v2_improvements.py`.

This document does **not** authorize skipping the deterministic engine or returning decisions to the LLM. Preserve: Evidence → Control assessment → Risk factors → Deterministic scoring → Governance triage → Human Jira gates.

## 1. Assessment of five governance proposals

| # | Proposal | Verdict | Status |
|---|---------|---------|--------|
| 1 | DPIA/PIA as formal object (not only `has_dpa`) | Yes. GDPR is not a checkbox. | Done (API v1.1) |
| 2 | EvidenceItem separate from Findings | Yes. Finding ≠ proof. | Done (`evidence_items`) |
| 3 | LLM must not set risk/decision | Yes. Without this it is a compliance chatbot. | Done (`app/scoring.py`) |
| 4 | DecisionRecord / audit trail | Yes. Enterprise audit and sales. | Done + Jira human gates (v1.5) |
| 5 | Replace in-memory store | Yes for production. Not for demo. | Deferred — see §2. Auth tokens and `TRUSTED_PROXIES` are in this release. |

The evidence pack (assessment, matrix, GDPR/DPIA, AI Act, NIST, ISO, findings, remediation, Jira, audit trail) is already generated as JSON (`evidence_pack`). Missing: immutable PDF and durable storage.

## 2. Item 5 — session store (production)

Today `app/store.py` holds **assessments** in process memory (TTL-capped). Fine for demo and interviews. Not fine for multiple workers, restarts, multiple tenants, or audits asking who decided what on Tuesday.

Webhook **event IDs** can already persist in SQLite when `WEBHOOK_EVENT_STORE` is a file path. That stops the same Jira webhook from being applied twice across workers that share the file. It does **not** share assessments. Production still needs PostgreSQL for sessions (this item).

### Target design

| Piece | Role |
|-------|------|
| PostgreSQL | Assessments, intake, evidence_items, decision_record, evidence_pack. Stable relational IDs. |
| Redis | UI session, short locks, cache of latest pack by `assessment_id`. **Not** source of truth. |
| Immutable audit log | Each DecisionRecord written once, never updated. Correction = new record with `supersedes_id`. |
| Event store (optional) | Append-only JSONL or DB triggers blocking UPDATE/DELETE. |

### Must persist

| Object | Why RAM is not enough |
|--------|------------------------|
| VendorInput + version | Reproduce decision: same facts, same engine, same result. |
| EvidenceItem | Auditor asks for proof, not chat prose. |
| DecisionRecord | Who, when, score, rules, engine version, human approvers. |
| GovernanceEvidencePack | Legal deliverable; hash for integrity. |
| Chat history (optional) | Lost on F5 except summary. If stored in prod: retention + minimization. |

### Acceptance criteria

- Restarting uvicorn does not erase assessments.
- Two workers serve the same `assessment_id`.
- Direct UPDATE on DecisionRecord in DB must fail or emit a tamper event.
- Export pack with SHA-256 hash and engine version (`deterministic-rules-v1`, v2, …).

## 3. Evidence pack — remaining work (PDF / portal)

JSON already has the sellable tree. Operational differentiation is an artifact Legal can archive without the console:

| Deliverable | Notes |
|-------------|--------|
| PDF or DOCX on assessment close | Corporate template, pagination, signatures. |
| Evidence portal with NDA | Vendor SOC 2 attached, not only `missing`. |
| Jira hardening | `accountId` mapping; webhook keyed by issue → assessment. Dry-run remains valid without credentials. |
| EU AI Act Annex III | Structured questionnaire object, not a paragraph. |

## 4. Other production improvements

| Topic | Why | Priority | Effort |
|-------|-----|----------|--------|
| Corporate SSO and RACI roles | No owner means no control layer. | High | L |
| Attachments (DPA/DPIA PDF) with hash | Move from `declared_unverified` to `present`. | High | L |
| RAG on approved internal KB | OSINT and contracts; never model memory. | Medium | XL |
| Multitenancy / environments | Do not mix demos with live cases. | High | L |
| Chat retention and erasure | Chat is also processing. | Medium | M |
| Engine calibration on historical cases | Rules v1 are conservative by design. | Medium | M |

## 5. Commercial framing

**Do not position as:** “AI Compliance Assistant” / chatbot that scores vendors.  
**Do not position v1 as:** “production-ready enterprise platform.”

**Position as:** production-oriented reference implementation of an Enterprise AI Governance & Risk Control Layer: facts → controls → evidence → deterministic score → traceable triage → remediation (Jira) → audit pack.

Public “RAG + scoring” products already exist. The difference is operational governance: the model explains, the engine triages, the log is durable, Legal gets a pack.

## 6. Suggested implementation order

Effort is T-shirt size for a small senior team (not calendar weeks). XL includes org/legal work, not only code.

| Step | Deliverable | Depends on | Effort |
|------|-------------|------------|--------|
| A | PostgreSQL + `assessment_id` + append-only DecisionRecord | Item 5 | L |
| B | Attachments and `evidence_status=present` | A | L |
| C | Harden Jira automation (`accountId`, webhook keyed by issue) | A | M |
| D | PDF Evidence Pack with SHA-256 hash | A + Legal template | L |
| E | SSO and environments | A | XL |
| F | Redis as UI session cache only | A (never source of truth) | S |
