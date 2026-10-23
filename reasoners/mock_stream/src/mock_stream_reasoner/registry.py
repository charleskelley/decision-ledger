"""The stream mock's registration record.

``MOCK_STREAM_REGISTRATION`` is what the ledger knows about the stream mock,
whichever way it is registered: by its plugin, by a registration file
(``registration.yaml``, generated from this module), or over the API
(``PUT /api/v1/reasoners/mock-stream-reasoner``). All three carry this one
record, so all three serve the same ``registration_version``.

- **Gate set**: the RAG policy gate (``"policy"``), advisory — it runs when
  the mock asks the ledger to gate a transfer (DR-29). The binding names the
  corpus the gate reads (DR-34): ``mock-stream-practices`` (the mock's
  ``assets/corpus/`` directory, loaded under that id) at tier 2, practice
  notes rather than regulation, held by the reference provider with every
  passage pinned to its document version.
- **Bypass rules** (DR-30), one per recordable action: ``steady-activity``
  (``activity_score`` below ``STEADY_THRESHOLD`` → record ALLOW) and
  ``activity-surge`` (above ``SURGE_THRESHOLD`` → record BLOCK). Every score
  between them asks for the gate.
- **Recordable actions**: ALLOW and BLOCK.
- **Native namespace** (DR-32): the mock names the subject it scores, an
  account, as ``(mock-stream-account, account_id)``; the declaration carries
  the UUID5 namespace its account ids derive under.
- **Enforcement profile**: Tier 3 fires on ``thin_history`` (an account the
  stream has seen fewer than ``THIN_HISTORY_EVENTS`` transfers from), on
  gated decisions: a fresh account the mock asks the gate about is held for
  review whatever the gate permits.
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

#: The corpus the stream mock's binding to the policy gate names: its
#: ``assets/corpus/`` directory, loaded under this id.
MOCK_STREAM_CORPUS_ID = "mock-stream-practices"

#: The authority tier of that corpus: practice notes, below regulation
#: (tier 1 is the highest authority).
MOCK_STREAM_CORPUS_TIER = 2

#: The stream mock's binding to the RAG policy gate (DR-34): one corpus at
#: tier 2.
POLICY_GATE_BINDING = GateBinding(
    gate_id="policy",
    mandatory=False,
    config={
        "corpora": [
            {
                "corpus_id": MOCK_STREAM_CORPUS_ID,
                "provider": "reference",
                "tier": MOCK_STREAM_CORPUS_TIER,
                "min_provenance": "pinned_version",
            }
        ],
        "k_per_corpus": 5,
    },
)

#: Below this ``activity_score`` the mock records ALLOW.
STEADY_THRESHOLD = 0.2
#: Above this ``activity_score`` the mock records BLOCK.
SURGE_THRESHOLD = 0.9

#: The native namespace of the mock's subjects: an account, keyed by
#: ``account_id``, whose UUID5 ids derive under this declaration.
MOCK_STREAM_ACCOUNT_NAMESPACE = NativeNamespace(
    native_namespace="mock-stream-account",
    namespace_uuid=UUID("6a6646c1-01bb-40f2-949c-d641b1ff6733"),
    entity_type="account",
)

STEADY_ACTIVITY_RULE = BypassRule(
    rule_id="steady-activity",
    version="1",
    description=(
        f"activity_score below {STEADY_THRESHOLD}: the transfer fits the "
        "account's recent activity; record ALLOW without the policy gate"
    ),
    predicate=Compare(field="label_value", op="lt", value=STEADY_THRESHOLD),
    action=DecisionAction.ALLOW,
)

ACTIVITY_SURGE_RULE = BypassRule(
    rule_id="activity-surge",
    version="1",
    description=(
        f"activity_score above {SURGE_THRESHOLD}: the account is sending a "
        "burst of transfers far above its baseline; record BLOCK without the "
        "policy gate"
    ),
    predicate=Compare(field="label_value", op="gt", value=SURGE_THRESHOLD),
    action=DecisionAction.BLOCK,
)

MOCK_STREAM_REGISTRATION = ReasonerRegistration(
    reasoner_id="mock-stream-reasoner",
    reasoner_name="Mock Stream Reasoner",
    gates=(POLICY_GATE_BINDING,),
    bypass_rules=(STEADY_ACTIVITY_RULE, ACTIVITY_SURGE_RULE),
    recordable_actions=frozenset({DecisionAction.ALLOW, DecisionAction.BLOCK}),
    native_namespaces=(MOCK_STREAM_ACCOUNT_NAMESPACE,),
    novel_entity_feature="thin_history",
)


#: The header of the generated registration file.
_REGISTRATION_FILE_HEADER = """\
# The stream mock's registration, for a DecisionLedger deployment that does
# not install (or leaves out) its plugin. Generated from
# mock_stream_reasoner/registry.py by
#
#   uv run python -m mock_stream_reasoner registration-yaml
#
# (make registration-mock_stream); do not edit it by hand: a test holds it to
# the module's registration_version. Point a ledger at it with
#
#   DECISIONLEDGER_REGISTRATIONS=reasoners/mock_stream/registration.yaml
#
# with the plugin excluded (DECISIONLEDGER_REASONERS naming the plugins to
# keep), or PUT the same entry as JSON to /api/v1/reasoners/mock-stream-reasoner
# with the operator key. A ledger that composes the RAG policy gate needs the
# mock's prompts and corpus under its asset root
# (POLICY_RAG_GATE_ASSETS_ROOT/mock-stream-reasoner/, a copy of
# src/mock_stream_reasoner/assets/) and the corpus loaded as
# mock-stream-practices, the corpus the binding names, before it starts.
"""


def registration_yaml(
    registration: ReasonerRegistration = MOCK_STREAM_REGISTRATION,
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
