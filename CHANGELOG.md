# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/) and uses
semantic versioning for releases.

## [Unreleased]

### Added

- Durable SQLite persistence for assessments, access tokens, workflow state,
  webhook replay IDs, and Jira issue-to-assessment mappings.
- Optional API bearer/token protection and configurable compliance-framework
  metadata.
- OSS contribution and issue templates.

### Changed

- Assessment retrieval and chat are scoped to an explicit `assessment_id`;
  there is no process-wide “latest assessment”.
- Markdown is the documentation source of truth; DOCX is generated as an
  ignored build artifact.
- Product examples use the reserved `example.com` domain.

### Security

- Mutating console endpoints can be protected with `API_ACCESS_TOKEN`.
- Jira webhooks resolve persisted issue mappings and no longer depend on a
  global in-memory assessment.
