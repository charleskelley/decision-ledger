# Identity

The identity contract (DR-32) spans `core.identity` (the pure contract) and
`app.identity` (the ledger's identity step). Both render in full on the
[`core`](core.md) and [`app`](app.md) pages; this page is the index into
them, in the order a decision meets the contract. The design is in
[Identity](../design/identity.md).

## The contract (`core.identity`)

- [`NativeSubject`][core.identity.NativeSubject]: the subject a reasoner
  scored, in its own terms (a native namespace and the subject's key in it),
  carried on its `Submission`.
- [`NativeNamespace`][core.identity.NativeNamespace]: a native namespace a
  registration declares: the subject type it names and the UUID5 namespace
  its ids derive under.
- [`IdentityResolver`][core.identity.IdentityResolver]: the protocol an
  enterprise's resolver implements.
- [`ResolvedIdentity`][core.identity.ResolvedIdentity]: the decision's
  subject as the ledger records it, with its
  [`EntityDerivation`][core.identity.EntityDerivation],
  [`IdentityProvenance`][core.identity.IdentityProvenance], and flat, typed
  [`RelatedEntity`][core.identity.RelatedEntity] values.
- [`derive_entity_id`][core.identity.derive_entity_id]: the UUID5 arithmetic
  the resolver names and core performs.
- The journal and its projection:
  [`EntityEvent`][core.identity.EntityEvent] (with
  [`JournalProvenance`][core.identity.JournalProvenance]),
  [`Membership`][core.identity.Membership], the fold
  [`project_membership`][core.identity.project_membership], and the
  timeline's walk [`linked_entities`][core.identity.linked_entities].

## The identity step (`app.identity`)

- [`IdentityResolverPlugin`][app.identity.resolvers.IdentityResolverPlugin]:
  what an enterprise publishes under the `decisionledger.identity_resolvers`
  entry-point group; [`select_resolver`][app.identity.resolvers.select_resolver]
  and [`build_resolver`][app.identity.resolvers.build_resolver] compose the
  one that `DECISIONLEDGER_IDENTITY_RESOLVER` names.
- [`IdentityService`][app.identity.resolvers.IdentityService]: the bounded
  call to the active resolver that `app.submit` makes after intake and
  before the gates, with its fallback.
- [`passthrough_identity`][app.identity.passthrough.passthrough_identity]:
  the passthrough, the subject the reasoner named, recorded unresolved;
  [`declared_identity`][app.identity.passthrough.declared_identity] is its
  derivation for a native subject in a namespace the registration declares.

## Storage and routes

- [`EntityJournal`][app.audit.entity_journal.EntityJournal]: appends to the
  entity journal and rebuilds the membership projection.
- [`resolve_identity`][app.api.identity.resolve_identity]: the operation
  behind `GET /api/v1/identity/resolve`;
  [`entity_decisions`][app.api.entities.entity_decisions]: the subject
  timeline behind `GET /api/v1/entities/{entity_id}/decisions`.
