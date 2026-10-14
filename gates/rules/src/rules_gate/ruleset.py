"""Rule sets — the rules gate's registered configuration — and its config model.

A rule set is a versioned, ordered list of rules registered for one reasoner
(its tenant). Each rule pairs a predicate from the framework's closed
predicate language (``core.gate.predicate``) with the action it permits and
any controls it requires. Rule sets live in YAML files the deployment mounts
(``RULES_GATE_RULESETS_DIR``), one rule set per file::

    ruleset_id: widget-guardrails
    version: "1.0.0"
    reasoner_id: example-reasoner
    description: Hold thin-history widgets; block extreme scores.
    default_action: ALLOW
    rules:
      - rule_id: thin-history-hold
        when: {kind: compare, field: feature_set.thin_history, op: eq, value: true}
        action: HOLD
        required_controls: [calibration_review]
      - rule_id: extreme-score-block
        when: {kind: compare, field: label_value, op: gt, value: 0.9}
        action: BLOCK

A reasoner selects a registered rule set by id through its gate context
(``gate_config = {"ruleset_id": "widget-guardrails"}``, validated by
``RulesGateConfig``); it never submits rules itself, so the rules a decision
is held to are the deployment's, not the submitter's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.actions import DecisionAction
from core.gate.predicate import Predicate

if TYPE_CHECKING:
    from pathlib import Path


class Rule(BaseModel):
    """One declarative rule: when the predicate holds, permit ``action``.

    Attributes:
        rule_id: Identifier unique within its rule set; recorded in the
            gate's evidence when the rule matches.
        description: Human-readable intent of the rule.
        when: Predicate over the reasoner's ``label_value`` / ``feature_set``.
        action: The action the rule permits when it matches.
        required_controls: Controls the decision must carry when the rule
            matches (passed through on the gate's verdict).
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1)
    description: str = ""
    when: Predicate
    action: DecisionAction
    required_controls: tuple[str, ...] = ()


class RuleSet(BaseModel):
    """A versioned rule set registered for one reasoner.

    Evaluation: every rule is evaluated in order; the verdict permits the
    actions of all matching rules (enforcement takes the most conservative),
    or ``default_action`` when none matches.

    Attributes:
        ruleset_id: Identifier a gate context selects the rule set by.
        version: Version label recorded in every decision the set governs.
        reasoner_id: The tenant this rule set belongs to. The gate refuses
            (fails closed) when another reasoner selects it.
        description: Human-readable intent of the rule set.
        default_action: The action permitted when no rule matches.
        rules: The rules, in evaluation order. At least one.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    ruleset_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    reasoner_id: str = Field(min_length=1)
    description: str = ""
    default_action: DecisionAction
    rules: tuple[Rule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _rule_ids_are_unique(self) -> RuleSet:
        """A rule id identifies one rule in audit records."""
        ids = [rule.rule_id for rule in self.rules]
        duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
        if duplicates:
            msg = f"duplicate rule_id(s) in rule set {self.ruleset_id!r}: {duplicates}"
            raise ValueError(msg)
        return self

    @property
    def ref(self) -> str:
        """``<ruleset_id>@<version>`` — the source id the gate cites."""
        return f"{self.ruleset_id}@{self.version}"


class RulesGateConfig(BaseModel):
    """The rules gate's ``config_model``: what a gate context may carry.

    The framework validates ``GateContext.gate_config`` against this model at
    intake, so a malformed configuration is rejected before any gate runs.

    Attributes:
        ruleset_id: The registered rule set to evaluate the decision against.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    ruleset_id: str = Field(min_length=1)


def parse_ruleset(data: object) -> RuleSet:
    """Validate a rule set from JSON-shaped data (e.g. parsed YAML).

    Validation is lax at this boundary only so that YAML scalars and
    sequences map onto the strict models (``"HOLD"`` onto
    ``DecisionAction.HOLD``, a list onto a tuple); the models themselves stay
    strict for in-process construction.

    Args:
        data: The mapping loaded from one rule-set file.

    Returns:
        The validated ``RuleSet``.

    Raises:
        pydantic.ValidationError: The data is not a well-formed rule set.
    """
    return RuleSet.model_validate(data, strict=False)


def load_rulesets(directory: Path) -> dict[str, RuleSet]:
    """Load every ``*.yaml`` / ``*.yml`` rule set under ``directory``.

    Args:
        directory: Directory of rule-set files, one rule set per file.

    Returns:
        Rule sets keyed by ``ruleset_id``, in file-name order.

    Raises:
        FileNotFoundError: ``directory`` does not exist.
        ValueError: Two files declare the same ``ruleset_id``, or a file is
            not a YAML mapping.
        pydantic.ValidationError: A file is not a well-formed rule set.
    """
    if not directory.is_dir():
        msg = f"Rule-set directory not found: {directory}"
        raise FileNotFoundError(msg)
    paths = sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])
    rulesets: dict[str, RuleSet] = {}
    origins: dict[str, str] = {}
    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            msg = f"Rule-set file {path} must contain a YAML mapping"
            raise ValueError(msg)
        ruleset = parse_ruleset(data)
        if ruleset.ruleset_id in rulesets:
            msg = (
                f"Duplicate ruleset_id {ruleset.ruleset_id!r} in {path.name} "
                f"(already loaded from {origins[ruleset.ruleset_id]})"
            )
            raise ValueError(msg)
        rulesets[ruleset.ruleset_id] = ruleset
        origins[ruleset.ruleset_id] = path.name
    return rulesets
