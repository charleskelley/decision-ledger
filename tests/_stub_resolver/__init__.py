"""The stub identity resolver — a test-only resolver for the identity contract.

The resolver-side twin of ``tests/_stub_reasoner`` and ``tests/_fake_gate``
(DR-32): the smallest thing that satisfies what the framework asks of an
enterprise's identity resolver — an ``IdentityResolverPlugin`` (``PLUGIN``)
that declares the one relationship type it emits (``fleet``) and builds a
synchronous ``IdentityResolver`` answering from an in-memory crosswalk.

The domain is the stub reasoner's widgets: a widget named in the native
namespace ``widget-registry`` belongs to an enterprise *owner*, and an owner
may belong to a *fleet*. Tests script the resolver (a delay, an error, an
answer of their own) to drive every fallback the ledger takes.

Framework tests compose it the way a deployment would — passed to
``memory_framework(resolver_plugin=...)`` or
``build_framework_services(..., resolver_plugins=[...])``, or published as an
``importlib.metadata.EntryPoint`` under ``decisionledger.identity_resolvers``
— so no framework code ever names it.
"""
