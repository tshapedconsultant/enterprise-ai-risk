# V2 improvements — closing the remaining gaps

**Date:** 2026-08-18  
**Honest label today:** Production-oriented reference implementation (Demo / PoC). **Not** a production-ready enterprise platform.  
**Companion Word file:** generate with `python docs/build_v2_improvements.py`.

v1 already has the hard parts most “AI compliance” demos skip: deterministic engine, `EvidenceItem` ≠ finding, `PrivacyTriage`, `DecisionRecord`, Jira gates, `EVIDENCE NOT FOUND`. It still loses points in three places a DPO or sceptical reviewer will find in minutes. Closing them is the jump from ~8.5 (strong reference) to ~9.0+ (credible control plane).

**Must not change:** Evidence → controls → risk factors → deterministic score → triage → human Jira gates. The LLM never decides.

## 1. Persistence

`app/store.py` is keyed by `assessment_id`, locked, TTL-capped, and optionally token-gated. Enough for a demo. Still RAM.

| Failure | Consequence |
|---------|-------------|
| Process restart | Assessments, packs, webhook event ids vanish |
| Two workers | Same `assessment_id` is not one object |
| No `tenant_id` | One process = one implicit tenant |
| In-place `DecisionRecord` update | Not an append-only audit log |

**v2:** PostgreSQL is source of truth. Redis, if any, is UI cache only. Pydantic models stay the contract (ADR-7).

| Object | Rule |
|--------|------|
| `tenants` | Every other row carries `tenant_id`. RLS on every query. |
| `assessments` | Intake JSON + `engine_version` frozen after score. |
| `decision_records` | INSERT only. Correction = new row with `supersedes_id`. |
| `jira_issues` | `issue.key` → `assessment_id`. Webhooks must not bind to “latest in RAM”. |

**Accept:** restart-safe; two workers consistent; tenant A cannot read tenant B by UUID; UPDATE on decision records fails or tampers.

## 2. DPIA Workspace / Generator

v1 can say “DPIA review indicated” and check `dpia_status` / `dpia_reference`. That is a **control over a DPIA**, not a DPIA. No screening, necessity, risks, residual risk, DPO approval, or version.

`PrivacyTriage` stays the engine snapshot. The workspace is the living Art. 35 work product. The engine still does not approve the vendor. The DPO approves the DPIA. CTRL-DPIA becomes `present` only then.

| Stage | Owner | Engine effect |
|-------|--------|----------------|
| 1. Trigger | Engine | Opens workspace when indicated |
| 2. Screening | Assessor + Legal | `not_required` only after DPO confirms |
| 3. Necessity & proportionality | Business + Legal | Incomplete → CTRL-DPIA stays fail |
| 4. Risks | Assessor + DPO | No risks documented → cannot complete |
| 5. Controls | Legal + InfoSec | Unbound controls stay residual |
| 6. Residual risk | DPO | Stored beside engine score; never overwrites it |
| 7. DPO review | DPO | Required before approval |
| 8. Approval | DPO / owner | Named reviewer + timestamp |
| 9. Version | System | Edits create n+1; n remains readable |
| 10. Review date | System + DPO | Expired → CTRL-DPIA = insufficient |

LLM may draft language. It must not set `screening_outcome`, DPO residual risk, or approval. Drafts are labelled “AI-assisted — not evidence.”

**Accept:** all ten stages in-product; a typed `dpia_reference` string alone cannot pass CTRL-DPIA; material change of processing reopens screening.

## 3. Evidence Repository

Philosophy is already right: evidence > assumptions; missing evidence never reduces risk. The archive is missing. `EvidenceItem.document` is a string. CTRL-SOC2 is always missing. A DPA checkbox stays `declared_unverified`.

v2 chain on **every** control:

**Control → Evidence → Source → Reviewer → Timestamp → Decision**

| Artifact | Binds to | Pass condition (indicative) |
|----------|----------|-------------------------------|
| DPA PDF | CTRL-DPA | Signed, in force, Legal accepted |
| DPIA | CTRL-DPIA | Approved workspace version, not an orphan Word file |
| SOC 2 Type II | CTRL-SOC2 | In-period; InfoSec accepted |
| ISO 27001 (+ SoA) | CTRL-SOC2 / CTRL-ISO | Valid; scope covers the service |
| Security questionnaire | several | Dated; reviewer accepted; gaps become findings |
| Vendor documentation | CTRL-MODEL, transfers | Supports a claim; never sufficient alone for SOC 2 |
| Model card | CTRL-MODEL | Model, intended use, limitations, evals |
| Subprocessors | CTRL-TRANSFERS | Current list; locations known |
| Contractual clauses (SCCs, addenda) | CTRL-DPA, transfers | Executed; transfer tool identified |

`EvidenceArtifact` = hashed bytes in object storage. `EvidenceBinding` = the chain row the engine reads. `EvidenceItem` remains the API projection.

Only `decision=accepted` (and in-date) may move a control toward pass. A filename is never a pass. Rejected artifacts are retained.

**Accept:** upload DPA → Legal accept → CTRL-DPA present with reviewer + timestamp; website ISO claims still do not pass; pack export includes hashes and binding decisions.

## 4. Implementation order

Do not start with DPIA UI or RAG. Without persistence both are theatre.

| Step | Deliverable | Gap | Effort |
|------|-------------|-----|--------|
| A | PostgreSQL + tenant RLS + append-only DecisionRecord | 1 | L |
| B | Object storage + artifacts + bindings + upload API | 3 | L |
| C | Engine reads bindings (`present` only if accepted and in-date) | 3 | M |
| D | DPIA Workspace stages 1–10 | 2 | L |
| E | CTRL-DPIA binds to approved version | 2 | S |
| F | Security questionnaire as artifact | 3 | M |
| G | Jira `issue.key` → `assessment_id` | 1 | M |
| H | SSO / RACI | 1 | XL |
| I | PDF/DOCX pack with SHA-256 | 3 | L |

A–E are the portfolio jump. H is required before real customer data.

## 5. What v2 must not do

- Let the LLM set residual risk, triage, DPIA approval, or evidence decision
- Treat `soc2.pdf` as a passed control without a reviewer
- Add engine `APPROVE` because a workspace exists
- Lower the score for a marketing ISO claim
- Use Redis or chat as system of record
- Mix demo and live tenants in one unscoped table
