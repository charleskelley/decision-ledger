"""Framework exception types for DecisionLedger intake and contract violations."""

from __future__ import annotations

from typing import Any


class UnregisteredReasonerError(Exception):
    """Raised when a submission names a reasoner the ledger does not know.

    This is a configuration error: the submission is structurally valid but
    the framework does not recognize the submitting reasoner. The reasoner
    must be registered via a ``ReasonerRegistry`` before its submissions can
    be accepted. Replay raises it too when a stored bundle's reasoner is not
    registered in this deployment.

    Args:
        reasoner_id: The unrecognized ``reasoner_id``.
    """

    def __init__(self, reasoner_id: str) -> None:  # noqa: D107
        self.reasoner_id = reasoner_id
        super().__init__(
            f"Unregistered reasoner [{reasoner_id}]: reasoner must be registered "
            "with the DecisionLedger framework before submitting decisions"
        )


class SubmissionContractError(Exception):
    """Raised when a reasoner's ``Submission`` violates the intake contract.

    Identifies the violating reasoner, the contract clause that was broken,
    and a stable machine-readable ``code`` so callers (and, from the HTTP
    surface on, API clients) can tell the failures apart without parsing the
    message. Raised before any gate runs; nothing is persisted.

    Codes:
        ``SUBMISSION_INVALID``: a shape or consistency rule (the default).
        ``BYPASS_RULE_UNREGISTERED``: a record cites a rule the reasoner's
            registration does not declare.
        ``BYPASS_RULE_VERSION_MISMATCH``: a record pins a rule version other
            than the registered one.
        ``BYPASS_ACTION_MISMATCH``: a record's proposed action is not the
            action its cited rule justifies.
        ``ACTION_NOT_RECORDABLE``: the reasoner may not record that action.
        ``NATIVE_SUBJECT_MISMATCH``: the submission names a native subject in
            a namespace its registration declares, but not the ``entity_id``
            or ``entity_type`` that declaration derives (DR-32).
        ``GATE_NOT_BOUND``: a gate context names a gate the registration
            does not bind (a gate the deployment mandates without a binding
            included: it runs with an empty configuration).
        ``GATE_CONTEXT_MISSING``: a gate the registration binds has no gate
            context.
        ``NO_GATES``: a gate-mode submission has no gate to evaluate it.
        ``GATE_CONFIG_INVALID``: a gate context fails its gate's
            ``config_model``.
        ``GATE_CONFIG_UNAUTHORIZED``: a gate refuses the configuration for
            this reasoner.

    Args:
        reasoner_id: The submitting reasoner's ``reasoner_id``.
        violation: Human-readable description of the contract clause violated.
        code: Stable error code (one of the codes above).
    """

    def __init__(  # noqa: D107
        self, reasoner_id: str, violation: str, *, code: str = "SUBMISSION_INVALID"
    ) -> None:
        self.reasoner_id = reasoner_id
        self.violation = violation
        self.code = code
        super().__init__(
            f"Submission contract violation [{reasoner_id}] {code}: {violation}"
        )


class BundleSchemaUnsupportedError(Exception):
    """Raised when an operation needs a bundle schema the store cannot serve.

    Bundles are versioned (``decision_bundles.schema_version``). Lookup
    returns any version as stored; typed loading and replay need the current
    schema. A schema-version-1 bundle (written before the submission
    contract) carries a reasoner-typed event that the framework can no
    longer reconstruct, so it is refused rather than converted (HTTP 409
    ``BUNDLE_SCHEMA_UNSUPPORTED``).

    Args:
        decision_id: The stored decision.
        schema_version: Its stored schema version.
        supported: The schema version the operation needs.
    """

    def __init__(  # noqa: D107
        self, decision_id: str, schema_version: int, *, supported: int
    ) -> None:
        self.decision_id = decision_id
        self.schema_version = schema_version
        self.supported = supported
        super().__init__(
            f"Bundle {decision_id!r} has schema version {schema_version}; this "
            f"operation supports schema version {supported} only"
        )


class ReasonerPluginError(Exception):
    """Raised when the framework cannot discover or validate a reasoner plugin.

    This is a deployment-composition error, distinct from a runtime contract
    violation: the installed set of reasoner plugins is malformed (a plugin
    fails to load, does not satisfy the ``ReasonerPlugin`` protocol, declares
    an entry-point name that differs from its ``reasoner_id``, or collides
    with another plugin's ``reasoner_id``). Raised at startup so a broken
    deployment fails fast rather than silently running without a reasoner.
    The eval harness raises it for the same faults in a reasoner's eval
    adapter (missing, unloadable, non-conforming, or mismatched id).

    Args:
        plugin_name: The entry-point name (or best available identifier) of
            the offending plugin.
        reason: Human-readable description of what went wrong.
    """

    def __init__(self, plugin_name: str, reason: str) -> None:  # noqa: D107
        self.plugin_name = plugin_name
        self.reason = reason
        super().__init__(f"Reasoner plugin error [{plugin_name}]: {reason}")


class GatePluginError(Exception):
    """Raised when the framework cannot discover, build, or trust a gate.

    A deployment-composition error, the gate-side twin of
    ``ReasonerPluginError``: a ``decisionledger.gates`` entry point fails to
    load, does not satisfy the ``GatePlugin`` protocol, declares a name that
    differs from its ``gate_id`` or an unknown ``kind``, or collides with
    another gate; or the deployment mandates a gate
    (``DECISIONLEDGER_MANDATORY_GATES``) it does not compose. Raised at
    startup so a broken deployment fails before it serves a decision. A gate
    that misbehaves at decision time — raises, times out, or returns
    something other than a plain ``GateOutput`` for itself — is not this
    error: the ledger fails that gate closed and records why (DR-29).

    Args:
        gate_id: The entry-point name or ``gate_id`` of the offending gate.
        reason: Human-readable description of what went wrong.
    """

    def __init__(self, gate_id: str, reason: str) -> None:  # noqa: D107
        self.gate_id = gate_id
        self.reason = reason
        super().__init__(f"Gate plugin error [{gate_id}]: {reason}")


class GateConfigError(ValueError):
    """Raised by a gate that refuses a reasoner's binding to it (DR-33).

    A registration binds each gate in its gate set with an optional
    ``GateBinding.config`` the framework never interprets. A gate that admits
    bindings (``app.gates.BindingValidator``) raises this error from
    ``validate_binding`` when it cannot serve that reasoner with that
    configuration: a configuration it does not accept, or assets it would
    need and cannot find. The framework names the gate when it reports the
    refusal: startup fails with ``GatePluginError``, and a registration
    written over the API is refused (422 ``GATE_BINDING_REFUSED``) with
    nothing stored. A subclass of ``ValueError``, as a Pydantic validation
    error is, so one handler covers both.

    Args:
        reason: Human-readable description of why the binding is refused.
    """

    def __init__(self, reason: str) -> None:  # noqa: D107
        self.reason = reason
        super().__init__(reason)


class ResolverPluginError(Exception):
    """Raised when the deployment's identity resolver cannot be composed.

    A deployment-composition error, the identity twin of
    ``GatePluginError`` (DR-32): ``DECISIONLEDGER_IDENTITY_RESOLVER`` names a
    resolver that is not installed under the
    ``decisionledger.identity_resolvers`` entry-point group, the entry point
    fails to load, the loaded object does not satisfy the resolver plugin
    protocol (or declares a malformed relationship type, or a name other
    than its ``resolver_id``), or its ``build`` returns something that is
    not a synchronous ``IdentityResolver``. Raised at startup. An exception
    ``build`` itself raises is not wrapped in this error: it propagates as
    itself and stops startup too. A resolver that misbehaves at decision
    time is not this error: the ledger records that decision's subject
    unresolved, with the reason, and writes it.

    Args:
        resolver_id: The configured or published resolver id.
        reason: Human-readable description of what went wrong.
    """

    def __init__(self, resolver_id: str, reason: str) -> None:  # noqa: D107
        self.resolver_id = resolver_id
        self.reason = reason
        super().__init__(f"Identity resolver error [{resolver_id}]: {reason}")


class PredicateError(ValueError):
    """Raised when a predicate cannot be evaluated against its subject.

    The predicate language (``core.gate.predicate``) is closed and pure; it
    refuses to guess. A field the subject does not carry, or an ordering
    comparison whose operand is not a number, raises this error so the
    caller (a rules gate, a bypass-rule check) can fail closed.

    Args:
        reason: Human-readable description of what could not be evaluated.
    """

    def __init__(self, reason: str) -> None:  # noqa: D107
        self.reason = reason
        super().__init__(f"Predicate evaluation error: {reason}")


class RegistrationError(Exception):
    """Raised when the deployment's reasoner registrations cannot be composed.

    A deployment registers reasoners from three sources (DR-31, DR-33): the
    installed reasoner plugins, an optional registration file
    (``DECISIONLEDGER_REGISTRATIONS``), and the registrations written over
    the API (``PUT /api/v1/reasoners/{reasoner_id}``), which the ledger keeps
    in Postgres. A file that cannot be read, parsed, or validated, two
    sources declaring the same ``reasoner_id``, or two registrations
    declaring one native namespace differently (DR-32) raise this error at
    startup, so a deployment never serves decisions under a registration it
    did not mean to have. Replacing a plugin's or the file's registration at
    runtime raises it too: declared configuration stays declared.

    Args:
        source: Where the offending registration came from (a file path, a
            plugin entry point, ``api``, or the reasoner).
        reason: Human-readable description of what went wrong.
    """

    def __init__(self, source: str, reason: str) -> None:  # noqa: D107
        self.source = source
        self.reason = reason
        super().__init__(f"Registration error [{source}]: {reason}")


class LedgerError(Exception):
    """A ledger operation refused, as a problem with a stable code (DR-31).

    The ledger's API operations raise it; the HTTP surface renders it as an
    RFC 9457 problem document (``application/problem+json``) carrying
    ``code``; ``HttpClient`` raises it again from that document, and
    ``LocalClient`` lets the operation's own error through. A caller sees the
    same ``status`` and ``code`` whether it talks to the ledger over HTTP or
    in process.

    Args:
        status: The HTTP status the problem is served with.
        code: Stable machine-readable error code (``DECISION_NOT_FOUND``,
            ``REASONER_MISMATCH``, a ``SubmissionContractError`` code, ...).
        detail: Human-readable explanation of this occurrence.
        title: Short summary of the problem type; derived from ``code`` when
            omitted.
        extra: Extension members of the problem document (JSON values), such
            as a validation error list.
    """

    def __init__(  # noqa: D107
        self,
        status: int,
        code: str,
        detail: str,
        *,
        title: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        self.title = title or code.replace("_", " ").capitalize()
        self.extra = dict(extra or {})
        super().__init__(f"{status} {code}: {detail}")

    @property
    def type_uri(self) -> str:
        """The problem type URI (a URN derived from ``code``)."""
        return f"urn:decisionledger:problem:{self.code.lower().replace('_', '-')}"

    def problem(self, *, instance: str | None = None) -> dict[str, Any]:
        """Return the RFC 9457 problem document for this error.

        Args:
            instance: The request path the problem occurred on, if any.

        Returns:
            ``type``, ``title``, ``status``, ``detail``, ``code``, the
            optional ``instance``, and every extension member.
        """
        document: dict[str, Any] = {
            **self.extra,
            "type": self.type_uri,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "code": self.code,
        }
        if instance is not None:
            document["instance"] = instance
        return document


class RegistrationSnapshotMissingError(Exception):
    """Raised when replay cannot find the registration a bundle was decided under.

    Every bundle records its reasoner's ``registration_version``, and the
    ledger keeps a snapshot of every version it serves (DR-31). A bundle
    whose version has no snapshot, and whose reasoner is now registered under
    a different version, cannot be replayed faithfully: enforcing it against
    today's profile could change its outcome. Replay refuses it (HTTP 409
    ``REGISTRATION_SNAPSHOT_MISSING``) rather than guess.

    Args:
        reasoner_id: The bundle's reasoner.
        version: The registration version the bundle cites.
    """

    def __init__(self, reasoner_id: str, version: str) -> None:  # noqa: D107
        self.reasoner_id = reasoner_id
        self.version = version
        super().__init__(
            f"No snapshot of registration version {version!r} of reasoner "
            f"{reasoner_id!r}, and the reasoner is now registered under another "
            "version"
        )
