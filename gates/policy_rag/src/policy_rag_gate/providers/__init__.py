"""Retrieval providers of the RAG policy gate (DR-34).

The gate retrieves through providers, one per corpus a reasoner's binding
names (``CorpusRef.provider``). Providers are the gate's own plugins,
discovered under the gate-owned entry-point group
``policy_rag_gate.providers`` (``PROVIDER_GROUP``); the framework never sees
them, and no reasoner imports this package. ``reference`` — today's hybrid
pgvector + Elasticsearch + cross-encoder stack over the corpora the corpus
loader loads (``policy_rag_gate.providers.reference``) — ships in this
member and is the default.

``ProviderRegistry`` holds the discovered plugins and builds each provider
the first time a binding names it (at startup, when the gate admits the
binding), so a deployment opens only the providers its registrations use.

Public API:
    RetrievalProvider, ProviderResult, ProviderPlugin, ProviderServices,
    ProviderContractError: the seam (``policy_rag_gate.providers.protocol``).
    DocumentFetcher, DocumentBlob, DocumentNotHeldError: the optional
    capability to fetch whole documents (escrow and verification).
    ProviderRegistry: discovered plugins, built on first use.
    discover_provider_plugins: read the entry-point group.
"""

from __future__ import annotations

import importlib.metadata as metadata
import threading
from typing import TYPE_CHECKING

import structlog

from policy_rag_gate.providers.protocol import (
    DocumentBlob,
    DocumentFetcher,
    DocumentNotHeldError,
    ProviderContractError,
    ProviderPlugin,
    ProviderResult,
    ProviderServices,
    RetrievalProvider,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from importlib.metadata import EntryPoint

log = structlog.get_logger(__name__)

#: The gate-owned entry-point group providers are published under.
PROVIDER_GROUP = "policy_rag_gate.providers"


def discover_provider_plugins(
    entry_points: Iterable[EntryPoint] | None = None,
) -> dict[str, ProviderPlugin]:
    """Load every provider plugin published under ``PROVIDER_GROUP``.

    Args:
        entry_points: Explicit entry points instead of the installed ones
            (tests).

    Returns:
        ``provider_id`` → plugin.

    Raises:
        ValueError: Two entry points share a name, or a plugin's
            ``provider_id`` is not its entry point's name.
    """
    eps = (
        list(entry_points)
        if entry_points is not None
        else list(metadata.entry_points(group=PROVIDER_GROUP))
    )
    plugins: dict[str, ProviderPlugin] = {}
    for ep in sorted(eps, key=lambda ep: ep.name):
        if ep.name in plugins:
            msg = f"two {PROVIDER_GROUP} entry points are named {ep.name!r}"
            raise ValueError(msg)
        plugin = ep.load()
        if getattr(plugin, "provider_id", None) != ep.name:
            msg = (
                f"the {PROVIDER_GROUP} entry point {ep.name!r} loads a plugin whose "
                f"provider_id is {getattr(plugin, 'provider_id', None)!r}"
            )
            raise ValueError(msg)
        plugins[ep.name] = plugin
    return plugins


class ProviderRegistry:
    """The providers a policy gate retrieves through, each built on first use.

    Args:
        plugins: ``provider_id`` → plugin, built lazily with ``services``.
        services: What plugins build their providers from; required when
            ``plugins`` is not empty.
        providers: Providers already built (tests, or a gate assembled by
            hand), served as they are.

    Raises:
        ValueError: A provider id is both built and a plugin, or plugins are
            given without services.
    """

    def __init__(
        self,
        plugins: Mapping[str, ProviderPlugin] | None = None,
        *,
        services: ProviderServices | None = None,
        providers: Iterable[RetrievalProvider] = (),
    ) -> None:
        """Hold the plugins and any providers already built."""
        self._plugins = dict(plugins or {})
        self._services = services
        self._built: dict[str, RetrievalProvider] = {}
        for provider in providers:
            if provider.provider_id in self._built:
                msg = f"two providers are named {provider.provider_id!r}"
                raise ValueError(msg)
            self._built[provider.provider_id] = provider
        clash = sorted(set(self._plugins) & set(self._built))
        if clash:
            msg = f"providers {clash} are given both built and as plugins"
            raise ValueError(msg)
        if self._plugins and services is None:
            msg = "provider plugins need ProviderServices to be built"
            raise ValueError(msg)
        self._lock = threading.Lock()

    @classmethod
    def of(cls, *providers: RetrievalProvider) -> ProviderRegistry:
        """A registry of providers already built (tests, hand-assembled gates)."""
        return cls(providers=providers)

    def provider_ids(self) -> list[str]:
        """Every provider this registry can serve, sorted."""
        return sorted({*self._plugins, *self._built})

    def get(self, provider_id: str) -> RetrievalProvider:
        """Return the provider, building it on first use.

        Args:
            provider_id: The id a corpus reference names.

        Returns:
            The provider.

        Raises:
            LookupError: No provider of that id is installed.
            Exception: Whatever the plugin's ``build`` raises (it is tried
                again on the next call).
        """
        built = self._built.get(provider_id)
        if built is not None:
            return built
        if provider_id not in self._plugins:
            msg = (
                f"no retrieval provider {provider_id!r} is installed (installed: "
                f"{self.provider_ids()}; providers are published under the "
                f"{PROVIDER_GROUP} entry-point group)"
            )
            raise LookupError(msg)
        with self._lock:
            built = self._built.get(provider_id)
            if built is None:
                assert self._services is not None
                built = self._plugins[provider_id].build(self._services)
                self._built = {**self._built, provider_id: built}
                log.info(
                    "policy_gate.provider_built",
                    component="policy_gate",
                    provider_id=provider_id,
                )
        return built

    def close(self) -> None:
        """Close every provider built here that has a ``close()``."""
        for provider in self._built.values():
            close = getattr(provider, "close", None)
            if callable(close):
                close()


__all__ = [
    "PROVIDER_GROUP",
    "DocumentBlob",
    "DocumentFetcher",
    "DocumentNotHeldError",
    "ProviderContractError",
    "ProviderPlugin",
    "ProviderRegistry",
    "ProviderResult",
    "ProviderServices",
    "RetrievalProvider",
    "discover_provider_plugins",
]
