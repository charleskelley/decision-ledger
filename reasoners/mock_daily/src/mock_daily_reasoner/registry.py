"""The daily mock's registration record.

``MOCK_DAILY_REGISTRATION`` is what the ledger knows about the daily mock,
whichever way it is registered: by its plugin, by a registration file
(``registration.yaml``, generated from this module), or over the API
(``PUT /api/v1/reasoners/mock-daily-reasoner``). All three carry this one
record, so all three serve the same ``registration_version``.

- **Gate set**: the rules gate (``"rules"``), **mandatory** — it runs on
  every decision the mock submits, recorded ones too (DR-29), and its
  verdict joins the combinator with the record's own action, so a rule that
  fires on a recorded account raises its action; a rules gate that fails
  holds the decision, recorded or not. The binding carries no
  configuration: the rules gate declares no binding model, and every
  submission selects the rule set it is decided under (``RULESET_ID``,
  ``rulesets/mock-daily-v1.yaml`` at the member root) in its own gate
  context.
- **Bypass rule** (DR-30): ``within-tolerance`` — an account whose
  settlement drifted less than ``TOLERANCE_PCT`` percent from its target
  (``abs_drift_pct``) is recorded ALLOW. Every other account is an
  exception, submitted in gate mode with no proposal: the rules gate
  decides it, by the size of the drift.
- **Recordable actions**: ALLOW only.
- **Native namespace** (DR-32): the mock names the subject it reconciles,
  an account, as ``(mock-daily-account, account_id)``; the declaration
  carries the UUID5 namespace its account ids derive under.
"""

from __future__ import annotations

from uuid import UUID

import yaml

from core.actions import DecisionAction
from core.gate import Compare
from core.identity import NativeNamespace
from core.observation import (
    BypassRule,
    GateBinding,
    ReasonerRegistration,
    canonical_registration,
)

#: The rule set every submission names for the rules gate
#: (``rulesets/mock-daily-v1.yaml`` at the member root).
RULESET_ID = "mock-daily-v1"

#: The daily mock's binding to the rules gate: mandatory, so it runs on every
#: decision; no configuration (the rules gate declares no binding model).
RULES_GATE_BINDING = GateBinding(gate_id="rules", mandatory=True)

#: Below this ``abs_drift_pct`` an account's settlement is within tolerance
#: and recorded; from it, the account is an exception. The rule set's mildest
#: rule fires from the same number, so a recorded account matches no rule.
TOLERANCE_PCT = 1.0

#: The native namespace of the mock's subjects: an account, keyed by
#: ``account_id``, whose UUID5 ids derive under this declaration.
MOCK_DAILY_ACCOUNT_NAMESPACE = NativeNamespace(
    native_namespace="mock-daily-account",
    namespace_uuid=UUID("935d6f19-46e0-4146-860a-ab1022ecd4e5"),
    entity_type="account",
)

WITHIN_TOLERANCE_RULE = BypassRule(
    rule_id="within-tolerance",
    version="1",
    description=(
        f"abs_drift_pct below {TOLERANCE_PCT}: the account settled within "
        "tolerance of its target; record ALLOW (release the settlement), and "
        "the mandatory rules gate still runs"
    ),
    predicate=Compare(field="feature_set.abs_drift_pct", op="lt", value=TOLERANCE_PCT),
    action=DecisionAction.ALLOW,
)

MOCK_DAILY_REGISTRATION = ReasonerRegistration(
    reasoner_id="mock-daily-reasoner",
    reasoner_name="Mock Daily Reasoner",
    gates=(RULES_GATE_BINDING,),
    bypass_rules=(WITHIN_TOLERANCE_RULE,),
    recordable_actions=frozenset({DecisionAction.ALLOW}),
    native_namespaces=(MOCK_DAILY_ACCOUNT_NAMESPACE,),
)


#: The header of the generated registration file.
_REGISTRATION_FILE_HEADER = """\
# The daily mock's registration, for a DecisionLedger deployment that does
# not install (or leaves out) its plugin. Generated from
# mock_daily_reasoner/registry.py by
#
#   uv run python -m mock_daily_reasoner registration-yaml
#
# (make registration-mock_daily); do not edit it by hand: a test holds it to
# the module's registration_version. Point a ledger at it with
#
#   DECISIONLEDGER_REGISTRATIONS=reasoners/mock_daily/registration.yaml
#
# with the plugin excluded (DECISIONLEDGER_REASONERS naming the plugins to
# keep), or PUT the same entry as JSON to /api/v1/reasoners/mock-daily-reasoner
# with the operator key. The rules gate is bound mandatory: the ledger refuses
# to start, or the PUT, unless it composes the gate, and the gate decides
# every decision of the mock, recorded ones too, under the rule set the
# decision names (mock-daily-v1), which the ledger reads from
# RULES_GATE_RULESETS_DIR when it starts: run `make prepare-rulesets` and
# export RULES_GATE_RULESETS_DIR=$(pwd)/outputs/stage/rulesets first.
"""


def registration_yaml(
    registration: ReasonerRegistration = MOCK_DAILY_REGISTRATION,
) -> str:
    """The registration file a ledger reads (``DECISIONLEDGER_REGISTRATIONS``).

    Args:
        registration: The registration to write.

    Returns:
        YAML holding ``{registrations: [<canonical registration>]}``, under a
        header that says how to use it. It loads back into an equal
        registration, so it has the same ``registration_version``.
    """
    body = yaml.safe_dump(
        {"registrations": [canonical_registration(registration)]},
        sort_keys=False,
        allow_unicode=True,
        width=80,
    )
    return _REGISTRATION_FILE_HEADER + body
