# Contributing

Contributions are welcome through focused issues and pull requests.

## Development

```bash
python -m venv .venv
pip install -r requirements.txt -r requirements-dev.txt
pytest --cov=app --cov-report=term-missing --cov-fail-under=80
```

Copy `.env.example` to `.env` for local execution. Never commit `.env`, API
tokens, Jira credentials, SQLite databases, logs, generated DOCX files, or
evidence artifacts containing real personal data.

## Pull requests

1. Open or reference an issue for behavior changes.
2. Keep scoring deterministic: the LLM must not set risk or decisions. GDPR
   triage lives in `rules/gdpr.yaml`; alignment YAML must not gain decision rules.
3. Add tests for API, persistence, audit-chain, security, or rule changes.
4. Update golden snapshots only when a scoring change is intentional and
   explained in the PR.
5. Update Markdown documentation; do not hand-edit generated DOCX files.
6. Ensure CI-equivalent tests and the Docker build pass.

## Security

Do not open a public issue for a vulnerability that could expose assessment
data or credentials. Report privately via
[GitHub Security Advisories](https://github.com/tshapedconsultant/enterprise-ai-risk/security/advisories/new).
The threat model is in [docs/SECURITY.md](docs/SECURITY.md).

By contributing, you agree that your contribution is licensed under the
repository's [MIT License](LICENSE).
