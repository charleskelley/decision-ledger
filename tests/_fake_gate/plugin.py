"""Fake gate plugins — scripted gates the framework tests compose.

``PLUGIN`` (``gate_id="fake"``, non-deterministic, kind ``llm``) and
``DETERMINISTIC_PLUGIN`` (``gate_id="fake-rules"``, deterministic, kind
``rules``) satisfy ``app.gates.GatePlugin``. Each ``build`` returns a fresh
``FakeGate`` and remembers it on ``plugin.built`` so tests can assert on the
requests a gate received and on its lifecycle. A plugin built with
``refused_reasoners`` returns an ``AuthorizingFakeGate`` instead — the same
gate plus the optional ``ConfigAuthorizer`` capability, refusing exactly
those reasoners.

``LOOKUP_PLUGIN`` (``gate_id="fake-lookup"``, deterministic, kind
``lookup``) is the lookup-style gate the gate-set tests bind as mandatory:
like a do-not-contact or sanctions adapter, it answers from a list keyed by
the decision subject (``listed``: entity id → action) and needs no
configuration.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from app.gates import GateKind
from core.actions import DecisionAction
from core.gate import EvidenceRef, GateOutput, GateVerdict

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

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
        delay_ms: Sleep this long before answering (concurrency and timeout
            tests).
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
    delay_ms: float = Field(default=0.0, ge=0.0)


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
        if config.delay_ms:
            await asyncio.sleep(config.delay_ms / 1_000)
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


class AuthorizingFakeGate(FakeGate):
    """A ``FakeGate`` that also authorizes ``gate_config`` per reasoner.

    Attributes:
        refused_reasoners: Reasoners whose configurations it refuses.
        authorizations: Every ``(config, reasoner_id)`` it was asked about.
    """

    def __init__(
        self,
        *,
        gate_id: str,
        deterministic: bool,
        framework: FrameworkServices,
        refused_reasoners: frozenset[str],
    ) -> None:
        super().__init__(
            gate_id=gate_id, deterministic=deterministic, framework=framework
        )
        self.refused_reasoners = refused_reasoners
        self.authorizations: list[tuple[object, str]] = []

    def authorize_config(self, config: object, *, reasoner_id: str) -> None:
        """Record the question; refuse the listed reasoners."""
        self.authorizations.append((config, reasoner_id))
        if reasoner_id in self.refused_reasoners:
            msg = f"reasoner {reasoner_id!r} may not use fake gate {self.gate_id!r}"
            raise ValueError(msg)


class FakeGatePlugin:
    """``GatePlugin`` for a scripted fake gate.

    Args:
        gate_id: The id it registers under.
        deterministic: Whether its outputs are deterministic.
        kind: Its declared ``GateKind``.
        refused_reasoners: When given, ``build`` returns an
            ``AuthorizingFakeGate`` that refuses these reasoners (an empty
            set authorizes everyone but still records each question).

    Attributes:
        built: Every ``FakeGate`` this plugin has built, in order.
    """

    def __init__(
        self,
        gate_id: str = "fake",
        *,
        deterministic: bool = False,
        kind: GateKind = GateKind.LLM,
        refused_reasoners: frozenset[str] | None = None,
    ) -> None:
        self.gate_id = gate_id
        self.kind = kind
        self.deterministic = deterministic
        self.config_model: type[BaseModel] | None = FakeGateConfig
        self.refused_reasoners = refused_reasoners
        self.built: list[FakeGate] = []

    def build(self, framework: FrameworkServices) -> FakeGate:
        """Build a fresh ``FakeGate`` (or ``AuthorizingFakeGate``)."""
        gate = (
            FakeGate(
                gate_id=self.gate_id,
                deterministic=self.deterministic,
                framework=framework,
            )
            if self.refused_reasoners is None
            else AuthorizingFakeGate(
                gate_id=self.gate_id,
                deterministic=self.deterministic,
                framework=framework,
                refused_reasoners=self.refused_reasoners,
            )
        )
        self.built.append(gate)
        return gate


class FakeLookupConfig(BaseModel):
    """The lookup gate's ``config_model``: it takes no configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class FakeLookupGate:
    """A deterministic lookup gate: listed subjects get their listed action.

    Attributes:
        gate_id: The id it answers for.
        listed: Entity id → the action permitted for it (shared with the
            plugin, so tests can list a subject after composition). An
            unlisted subject is permitted ``ALLOW``.
        requests: Every request received, in order.
        closed: ``True`` once ``close()`` ran.
    """

    deterministic = True

    def __init__(self, *, gate_id: str, listed: Mapping[UUID, DecisionAction]) -> None:
        self.gate_id = gate_id
        self.listed = listed
        self.requests: list[GateRequest] = []
        self.closed = False

    async def evaluate(self, request: GateRequest) -> GateOutput:
        """Look the decision subject up and permit its listed action."""
        self.requests.append(request)
        entity_id = request.submission.entity_id
        action = self.listed.get(entity_id)
        return GateOutput(
            gate_id=self.gate_id,
            deterministic=True,
            verdict=GateVerdict(
                gate_id=self.gate_id,
                permitted_actions=[action or DecisionAction.ALLOW],
                required_controls=["listed_subject"] if action else [],
                confidence=1.0,
                escalate_to_human=False,
                escalation_reason=None,
            ),
            evidence=(
                EvidenceRef(
                    source_id=f"{self.gate_id}-list",
                    locator=str(entity_id) if action else None,
                ),
            ),
            evidence_snapshot={
                "entity_id": str(entity_id),
                "listed": action is not None,
            },
        )

    def close(self) -> None:
        """Mark the gate closed (there is nothing to release)."""
        self.closed = True


class FakeLookupGatePlugin:
    """``GatePlugin`` for the deterministic lookup-style fake gate.

    Args:
        gate_id: The id it registers under.
        listed: Initial entity id → action list.

    Attributes:
        listed: The list every gate it builds reads (mutable).
        built: Every gate this plugin has built, in order.
    """

    kind = GateKind.LOOKUP
    deterministic = True

    def __init__(
        self,
        gate_id: str = "fake-lookup",
        *,
        listed: Mapping[UUID, DecisionAction] | None = None,
    ) -> None:
        self.gate_id = gate_id
        self.config_model: type[BaseModel] | None = FakeLookupConfig
        self.listed: dict[UUID, DecisionAction] = dict(listed or {})
        self.built: list[FakeLookupGate] = []

    def build(self, framework: FrameworkServices) -> FakeLookupGate:
        """Build a gate that reads this plugin's ``listed`` table."""
        del framework
        gate = FakeLookupGate(gate_id=self.gate_id, listed=self.listed)
        self.built.append(gate)
        return gate


#: The entry-point target: ``fake = "tests._fake_gate.plugin:PLUGIN"``.
PLUGIN = FakeGatePlugin()

#: A deterministic twin, published as
#: ``fake-rules = "tests._fake_gate.plugin:DETERMINISTIC_PLUGIN"``.
DETERMINISTIC_PLUGIN = FakeGatePlugin(
    "fake-rules", deterministic=True, kind=GateKind.RULES
)

#: A deterministic lookup-style gate, published as
#: ``fake-lookup = "tests._fake_gate.plugin:LOOKUP_PLUGIN"``.
LOOKUP_PLUGIN = FakeLookupGatePlugin()
