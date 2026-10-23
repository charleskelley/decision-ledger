"""A scripted stand-in for the RAG policy gate, for tests and the mock's eval.

The stream mock binds the RAG policy gate, which needs Elasticsearch, a
loaded corpus, and a model. Its Docker-free tests and its eval
(``make eval-mocks``: Postgres only, no model call) compose this double under
the same gate id instead. ``ScriptedPolicyGatePlugin`` is a ``GatePlugin``
the caller passes explicitly; it is never published under the
``decisionledger.gates`` entry-point group, where it would collide with the
real gate. It calls no model and imports no gate package:

- it validates a binding in the shape of the real gate's binding model
  (``ScriptedBindingConfig``: corpora with authority tiers, each corpus once,
  and ``k_per_corpus``) and admits it (``validate_binding``), refusing a
  corpus it was told is not loaded;
- it validates each gate context in the shape of the real gate's
  per-submission configuration (``ScriptedGateConfig``) at intake;
- it returns a fixed verdict (by default CHALLENGE at confidence 0.9, with a
  rationale) and no evidence, so enforcement treats it as a real advisory
  verdict: a fresh account the mock asks the gate about is held by Tier 3,
  not by a failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.gates import GateKind
from core.actions import DecisionAction
from core.exceptions import GateConfigError
from core.gate import GateOutput, GateVerdict

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.plugin import FrameworkServices
    from core.gate_request import GateRequest

#: The gate id the double answers to: the RAG policy gate's.
POLICY_GATE_ID = "policy"

#: The rationale the double's verdict carries.
SCRIPTED_RATIONALE = (
    "Scripted policy verdict: confirm the transfer with the account holder "
    "before release (mock-stream practice notes)."
)

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"


class ScriptedCorpusRef(BaseModel):
    """One corpus a binding names, in the shape of the real gate's reference.

    Attributes:
        corpus_id: The corpus.
        provider: The retrieval provider holding it.
        resource: A provider-specific resource name, if any.
        tier: Its authority tier (1 is the highest); a JSON integer.
        jurisdictions: Jurisdictions the binding reads from it.
        min_provenance: The weakest provenance the binding accepts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpus_id: str = Field(pattern=_ID_PATTERN, max_length=128)
    provider: str = Field(default="reference", pattern=_ID_PATTERN, max_length=64)
    resource: str | None = Field(default=None, min_length=1)
    tier: int = Field(strict=True, ge=1)
    jurisdictions: frozenset[Annotated[str, Field(min_length=1)]] = frozenset()
    min_provenance: Literal["pinned_version", "uri_and_hash", "text_only"] = "text_only"


class ScriptedBindingConfig(BaseModel):
    """A binding's configuration, in the shape of the real gate's binding model.

    Attributes:
        corpora: The corpora the binding reads, each once.
        k_per_corpus: Passages retrieved per corpus.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    corpora: tuple[ScriptedCorpusRef, ...] = Field(min_length=1)
    k_per_corpus: int = Field(default=5, strict=True, ge=1, le=50)

    @model_validator(mode="after")
    def _each_corpus_once(self) -> ScriptedBindingConfig:
        """A corpus has one tier in a binding."""
        ids = [corpus.corpus_id for corpus in self.corpora]
        repeated = sorted({c for c in ids if ids.count(c) > 1})
        if repeated:
            msg = f"corpora {repeated} are bound more than once"
            raise ValueError(msg)
        return self


class ScriptedGateConfig(BaseModel):
    """A gate context, in the shape of the real gate's per-submission config.

    Attributes:
        template_id: The prompt template the context names.
        template_vars: Its variables (strings).
        retrieval_query: The query the real gate would retrieve with.
        jurisdictions: The retrieval filter.
        risk_tier: The dense-store filter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    template_vars: dict[str, str] = Field(default_factory=dict)
    retrieval_query: str | None = None
    jurisdictions: list[str] = Field(default_factory=list)
    risk_tier: str | None = None


class ScriptedPolicyGate:
    """The double's runtime gate: a fixed verdict, and binding admission.

    Args:
        action: The one action the verdict permits.
        confidence: The verdict's confidence (0.60 or more keeps enforcement's
            low-confidence tier out of it).
        loaded_corpora: The corpora the double treats as loaded; a binding
            naming another is refused, as the real gate refuses a corpus its
            provider does not hold. ``None`` admits every well-formed binding.
    """

    def __init__(
        self,
        *,
        action: DecisionAction = DecisionAction.CHALLENGE,
        confidence: float = 0.9,
        loaded_corpora: frozenset[str] | None = None,
    ) -> None:
        """Script the verdict and the corpora."""
        self.action = action
        self.confidence = confidence
        self.loaded_corpora = loaded_corpora
        self.requests: list[GateRequest] = []
        self.bindings: list[tuple[str, ScriptedBindingConfig]] = []
        self.closed = False

    def validate_binding(
        self, reasoner_id: str, config: BaseModel | Mapping[str, Any]
    ) -> None:
        """Admit ``reasoner_id``'s binding, or refuse it.

        Args:
            reasoner_id: The reasoner whose registration binds the gate.
            config: The binding's configuration (already validated by the
                plugin's ``binding_config_model`` when the framework asks).

        Raises:
            GateConfigError: The configuration is malformed, or names a
                corpus the double was told is not loaded.
        """
        try:
            binding = (
                config
                if isinstance(config, ScriptedBindingConfig)
                else ScriptedBindingConfig.model_validate(dict(config))
            )
        except (ValidationError, TypeError) as err:
            msg = f"the binding configuration is invalid: {err}"
            raise GateConfigError(msg) from err
        if self.loaded_corpora is not None:
            missing = sorted(
                corpus.corpus_id
                for corpus in binding.corpora
                if corpus.corpus_id not in self.loaded_corpora
            )
            if missing:
                msg = f"corpora {missing} are not loaded"
                raise GateConfigError(msg)
        self.bindings.append((reasoner_id, binding))

    async def evaluate(self, request: GateRequest) -> GateOutput:
        """Return the scripted verdict for one decision.

        Args:
            request: The framework's request (recorded in ``requests``).

        Returns:
            A verdict permitting ``action``, with the rationale and the
            corpora the binding names in ``extra``, and no evidence.
        """
        self.requests.append(request)
        bound = request.binding_config.get("corpora")
        corpora = [
            str(corpus["corpus_id"])
            for corpus in (bound if isinstance(bound, list) else [])
            if isinstance(corpus, dict) and "corpus_id" in corpus
        ]
        return GateOutput(
            gate_id=POLICY_GATE_ID,
            deterministic=False,
            verdict=GateVerdict(
                gate_id=POLICY_GATE_ID,
                permitted_actions=[self.action],
                required_controls=["account_holder_confirmation"],
                confidence=self.confidence,
                escalate_to_human=False,
                escalation_reason=None,
            ),
            extra={
                "rationale": SCRIPTED_RATIONALE,
                "scripted": True,
                "corpora": list(corpora),
            },
        )

    def close(self) -> None:
        """Nothing to release."""
        self.closed = True


class ScriptedPolicyGatePlugin:
    """A ``GatePlugin`` for the double, passed to the framework explicitly.

    Attributes:
        gate_id: ``"policy"``.
        kind: ``GateKind.LLM``, as the real gate declares.
        deterministic: ``False``, as the real gate declares.
        config_model: ``ScriptedGateConfig``.
        binding_config_model: ``ScriptedBindingConfig``.
        gate: The gate every ``build`` returns.
    """

    gate_id = POLICY_GATE_ID
    kind = GateKind.LLM
    deterministic = False
    config_model: type[BaseModel] | None = ScriptedGateConfig
    binding_config_model: type[BaseModel] | None = ScriptedBindingConfig

    def __init__(self, gate: ScriptedPolicyGate | None = None) -> None:
        """Bind the gate the plugin builds (a default double when omitted)."""
        self.gate = gate if gate is not None else ScriptedPolicyGate()

    def build(self, framework: FrameworkServices) -> ScriptedPolicyGate:
        """Return the scripted gate (it reads nothing from the framework).

        Args:
            framework: The shared services (unused).

        Returns:
            ``gate``.
        """
        del framework
        return self.gate
