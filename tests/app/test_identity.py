"""Behavioral tests for the ledger's identity step (``app/identity``, DR-32).

The framework implements no identity resolution: it establishes every
decision's subject through the deployment's one active resolver, and records
the subject the reasoner named — unresolved, with the reason — whenever that
resolver cannot answer usefully. These tests pin:

- **Passthrough**: no native subject → the reasoner's ``entity_id``; a native
  subject in a namespace the reasoner's registration declares → that
  declaration's derivation (recorded); any other → the reasoner's
  ``entity_id``, saying the namespace is undeclared. Deployment-wide, one
  namespace name has one declaration.
- **Discovery**: a resolver plugin is well formed (protocol, id, a frozenset
  of storable relationship types, never ``passthrough``); exactly the
  configured one is selected and loaded; a build that returns no usable
  resolver is refused and closed.
- **The bounded call**: a match is recorded as the resolver gave it; no match,
  an error, a timeout, or an unusable answer (wrong type, unresolved, another
  resolver's or another subject's, an undeclared relationship type, a
  derivation that does not hold) falls back to the passthrough with a
  storable reason — never an exception; the caller's own cancellation still
  propagates.
- **The read path** (``lookup``): a match, else a declared namespace's
  passthrough subject, else no identity.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from importlib.metadata import EntryPoint

import pytest

from app.identity import (
    NO_NATIVE_SUBJECT,
    NO_RESOLVER,
    PASSTHROUGH,
    RESOLVER_GROUP,
    IdentityService,
    build_resolver,
    check_resolver_plugin,
    declared_namespaces,
    identity_problem,
    native_subject,
    passthrough_identity,
    select_resolver,
)
from core.exceptions import RegistrationError, ResolverPluginError
from core.identity import (
    EntityDerivation,
    IdentityProvenance,
    NativeNamespace,
    NativeSubject,
    RelatedEntity,
    ResolvedIdentity,
)
from core.observation import ReasonerRegistration
from tests._stub_resolver.plugin import (
    CROSSWALK_VERSION,
    FLEET,
    WIDGET_REGISTRY,
    StubResolver,
    StubResolverError,
    StubResolverPlugin,
    fleet_id,
    owner_id,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
WIDGET_NS = NativeNamespace(
    native_namespace=WIDGET_REGISTRY,
    namespace_uuid=uuid.UUID("3f6e1b7c-9d2a-5e48-8b1f-0c7a4d9e2b61"),
    entity_type="widget",
)


def _declaring(registration: ReasonerRegistration, *namespaces: NativeNamespace):
    fields = {
        name: getattr(registration, name) for name in ReasonerRegistration.model_fields
    }
    return ReasonerRegistration(**{**fields, "native_namespaces": namespaces})


def _named(submission, key: str = "widget-12345", namespace: str = WIDGET_REGISTRY):
    """The submission naming a native subject (its entity left as it is)."""
    return submission.model_copy(
        update={
            "native_subject": NativeSubject(
                native_namespace=namespace, native_subject_id=key
            )
        }
    )


def _service(
    plugin: StubResolverPlugin | None = None, *, budget_ms: int = 1_000
) -> tuple[IdentityService, StubResolver | None]:
    if plugin is None:
        return IdentityService(budget_ms=budget_ms), None
    composed = build_resolver(plugin, object())  # type: ignore[arg-type]
    resolver = composed.resolver
    assert isinstance(resolver, StubResolver)
    return IdentityService(composed, budget_ms=budget_ms), resolver


# ---------------------------------------------------------------------------
# Passthrough
# ---------------------------------------------------------------------------


def test_without_a_native_subject_the_subject_is_the_reasoners_own(
    submission, stub_registration
):
    identity = passthrough_identity(
        submission, stub_registration, reason=NO_NATIVE_SUBJECT, resolved_at=NOW
    )
    assert (identity.entity_id, identity.entity_type) == (
        submission.entity_id,
        submission.entity_type,
    )
    assert identity.derivation is None
    provenance = identity.provenance
    assert (provenance.native_namespace, provenance.native_subject_id) == (None, None)
    assert (provenance.resolved_by, provenance.method) == (PASSTHROUGH, "unresolved")
    assert provenance.reason == NO_NATIVE_SUBJECT
    assert provenance.resolved_at == NOW
    assert identity.related == ()


def test_a_declared_namespace_derives_the_subject(submission, stub_registration):
    registration = _declaring(stub_registration, WIDGET_NS)
    identity = passthrough_identity(
        _named(submission), registration, reason=NO_RESOLVER, resolved_at=NOW
    )
    assert identity.entity_id == uuid.uuid5(WIDGET_NS.namespace_uuid, "widget-12345")
    assert identity.entity_type == "widget"
    assert identity.derivation == EntityDerivation(
        namespace_uuid=WIDGET_NS.namespace_uuid, enterprise_key="widget-12345"
    )
    assert identity.provenance.native_subject_id == "widget-12345"
    assert identity.provenance.reason == NO_RESOLVER


def test_an_undeclared_namespace_keeps_the_reasoners_entity_and_says_so(
    submission, stub_registration
):
    identity = passthrough_identity(
        _named(submission, namespace="other-registry"),
        stub_registration,
        reason=NO_RESOLVER,
        resolved_at=NOW,
    )
    assert identity.entity_id == submission.entity_id
    assert identity.derivation is None
    assert identity.provenance.native_namespace == "other-registry"
    reason = identity.provenance.reason or ""
    assert reason.startswith(NO_RESOLVER)
    assert "does not declare namespace 'other-registry'" in reason


def test_a_deployment_declares_each_namespace_once(stub_registration):
    one = _declaring(stub_registration, WIDGET_NS)
    other = one.model_copy(update={"reasoner_id": "other-reasoner"})
    assert declared_namespaces([one, other]) == {WIDGET_REGISTRY: WIDGET_NS}
    assert declared_namespaces([stub_registration]) == {}
    conflicting = _declaring(
        stub_registration,
        WIDGET_NS.model_copy(update={"namespace_uuid": uuid.uuid4()}),
    ).model_copy(update={"reasoner_id": "other-reasoner"})
    with pytest.raises(RegistrationError, match="declared differently"):
        declared_namespaces([one, conflicting])


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class _NotAPlugin:
    resolver_id = "stub-resolver"


@pytest.mark.parametrize(
    ("plugin", "name", "fragment"),
    [
        (_NotAPlugin(), "stub-resolver", "protocol"),
        (StubResolverPlugin(), "other-name", "must equal resolver_id"),
        (StubResolverPlugin("passthrough"), "passthrough", "default"),
        (
            StubResolverPlugin(relationship_types={"fleet"}),  # type: ignore[arg-type]
            "stub-resolver",
            "frozenset",
        ),
        (
            StubResolverPlugin(relationship_types=frozenset({""})),
            "stub-resolver",
            "frozenset",
        ),
        (
            StubResolverPlugin(relationship_types=frozenset({"a\x00b"})),
            "stub-resolver",
            "frozenset",
        ),
    ],
    ids=["protocol", "name", "reserved", "not-frozen", "empty-type", "nul-type"],
)
def test_a_malformed_resolver_plugin_is_refused(plugin, name, fragment):
    with pytest.raises(ResolverPluginError, match=fragment):
        check_resolver_plugin(plugin, name=name)


def test_a_plugin_may_declare_that_it_emits_no_relationship():
    plugin = StubResolverPlugin(relationship_types=frozenset())
    assert check_resolver_plugin(plugin, name="stub-resolver") is plugin


def test_the_passthrough_is_selected_unless_a_resolver_is_named():
    assert select_resolver(None, plugins=[StubResolverPlugin()]) is None
    assert select_resolver(None, entry_points=[]) is None


def test_the_named_resolver_is_selected_from_explicit_plugins():
    wanted, other = StubResolverPlugin(), StubResolverPlugin("other-resolver")
    assert select_resolver("stub-resolver", plugins=[other, wanted]) is wanted
    with pytest.raises(ResolverPluginError, match="exactly one"):
        select_resolver("missing", plugins=[wanted])
    with pytest.raises(ResolverPluginError, match="exactly one"):
        select_resolver("stub-resolver", plugins=[wanted, StubResolverPlugin()])


def _entry_point(name: str, value: str) -> EntryPoint:
    return EntryPoint(name=name, value=value, group=RESOLVER_GROUP)


def test_only_the_named_resolvers_entry_point_is_loaded():
    eps = [
        _entry_point("stub-resolver", "tests._stub_resolver.plugin:PLUGIN"),
        _entry_point("broken", "no_such_module.anywhere:PLUGIN"),
    ]
    plugin = select_resolver("stub-resolver", entry_points=eps)
    assert plugin is not None
    assert plugin.resolver_id == "stub-resolver"


def test_a_named_resolver_that_is_not_installed_refuses_startup():
    eps = [_entry_point("stub-resolver", "tests._stub_resolver.plugin:PLUGIN")]
    with pytest.raises(ResolverPluginError, match=r"installed: \['stub-resolver'\]"):
        select_resolver("crosswalk", entry_points=eps)


def test_a_resolver_entry_point_that_fails_to_load_refuses_startup():
    eps = [_entry_point("broken", "no_such_module.anywhere:PLUGIN")]
    with pytest.raises(ResolverPluginError, match="failed to load"):
        select_resolver("broken", entry_points=eps)


class _ClosingBuild:
    """A plugin whose build returns something that is not a resolver."""

    resolver_id = "stub-resolver"
    relationship_types = frozenset({FLEET})

    def __init__(self, built: object) -> None:
        self.built = built

    def build(self, framework: object) -> object:
        return self.built


class _Closeable:
    closed = False

    def close(self) -> None:
        self.closed = True


class _AsyncResolver(_Closeable):
    async def resolve(self, native_namespace: str, native_subject_id: str) -> None:
        return None


@pytest.mark.parametrize(
    ("built", "fragment"),
    [(_Closeable(), "IdentityResolver protocol"), (_AsyncResolver(), "synchronous")],
    ids=["no-resolve", "coroutine"],
)
def test_a_build_without_a_usable_resolver_is_refused_and_closed(built, fragment):
    with pytest.raises(ResolverPluginError, match=fragment):
        build_resolver(_ClosingBuild(built), object())  # type: ignore[arg-type]
    assert built.closed


# ---------------------------------------------------------------------------
# The bounded call — identify
# ---------------------------------------------------------------------------


async def test_with_no_resolver_every_subject_is_the_passthrough(
    submission, stub_registration
):
    service, _ = _service()
    assert service.resolver_id == PASSTHROUGH
    assert service.relationship_types == frozenset()
    identity = await service.identify(_named(submission), stub_registration)
    assert identity.provenance.resolved_by == PASSTHROUGH
    assert (identity.provenance.reason or "").startswith(NO_RESOLVER)


async def test_a_submission_naming_no_native_subject_is_not_resolved(
    submission, stub_registration
):
    service, resolver = _service(StubResolverPlugin())
    identity = await service.identify(submission, stub_registration)
    assert resolver is not None
    assert resolver.calls == []
    assert identity.entity_id == submission.entity_id
    assert identity.provenance.reason == NO_NATIVE_SUBJECT


async def test_a_match_is_recorded_as_the_resolver_gave_it(
    submission, stub_registration
):
    service, resolver = _service(StubResolverPlugin())
    identity = await service.identify(_named(submission), stub_registration)
    assert resolver is not None
    assert resolver.calls == [(WIDGET_REGISTRY, "widget-12345")]
    assert identity.entity_id == owner_id("owner-7")
    assert identity.entity_type == "owner"
    provenance = identity.provenance
    assert (provenance.resolved_by, provenance.method) == (
        "stub-resolver",
        "deterministic",
    )
    assert provenance.crosswalk_version == CROSSWALK_VERSION
    assert identity.related == (
        RelatedEntity(
            relationship_type=FLEET,
            entity_type="fleet",
            entity_id=fleet_id("fleet-north"),
        ),
    )
    assert service.relationship_types == frozenset({FLEET})


@pytest.mark.parametrize(
    ("key", "namespace"),
    [("widget-unknown", WIDGET_REGISTRY), ("widget-12345", "other-registry")],
    ids=["unknown-key", "unknown-namespace"],
)
async def test_no_match_falls_back_to_the_passthrough(
    submission, stub_registration, key, namespace
):
    service, _ = _service(StubResolverPlugin())
    identity = await service.identify(
        _named(submission, key=key, namespace=namespace), stub_registration
    )
    assert identity.entity_id == submission.entity_id
    assert identity.provenance.resolved_by == PASSTHROUGH
    assert identity.provenance.method == "unresolved"
    assert (identity.provenance.reason or "").startswith(
        "resolver 'stub-resolver' found no match"
    )


async def test_an_unmatched_subject_in_a_declared_namespace_is_its_derivation(
    submission, stub_registration
):
    registration = _declaring(stub_registration, WIDGET_NS)
    service, _ = _service(StubResolverPlugin(crosswalk={}))
    named = _named(submission).model_copy(
        update={"entity_id": WIDGET_NS.entity_id_of("widget-12345")}
    )
    identity = await service.identify(named, registration)
    assert identity.entity_id == WIDGET_NS.entity_id_of("widget-12345")
    assert identity.derivation is not None
    assert "found no match" in (identity.provenance.reason or "")


async def test_a_resolver_error_falls_back_with_a_storable_reason(
    submission, stub_registration
):
    service, resolver = _service(StubResolverPlugin())
    assert resolver is not None
    resolver.error = StubResolverError("crosswalk\x00 unreadable \ud800")
    identity = await service.identify(_named(submission), stub_registration)
    reason = identity.provenance.reason or ""
    assert reason.startswith("resolver 'stub-resolver' raised StubResolverError:")
    assert "\x00" not in reason
    assert "\\x00" in reason
    ResolvedIdentity.model_validate_json(identity.model_dump_json())  # storable


async def test_a_resolver_that_outlives_its_budget_is_not_waited_for(
    submission, stub_registration
):
    service, resolver = _service(StubResolverPlugin(), budget_ms=20)
    assert resolver is not None
    resolver.delay_s = 0.5
    started = time.perf_counter()
    identity = await service.identify(_named(submission), stub_registration)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.4
    assert (identity.provenance.reason or "").startswith(
        "resolver 'stub-resolver' timed out after 20 ms;"
    )
    assert identity.entity_id == submission.entity_id


async def test_a_resolvers_own_timeout_error_is_an_error_not_the_budget(
    submission, stub_registration
):
    service, resolver = _service(StubResolverPlugin())
    assert resolver is not None
    resolver.error = TimeoutError("upstream slow")
    identity = await service.identify(_named(submission), stub_registration)
    assert (identity.provenance.reason or "").startswith(
        "resolver 'stub-resolver' raised TimeoutError: upstream slow;"
    )


def _answer(native: NativeSubject, **overrides: object) -> ResolvedIdentity:
    derivation = EntityDerivation(
        namespace_uuid=uuid.UUID("fc8e0cd7-dbd8-4155-a211-129e7b1af534"),
        enterprise_key="owner-7",
    )
    fields: dict[str, object] = {
        "entity_id": derivation.entity_id,
        "entity_type": "owner",
        "derivation": derivation,
        "provenance": IdentityProvenance(
            native_namespace=native.native_namespace,
            native_subject_id=native.native_subject_id,
            resolved_by="stub-resolver",
            method="deterministic",
            resolved_at=NOW,
        ),
    }
    fields.update(overrides)
    return ResolvedIdentity(**fields)  # type: ignore[arg-type]


_NATIVE = NativeSubject(
    native_namespace=WIDGET_REGISTRY, native_subject_id="widget-12345"
)


def _unusable_answers() -> list[tuple[object, str]]:
    base = _answer(_NATIVE)
    provenance = base.provenance
    return [
        ({"entity_id": str(base.entity_id)}, "not the contract's ResolvedIdentity"),
        (
            base.model_copy(
                update={
                    "derivation": None,
                    "provenance": provenance.model_copy(
                        update={"method": "unresolved", "reason": "unknown"}
                    ),
                }
            ),
            "it is unresolved",
        ),
        (
            base.model_copy(
                update={
                    "provenance": provenance.model_copy(
                        update={"resolved_by": "someone-else"}
                    )
                }
            ),
            "names 'someone-else' in resolved_by",
        ),
        (
            base.model_copy(
                update={
                    "provenance": provenance.model_copy(
                        update={"native_subject_id": "widget-67890"}
                    )
                }
            ),
            "different native subject",
        ),
        (
            _answer(
                _NATIVE,
                related=(
                    RelatedEntity(
                        relationship_type="household",
                        entity_type="g",
                        entity_id=uuid.uuid4(),
                    ),
                ),
            ),
            "undeclared relationship type(s) ['household']",
        ),
        (
            ResolvedIdentity.model_construct(
                entity_id=uuid.uuid4(),
                entity_type="owner",
                derivation=base.derivation,
                provenance=provenance,
                related=(),
            ),
            "the contract does not accept",
        ),
        (
            base.model_copy(update={"entity_type": "own\x00er"}),
            "a NUL character",
        ),
    ]


@pytest.mark.parametrize(
    ("answer", "fragment"),
    _unusable_answers(),
    ids=[
        "wrong-type",
        "unresolved",
        "another-resolver",
        "another-subject",
        "undeclared-type",
        "derivation-does-not-hold",
        "unstorable",
    ],
)
async def test_an_unusable_answer_falls_back_to_the_passthrough(
    submission, stub_registration, answer, fragment
):
    service, resolver = _service(StubResolverPlugin())
    assert resolver is not None
    resolver.answer = answer
    identity = await service.identify(_named(submission), stub_registration)
    assert identity.provenance.resolved_by == PASSTHROUGH
    reason = identity.provenance.reason or ""
    assert reason.startswith("resolver 'stub-resolver' returned an unusable identity")
    assert fragment in reason


def test_identity_problem_accepts_the_contract():
    assert (
        identity_problem(
            _answer(_NATIVE),
            resolver_id="stub-resolver",
            relationship_types=frozenset({FLEET}),
            native=_NATIVE,
        )
        is None
    )


async def test_the_callers_cancellation_still_propagates(submission, stub_registration):
    service, resolver = _service(StubResolverPlugin(), budget_ms=5_000)
    assert resolver is not None
    resolver.delay_s = 0.3
    task = asyncio.create_task(service.identify(_named(submission), stub_registration))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_the_budget_is_positive():
    with pytest.raises(ValueError, match="positive"):
        IdentityService(budget_ms=0)


def test_closing_the_service_closes_its_resolver():
    service, resolver = _service(StubResolverPlugin())
    assert resolver is not None
    service.close()
    assert resolver.closed
    IdentityService().close()  # the passthrough holds nothing


# ---------------------------------------------------------------------------
# The read path — lookup
# ---------------------------------------------------------------------------


async def test_lookup_returns_the_resolvers_match():
    service, _ = _service(StubResolverPlugin())
    result = await service.lookup(_NATIVE, namespaces={})
    assert result.identity is not None
    assert result.identity.entity_id == owner_id("owner-7")
    assert result.reason is None


async def test_lookup_falls_back_to_a_declared_namespace():
    service, _ = _service()
    result = await service.lookup(_NATIVE, namespaces={WIDGET_REGISTRY: WIDGET_NS})
    assert result.identity is not None
    assert result.identity.entity_id == WIDGET_NS.entity_id_of("widget-12345")
    assert result.identity.provenance.method == "unresolved"
    assert result.reason == NO_RESOLVER


async def test_lookup_finds_nothing_for_an_unmatched_undeclared_subject():
    service, _ = _service(StubResolverPlugin())
    native = NativeSubject(native_namespace="other-registry", native_subject_id="k")
    result = await service.lookup(native, namespaces={WIDGET_REGISTRY: WIDGET_NS})
    assert result.identity is None
    assert result.reason == "resolver 'stub-resolver' found no match"


@pytest.mark.parametrize(
    ("namespace", "key"), [("", "k"), ("n", ""), ("n\x00", "k"), ("n", "\ud800")]
)
def test_a_read_path_native_subject_is_non_empty_and_storable(namespace, key):
    with pytest.raises(ValueError, match="native subject"):
        native_subject(namespace, key)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", None), ("  ", None), ("passthrough", None), (" crosswalk ", "crosswalk")],
)
def test_the_setting_names_the_active_resolver_or_keeps_the_passthrough(
    value, expected
):
    from app.settings import FrameworkSettings

    settings = FrameworkSettings(decisionledger_identity_resolver=value)
    assert settings.identity_resolver_id == expected
