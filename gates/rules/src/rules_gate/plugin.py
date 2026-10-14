"""Rules gate plugin — what the framework discovers and composes.

``PLUGIN`` is the object published under the ``decisionledger.gates`` entry
point (name ``rules``). It declares the gate's identity (``gate_id="rules"``,
``kind=rules``, ``deterministic=True``), its ``config_model``
(``RulesGateConfig``, validated by the framework at intake), and ``build`` —
which loads the deployment's rule sets from ``RULES_GATE_RULESETS_DIR`` and
returns a ``RulesGate``. The gate needs no infrastructure: it never touches
the framework's Postgres connection or any index.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from app.gates import GateKind
from rules_gate.gate import RULES_GATE_ID, RulesGate
from rules_gate.ruleset import RulesGateConfig, load_rulesets
from rules_gate.settings import RulesGateSettings

if TYPE_CHECKING:
    from pydantic import BaseModel

    from app.plugin import FrameworkServices

log = structlog.get_logger(__name__)


class RulesGatePlugin:
    """``GatePlugin`` implementation for the rules gate.

    Args:
        settings: Optional explicit ``RulesGateSettings``. When omitted,
            settings are read from the environment the first time ``build``
            runs, so importing the plugin never touches ``.env``.

    Attributes:
        gate_id: ``"rules"``.
        kind: ``GateKind.RULES``.
        deterministic: ``True``.
        config_model: ``RulesGateConfig``.
    """

    gate_id: str = RULES_GATE_ID
    kind: GateKind = GateKind.RULES
    deterministic: bool = True
    config_model: type[BaseModel] | None = RulesGateConfig

    def __init__(self, settings: RulesGateSettings | None = None) -> None:
        """Bind the plugin; defer reading settings to build time."""
        self._settings = settings

    @property
    def settings(self) -> RulesGateSettings:
        """The settings ``build`` will use (read lazily)."""
        if self._settings is None:
            self._settings = RulesGateSettings()
        return self._settings

    def build(self, framework: FrameworkServices) -> RulesGate:
        """Load the registered rule sets and build the gate.

        Args:
            framework: Shared services. Unused — the rules gate owns no
                infrastructure — but every gate plugin receives it.

        Returns:
            A ``RulesGate`` over every rule set in ``rulesets_dir``.

        Raises:
            FileNotFoundError: ``rulesets_dir`` is set but does not exist.
            ValueError: Two rule-set files share a ``ruleset_id``.
            pydantic.ValidationError: A rule-set file is malformed.
        """
        del framework
        directory = self.settings.rulesets_dir
        rulesets = load_rulesets(Path(directory)) if directory else {}
        log.info(
            "rules_gate.built",
            component="rules_gate",
            rulesets_dir=directory or None,
            ruleset_ids=sorted(rulesets),
        )
        return RulesGate(rulesets)


#: The entry-point target: ``rules = "rules_gate.plugin:PLUGIN"``.
PLUGIN = RulesGatePlugin()
