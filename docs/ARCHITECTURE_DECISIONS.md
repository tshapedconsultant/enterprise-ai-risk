# Architecture Decisions

**Product:** Enterprise AI Vendor Risk Console  
**Audience:** engineers, reviewers, and anyone who needs the *why* behind the design  
**Companion docs:** [ARCHITECTURE.md](ARCHITECTURE.md) (how it is built), [USER_GUIDE.md](USER_GUIDE.md) (how to use it)

This file records **decisions**, not the component map. Each item states the choice, why it was made, what was rejected, the trade-off, and the limitation that follows.

Format follows [MADR](https://adr.github.io/madr/) / Nygard ADRs. Every record below is **Status: Accepted** unless a later ADR supersedes it.

---

## How to read this

A **decision** here is something we would have to justify in a design review. Changing one of these is a product change, not a refactor.

Non-goals of this document:

- API field lists (see `app/models.py` and OpenAPI)
- Control-by-control scoring rules (see `app/scoring.py` and ARCHITECTURE.md)
- Roadmap items that were explicitly deferred (see Future Improvements)

---

## ADR-1 — Product is a control layer, not a compliance chatbot

**Status:** Accepted

**Decision:** Position and build this as an **Enterprise AI Governance & Risk Control Layer**: intake → controls → evidence → deterministic score → triage → Jira remediation → audit pack.

**Why:** Public “RAG + LLM scores the vendor” products already exist. They fail an audit question (“who decided, on which evidence, with which engine version?”). A chatbot that *sounds* like Legal is a liability. A control layer that *refuses* to approve without evidence is the sellable difference.

**Rejected:** Framing as “AI Compliance Assistant”, letting the model invent SOC 2 / DPA / fines, or treating chat as the system of record.

**Trade-off:** The UI looks less “smart” than a free-form GPT wrapper. Chat is secondary and can say `EVIDENCE NOT FOUND`. That is intentional.

**Limitation:** This product does not replace a DPO opinion, a signed contract, or an independent security review. Copy in the UI and reports must keep saying that.

---

## ADR-2 — Deterministic engine decides; LLM only explains

**Status:** Accepted

**Decision:** `app/scoring.py` (`ENGINE_VERSION = deterministic-rules-v1`) owns residual risk (1–5) and the triage decision. `app/llm.py` may answer chat questions. It never writes `decision` or `risk_score`. `run_assessment()` always returns `used_llm_for_decision=False`.

**Why:**

- Same intake + same engine version → same result (reproducible for audit).
- Model drift, temperature, and prompt injection cannot silently approve a vendor.
- Rules in `DecisionRecord.decision_basis` are reviewable by Legal/Security without reading a prompt.

**Rejected:** Structured LLM output as the assessment (even with a JSON schema). Schema compliance ≠ governance correctness.

**Trade-off:** Rules are conservative and incomplete vs. a model that “understands” a 40-page SOC 2. Adding a control requires a code change, not a prompt tweak. Calibration against historical cases is future work.

**Limitation:** The engine cannot read attached PDFs. Declared DPA (`has_dpa=true`) stays `declared_unverified` until a file exists in the store. Vendor marketing never lowers residual risk.

---

## ADR-3 — Engine never issues final APPROVE (intelligent triage)

**Status:** Accepted

**Decision:** Engine outputs only `PENDING REVIEW`, `REQUIRES REMEDIATION`, `ESCALATE TO AI GOVERNANCE / LEGAL / SECURITY`, or (reserved) `REJECT`. Final `APPROVE WITH CONDITIONS` is a **human** decision after Jira gates close. Engine `APPROVE` is not in the decision matrix.

**Why:** Automatic approval of a third-party AI vendor is the failure mode this product exists to prevent. Personal data without a DPA, Art. 9, missing SOC 2, unnamed models — none of these should get a green banner from software. Legal, SecOps, and AI Governance already own those gates.

**Rejected:** Engine `APPROVE` when score is low; LLM “approve with conditions”; collapsing ESCALATE into PENDING REVIEW.

**Trade-off:** Even a well-documented, low-risk internal tool still opens three Jira department Tasks. Throughput is slower than a single “Approve” button. That is the point of a control layer.

**Limitation:** `ESCALATE` is still *triage*, not a closed case. The engine does not page a human or block production deploy by itself. Downstream CI/CD must consume the JSON if you want a hard gate.

---

## ADR-4 — Humans approve in Jira, not in this console

**Status:** Accepted

**Decision:** After triage, create a **parent Epic** plus one **Task** per department (Legal / SecOps / AI Governance). Jira Sub-tasks cannot be children of an Epic, so they are not used as gates. Approvers work in Jira. Inbound webhook `POST /api/v1/webhooks/jira` records who closed which gate. The transitioning user's email (`user.emailAddress`) is the only approver identity; assignee fallback is rejected. When all **required** department tickets exist and are closed by allow-listed approvers, `workflow_status=DEPARTMENT_GATES_COMPLETED`. That is not a vendor approval. Domain suffix alone is not sufficient; missing tickets are not treated as approval.

**Why:**

- Departments already live in Jira; forcing them onto a new console kills adoption.
- Decentralized RACI is visible as separate tickets and labels (`legal-review`, `infosec`, `ai-governance-review`).
- Audit trail stores emails (`legal_approver`, `secops_approver`, `aigov_approver`) without inventing a second identity system.

**Rejected:** In-console approval buttons; email-only workflow; a single generic “Task” per finding (previous shape).

**Trade-off:** Integration complexity (outbound REST + inbound webhook + ADF descriptions + stripping `department`). Demo must work **without** Jira credentials (dry-run payloads). Live Jira needs `accountId` mapping, project `AIGOV` issue types Epic/Task, and automation on Done.

**Limitation:**

- Parent key `AIGOV-PARENT` is a placeholder until outbound create returns a real key.
- Jira issue keys are persisted after outbound create and webhooks resolve
  `issue.key` → `assessment_id`; dry-run events should still include the UUID.
- Closing the Epic in Jira does not by itself update this app; the webhook must fire when a department **Task** moves to Done.
- Engine triage on `assessment_metadata.decision` is **not rewritten** when humans approve (by design: preserve what the engine said).

---

## ADR-5 — FastAPI + static HTML/JS, not a SPA framework

**Status:** Accepted

**Decision:** One FastAPI app serves `/` (`static/index.html`), `/static/*`, and `/api/v1/*`. Frontend is vanilla JS (`static/app.js`). No React/Vue, no separate Node build.

**Why:** One process to run (`uvicorn`), one origin (no CORS for local use), OpenAPI for free, Pydantic as the shared contract. The audience is Legal/business; the UI is a form + report + chat, not a design system.

**Rejected:** Next.js/React split, FastAPI-only API with a separately hosted UI, Django admin as the console.

**Trade-off:** No component library, weaker client-side routing, chat history lives in the browser. Fine for a governance console; painful if this becomes a multi-page portal.

**v1.7 UX (2026-08):** Right panel uses a single scroll container (no nested scroll areas). Report sections are tabs with row counts. Findings and evidence use progressive disclosure (`<details>` rows). Audit trail is a labelled grid plus gate status chips. The first assistant message is a structured card; plain text is kept for clipboard copy and LLM context.

**Limitation:** XSS is mitigated by escaping report HTML (`escapeHtml`), not by a framework. Keep using it for any user/vendor text rendered as HTML. Colour chips always pair colour with a text label for accessibility.

---

## ADR-6 — SQLite assessment store for the reference deployment

**Status:** Accepted

**Decision:** `app/store.py` persists intake, assessment/evidence pack, access
token, Jira mappings, and webhook IDs in SQLite (`DATA_STORE`). All retrieval
is keyed by explicit `assessment_id`; there is no global latest assessment.

**Why:** Restart loss was the largest operational gap. SQLite keeps the local
and portfolio deployment simple while making state durable and preserving
Pydantic as the persistence boundary.

**Rejected:** Keeping process RAM as source of truth; adding Redis as source of
truth; claiming SQLite provides production multi-tenancy.

**Trade-off:** One local SQLite file is appropriate for one deployment and
replica. Horizontal scaling and tenant isolation still require PostgreSQL.

**Limitation:** No `tenant_id`, RLS, per-user ownership, or append-only
`DecisionRecord`. A durable file is not an immutable audit system.

---

## ADR-7 — Pydantic models are the governance contract

**Status:** Accepted

**Decision:** `VendorInput`, `AssessmentResponse`, `EvidenceItem`, `DecisionRecord`, `JiraTicket`, `GovernanceEvidencePack` live in `app/models.py` and are the API response models. Jira fields mirror Cloud REST `POST /rest/api/3/issue` plus an extension field `department` stripped before POST.

**Why:** One schema for UI, OpenAPI, chat context, and (later) CI posting to Jira. Intake validation happens before scoring. The LLM, when used, is constrained to explain JSON that already exists — not to invent a parallel shape.

**Rejected:** Free-form LLM JSON “close enough”; separate TypeScript types that drift from Python.

**Trade-off:** Changing a field is an API break. `department` is not a real Jira field — callers must not send it to Atlassian.

**Limitation:** `JiraFields.description` is a string in our model; outbound convert to Atlassian Document Format. Assignees use `emailAddress`; Jira Cloud often requires `accountId`.

---

## ADR-8 — EvidenceItem is not a Finding

**Status:** Accepted

**Decision:** A **finding** is a conclusion (`critical_findings`). An **evidence item** is a proof record (`control_id`, `document`, `excerpt`, `evidence_status`, `confidence`). Controls produce both.

**Why:** Auditors ask “show the proof,” not “show the chatbot paragraph.” Mixing them makes “missing SOC 2” look like a narrative opinion instead of `evidence_status=missing`.

**Rejected:** Only string lists of findings; treating vendor claims as evidence.

**Trade-off:** More JSON, more UI surface. Status vocabulary (`present | missing | insufficient | declared_unverified`) must stay disciplined.

**Limitation:** Without file upload, many controls stay `missing` or `declared_unverified`. That is correct, not a bug.

---

## ADR-9 — Conservative scoring: missing evidence never reduces risk

**Status:** Accepted

**Decision:** Score starts at 2 and **only increases**. Personal data, no DPA, Art. 9, ADM, incomplete DPIA, unknown transfers add points. Cap 5. Marketing claims and unchecked “we are ISO 27001” do not subtract.

**Why:** The unsafe product is one that greens a vendor because the model believed a website. Residual risk is a property of **evidenced controls**, not vendor self-attestation.

**Rejected:** Bayesian “probably fine”; LLM qualitative residual; lowering score when `has_dpa` is checked without a file.

**Trade-off:** Almost every realistic intake without attachments lands High/Critical or ESCALATE. Demo of OpenAI + employee prompts + no DPA will escalate. Stakeholders may call it “harsh.” It is calibrated for governance, not for vendor onboarding speed.

**Limitation:** Rules v1 are not statistically calibrated. False positives (over-escalation) are preferred to false negatives (silent approve). Keyword inference of personal data (`PERSONAL_KEYWORDS`) can both miss and over-trigger.

---

## ADR-10 — DPIA is a first-class object; DPA is not GDPR

**Status:** Accepted

**Decision:** Intake includes DPIA status, reference, purposes, subjects, Art. 9, transfers, retention, ADM. `PrivacyTriage` is a structured output. `has_dpa` is one control, not the privacy assessment.

**Why:** GDPR is not a checkbox. A signed DPA without DPIA (when required), transfers, or retention is still a gap. Legal’s Jira ticket is explicitly “Review DPA and approve DPIA.”

**Rejected:** Single `has_dpa` boolean as the privacy story.

**Trade-off:** Longer form; optional fields default to inference. Business users may skip the privacy `<details>` — the engine then infers from `data_processed` text.

**Limitation:** Engine does not produce a DPIA document. `privacy_risk_level` declared by the business does not override the score.

---

## ADR-11 — Chat is optional and keyword-mocked without an API key

**Status:** Accepted

**Decision:** No `OPENAI_API_KEY` → `mock_chat()` keyword router. With key → LLM grounded in assessment JSON + intake, system prompt forbids inventing documents or changing the decision. History is sent by the client (last 12 turns server-side for LLM).

**Why:** Demos and air-gapped reviews must work. Chat is a convenience on top of a complete JSON report, not the assessment path.

**Rejected:** Blocking the product on OpenAI; using the LLM for scoring when a key is present.

**Trade-off:** Mock answers are brittle (English keywords). LLM chat can still hallucinate around the JSON if the prompt is ignored — mitigate with temperature 0.2 and “EVIDENCE NOT FOUND” instruction, not with trust.

**Limitation:** Chat history is not stored server-side (privacy + store ADR). F5 restores the **report seed**, not the full conversation unless the client still has it (current UI reseeds from the assessment).

---

## ADR-12 — Same origin, no CORS, secrets in env

**Status:** Accepted

**Decision:** UI and API on one host. Jira and OpenAI credentials from the process environment or a gitignored `.env` (`python-dotenv`). `JIRA_WEBHOOK_SECRET` is required; inbound Jira events must present HMAC-SHA256 of the raw body (`X-Hub-Signature-256`). Approver emails must end with `@JIRA_APPROVER_DOMAIN` (required env; no application default).

**Why:** Local simplicity; webhook is otherwise an unauthenticated write to `DecisionRecord`. Domain check stops a random Gmail from “approving” Legal.

**Rejected:** Public unauthenticated webhook; storing tokens in the repo; CORS-open API.

**Trade-off:** Local tests set the webhook secret and approver domain in `tests/conftest.py`. Production must set both. Domain check is not SSO; anyone who can POST a matching corporate email string can forge an approval if the HMAC secret is stolen.

**Limitation:** `API_ACCESS_TOKEN` can protect `/assess-vendor` and `/chat`, and
per-assessment tokens protect retrieval, but these are shared capabilities—not
user authentication, roles, or tenant authorization. SSO/RACI remains future
work. All runtime state persists in `DATA_STORE`.

---

## ADR-13 — Dry-run Jira by default

**Status:** Accepted

**Decision:** If `JIRA_BASE_URL` and `JIRA_API_TOKEN` are unset, `publish_to_jira()` returns `{published: false}` and the API still returns full `jira_tickets[]`. Outbound failures are recorded on `decision_basis` as `JIRA_PUBLISH:...`, not as a 500 that hides the assessment.

**Why:** Assessment value must not depend on Atlassian availability. Interview/demo without a Jira site.

**Rejected:** Hard-fail assess if Jira is down; fake ticket keys that look real.

**Trade-off:** Users may think tickets were created. The UI health banner states dry-run vs outbound active.

**Limitation:** No retry queue or idempotent create. Real keys are persisted
only when a live outbound create succeeds; dry-run tickets keep placeholders.

---

## ADR-14 — Always open three department gates (for this version)

**Status:** Accepted

**Decision:** `need_legal`, `need_infosec`, and `need_aigov` are all `True` on every assessment. InfoSec is always required because SOC 2 is never in intake. AI Governance is always required because model evals are never in intake.

**Why:** Avoid a path where “no personal data” skips Legal and the vendor ships with no DPO look. Skipping gates recreates engine-APPROVE in disguise.

**Rejected:** Conditional Legal-only-if-personal-data (earlier draft). That under-routed cases where personal data was poorly described.

**Trade-off:** Ticket noise for a clearly internal, non-personal PoC. Governance prefers noise over a skipped DPO.

**Limitation:** Cannot express “Security already reviewed this vendor last quarter” without a future control/evidence object. Re-assessments create duplicate Epics.

---

## ADR-15 — English as the product language

**Status:** Accepted

**Decision:** UI, API messages, findings, Jira summaries, mock chat, and docs are English. Engine decision **codes** stay uppercase English (`REQUIRES REMEDIATION`, etc.) as the machine contract.

**Why:** Shared engineering/governance vocabulary (NIST, ISO, SOC 2, Jira). One language in tickets and audit JSON. Avoid dual-language drift.

**Rejected:** Spanish UI with English API; locale files in v1.

**Trade-off:** Native Spanish Legal users get English console. Codes in the decision bar remain the system of record even if labels are later translated.

**Limitation:** `DONE_STATUSES` still accepts legacy Spanish Jira names (`cerrado`, `aprobado`) so existing boards keep working.

---

## Cross-cutting limitations (honest list)

These are accepted for the current release, not accidental omissions:

| Limitation | Consequence |
|------------|-------------|
| Single SQLite store | Durable on one mounted replica; not tenant-isolated or horizontally scalable |
| Shared API token, no SSO/RBAC | Cannot attribute assess/chat actions to an application user |
| No file attachments | SOC 2 / DPA / DPIA stay unverified |
| No EU AI Act questionnaire | Annex III is a narrative string |
| Keyword PII inference | Can miss or over-flag `data_processed` text |
| Mutable decision row | Workflow updates are durable but not append-only/tamper-evident |
| Jira assignee emails | Cloud often needs `accountId` |
| Chat not a system of record | Trust the decision bar and JSON, not assistant prose |
| Conservative score | High/Critical is the usual demo outcome |
| Not legal advice | Stated in UI, reports, and user guide |

---

## Decision that must not be reversed casually

Any future change **must preserve**:

> Evidence → Control assessment → Risk factors → Deterministic scoring → Governance **triage** → Human Jira gates.

Returning the decision or residual risk to an LLM, or adding engine `APPROVE` without verified evidence **and** closed human gates, would contradict ADR-1 through ADR-4.

---

## Index

All records: **Status: Accepted**.

| Category | ADR | Title |
|----------|-----|--------|
| Product philosophy | 1 | Control layer, not chatbot |
| Product philosophy | 3 | Engine never final APPROVE |
| Product philosophy | 15 | English product language |
| Scoring engine | 2 | Deterministic engine; LLM explains |
| Scoring engine | 8 | Evidence ≠ finding |
| Scoring engine | 9 | Conservative scoring |
| Scoring engine | 10 | DPIA object; DPA ≠ GDPR |
| Jira integration | 4 | Humans approve in Jira |
| Jira integration | 13 | Jira dry-run by default |
| Jira integration | 14 | Always three department gates |
| Persistence and API | 6 | SQLite assessment store |
| Persistence and API | 7 | Pydantic as contract |
| UX and chat | 5 | FastAPI + static UI |
| UX and chat | 11 | Optional / mocked chat |
| Security | 12 | Same origin; env secrets; domain-gated webhook |
