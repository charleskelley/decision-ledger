# Identity

DecisionLedger requires every decision to carry a resolved subject identity,
obtains it through a resolver the enterprise supplies, records the mapping and
its version on the bundle, and journals merges and detaches. It does not
implement identity resolution: the matching logic, the crosswalk, and the
master entity index belong to the enterprise. A wrong mapping becomes visible
and replayable rather than silent.

In practice: a reasoner names the subject it scored in its own terms. The
ledger asks the deployment's one active identity resolver who that subject is,
before any gate runs, and writes the answer (or the reason there is none) into
the bundle's identity layer. When no resolver is installed, the built-in
**passthrough** records the reasoner's own subject, unresolved. Merges,
detaches, and relationships between subjects are appended to a journal, and a
subject's timeline reads a projection of that journal. The record is
[DR-32](decisions.md#dr-32-identity-contract-resolved-subjects-provenance-entity-journals);
the code is `core/identity.py` (the contract, pure) and `app/identity/` (the
step, the passthrough, discovery).

## The contract

Every type below lives in `core/identity.py`, a leaf of `core/` that imports
nothing from the rest of it. Every model is strict, frozen, and forbids extra
fields.

The **subject** is who the decision affects. The **native subject** is what
the reasoner scored. They can differ: a reasoner may score a login account
while the enterprise's subject is the customer who owns it.

| Type | What it is | Fields |
|------|------------|--------|
| `NativeSubject` | The subject as the reasoner knows it, carried on `Submission.native_subject` (optional). | `native_namespace`, `native_subject_id` (both non-empty) |
| `NativeNamespace` | A namespace a reasoner declares in its registration. | `native_namespace`, `namespace_uuid` (the UUID5 namespace its ids derive under), `entity_type` (the subject type it names) |
| `ResolvedIdentity` | The decision's subject: the bundle's identity layer. | `entity_id`, `entity_type`, `derivation`, `provenance`, `related` |
| `EntityDerivation` | How an id follows from an enterprise key. | `namespace_uuid`, `enterprise_key`; property `entity_id` |
| `IdentityProvenance` | How the subject was established. | see below |
| `RelatedEntity` | A subject related to the decision's subject. | `relationship_type`, `entity_type`, `entity_id` |

`derive_entity_id(namespace_uuid, enterprise_key)` is UUID5 of the key under
the namespace (an empty key is a `ValueError`). The same key under the same
namespace is the same id in every process, and the id does not reveal the key.
A resolver names the namespace and the key; core does the arithmetic.
`ResolvedIdentity` recomputes `derivation.entity_id` and refuses an identity
whose `entity_id` does not match it.

**`ResolvedIdentity`** rules, enforced by the model:

- `derivation` is required unless the method is `unresolved`. It is `None`
  only for an unresolved subject that is the reasoner's own asserted
  `entity_id`.
- `related` holds each `(relationship_type, entity_id)` pair once, never the
  subject itself, and is empty when the subject is unresolved.
- The `resolved` property is `True` when the method is not `unresolved`.

**`IdentityProvenance`** fields:

| Field | Meaning |
|-------|---------|
| `native_namespace`, `native_subject_id` | The native subject the reasoner named. Both `None` when its submission named none; set together or not at all. |
| `resolved_by` | The active resolver's id, or `passthrough` when the ledger kept the reasoner's own subject. |
| `crosswalk_version` | The version of the mapping the resolver used, if it versions one. Optional. |
| `method` | `deterministic` (an exact crosswalk match), `probabilistic` (a scored match), or `unresolved` (no enterprise match). |
| `confidence` | In [0, 1]. Set exactly when the method is `probabilistic`. |
| `resolved_at` | When the subject was established (timezone-aware). |
| `reason` | Why the subject is unresolved. Set exactly when the method is `unresolved`. |

**`IdentityResolver`** is a runtime-checkable protocol with one synchronous
method:

```python
def resolve(
    self, native_namespace: str, native_subject_id: str
) -> ResolvedIdentity | None: ...
```

It returns a `deterministic` or `probabilistic` identity, with its derivation,
its provenance (echoing the native subject and naming the resolver in
`resolved_by`), and its related subjects, or `None` when it has no match. A
resolver never returns an unresolved identity: recording one is the ledger's
job.

The same module holds the journal's types (`EntityEvent`, `JournalProvenance`,
`Membership`), the pure fold `project_membership`, and the timeline walk
`linked_entities`; see [Journal and projection](#journal-and-projection) and
[Timelines](#timelines).

## The identity step

The step is step 2 of `app/submit.py`: after intake validation and before
any gate runs. `decide()` (and so `submit()`) calls
`IdentityService.identify(submission, registration)` on the deployment's
service, `FrameworkServices.identity`, which `app/bootstrap.py` builds with the
active resolver (or none). The same step serves every entry point: an HTTP
request, a `LocalClient` call, and an eval run.

1. A submission that names no native subject gets the passthrough subject at
   once (reason: the submission names no native subject).
2. Otherwise the service calls `resolver.resolve(native_namespace,
   native_subject_id)` in a worker thread (`asyncio.to_thread`) under a
   wall-clock budget, `DEFAULT_IDENTITY_BUDGET_MS = 50` milliseconds.
3. The answer is used only if all of these hold (`identity_problem` in
   `app/identity/resolvers.py`):
    - it is exactly a `ResolvedIdentity`, not a subclass;
    - it serializes, survives the JSON round trip unchanged, and holds only
      values the ledger can store;
    - its method is `deterministic` or `probabilistic`;
    - its `provenance.resolved_by` is the active resolver's id;
    - its provenance names the native subject that was asked about;
    - every `related` entry uses a relationship type the resolver declares.
4. Anything else falls back to the [passthrough](#the-passthrough): no active
   resolver, `None` (no match), an exception the resolver raises, the budget
   running out, or an unusable answer. The fallback's `reason` says which.
   When a resolver is active and its answer is not used, the step logs
   `identity.unresolved` with the reason and its `duration_ms`.

The result becomes the bundle's `identity` and every gate's
`GateRequest.identity`, so a gate about a person (a do-not-contact list, a
sanctions screen) keys on the resolved subject rather than the reasoner's own
id. The step's time is `latency_breakdown["identity_ms"]`.

The step never refuses a decision: `identify` does not raise for the
resolver's sake. The budget bounds the wait, not the work. Python cannot stop
a thread, so a resolver that never returns keeps its worker thread busy while
the decision proceeds unresolved. Resolvers are in-process lookups by
contract. The budget is a constructor argument of `IdentityService`; no
environment variable sets it.

## The passthrough

The passthrough (`app/identity/passthrough.py`) is the default when no
resolver is active, and every resolver's fallback. It records the subject the
reasoner itself named, always `unresolved`, with `resolved_by` set to
`passthrough` and the reason:

- A native subject in a namespace the reasoner's registration declares is the
  subject that declaration derives: `derive_entity_id(namespace_uuid,
  native_subject_id)`, of the declared `entity_type`, with the derivation
  recorded so anyone can recompute the id from the bundle.
- Any other subject (no native subject, or one in a namespace the
  registration does not declare) is the submission's own `entity_id` and
  `entity_type`, with no derivation. For an undeclared namespace the reason
  says so.

The ledger invents no second derivation: an unresolved subject in an
undeclared namespace is the id the reasoner derived under its own namespace.
An unresolved subject is related to nothing. It joins an enterprise subject
only through a later journal event (a `merge`).

ATO, the reference reasoner, is the worked example. ATO's registration
declares the namespace `ato-account` (subject type `account`, the UUID5
namespace ATO's account ids have always derived under), and every ATO
submission names `(ato-account, account_id)` as its native subject. With no
resolver installed, the passthrough derives exactly the `entity_id` and
`entity_type` ATO decisions always had. These names are ATO's, declared in
`ato_reasoner/registry.py` (`ATO_ACCOUNT_NAMESPACE`); the framework knows
none of them.

The example reasoner (`reasoners/example`) names no native subject, so the
passthrough records its decisions under the `entity_id` it sends.

## Registration and intake

A reasoner declares each native namespace it names subjects in under
`ReasonerRegistration.native_namespaces`: a tuple of `NativeNamespace`, each
namespace name once.

- **One declaration per name per deployment.** Registrations that declare the
  same namespace name must declare it identically (the same UUID5 namespace
  and subject type), so two reasoners naming subjects in one enterprise
  namespace derive the same ids. `app/bootstrap.py` checks this
  (`declared_namespaces`) and refuses startup with a `RegistrationError` on a
  conflict.
- **Versions.** The canonical form of a registration
  (`canonical_registration`) sorts the declarations by name and omits the
  field when it is empty, so a registration that declares no namespace keeps
  the `registration_version` it had before the field existed.
- **Intake.** A native subject in a namespace the reasoner's registration
  declares must come with the `entity_id` and `entity_type` that declaration
  derives. Otherwise `validate_submission` raises `NATIVE_SUBJECT_MISMATCH`,
  which the API answers as `422`. The detail names the namespace and the
  expected and submitted ids, never the native key. A namespace the
  registration does not declare is not checked.

`Submission.entity_id` stays required. A reasoner still sends its own id for
the subject it scored; the ledger's subject is the resolved identity.

## The bundle's identity layer

`DecisionBundle.identity` is the `ResolvedIdentity` the step produced. It is
`None` only on a bundle written before the identity contract. When set, its
provenance must name the native subject the submission named (or none, when
the submission named none); the bundle model refuses anything else.

- `DecisionBundle.entity_id` and `entity_type` (properties) are the resolved
  subject; on a bundle without an identity they fall back to the
  submission's.
- The `decisionledger.decision_bundles.entity_id` column is the resolved
  subject, so a timeline is filed under it.
- The submission inside the bundle keeps the reasoner's own `entity_id` and
  `entity_type`, verbatim.
- The receipt a write returns (`DecisionReceipt`) carries the resolved
  `entity_id` and its `entity_type`.

Replay is unaffected. Enforcement never reads the identity, so replaying a
decision re-runs the same tiers on the same stored gate outputs whatever the
identity says. A stored identity whose id no longer follows from its
derivation does not reload.

## Journal and projection

Two framework tables, both in `app/audit/sql/02_tables.sql`, hold how subjects
relate over time. See [Infrastructure](infrastructure.md#database-schema) for
their columns.

**`decisionledger.entity_events`** is the journal: every change to the
identity graph, append-only.

| Event | Meaning |
|-------|---------|
| `merge(subject, object)` | The two ids are one subject; `subject` is the id kept for display. |
| `detach(subject, object)` | Ends a merge of the two ids, in either order. |
| `relate(subject, object, type)` | `subject` stands in a relationship of `type` to `object`. |
| `unrelate(subject, object, type)` | Ends that relationship. |

Events carry framework UUIDs only (native keys stay in the bundles), an
`effective_at` time, and a `JournalProvenance` (`recorded_by`, optional
`crosswalk_version`, optional `reference`). Rows are never updated or deleted;
a correction is a new event. An identity column (`event_sequence`) orders
appends, and check constraints keep events well formed (a known type, a
relationship type exactly on `relate` and `unrelate`, two different ids).
Bundles never change: a decision keeps the identity it was made under, and
the journal records what changed since.

**`decisionledger.entity_membership`** is the projection: validity intervals
(`valid_from`, `valid_to`; `valid_to` null while the membership holds),
derived and never authoritative. `core.identity.project_membership(events,
identities)` is its definition. It folds two sources in time order: each
relationship a decision's identity asserts (`ResolvedIdentity.related`), as a
`relate` at the identity's `resolved_at`, and each journal event at its
`effective_at`. The fold's rules:

- A `relate` or `merge` opens an interval unless one is already open for the
  same link; an `unrelate` or `detach` closes the open one, if any.
- A merge is a membership row with a null relationship type. Its link is the
  unordered pair of ids, so `detach` matches the two ids in either order. A
  relationship's link is `(subject, object, relationship_type)`.
- An interval that would end the instant it begins is dropped.
- At one instant, asserted relationships apply before journal events, and
  journal events apply in the order given, so the journal has the last word.

**The write path.** Writing a new bundle (`BundleStore.write` and
`write_many`) opens, in the projection, the relationships its identity
asserts, unless they are already open (a partial unique index on open links,
`nulls not distinct`, makes a repeat a no-op). The write path only opens
intervals; the rebuild is the definition.

**`EntityJournal`** (`app/audit/entity_journal.py`) appends and rebuilds, in
process. There is no HTTP route that writes the journal: who appends is the
enterprise's business (its resolver tooling when a crosswalk version changes,
an operator correcting a mapping).

- Give it a connection of its own. `append` and `rebuild_membership` each run
  in one transaction, which the framework's shared autocommit connection
  never opens.
- It is constructed with the relationship types the active resolver declares.
  A `relate` of any other type is refused; an `unrelate` may close any type,
  so a link a former resolver emitted can still be ended.
- `append(events)` checks every event before any is written, then writes them
  and (by default) rebuilds the projection in the same transaction. Pass
  `rebuild=False` to append several batches and rebuild once.
- `rebuild_membership()` locks the projection against writes, folds the
  journal and every version-2 bundle whose identity asserts a relationship,
  and replaces the projection with the result.

## Timelines

`GET /api/v1/entities/{entity_id}/decisions` lists one subject's decisions,
newest first, paged with `limit` (at most 500), `before`, and `before_id`. A
reasoner key sees its own reasoner's decisions only; the operator key sees
every reasoner's, optionally narrowed with `reasoner_id`.

The timeline walks the projection's current rows (`linked_entities`):

1. The subject and every id merged with it, transitively, in either
   direction.
2. With `include_related=<relationship_type>`, every subject one current
   relationship of that type away from any of those ids, in either direction
   (a subject's group, or a group's members), plus the ids merged with each of
   them.

Each entry (`EntityDecision`) says which subject it is about (`entity_id`,
`entity_type`) and how that subject is linked: `relationship_type` is `None`
for the subject and the ids merged with it, and the requested type for a
related subject. The clients take the same parameter
(`LedgerClient.entity_decisions(..., include_related=...)`), as does the MCP
tool `list_decisions_for_entity`.

What it does not do:

- **No as-of-time timeline.** The walk reads the projection as it holds now.
  The bundle itself records the subject's identity, and its asserted
  relationships, as of the decision.
- **No transitive walk over relationships.** `include_related` follows one
  step of one type; only merges are followed transitively.
- **No relationship meaning.** A relationship type is an opaque label the
  resolver declared; the framework attaches no semantics to it.

## The resolve route

`GET /api/v1/identity/resolve?namespace=<native namespace>&native_id=<key>`
answers with the `ResolvedIdentity` a decision naming that native subject
would record.

- Any authenticated key may call it (a reasoner key or the operator key): an
  identity is the enterprise's mapping, not a tenant's decisions.
- The active resolver's match, when it matches and the answer is usable.
- Otherwise, when a registration in the deployment declares the namespace,
  the passthrough subject of that declaration, unresolved, with the reason.
- Otherwise `404 IDENTITY_UNRESOLVED`. The problem detail names the namespace
  and the reason, never the native key.
- An empty or unstorable parameter is `422 REQUEST_INVALID`.

Both clients expose it as `LedgerClient.resolve_identity(native_namespace,
native_subject_id)` (`app/client.py`). A subject selector (search by any
native id, then read the subject's timeline) is built on this route and the
timeline route.

## Shipping a resolver

An enterprise ships its resolver as an installed package that publishes a
plugin object under the `decisionledger.identity_resolvers` entry-point
group, the way reasoners and gates join a deployment. The plugin satisfies
`app.identity.IdentityResolverPlugin`:

| Member | Requirement |
|--------|-------------|
| `resolver_id: str` | Equal to the entry-point name, never `passthrough`. It is the value of `DECISIONLEDGER_IDENTITY_RESOLVER` and the `resolved_by` of every identity the resolver returns. |
| `relationship_types: frozenset[str]` | Every relationship type the resolver emits in `ResolvedIdentity.related`: non-empty, storable strings; empty when it emits none. |
| `build(framework) -> IdentityResolver` | Builds the runtime resolver against the shared services (settings, Postgres, the store, the registry; gates are not built yet). If the returned object defines `close()`, the framework calls it on shutdown. |

The runtime resolver is synchronous (a coroutine `resolve` is refused), runs
in process, and must answer well inside the 50 ms budget. It returns `None`
for no match, never an unresolved identity.

**Selection and failure.** One resolver is active per deployment, named by
`DECISIONLEDGER_IDENTITY_RESOLVER`. Blank or `passthrough` keeps the
passthrough even when resolver plugins are installed; an installed resolver
is never activated implicitly. Only the selected entry point is loaded. At
startup, `ResolverPluginError` refuses a name with no (or more than one)
matching entry point, an entry point that fails to load, a plugin that does
not satisfy the protocol, whose `resolver_id` differs from the entry-point
name or is `passthrough`, or whose `relationship_types` is not a frozenset of
storable non-empty strings, and a `build()` that returns something other
than a synchronous `IdentityResolver` (that object is closed first). An
exception `build()` raises propagates as itself and also stops startup. The
selection checks run before the bootstrap opens its Postgres connection; when
`build()` fails, the bootstrap closes that connection before the error
propagates.

A minimal package, with every framework name real (the package, the
namespace, and the relationship type are illustrative, one enterprise's):

```toml
# pyproject.toml of the enterprise's resolver package
[project]
name = "acme-identity"
dependencies = ["decision-ledger"]

[project.entry-points."decisionledger.identity_resolvers"]
acme-crosswalk = "acme_identity.plugin:PLUGIN"
```

```python
# acme_identity/plugin.py
from datetime import UTC, datetime
from uuid import UUID

from app.plugin import FrameworkServices
from core.identity import (
    EntityDerivation,
    IdentityProvenance,
    RelatedEntity,
    ResolvedIdentity,
)

# The enterprise's own UUID5 namespaces (illustrative values).
CUSTOMER_NAMESPACE = UUID("5d0c7a52-3f1e-4b8e-9a61-2c4f7e9b1d03")
HOUSEHOLD_NAMESPACE = UUID("b7e2c1f4-8a3d-4e5b-9c6f-0d1a2b3c4e5f")


def load_crosswalk() -> tuple[dict[tuple[str, str], tuple[str, str]], str]:
    """The enterprise's own loader: the crosswalk and its version."""
    raise NotImplementedError


class CrosswalkResolver:
    """(native namespace, native key) -> (customer key, household key)."""

    def __init__(
        self, crosswalk: dict[tuple[str, str], tuple[str, str]], version: str
    ) -> None:
        self._crosswalk = crosswalk
        self._version = version

    def resolve(
        self, native_namespace: str, native_subject_id: str
    ) -> ResolvedIdentity | None:
        match = self._crosswalk.get((native_namespace, native_subject_id))
        if match is None:
            return None  # no match: the ledger records the subject unresolved
        customer_key, household_key = match
        derivation = EntityDerivation(
            namespace_uuid=CUSTOMER_NAMESPACE, enterprise_key=customer_key
        )
        return ResolvedIdentity(
            entity_id=derivation.entity_id,
            entity_type="customer",
            derivation=derivation,
            provenance=IdentityProvenance(
                native_namespace=native_namespace,
                native_subject_id=native_subject_id,
                resolved_by="acme-crosswalk",
                crosswalk_version=self._version,
                method="deterministic",
                resolved_at=datetime.now(UTC),
            ),
            related=(
                RelatedEntity(
                    relationship_type="household",
                    entity_type="household",
                    entity_id=EntityDerivation(
                        namespace_uuid=HOUSEHOLD_NAMESPACE,
                        enterprise_key=household_key,
                    ).entity_id,
                ),
            ),
        )

    def close(self) -> None:
        """Release the crosswalk (called on shutdown)."""


class CrosswalkResolverPlugin:
    resolver_id = "acme-crosswalk"
    relationship_types = frozenset({"household"})

    def build(self, framework: FrameworkServices) -> CrosswalkResolver:
        crosswalk, version = load_crosswalk()
        return CrosswalkResolver(crosswalk, version)


PLUGIN = CrosswalkResolverPlugin()
```

**The reference double.** `tests/_stub_resolver` is the test-only resolver
the framework's tests compose (it is never installed as an entry point, and
no framework file names it). Its plugin (`resolver_id` `stub-resolver`)
declares one relationship type, `fleet`. Its resolver maps a widget in the
native namespace `widget-registry` to an enterprise owner (`entity_type`
`owner`, `deterministic`, crosswalk version `stub-crosswalk-1`) with the
owner's fleet as its one related subject; any other namespace, or an unknown
widget, is no match. Tests script a delay, an error, or an answer of their
own to drive every fallback.

## What the framework does not do

- **No matching.** The framework never decides that two native subjects are
  one person. The resolver does, and the journal records what changed.
- **No crosswalk and no master entity index.** Both are the enterprise's,
  behind its resolver. The passthrough derives an id from a declared
  namespace; it does not look anything up.
- **No relationship semantics.** Relationship types are labels the resolver
  declares, validated, journaled, and projected as opaque strings. A
  household is an example of a type an enterprise resolver might declare,
  never a framework concept.
- **No as-of timelines.** The timeline reads the current projection; the
  bundle is the record of what was known at decision time.
- **No dropped decisions.** A subject that does not resolve is recorded
  unresolved, with the reason, and the decision is written.

## Related

- [DR-12](decisions.md#dr-12-entity_id-as-the-universal-subject-identity-primitive)
  (`entity_id` as the universal subject), extended by DR-32.
- [DR-14](decisions.md) (the reasoner's assembled output), amended by DR-32:
  the framework, not the reasoner, assigns a decision's subject.
- [DR-32](decisions.md#dr-32-identity-contract-resolved-subjects-provenance-entity-journals)
  (the identity contract), including its implementation notes and judgment
  calls.
- [Submission contract](reasoners/reasoner-handoff.md): where
  `native_subject` and `native_namespaces` sit in a submission and a
  registration.
- [Infrastructure](infrastructure.md): the `entity_events` and
  `entity_membership` tables.
- [Identity reference](../reference/identity.md): the API reference for
  `core.identity` and `app.identity`.
