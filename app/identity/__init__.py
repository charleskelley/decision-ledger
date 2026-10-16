"""The ledger's identity step: resolvers, the passthrough, discovery (DR-32).

Every decision's subject is established by the ledger, after intake
validation and before any gate runs, through the deployment's one active
identity resolver — a plugin the enterprise supplies under the
``decisionledger.identity_resolvers`` entry-point group, named by
``DECISIONLEDGER_IDENTITY_RESOLVER`` — and recorded on the bundle with its
provenance. The framework implements no identity resolution: its default,
the **passthrough**, records the subject the reasoner named, unresolved,
and it is also what every resolver falls back to (no match, an error, a
timeout, an unusable answer). A decision is never dropped over its
identity.

Modules:
    passthrough: The passthrough subject, and the deployment's native
                 namespace declarations.
    resolvers:   The resolver plugin seam, its discovery, the validation of
                 a resolver's answer, and ``IdentityService`` (the bounded
                 call with its fallback).
"""

from app.identity.passthrough import (
    NO_NATIVE_SUBJECT,
    NO_RESOLVER,
    PASSTHROUGH,
    declared_identity,
    declared_namespaces,
    passthrough_identity,
)
from app.identity.resolvers import (
    DEFAULT_IDENTITY_BUDGET_MS,
    RESOLVER_GROUP,
    ComposedResolver,
    IdentityLookup,
    IdentityResolverPlugin,
    IdentityService,
    build_resolver,
    check_resolver_plugin,
    identity_problem,
    native_subject,
    select_resolver,
)

__all__ = [
    "DEFAULT_IDENTITY_BUDGET_MS",
    "NO_NATIVE_SUBJECT",
    "NO_RESOLVER",
    "PASSTHROUGH",
    "RESOLVER_GROUP",
    "ComposedResolver",
    "IdentityLookup",
    "IdentityResolverPlugin",
    "IdentityService",
    "build_resolver",
    "check_resolver_plugin",
    "declared_identity",
    "declared_namespaces",
    "identity_problem",
    "native_subject",
    "passthrough_identity",
    "select_resolver",
]
