"""The daily mock's plugin — what a ledger deployment that installs it discovers.

``PLUGIN`` is published under the ``decisionledger.reasoners`` entry point
(name ``mock-daily-reasoner``): the registration. The ledger runs none of the
mock's code (DR-31); the mock's CLI computes a day's settlements and submits
them over HTTP.

The mock ships no prompt template and no policy corpus: the gate it binds,
the rules gate, reads neither. ``prompts_dir`` and ``corpus_dir`` are empty
directories, as the ``ReasonerPlugin`` protocol allows. The rule set its
submissions name is deployment configuration, not a plugin asset: it lives
in the member's ``rulesets/`` directory, which ``make prepare-rulesets``
stages for the rules gate (``RULES_GATE_RULESETS_DIR``).

A deployment that does not install the plugin (or leaves it out of
``DECISIONLEDGER_REASONERS``) registers the mock by file
(``registration.yaml``) or over the API instead. Either way the
registration binds the rules gate mandatory, so a deployment that does not
compose the rules gate refuses it.
"""

from __future__ import annotations

from pathlib import Path

from mock_daily_reasoner.registry import MOCK_DAILY_REGISTRATION

_PACKAGE_DIR = Path(__file__).resolve().parent

#: The member's rule-set directory (``reasoners/mock_daily/rulesets``): what
#: ``make prepare-rulesets`` stages, and what the eval driver and the tests
#: point the rules gate at. Resolved in the workspace checkout, where both run.
RULESETS_DIR = _PACKAGE_DIR.parents[1] / "rulesets"


class MockDailyPlugin:
    """``ReasonerPlugin`` implementation for the daily mock.

    Attributes:
        registration: ``MOCK_DAILY_REGISTRATION``.
        prompts_dir: ``prompts/``, empty: the mock binds no gate that renders
            a prompt.
        corpus_dir: ``corpus/``, empty: the mock binds no gate that
            retrieves.
    """

    def __init__(self) -> None:
        """Bind the plugin's static surface."""
        self.registration = MOCK_DAILY_REGISTRATION
        self.prompts_dir = _PACKAGE_DIR / "prompts"
        self.corpus_dir = _PACKAGE_DIR / "corpus"


#: The entry-point target:
#: ``mock-daily-reasoner = "mock_daily_reasoner.plugin:PLUGIN"``.
PLUGIN = MockDailyPlugin()
