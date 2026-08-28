"""Regression snapshots for deterministic-rules-v1.

If a rule change is intentional, regenerate goldens:

  python tests/test_regression.py --write

Otherwise a failing test means an old case would get a different triage — stop and review.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import VendorInput
from app.scoring import ENGINE_VERSION, evaluate

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

# Fields that must stay stable across refactors (not timestamps).
SNAPSHOT_KEYS = (
    "decision",
    "residual_risk",
    "risk_score",
    "workflow_status",
    "engine_version",
    "decision_basis",
    "controls_triggered",
    "ticket_departments",
)


def _snapshot(result) -> dict:
    record = result.decision_record
    return {
        "decision": result.assessment_metadata.decision,
        "residual_risk": result.assessment_metadata.overall_residual_risk,
        "risk_score": record.risk_score,
        "workflow_status": record.workflow_status,
        "engine_version": record.model_version,
        "decision_basis": record.decision_basis,
        "controls_triggered": record.controls_triggered,
        "ticket_departments": [t.fields.department for t in result.jira_tickets],
    }


def _cases() -> list[Path]:
    return sorted(GOLDEN_DIR.glob("*.intake.json"))


def test_golden_cases_unchanged():
    assert _cases(), "No golden intake files in tests/golden/"
    mismatches = []
    for intake_path in _cases():
        expected_path = intake_path.with_name(intake_path.name.replace(".intake.json", ".expected.json"))
        intake = json.loads(intake_path.read_text(encoding="utf-8"))
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        actual = _snapshot(evaluate(VendorInput(**intake)))
        assert actual["engine_version"] == ENGINE_VERSION
        if actual != expected:
            mismatches.append(intake_path.stem)
            for key in SNAPSHOT_KEYS:
                if actual.get(key) != expected.get(key):
                    print(f"{intake_path.name} {key}: {expected.get(key)!r} -> {actual.get(key)!r}")
    assert not mismatches, f"Regression in {mismatches}. If intentional, run: python tests/test_regression.py --write"


def write_goldens() -> None:
    GOLDEN_DIR.mkdir(exist_ok=True)
    for intake_path in _cases():
        intake = json.loads(intake_path.read_text(encoding="utf-8"))
        expected_path = intake_path.with_name(intake_path.name.replace(".intake.json", ".expected.json"))
        payload = _snapshot(evaluate(VendorInput(**intake)))
        expected_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {expected_path.name}")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_goldens()
    else:
        raise SystemExit("Pass --write to regenerate expected.json files")
