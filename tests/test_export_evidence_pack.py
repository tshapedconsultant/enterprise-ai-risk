"""Evidence pack export used by CI artifact upload."""

import hashlib
import json
from pathlib import Path

from scripts.export_evidence_pack import export_pack


def test_export_evidence_pack_writes_json_and_sha256(tmp_path):
    intake = Path("tests/golden/personal_no_dpa.intake.json")
    out = tmp_path / "artifacts"
    pack_path = export_pack(intake, out, git_sha="test-sha")
    payload = json.loads(pack_path.read_text(encoding="utf-8"))
    assert payload["engine_version"] == "deterministic-rules-v1"
    assert payload["git_sha"] == "test-sha"
    assert payload["vendor"] == "OpenAI"
    assert payload["evidence_pack"]["risk_decision"]["decision"]
    digest_line = (out / "evidence-pack.sha256").read_text(encoding="utf-8").strip()
    expected = hashlib.sha256(pack_path.read_bytes()).hexdigest()
    assert digest_line.startswith(expected)
    assert "evidence-pack.json" in digest_line
