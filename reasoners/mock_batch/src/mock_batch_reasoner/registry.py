"""The batch mock's registration record.

``MOCK_BATCH_REGISTRATION`` is what the ledger knows about the batch mock,
whichever way it is registered: by its plugin, by a registration file
(``registration.yaml``, generated from this module), or over the API
(``PUT /api/v1/reasoners/mock-batch-reasoner``). All three carry this one
record, so all three serve the same ``registration_version``.

- **Gate set**: the rules gate (``"rules"``), advisory — it runs when the
  mock submits an exception in gate mode (DR-29), and never on a recorded
  row. The binding carries no configuration: the rules gate declares no
  binding model, and every submission selects the rule set it is decided
  under (``RULESET_ID``, ``rulesets/mock-batch-v1.yaml`` at the member root)
  in its own gate context.
- **Bypass rules** (DR-30), one per recordable action, over the
  ``delinquency_score`` bands of an account no one flagged:
  ``current-account`` (below ``WATCH_THRESHOLD`` → record ALLOW),
  ``watch-list`` (up to ``HOLD_THRESHOLD`` → record CHALLENGE),
  ``delinquent-hold`` (up to ``CHARGE_OFF_THRESHOLD`` → record HOLD), and
  ``charge-off`` (from ``CHARGE_OFF_THRESHOLD`` → record BLOCK). The bands
  are disjoint and cover every score; the gap is deliberate: a row whose
  ``needs_review`` feature is set matches no rule, so it is an exception the
  mock submits in gate mode.
- **Recordable actions**: all four.
- **Native namespace** (DR-32): the mock names the subject it scores, an
  account, as ``(mock-batch-account, account_id)``; the declaration carries
  the UUID5 namespace its account ids derive under.
"""

from __future__ import annotations

from uuid import UUID

import yaml

from core.actions import DecisionAction
from core.gate import AllOf, Compare
from core.identity import NativeNamespace
from core.observation import (
    BypassRule,
    GateBinding,
    ReasonerRegistration,
    canonical_registration,
)

#: The rule set every submission names for the rules gate
#: (``rulesets/mock-batch-v1.yaml`` at the member root).
RULESET_ID = "mock-batch-v1"

#: The batch mock's binding to the rules gate: advisory, with no
#: configuration (the rules gate declares no binding model).
RULES_GATE_BINDING = GateBinding(gate_id="rules", mandatory=False)

#: From this ``delinquency_score`` an unflagged account is on the watch list.
WATCH_THRESHOLD = 0.3
#: From this score an unflagged account is held as delinquent.
HOLD_THRESHOLD = 0.6
#: From this score an unflagged account is blocked for charge-off.
CHARGE_OFF_THRESHOLD = 0.85

#: The native namespace of the mock's subjects: an account, keyed by
#: ``account_id``, whose UUID5 ids derive under this declaration.
MOCK_BATCH_ACCOUNT_NAMESPACE = NativeNamespace(
    native_namespace="mock-batch-account",
    namespace_uuid=UUID("f9de1e07-b713-4718-bef9-7f78585b0608"),
    entity_type="account",
)

#: Every bypass rule requires it: a flagged row matches none of them.
_NOT_FLAGGED = Compare(field="feature_set.needs_review", op="eq", value=False)

CURRENT_ACCOUNT_RULE = BypassRule(
    rule_id="current-account",
    version="1",
    description=(
        f"delinquency_score below {WATCH_THRESHOLD} and no review flag: the "
        "account is current; record ALLOW without the rules gate"
    ),
    predicate=AllOf(
        predicates=[
            Compare(field="label_value", op="lt", value=WATCH_THRESHOLD),
            _NOT_FLAGGED,
        ]
    ),
    action=DecisionAction.ALLOW,
)

WATCH_LIST_RULE = BypassRule(
    rule_id="watch-list",
    version="1",
    description=(
        f"delinquency_score from {WATCH_THRESHOLD} to below {HOLD_THRESHOLD} "
        "and no review flag: the account is falling behind; record CHALLENGE "
        "(contact the holder) without the rules gate"
    ),
    predicate=AllOf(
        predicates=[
            Compare(field="label_value", op="ge", value=WATCH_THRESHOLD),
            Compare(field="label_value", op="lt", value=HOLD_THRESHOLD),
            _NOT_FLAGGED,
        ]
    ),
    action=DecisionAction.CHALLENGE,
)

DELINQUENT_HOLD_RULE = BypassRule(
    rule_id="delinquent-hold",
    version="1",
    description=(
        f"delinquency_score from {HOLD_THRESHOLD} to below "
        f"{CHARGE_OFF_THRESHOLD} and no review flag: the account is "
        "delinquent; record HOLD (new spending held for review) without the "
        "rules gate"
    ),
    predicate=AllOf(
        predicates=[
            Compare(field="label_value", op="ge", value=HOLD_THRESHOLD),
            Compare(field="label_value", op="lt", value=CHARGE_OFF_THRESHOLD),
            _NOT_FLAGGED,
        ]
    ),
    action=DecisionAction.HOLD,
)

CHARGE_OFF_RULE = BypassRule(
    rule_id="charge-off",
    version="1",
    description=(
        f"delinquency_score from {CHARGE_OFF_THRESHOLD} and no review flag: "
        "the account is headed for charge-off; record BLOCK without the rules "
        "gate"
    ),
    predicate=AllOf(
        predicates=[
            Compare(field="label_value", op="ge", value=CHARGE_OFF_THRESHOLD),
            _NOT_FLAGGED,
        ]
    ),
    action=DecisionAction.BLOCK,
)

MOCK_BATCH_REGISTRATION = ReasonerRegistration(
    reasoner_id="mock-batch-reasoner",
    reasoner_name="Mock Batch Reasoner",
    gates=(RULES_GATE_BINDING,),
    bypass_rules=(
        CURRENT_ACCOUNT_RULE,
        WATCH_LIST_RULE,
        DELINQUENT_HOLD_RULE,
        CHARGE_OFF_RULE,
    ),
    recordable_actions=frozenset(DecisionAction),
    native_namespaces=(MOCK_BATCH_ACCOUNT_NAMESPACE,),
)


#: The header of the generated registration file.
_REGISTRATION_FILE_HEADER = """\
# The batch mock's registration, for a DecisionLedger deployment that does
# not install (or leaves out) its plugin. Generated from
# mock_batch_reasoner/registry.py by
#
#   uv run python -m mock_batch_reasoner registration-yaml
#
# (make registration-mock_batch); do not edit it by hand: a test holds it to
# the module's registration_version. Point a ledger at it with
#
#   DECISIONLEDGER_REGISTRATIONS=reasoners/mock_batch/registration.yaml
#
# with the plugin excluded (DECISIONLEDGER_REASONERS naming the plugins to
# keep), or PUT the same entry as JSON to /api/v1/reasoners/mock-batch-reasoner
# with the operator key. The rules gate decides the mock's exceptions under
# the rule set they name (mock-batch-v1), which the ledger reads from
# RULES_GATE_RULESETS_DIR when it starts: run `make prepare-rulesets` and
# export RULES_GATE_RULESETS_DIR=$(pwd)/outputs/stage/rulesets first.
"""


def registration_yaml(
    registration: ReasonerRegistration = MOCK_BATCH_REGISTRATION,
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
