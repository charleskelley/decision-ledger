"""Framework exception types for DecisionLedger intake and contract violations."""


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
