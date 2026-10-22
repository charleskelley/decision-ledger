"""Fake gate plugins — scripted gates the framework tests compose.

``PLUGIN`` (``gate_id="fake"``, non-deterministic, kind ``llm``) and
``DETERMINISTIC_PLUGIN`` (``gate_id="fake-rules"``, deterministic, kind
``rules``) satisfy ``app.gates.GatePlugin``. Each ``build`` returns a fresh
``FakeGate`` and remembers it on ``plugin.built`` so tests can assert on the
requests a gate received and on its lifecycle. A plugin built with
``refused_reasoners`` returns an ``AuthorizingFakeGate`` instead — the same
gate plus the optional ``ConfigAuthorizer`` capability, refusing exactly
those reasoners.

Every ``FakeGate`` also admits bindings (the optional ``BindingValidator``
capability, DR-33): its plugin declares ``FakeBindingConfig`` as the
``binding_config_model``, the gate records every binding it is asked about
on ``gate.bindings``, and it refuses a binding whose config says
``refuse: true``.

Every ``FakeGate`` verifies its evidence too (the optional
``EvidenceVerifier`` capability, DR-34). Its ``gate_config`` scripts the
references (``evidence_sources``, each carrying the digest of
``fake_evidence(source)``), whether it escrows their content through the
store's evidence escrow (``escrow_evidence``), and what ``verify`` answers
for them, stored and live (``verify_stored``, ``verify_live``, or
``verify_raises``): every status, and malformed answers, are reachable
without Docker. The gate reads its script back from the stored output.

``LOOKUP_PLUGIN`` (``gate_id="fake-lookup"``, deterministic, kind
``lookup``) is the lookup-style gate the gate-set tests bind as mandatory:
like a do-not-contact or sanctions adapter, it answers from a list keyed by
the decision subject (``listed``: entity id → action) and needs no
configuration. When the subject is listed it escrows the row it answered
from and cites it by digest; it does not verify its evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from app.gates import GateKind
from core.actions import DecisionAction
from core.exceptions import GateConfigError
from core.gate import EvidenceRef, GateOutput, GateVerdict

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from app.audit import ArtifactStore
    from app.plugin import FrameworkServices
    from core.gate import EvidenceStatus
    from core.gate_request import GateRequest


class FakeGateError(RuntimeError):
    """Raised by a fake gate whose config asks it to fail."""


def fake_evidence(source_id: str) -> bytes:
    """The content a fake gate relies on from ``source_id``."""
    return f"fake evidence from {source_id}".encode()


def _escrow_of(framework: FrameworkServices | None) -> ArtifactStore | None:
    """The store's evidence escrow, as a gate plugin's ``build`` takes it.

    ``None`` without a framework, or with a stand-in that has no store (tests
    that only count builds pass a bare object).
    """
    return getattr(getattr(framework, "store", None), "artifacts", None)


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
        evidence_sources: One evidence reference per source, after those,
            with the digest of ``fake_evidence(source)``.
        escrow_evidence: Escrow each source's content and cite it by the
            digest the escrow returns (``None`` with escrow off).
        verify_stored: What ``verify`` answers without ``live``; ``None``
            answers ``intact`` for every reference. Any strings, so a
            malformed answer can be scripted.
        verify_live: The same, with ``live``.
        verify_raises: ``verify`` raises ``FakeGateError``.
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
    evidence_sources: tuple[str, ...] = ()
    escrow_evidence: bool = False
    verify_stored: tuple[str, ...] | None = None
    verify_live: tuple[str, ...] | None = None
    verify_raises: bool = False
    raise_error: bool = False
    delay_ms: float = Field(default=0.0, ge=0.0)


class FakeBindingConfig(BaseModel):
    """The fake gate's ``binding_config_model`` — scripts what admission does.

    Attributes:
        label: Free text, recorded with the binding.
        refuse: Refuse the binding (``GateConfigError``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str | None = None
    refuse: bool = False


class FakeGate:
    """A scripted gate: returns what its request's ``gate_config`` asks for.

    Attributes:
        gate_id: The id it answers for.
        deterministic: The flag it stamps on every output.
        framework: The ``FrameworkServices`` it was built against.
        requests: Every request received, in order.
        bindings: Every ``(reasoner_id, config)`` binding it was asked to
            admit, in order.
        escrow: The evidence escrow its plugin handed it (``None`` when
            built without a framework).
        verifications: Every ``(output, live)`` it was asked to verify.
        closed: ``True`` once ``close()`` ran.
        output_override: When set, returned verbatim from ``evaluate`` —
            lets a test hand the framework a malformed output.
    """

    def __init__(
        self,
        *,
        gate_id: str,
        deterministic: bool,
        framework: FrameworkServices,
        escrow: ArtifactStore | None = None,
    ) -> None:
        self.gate_id = gate_id
        self.deterministic = deterministic
        self.framework = framework
        self.escrow = escrow
        self.requests: list[GateRequest] = []
        self.bindings: list[tuple[str, object]] = []
        self.verifications: list[tuple[GateOutput, bool]] = []
        self.closed = False
        self.output_override: object | None = None

    def validate_binding(self, reasoner_id: str, config: object) -> None:
        """Record the binding; refuse it when its config says ``refuse``."""
        self.bindings.append((reasoner_id, config))
        if isinstance(config, FakeBindingConfig) and config.refuse:
            msg = (
                f"fake gate {self.gate_id!r} was scripted to refuse the binding "
                f"of reasoner {reasoner_id!r}"
            )
            raise GateConfigError(msg)

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
        evidence = [
            EvidenceRef(
                source_id=f"fake-source-{jurisdiction}",
                jurisdictions=frozenset({jurisdiction}),
            )
            for jurisdiction in config.evidence_jurisdictions
        ]
        for source in config.evidence_sources:
            content = fake_evidence(source)
            artifact = (
                await asyncio.to_thread(
                    self.escrow.put,
                    content,
                    media_type="text/plain",
                    decision_id=request.decision_id,
                    gate_id=self.gate_id,
                )
                if config.escrow_evidence and self.escrow is not None
                else None
            )
            evidence.append(
                EvidenceRef(
                    source_id=source,
                    content_hash=hashlib.sha256(content).hexdigest(),
                    artifact_hash=artifact,
                )
            )
        return GateOutput(
            gate_id=self.gate_id,
            deterministic=self.deterministic,
            verdict=verdict,
            evidence=tuple(evidence),
            evidence_snapshot={
                "config": config.model_dump(mode="json"),
                "request_number": len(self.requests),
            },
            extra={"scripted": True},
        )

    async def verify(
        self, output: GateOutput, *, live: bool
    ) -> tuple[EvidenceStatus, ...]:
        """Answer what the output's own config scripted (``intact`` by default)."""
        self.verifications.append((output, live))
        snapshot = output.evidence_snapshot
        stored = snapshot.get("config", {}) if isinstance(snapshot, dict) else {}
        config = FakeGateConfig.model_validate(stored)
        if config.verify_raises:
            msg = f"fake gate {self.gate_id!r} was scripted to fail verifying"
            raise FakeGateError(msg)
        scripted = config.verify_live if live else config.verify_stored
        if scripted is None:
            return ("intact",) * len(output.evidence)
        return scripted  # type: ignore[return-value]  # scripted, maybe malformed

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
        escrow: ArtifactStore | None = None,
    ) -> None:
        super().__init__(
            gate_id=gate_id,
            deterministic=deterministic,
            framework=framework,
            escrow=escrow,
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
        self.binding_config_model: type[BaseModel] | None = FakeBindingConfig
        self.refused_reasoners = refused_reasoners
        self.built: list[FakeGate] = []

    def build(self, framework: FrameworkServices) -> FakeGate:
        """Build a fresh ``FakeGate`` (or ``AuthorizingFakeGate``)."""
        gate = (
            FakeGate(
                gate_id=self.gate_id,
                deterministic=self.deterministic,
                framework=framework,
                escrow=_escrow_of(framework),
            )
            if self.refused_reasoners is None
            else AuthorizingFakeGate(
                gate_id=self.gate_id,
                deterministic=self.deterministic,
                framework=framework,
                refused_reasoners=self.refused_reasoners,
                escrow=_escrow_of(framework),
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
        escrow: The evidence escrow its plugin handed it (``None`` when
            built without a framework). A listed subject's row is escrowed
            as canonical JSON and cited by digest.
        requests: Every request received, in order.
        closed: ``True`` once ``close()`` ran.
    """

    deterministic = True

    def __init__(
        self,
        *,
        gate_id: str,
        listed: Mapping[UUID, DecisionAction],
        escrow: ArtifactStore | None = None,
    ) -> None:
        self.gate_id = gate_id
        self.listed = listed
        self.escrow = escrow
        self.requests: list[GateRequest] = []
        self.closed = False

    async def evaluate(self, request: GateRequest) -> GateOutput:
        """Look the decision subject up and permit its listed action."""
        self.requests.append(request)
        entity_id = request.submission.entity_id
        action = self.listed.get(entity_id)
        content_hash = artifact = None
        if action is not None:
            row = json.dumps(
                {"action": action.value, "entity_id": str(entity_id)},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            content_hash = hashlib.sha256(row).hexdigest()
            if self.escrow is not None:
                artifact = await asyncio.to_thread(
                    self.escrow.put,
                    row,
                    media_type="application/json",
                    decision_id=request.decision_id,
                    gate_id=self.gate_id,
                )
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
                    content_hash=content_hash,
                    artifact_hash=artifact,
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

    It declares no ``binding_config_model`` and its gate does not validate
    bindings, so a binding to it takes no configuration.

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
        gate = FakeLookupGate(
            gate_id=self.gate_id, listed=self.listed, escrow=_escrow_of(framework)
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

#: A deterministic lookup-style gate, published as
#: ``fake-lookup = "tests._fake_gate.plugin:LOOKUP_PLUGIN"``.
LOOKUP_PLUGIN = FakeLookupGatePlugin()
