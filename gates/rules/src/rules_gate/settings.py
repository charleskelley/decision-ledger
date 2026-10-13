"""Rules gate configuration — where the deployment's rule sets live.

``RulesGateSettings`` reads ``RULES_GATE_*`` variables from the process
environment or the project ``.env`` (the same file the framework reads; the
prefix keeps the gate's variables from colliding with anyone else's).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class RulesGateSettings(BaseSettings):
    """Rules gate settings.

    Attributes:
        rulesets_dir: Directory of rule-set YAML files
            (``RULES_GATE_RULESETS_DIR``). Blank (the default) registers no
            rule sets: every decision routed to the gate then fails closed
            with ``unknown_ruleset`` and is held for review.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="RULES_GATE_",
        extra="ignore",
    )

    rulesets_dir: str = ""
