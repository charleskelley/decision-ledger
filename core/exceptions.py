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
