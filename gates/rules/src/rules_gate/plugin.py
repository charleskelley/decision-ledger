"""Rules gate plugin — what the framework discovers and composes.

``PLUGIN`` is the object published under the ``decisionledger.gates`` entry
point (name ``rules``). It declares the gate's identity (``gate_id="rules"``,
``kind=rules``, ``deterministic=True``), its ``config_model``
(``RulesGateConfig``, validated by the framework at intake), and ``build`` —
which loads the deployment's rule sets from ``RULES_GATE_RULESETS_DIR``, each
with the file it came from, and returns a ``RulesGate`` holding the
framework store's evidence escrow (DR-34 part 2). The gate owns no
infrastructure: it opens no connection or index of its own, and the rule
sets it escrows go through the framework's store.
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

    def build(self, framework: FrameworkServices | None) -> RulesGate:
        """Load the registered rule sets and build the gate.

        Args:
            framework: Shared services: the gate takes the store's evidence
                escrow (``store.artifacts``) to escrow the rule sets it
                applies. ``None`` builds a gate that escrows nothing.

        Returns:
            A ``RulesGate`` over every rule set in ``rulesets_dir``.

        Raises:
            FileNotFoundError: ``rulesets_dir`` is set but does not exist.
            ValueError: Two rule-set files share a ``ruleset_id``.
            pydantic.ValidationError: A rule-set file is malformed.
        """
        directory = self.settings.rulesets_dir
        loaded = load_rulesets(Path(directory)) if directory else {}
        escrow = framework.store.artifacts if framework is not None else None
        log.info(
            "rules_gate.built",
            component="rules_gate",
            rulesets_dir=directory or None,
            ruleset_ids=sorted(loaded),
            escrow=escrow is not None,
        )
        return RulesGate(
            {ruleset_id: entry.ruleset for ruleset_id, entry in loaded.items()},
            origins={
                ruleset_id: Path(directory) / entry.origin
                for ruleset_id, entry in loaded.items()
            },
            escrow=escrow,
        )


#: The entry-point target: ``rules = "rules_gate.plugin:PLUGIN"``.
PLUGIN = RulesGatePlugin()
