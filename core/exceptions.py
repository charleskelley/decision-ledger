"""Framework exception types for DecisionLedger intake and contract violations."""


class UnregisteredReasonerError(Exception):
    """Raised when an Observation is submitted by an unregistered reasoner.

    This is a configuration error: the observation is structurally valid but
    the framework does not recognize the submitting reasoner. The reasoner
    must be registered via a ``ReasonerRegistry`` before its observations
    can be accepted.

    Args:
        reasoner_id: The unrecognized ``reasoner_id`` from ``ReasonerContext``.
    """

    def __init__(self, reasoner_id: str) -> None:  # noqa: D107
        self.reasoner_id = reasoner_id
        super().__init__(
            f"Unregistered reasoner [{reasoner_id}]: reasoner must be registered "
            "with the DecisionLedger framework before submitting observations"
        )


class ObservationContractError(Exception):
    """Raised when a domain reasoner submits an Observation that violates the contract.

    Identifies the violating reasoner and the specific contract clause that was
    broken, so it is unambiguous which domain component needs to be fixed.

    Args:
        reasoner_id: The ``reasoner_id`` from ``ReasonerContext``, or
            ``"unknown"`` if ``reasoner_context`` itself was absent.
        violation: Human-readable description of the contract clause violated.
    """

    def __init__(self, reasoner_id: str, violation: str) -> None:  # noqa: D107
        self.reasoner_id = reasoner_id
        self.violation = violation
        super().__init__(f"Observation contract violation [{reasoner_id}]: {violation}")


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
    another gate; an observation names a gate this deployment does not
    compose; or a gate returns a ``GateOutput`` that misstates its own
    ``gate_id`` or ``deterministic`` flag. Raised loudly so a broken
    deployment or a misbehaving gate never yields a silently wrong decision.

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
