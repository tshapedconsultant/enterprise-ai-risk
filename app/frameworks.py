"""
Compliance framework catalog.

v1 scoring rules in app/scoring.py are GDPR-anchored (DPA, DPIA, Art. 9, Art. 22).
NIST AI RMF, ISO/IEC 42001, and the EU AI Act are alignment tags on the same
controls — they are not separate engines. COMPLIANCE_FRAMEWORKS selects which
frameworks appear in metadata, health, and the evidence pack.

This is honest: toggling a framework does not invent a new rule set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence

from app.config import get_settings
from app.models import AssessmentResponse, GovernanceEvidencePack


@dataclass(frozen=True)
class FrameworkSpec:
    id: str
    name: str
    role: str  # "enforced" | "alignment"
    summary: str


CATALOG: Dict[str, FrameworkSpec] = {
    "gdpr": FrameworkSpec(
        id="gdpr",
        name="GDPR",
        role="enforced",
        summary="DPA, DPIA, special categories (Art. 9), ADM (Art. 22), Chapter V transfers.",
    ),
    "eu_ai_act": FrameworkSpec(
        id="eu_ai_act",
        name="EU AI Act",
        role="alignment",
        summary="Narrative Annex III / deployer applicability — not a structured high-risk questionnaire.",
    ),
    "iso_42001": FrameworkSpec(
        id="iso_42001",
        name="ISO/IEC 42001",
        role="alignment",
        summary="Control IDs mapped on each ControlAssessment (e.g. 6.1.2, 8.2).",
    ),
    "nist_ai_rmf": FrameworkSpec(
        id="nist_ai_rmf",
        name="NIST AI RMF",
        role="alignment",
        summary="GOVERN / MAP / MEASURE / MANAGE tags on the same controls.",
    ),
}

DEFAULT_FRAMEWORKS = ("gdpr", "eu_ai_act", "iso_42001", "nist_ai_rmf")


def parse_frameworks(raw: str) -> List[str]:
    """Return enabled IDs; GDPR stays present because it is the v1 rule engine."""
    if not raw or not raw.strip():
        return list(DEFAULT_FRAMEWORKS)
    seen: List[str] = []
    unknown: List[str] = []
    for part in raw.split(","):
        fid = part.strip().lower().replace(" ", "_").replace("-", "_")
        aliases = {
            "euai": "eu_ai_act",
            "eu_ai": "eu_ai_act",
            "aiact": "eu_ai_act",
            "iso": "iso_42001",
            "iso42001": "iso_42001",
            "nist": "nist_ai_rmf",
            "nist_rmf": "nist_ai_rmf",
        }
        fid = aliases.get(fid, fid)
        if fid in CATALOG and fid not in seen:
            seen.append(fid)
        elif fid not in CATALOG:
            unknown.append(part.strip())
    if unknown:
        raise ValueError(
            "Unknown COMPLIANCE_FRAMEWORKS: " + ", ".join(unknown)
        )
    if "gdpr" not in seen:
        seen.insert(0, "gdpr")
    return seen


def enabled_frameworks() -> List[str]:
    return parse_frameworks(get_settings().compliance_frameworks)


def framework_status() -> dict:
    enabled = enabled_frameworks()
    return {
        "enabled": enabled,
        "enforced": [fid for fid in enabled if CATALOG[fid].role == "enforced"],
        "alignment_only": [fid for fid in enabled if CATALOG[fid].role == "alignment"],
        "catalog": {
            fid: {"name": spec.name, "role": spec.role, "summary": spec.summary}
            for fid, spec in CATALOG.items()
        },
    }


def stamp_assessment(assessment: AssessmentResponse, frameworks: Sequence[str] | None = None) -> None:
    """Attach enabled frameworks and drop out-of-scope pack sections. Mutates in place."""
    enabled = list(frameworks if frameworks is not None else enabled_frameworks())
    if "gdpr" not in enabled:
        enabled.insert(0, "gdpr")
    assessment.assessment_metadata.applicable_frameworks = enabled
    if "nist_ai_rmf" not in enabled:
        assessment.framework_alignment.nist_ai_rmf_gaps = []
    if "iso_42001" not in enabled:
        assessment.framework_alignment.iso_42001_gaps = []
    pack = assessment.evidence_pack
    if pack is None:
        return
    assessment.evidence_pack = filter_evidence_pack(pack, enabled)


def filter_evidence_pack(pack: GovernanceEvidencePack, enabled: Sequence[str]) -> GovernanceEvidencePack:
    data = pack.model_dump()
    if "gdpr" not in enabled:
        data["gdpr_dpia"] = {"out_of_scope": True, "reason": "gdpr not in COMPLIANCE_FRAMEWORKS"}
    if "eu_ai_act" not in enabled:
        data["eu_ai_act_applicability"] = "Out of scope (eu_ai_act not enabled)."
    if "nist_ai_rmf" not in enabled:
        data["nist_ai_rmf_mapping"] = []
    if "iso_42001" not in enabled:
        data["iso_42001_mapping"] = []
    return GovernanceEvidencePack.model_validate(data)
