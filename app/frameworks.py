"""
Compliance framework catalog.

The validated rules/gdpr.yaml profile drives GDPR-anchored triage (DPA, DPIA,
Art. 9, and score thresholds). NIST AI RMF, ISO/IEC 42001, and the EU AI Act
profiles are alignment metadata on the same controls—they cannot contain
decision rules. COMPLIANCE_FRAMEWORKS selects which profiles appear in
metadata, health, and the evidence pack.

Enabling alignment metadata does not invent a framework-specific engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence

import yaml
from pydantic import BaseModel, Field, model_validator

from app.config import get_settings
from app.enums import EngineDecision
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
DECISIONS = {decision.value for decision in EngineDecision}
DECISION_FACTS = {
    "personal",
    "has_dpa",
    "special_category",
    "dpia_ok",
    "need_dpia",
    "score",
}


class DecisionRule(BaseModel):
    id: str = Field(min_length=1)
    priority: int = Field(ge=0)
    decision: str
    when: Dict[str, bool | int | str] = Field(default_factory=dict)
    any_false: List[str] = Field(default_factory=list)
    gte: Dict[str, float] = Field(default_factory=dict)
    default: bool = False

    @model_validator(mode="after")
    def validate_rule(self):
        unknown = (set(self.when) | set(self.any_false) | set(self.gte)) - DECISION_FACTS
        if unknown:
            raise ValueError(f"unknown decision facts: {sorted(unknown)}")
        if self.decision not in DECISIONS:
            raise ValueError(f"unsupported engine decision: {self.decision}")
        has_conditions = bool(self.when or self.any_false or self.gte)
        if self.default == has_conditions:
            raise ValueError("a rule must be default or conditional, never both/neither")
        return self


class RuleProfile(BaseModel):
    id: str
    name: str
    mode: str
    engine_version: str
    decision_rules: List[DecisionRule] = Field(default_factory=list)
    mappings: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_profile(self):
        if self.mode not in {"decision", "alignment"}:
            raise ValueError("profile mode must be decision or alignment")
        if self.mode == "alignment" and self.decision_rules:
            raise ValueError("alignment-only profiles cannot contain decision rules")
        if self.mode == "decision":
            defaults = [rule for rule in self.decision_rules if rule.default]
            if len(defaults) != 1:
                raise ValueError("decision profile must contain exactly one default rule")
            priorities = [rule.priority for rule in self.decision_rules]
            if len(priorities) != len(set(priorities)):
                raise ValueError("decision rule priorities must be unique")
            conditional_priorities = [
                rule.priority for rule in self.decision_rules if not rule.default
            ]
            if conditional_priorities and defaults[0].priority <= max(conditional_priorities):
                raise ValueError("the default decision rule must have the last priority")
        return self


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


def profiles_directory() -> Path:
    configured = get_settings().framework_rules_dir
    return (
        Path(configured)
        if configured
        else Path(__file__).resolve().parent.parent / "rules"
    )


def load_rule_profiles(
    enabled: Sequence[str] | None = None,
    directory: Path | None = None,
) -> Dict[str, RuleProfile]:
    """Load and validate enabled YAML profiles. Missing/invalid config fails closed."""
    profile_ids = list(enabled if enabled is not None else enabled_frameworks())
    root = directory or profiles_directory()
    profiles: Dict[str, RuleProfile] = {}
    for framework_id in profile_ids:
        path = root / f"{framework_id}.yaml"
        if not path.is_file():
            raise ValueError(f"Missing framework rule profile: {path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            profile = RuleProfile.model_validate(raw)
        except Exception as exc:
            raise ValueError(f"Invalid framework rule profile {path}: {exc}") from exc
        if profile.id != framework_id:
            raise ValueError(
                f"Framework profile id {profile.id!r} does not match {framework_id!r}"
            )
        if framework_id == "gdpr" and profile.mode != "decision":
            raise ValueError("GDPR must be the deterministic decision profile")
        if framework_id == "gdpr" and profile.engine_version != "deterministic-rules-v1":
            raise ValueError("GDPR profile engine_version must be deterministic-rules-v1")
        if framework_id != "gdpr" and profile.mode != "alignment":
            raise ValueError(
                f"{framework_id} is alignment-only and cannot affect decisions"
            )
        profiles[framework_id] = profile
    return profiles


def _rule_matches(rule: DecisionRule, facts: Dict[str, object]) -> bool:
    if rule.default:
        return True
    if any(facts.get(key) != expected for key, expected in rule.when.items()):
        return False
    if rule.any_false and not any(not bool(facts.get(key)) for key in rule.any_false):
        return False
    if any(float(facts.get(key, 0)) < minimum for key, minimum in rule.gte.items()):
        return False
    return True


def evaluate_gdpr_decision(facts: Dict[str, object]) -> tuple[str, str]:
    """Evaluate the validated GDPR profile; alignment profiles are never consulted."""
    profile = load_rule_profiles(["gdpr"])["gdpr"]
    for rule in sorted(profile.decision_rules, key=lambda item: item.priority):
        if _rule_matches(rule, facts):
            return rule.decision, rule.id
    raise RuntimeError("GDPR decision profile has no matching/default rule")


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
