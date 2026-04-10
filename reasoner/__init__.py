"""ATO Reasoner domain layer — business logic for identity risk decisioning.

This package contains the ATO Reasoner's domain types: the concrete
implementations of the framework contracts defined in core/decision/ and
core/policy/. Nothing in core/ imports from reasoner/; the dependency
arrow points inward only.

Subpackages:
    account_takeover: Login event schema, feature vector, policy enums,
        and scorer output for the account takeover / identity risk domain.
"""
