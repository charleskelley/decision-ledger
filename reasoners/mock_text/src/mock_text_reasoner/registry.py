"""The text mock's registration record.

``MOCK_TEXT_REGISTRATION`` is what the ledger knows about the text mock,
whichever way it is registered: by its plugin, by a registration file
(``registration.yaml``, generated from this module), or over the API
(``PUT /api/v1/reasoners/mock-text-reasoner``). All three carry this one
record, so all three serve the same ``registration_version``.

- **Gate set**: the RAG policy gate (``"policy"``), advisory (DR-29). The
  binding names two corpora of different authority (DR-34):
  ``mock-text-regulatory`` at tier 1 (the rules the assistant must follow:
  the mock's ``assets/corpus/regulatory/`` directory, loaded under that id)
  and ``mock-text-practice`` at tier 2 (the bank's practice notes:
  ``assets/corpus/practice/``), both held by the reference provider with
  every passage pinned to its document version. The two disagree on one
  category, financial hardship, so the gate's protocol hold is exercised.
- **No bypass rules**: the mock records nothing. Every text is gated, and
  the classifier's proposal is a floor under the gate's verdict (the final
  action is the more restrictive of the two). ``recordable_actions`` is
  therefore empty.
- **Native namespace** (DR-32): the mock names the subject a message is
  about, the customer who wrote it, as ``(mock-text-customer,
  customer_id)``; the declaration carries the UUID5 namespace its customer
  ids derive under.
- **Enforcement profile**: none. The mock keeps no history per customer, so
  it names no ``novel_entity_feature``.
"""

from __future__ import annotations

from uuid import UUID

import yaml

from core.identity import NativeNamespace
from core.observation import GateBinding, ReasonerRegistration, canonical_registration

#: The regulatory corpus: the mock's ``assets/corpus/regulatory/`` directory,
#: loaded under this id.
MOCK_TEXT_REGULATORY_CORPUS_ID = "mock-text-regulatory"
#: Its authority tier: the highest (tier 1).
MOCK_TEXT_REGULATORY_TIER = 1

#: The practice notes: the mock's ``assets/corpus/practice/`` directory,
#: loaded under this id.
MOCK_TEXT_PRACTICE_CORPUS_ID = "mock-text-practice"
#: Their authority tier: below the regulatory text (tier 2).
MOCK_TEXT_PRACTICE_TIER = 2

#: The text mock's binding to the RAG policy gate (DR-34): two corpora, one
#: per authority tier.
POLICY_GATE_BINDING = GateBinding(
    gate_id="policy",
    mandatory=False,
    config={
        "corpora": [
            {
                "corpus_id": MOCK_TEXT_REGULATORY_CORPUS_ID,
                "provider": "reference",
                "tier": MOCK_TEXT_REGULATORY_TIER,
                "min_provenance": "pinned_version",
            },
            {
                "corpus_id": MOCK_TEXT_PRACTICE_CORPUS_ID,
                "provider": "reference",
                "tier": MOCK_TEXT_PRACTICE_TIER,
                "min_provenance": "pinned_version",
            },
        ],
        "k_per_corpus": 5,
    },
)

#: The native namespace of the mock's subjects: a customer, keyed by
#: ``customer_id``, whose UUID5 ids derive under this declaration.
MOCK_TEXT_CUSTOMER_NAMESPACE = NativeNamespace(
    native_namespace="mock-text-customer",
    namespace_uuid=UUID("a2f8a070-abbb-4956-abdb-127755ca4b30"),
    entity_type="customer",
)

MOCK_TEXT_REGISTRATION = ReasonerRegistration(
    reasoner_id="mock-text-reasoner",
    reasoner_name="Mock Text Reasoner",
    gates=(POLICY_GATE_BINDING,),
    bypass_rules=(),
    recordable_actions=frozenset(),
    native_namespaces=(MOCK_TEXT_CUSTOMER_NAMESPACE,),
)


#: The header of the generated registration file.
_REGISTRATION_FILE_HEADER = """\
# The text mock's registration, for a DecisionLedger deployment that does
# not install (or leaves out) its plugin. Generated from
# mock_text_reasoner/registry.py by
#
#   uv run python -m mock_text_reasoner registration-yaml
#
# (make registration-mock_text); do not edit it by hand: a test holds it to
# the module's registration_version. Point a ledger at it with
#
#   DECISIONLEDGER_REGISTRATIONS=reasoners/mock_text/registration.yaml
#
# with the plugin excluded (DECISIONLEDGER_REASONERS naming the plugins to
# keep), or PUT the same entry as JSON to /api/v1/reasoners/mock-text-reasoner
# with the operator key. A ledger that composes the RAG policy gate needs the
# mock's prompts and corpora under its asset root
# (POLICY_RAG_GATE_ASSETS_ROOT/mock-text-reasoner/, a copy of
# src/mock_text_reasoner/assets/) and both corpora the binding names loaded
# before it starts, one loader call each: corpus/regulatory/ as
# mock-text-regulatory and corpus/practice/ as mock-text-practice.
"""


def registration_yaml(
    registration: ReasonerRegistration = MOCK_TEXT_REGISTRATION,
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
