"""Fake gate plugins — scripted gates the framework tests compose.

``PLUGIN`` (``gate_id="fake"``, non-deterministic, kind ``llm``) and
``DETERMINISTIC_PLUGIN`` (``gate_id="fake-rules"``, deterministic, kind
``rules``) satisfy ``app.gates.GatePlugin``. Each ``build`` returns a fresh
``FakeGate`` and remembers it on ``plugin.built`` so tests can assert on the
requests a gate received and on its lifecycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from app.gates import GateKind
from core.actions import DecisionAction
from core.gate import EvidenceRef, GateOutput, GateVerdict

if TYPE_CHECKING:
    from app.plugin import FrameworkServices
    from core.gate_request import GateRequest


class FakeGateError(RuntimeError):
    """Raised by a fake gate whose config asks it to fail."""


class FakeGateConfig(BaseModel):
    """The fake gate's ``config_model`` — scripts what the gate returns.

    Lax (non-strict) so plain dicts with string enum values validate, the way
    a reasoner's ``gate_config`` arrives.

    Attributes:
        permitted_actions: Actions the verdict permits.
        required_controls: Controls the verdict requires.
        confidence: Verdict confidence.
        escalate_to_human: Verdict escalation flag.
        escalation_reason: Verdict escalation reason.
        schema_failure: Return ``verdict=None`` (Tier 1).
        evidence_jurisdictions: One evidence reference per jurisdiction.
        raise_error: Raise ``FakeGateError`` instead of returning.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    permitted_actions: tuple[DecisionAction, ...] = (DecisionAction.ALLOW,)
    required_controls: tuple[str, ...] = ()
    confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    escalate_to_human: bool = False
    escalation_reason: str | None = None
    schema_failure: bool = False
    evidence_jurisdictions: tuple[str, ...] = ()
    raise_error: bool = False


class FakeGate:
    """A scripted gate: returns what its request's ``gate_config`` asks for.

    Attributes:
        gate_id: The id it answers for.
        deterministic: The flag it stamps on every output.
        framework: The ``FrameworkServices`` it was built against.
        requests: Every request received, in order.
        closed: ``True`` once ``close()`` ran.
        output_override: When set, returned verbatim from ``evaluate`` —
            lets a test hand the framework a malformed output.
    """

    def __init__(
        self, *, gate_id: str, deterministic: bool, framework: FrameworkServices
    ) -> None:
        self.gate_id = gate_id
        self.deterministic = deterministic
        self.framework = framework
        self.requests: list[GateRequest] = []
        self.closed = False
        self.output_override: object | None = None

    async def evaluate(self, request: GateRequest) -> GateOutput:
        """Return the scripted ``GateOutput`` (or raise, if scripted to)."""
        self.requests.append(request)
        if self.output_override is not None:
            return self.output_override  # type: ignore[return-value]
        config = FakeGateConfig.model_validate(request.gate_context.gate_config or {})
        if config.raise_error:
            msg = f"fake gate {self.gate_id!r} was scripted to fail"
            raise FakeGateError(msg)
        verdict = (
            None
            if config.schema_failure
            else GateVerdict(
                gate_id=self.gate_id,
                permitted_actions=list(config.permitted_actions),
                required_controls=list(config.required_controls),
                confidence=config.confidence,
                escalate_to_human=config.escalate_to_human,
                escalation_reason=config.escalation_reason,
            )
        )
        return GateOutput(
            gate_id=self.gate_id,
            deterministic=self.deterministic,
            verdict=verdict,
            evidence=tuple(
                EvidenceRef(
                    source_id=f"fake-source-{jurisdiction}",
                    jurisdictions=frozenset({jurisdiction}),
                )
                for jurisdiction in config.evidence_jurisdictions
            ),
            evidence_snapshot={
                "config": config.model_dump(mode="json"),
                "request_number": len(self.requests),
            },
            extra={"scripted": True},
        )

    def close(self) -> None:
        """Mark the gate closed (there is nothing to release)."""
        self.closed = True


class FakeGatePlugin:
    """``GatePlugin`` for a scripted fake gate.

    Args:
        gate_id: The id it registers under.
        deterministic: Whether its outputs are deterministic.
        kind: Its declared ``GateKind``.

    Attributes:
        built: Every ``FakeGate`` this plugin has built, in order.
    """

    def __init__(
        self,
        gate_id: str = "fake",
        *,
        deterministic: bool = False,
        kind: GateKind = GateKind.LLM,
    ) -> None:
        self.gate_id = gate_id
        self.kind = kind
        self.deterministic = deterministic
        self.config_model: type[BaseModel] | None = FakeGateConfig
        self.built: list[FakeGate] = []

    def build(self, framework: FrameworkServices) -> FakeGate:
        """Build a fresh ``FakeGate`` against ``framework``."""
        gate = FakeGate(
            gate_id=self.gate_id, deterministic=self.deterministic, framework=framework
        )
        self.built.append(gate)
        return gate


#: The entry-point target: ``fake = "tests._fake_gate.plugin:PLUGIN"``.
PLUGIN = FakeGatePlugin()

#: A deterministic twin, published as
#: ``fake-rules = "tests._fake_gate.plugin:DETERMINISTIC_PLUGIN"``.
DETERMINISTIC_PLUGIN = FakeGatePlugin(
    "fake-rules", deterministic=True, kind=GateKind.RULES
)
