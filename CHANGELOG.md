# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/) and uses
semantic versioning for releases. The FastAPI `version` field is `1.4.0`.

## [Unreleased]

### Changed

- Documentation aligned with shipped behaviour: YAML GDPR triage,
  alignment-only EU AI Act / ISO 42001 / NIST profiles, hash-chained SQLite
  audit, dual Jira webhook secrets, `GET /api/v1/config`, assessment-scoped
  restore/chat, and remaining Demo / PoC limits.

## [1.4.0] - 2026-09-01

### Added

- Durable SQLite persistence for assessments, access tokens, workflow state,
  webhook replay IDs, and Jira issue-to-assessment mappings.
- Optional API bearer/token protection and configurable compliance-framework
  metadata.
- SQLite hash-chained audit events for assessment creation and decision/workflow
  updates, with `GET /api/v1/assessments/{id}/audit`.
- Validated declarative YAML profiles; only GDPR can drive deterministic triage.
- OSS contribution and issue templates.

### Changed

- Assessment retrieval and chat are scoped to an explicit `assessment_id`;
  there is no process-wide “latest assessment”.
- Markdown is the documentation source of truth; DOCX is generated as an
  ignored build artifact (`scripts/export_docx.py`).
- Product examples use the reserved `example.com` domain.
- Jira webhook HMAC secrets support active/previous no-downtime rotation.

### Security

- Mutating console endpoints can be protected with `API_ACCESS_TOKEN`.
- Jira webhooks resolve persisted issue mappings and no longer depend on a
  global in-memory assessment.
