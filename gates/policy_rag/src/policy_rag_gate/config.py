"""The policy gate's configuration contract: ``gate_config`` and who may send it.

``PolicyGateConfig`` is the gate's ``config_model``. The framework validates
every ``GateContext.gate_config`` that names ``gate_id="policy"`` against it
at intake — recorded decisions included, so a recorded decision always
carries a context the gate could evaluate for shadow evaluation. Its typed fields
replace the convention keys the framework used to check by name.

``ReasonerProfile`` is what one reasoner may submit to the gate: the prompt
templates it ships (its ``prompts_dir``) and the jurisdictions its policy
corpus covers (the ``jurisdiction`` front matter of its ``corpus_dir``
documents). The gate derives one profile per composed reasoner when it is
built and authorizes every submission against the submitter's profile at
intake (``PolicyGate.authorize_config``). This replaces the registration's
former ``allowed_prompt_template_ids`` / ``allowed_jurisdictions`` (DR-28):
a reasoner may render only its own templates and may filter retrieval only
to jurisdictions its own corpus holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from policy_rag_gate.prompt_registry import YamlPromptRegistry

if TYPE_CHECKING:
    from pathlib import Path


class PolicyGateConfig(BaseModel):
    """Schema of the policy gate's ``gate_config`` (its ``config_model``).

    Lax (non-strict) so a reasoner's plain dict — string enum values
    included — validates the way it arrives. Unknown keys are rejected, so a
    misspelled key fails at intake instead of being silently ignored.

    Attributes:
        template_id: Prompt template to render (the registry key, and the
            audit key recorded in every bundle the gate decides).
        template_vars: Substitutions for the template's placeholders.
        retrieval_query: The reasoner-rendered retrieval query. ``None`` or
            blank makes the gate fail closed before it retrieves (no
            verdict; enforcement holds the decision).
        jurisdictions: Retrieval filter applied in both stores. Empty means
            no filter.
        risk_tier: Retrieval filter applied in the dense store only.
        corpus_version: Version label of the reasoner's corpus, recorded in
            the gate's snapshot.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    template_vars: dict[str, str] = Field(default_factory=dict)
    retrieval_query: str | None = None
    jurisdictions: list[str] = Field(default_factory=list)
    risk_tier: str | None = None
    corpus_version: str = "unknown"


@dataclass(frozen=True)
class ReasonerProfile:
    """What one reasoner may submit to the policy gate.

    Attributes:
        template_ids: Prompt templates the reasoner ships in its
            ``prompts_dir``.
        jurisdictions: Jurisdictions its policy corpus covers.
    """

    template_ids: frozenset[str]
    jurisdictions: frozenset[str]

    @classmethod
    def from_assets(cls, *, prompts_dir: Path, corpus_dir: Path) -> ReasonerProfile:
        """Derive a profile from the prompts and corpus a reasoner ships.

        Corpus documents whose front matter fails to parse are skipped, as
        the corpus loader skips them: they never reach the index, so a
        reasoner cannot claim their jurisdiction.

        Args:
            prompts_dir: The reasoner's directory of ``*.yaml`` templates.
            corpus_dir: The reasoner's directory of Markdown policy documents
                (searched recursively).

        Returns:
            The reasoner's profile.

        Raises:
            ValueError: A prompt template file is malformed.
        """
        # Imported here: the loader module imports the embedding stack.
        from policy_rag_gate.corpus_loader import parse_document

        jurisdictions: set[str] = set()
        for path in sorted(corpus_dir.rglob("*.md")):
            try:
                document, _ = parse_document(path)
            except (ValueError, KeyError):
                continue
            jurisdictions.add(document.jurisdiction)
        return cls(
            template_ids=frozenset(YamlPromptRegistry([prompts_dir]).registered_ids()),
            jurisdictions=frozenset(jurisdictions),
        )

    def authorize(self, config: PolicyGateConfig) -> None:
        """Check that ``config`` stays inside this profile.

        Args:
            config: A validated policy-gate configuration.

        Raises:
            ValueError: ``config`` names a template the reasoner does not ship
                or a jurisdiction its corpus does not cover.
        """
        if config.template_id not in self.template_ids:
            msg = (
                f"template_id {config.template_id!r} is not a prompt template "
                f"this reasoner ships: {sorted(self.template_ids)}"
            )
            raise ValueError(msg)
        foreign = sorted(set(config.jurisdictions) - self.jurisdictions)
        if foreign:
            msg = (
                f"jurisdictions {foreign} are not covered by this reasoner's "
                f"policy corpus: {sorted(self.jurisdictions)}"
            )
            raise ValueError(msg)
