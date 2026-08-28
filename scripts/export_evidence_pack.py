"""
Export a GovernanceEvidencePack JSON for CI artifact storage.

Uses the deterministic engine only (no LLM, no Jira network). Intended for
GitHub Actions upload-artifact and for local audit snapshots.

  python scripts/export_evidence_pack.py \\
    --intake tests/golden/personal_no_dpa.intake.json \\
    --out-dir artifacts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# CLI / CI must satisfy Settings before jira_workflow builds ticket payloads.
os.environ.setdefault("JIRA_WEBHOOK_SECRET", "ci-export-webhook-secret")
os.environ.setdefault("JIRA_APPROVER_DOMAIN", "example.com")
os.environ.setdefault("REQUIRE_ASSESSMENT_AUTH", "false")

from app.models import VendorInput
from app.scoring import ENGINE_VERSION, evaluate


def export_pack(intake_path: Path, out_dir: Path, git_sha: str = "local") -> Path:
    intake = json.loads(intake_path.read_text(encoding="utf-8"))
    assessment = evaluate(VendorInput(**intake))
    if assessment.evidence_pack is None:
        raise SystemExit("evaluate() did not produce evidence_pack")
    payload = {
        "engine_version": ENGINE_VERSION,
        "git_sha": git_sha,
        "intake_file": intake_path.name,
        "assessment_id": assessment.assessment_metadata.assessment_id,
        "vendor": assessment.assessment_metadata.vendor,
        "decision": assessment.assessment_metadata.decision,
        "evidence_pack": assessment.evidence_pack.model_dump(mode="json"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    pack_path = out_dir / "evidence-pack.json"
    raw = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    encoded = raw.encode("utf-8")
    pack_path.write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    (out_dir / "evidence-pack.sha256").write_bytes(f"{digest}  evidence-pack.json\n".encode("utf-8"))
    return pack_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export evidence_pack JSON + SHA-256 sidecar.")
    parser.add_argument(
        "--intake",
        type=Path,
        default=ROOT / "tests" / "golden" / "personal_no_dpa.intake.json",
    )
    parser.add_argument("--out-dir", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--git-sha", default=os.getenv("GITHUB_SHA", "local"))
    args = parser.parse_args()
    path = export_pack(args.intake.resolve(), args.out_dir.resolve(), git_sha=args.git_sha)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
