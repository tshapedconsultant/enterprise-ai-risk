# Future improvements — AI governance control layer

**Date:** 2026-09-01  
**Scope:** What is intentionally not in this Demo / PoC, and why  
**Current product:** DPIA object, EvidenceItem, deterministic engine, DecisionRecord, Jira gates, YAML GDPR triage, hash-chained SQLite audit, `evidence_pack` JSON  
**Out of scope here:** Tenant-grade PostgreSQL/WORM audit store and PDF export

Markdown is canonical. Optional Word export:
`python scripts/export_docx.py docs/FUTURE_IMPROVEMENTS.md --out-dir artifacts/docs`.
See also [V2 improvements](V2_IMPROVEMENTS.md).

This document does **not** authorize skipping the deterministic engine or returning decisions to the LLM. Preserve: Evidence → Control assessment → Risk factors → Deterministic scoring → Governance triage → Human Jira gates.

## 1. Assessment of five governance proposals

| # | Proposal | Verdict | Status |
|---|---------|---------|--------|
| 1 | DPIA/PIA as formal object (not only `has_dpa`) | Yes. GDPR is not a checkbox. | Done (API v1.1) |
| 2 | EvidenceItem separate from Findings | Yes. Finding ≠ proof. | Done (`evidence_items`) |
| 3 | LLM must not set risk/decision | Yes. Without this it is a compliance chatbot. | Done (`app/scoring.py`) |
| 4 | DecisionRecord / audit trail | Yes. Enterprise audit and sales. | Done + Jira human gates (v1.5) |
| 5 | Replace in-memory store | Yes. | SQLite persistence and tamper-evident hash chain done; PostgreSQL tenancy + externally immutable/WORM audit storage remain. |

The evidence pack is generated as JSON. SQLite now persists it across restart
and records hash-chained audit events. Remaining: immutable PDF, per-tenant
authorization, and an external append-only/WORM store (the SQLite chain is not
that).

## 2. Item 5 — session store (production)

Today `app/store.py` persists assessments, access tokens, workflow updates, Jira
issue mappings, webhook IDs, and hash-chained audit events in SQLite
(`DATA_STORE`). Restart recovery, explicit assessment isolation, and tamper
evidence are implemented. SQLite is still not the target for horizontally
scaled, multi-tenant, externally immutable production.

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

- Restarting uvicorn does not erase assessments when `DATA_STORE` is durable. **Done.**
- Webhooks resolve `issue.key` without a global latest assessment. **Done.**
- Hash-chained audit events with `GET /api/v1/assessments/{id}/audit`. **Done** (tamper-evident, not WORM).
- Two workers serve the same `assessment_id` through PostgreSQL.
- Direct UPDATE on DecisionRecord in DB must fail or emit a tamper event that cannot be rewritten with the checkpoint.
- Export pack with SHA-256 hash and engine version (`deterministic-rules-v1`, v2, …).

## 3. Evidence pack — remaining work (PDF / portal)

JSON already has the sellable tree. Operational differentiation is an artifact Legal can archive without the console:

| Deliverable | Notes |
|-------------|--------|
| PDF or DOCX on assessment close | Corporate template, pagination, signatures. |
| Evidence portal with NDA | Vendor SOC 2 attached, not only `missing`. |
| Jira hardening | `accountId` mapping. Issue-key → assessment mapping and dual HMAC secrets are already in this release. Dry-run remains valid without credentials. |
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
| C | Harden Jira automation (`accountId`) | A; issue-key map **done** | M |
| D | PDF Evidence Pack with SHA-256 hash | A + Legal template | L |
| E | SSO and environments | A | XL |
| F | Redis as UI session cache only | A (never source of truth) | S |
