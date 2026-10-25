"""A scripted stand-in for the RAG policy gate, for tests and the mock's eval.

The text mock binds the RAG policy gate, which needs Elasticsearch, two
loaded corpora, and a model. Its Docker-free tests and its eval
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
- by default it agrees with the classifier: its verdict permits the
  submission's proposal, at confidence 0.9, with a rationale and no
  evidence, so enforcement treats it as a real advisory verdict and the
  final action is the proposal (``action`` fixes the permitted action
  instead, so a test can show the proposal is a floor);
- for the labels it is told to (``protocol_hold_labels``), it answers as the
  real gate answers when its model reports that passages on point disagree:
  a verdict floored at HOLD, escalated with the gate's own reason, ``extra``
  carrying ``conflict_detected`` and ``protocol_hold``, and evidence naming
  the two planted passages (``PLANTED_EVIDENCE``, in the ``GLOBAL``
  jurisdiction, so enforcement's jurisdiction-conflict tier runs on it and
  does not fire). That scripts the hold's shape for the mock's tests;
  whether a model notices a disagreement is shown live, never by the
  double.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.gates import GateKind
from core.actions import DecisionAction
from core.exceptions import GateConfigError
from core.gate import EvidenceRef, GateOutput, GateVerdict

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from app.plugin import FrameworkServices
    from core.gate_request import GateRequest

#: The gate id the double answers to: the RAG policy gate's.
POLICY_GATE_ID = "policy"

#: The rationale of the double's verdict when it agrees with the classifier.
SCRIPTED_RATIONALE = (
    "Scripted policy verdict: the regulatory text and the practice notes "
    "agree with the classifier's proposal for this message."
)

#: The rationale of its scripted protocol hold.
SCRIPTED_HOLD_RATIONALE = (
    "Scripted policy verdict: the regulatory text requires a review the "
    "practice notes do not, so the passages on point disagree."
)

#: The escalation reason of a scripted protocol hold: the real gate's own
#: text for a disagreement, worded without "conflict" or "jurisdiction",
#: which enforcement would read as a jurisdiction conflict (Tier 5).
PROTOCOL_HOLD_REASON = (
    "Retrieved policy evidence on point disagrees; held for human review."
)

#: The corpus a scripted protocol hold names as governing.
GOVERNING_CORPUS_ID = "mock-text-regulatory"

#: The evidence of a scripted protocol hold: the two planted passages, as the
#: real gate cites a passage (its document, its section, its jurisdiction).
PLANTED_EVIDENCE: tuple[EvidenceRef, ...] = (
    EvidenceRef(
        source_id="MT-REG-ASSISTANT",
        locator="4. Statements of Financial Hardship",
        jurisdictions=frozenset({"GLOBAL"}),
    ),
    EvidenceRef(
        source_id="MT-PRAC-REPLIES",
        locator="4. Hardship Messages",
        jurisdictions=frozenset({"GLOBAL"}),
    ),
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
    """The double's runtime gate: a scripted verdict, and binding admission.

    Args:
        action: The one action the verdict permits; ``None`` (the default)
            permits the submission's proposal (HOLD when it has none).
        confidence: The verdict's confidence (0.60 or more keeps enforcement's
            low-confidence tier out of it).
        loaded_corpora: The corpora the double treats as loaded; a binding
            naming another is refused, as the real gate refuses a corpus its
            provider does not hold. ``None`` admits every well-formed binding.
        protocol_hold_labels: The labels (``label_value``) the double holds as
            the real gate does when its model reports a disagreement. Empty
            by default: the double agrees with every proposal.
    """

    def __init__(
        self,
        *,
        action: DecisionAction | None = None,
        confidence: float = 0.9,
        loaded_corpora: frozenset[str] | None = None,
        protocol_hold_labels: Iterable[str] = (),
    ) -> None:
        """Script the verdicts and the corpora."""
        self.action = action
        self.confidence = confidence
        self.loaded_corpora = loaded_corpora
        self.protocol_hold_labels = frozenset(protocol_hold_labels)
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
            A verdict permitting ``action``, or the submission's proposal
            (HOLD when it has none), with the rationale and the corpora the
            binding names in ``extra`` and no evidence; or, for a label in
            ``protocol_hold_labels``, the real gate's protocol hold, citing
            ``PLANTED_EVIDENCE``.
        """
        self.requests.append(request)
        bound = request.binding_config.get("corpora")
        corpora = [
            str(corpus["corpus_id"])
            for corpus in (bound if isinstance(bound, list) else [])
            if isinstance(corpus, dict) and "corpus_id" in corpus
        ]
        submission = request.submission
        label = submission.reasoner_context.label_value
        if label in self.protocol_hold_labels:
            return self._protocol_hold(corpora)
        action = self.action or submission.proposed_action or DecisionAction.HOLD
        return GateOutput(
            gate_id=POLICY_GATE_ID,
            deterministic=False,
            verdict=GateVerdict(
                gate_id=POLICY_GATE_ID,
                permitted_actions=[action],
                required_controls=[],
                confidence=self.confidence,
                escalate_to_human=False,
                escalation_reason=None,
            ),
            extra={
                "rationale": SCRIPTED_RATIONALE,
                "scripted": True,
                "corpora": list(corpora),
                "conflict_detected": False,
                "evidence_sufficient": True,
                "governing_corpus_id": GOVERNING_CORPUS_ID,
            },
        )

    def _protocol_hold(self, corpora: list[str]) -> GateOutput:
        """The real gate's answer when its model reports a disagreement."""
        return GateOutput(
            gate_id=POLICY_GATE_ID,
            deterministic=False,
            evidence=PLANTED_EVIDENCE,
            verdict=GateVerdict(
                gate_id=POLICY_GATE_ID,
                permitted_actions=[DecisionAction.HOLD],
                required_controls=[],
                confidence=self.confidence,
                escalate_to_human=True,
                escalation_reason=PROTOCOL_HOLD_REASON,
            ),
            extra={
                "rationale": SCRIPTED_HOLD_RATIONALE,
                "scripted": True,
                "corpora": list(corpora),
                "conflict_detected": True,
                "evidence_sufficient": True,
                "governing_corpus_id": GOVERNING_CORPUS_ID,
                "protocol_hold": "conflict_detected",
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
