"""Compliance framework catalog and evidence-pack filtering."""

import pytest

from app.frameworks import load_rule_profiles, parse_frameworks, stamp_assessment
from app.models import VendorInput
from app.scoring import evaluate


def _assessment():
    return evaluate(
        VendorInput(
            vendor_name="FrameworkCo",
            service_description="API",
            intended_use="Internal assistant",
            data_processed="No personal data",
            has_dpa=False,
        )
    )


def test_parse_frameworks_defaults_aliases_and_mandatory_gdpr():
    assert parse_frameworks("") == [
        "gdpr",
        "eu_ai_act",
        "iso_42001",
        "nist_ai_rmf",
    ]
    assert parse_frameworks("nist,iso42001") == [
        "gdpr",
        "nist_ai_rmf",
        "iso_42001",
    ]
    with pytest.raises(ValueError, match="Unknown COMPLIANCE_FRAMEWORKS"):
        parse_frameworks("unknown")


def test_stamp_assessment_filters_optional_alignment_sections():
    assessment = _assessment()
    stamp_assessment(assessment, frameworks=["nist_ai_rmf"])

    assert assessment.assessment_metadata.applicable_frameworks == [
        "gdpr",
        "nist_ai_rmf",
    ]
    assert assessment.framework_alignment.nist_ai_rmf_gaps
    assert assessment.framework_alignment.iso_42001_gaps == []
    pack = assessment.evidence_pack
    assert pack is not None
    assert pack.gdpr_dpia
    assert pack.nist_ai_rmf_mapping
    assert pack.iso_42001_mapping == []
    assert pack.eu_ai_act_applicability.startswith("Out of scope")


def test_rule_profiles_keep_only_gdpr_decision_capable():
    profiles = load_rule_profiles()
    assert profiles["gdpr"].mode == "decision"
    assert profiles["gdpr"].decision_rules
    for framework_id in ("eu_ai_act", "iso_42001", "nist_ai_rmf"):
        assert profiles[framework_id].mode == "alignment"
        assert profiles[framework_id].decision_rules == []


def test_invalid_alignment_decision_profile_fails_closed(tmp_path):
    (tmp_path / "eu_ai_act.yaml").write_text(
        """
id: eu_ai_act
name: invalid
mode: alignment
engine_version: metadata-v1
decision_rules:
  - id: INVALID
    priority: 1
    decision: PENDING REVIEW
    default: true
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="alignment-only profiles cannot"):
        load_rule_profiles(["eu_ai_act"], tmp_path)
