"""ATO Reasoner — account takeover / identity risk implementation.

This package is the ATO reasoner: its domain types, scorer, ingestion, and
the assembler that turns a scored login into the framework's ``Submission``
(DR-31). It depends on the framework contracts in ``core/`` and on ``app/``
and may import from them freely. Nothing in the framework layer imports from
this package.

Dependency rule: ``core/``, ``app/``, and ``eval/`` must not import from a
reasoner package. The arrow points inward only.

Submodules:
    events:    LoginEvent and authentication domain enums (AuthMethod,
               AuthOutcome, Geolocation) — ATO's own event, carried in a
               submission as opaque ``raw_event`` JSON.
    features:  AtoFeatureVector and WindowSpec.
    policy:    ATO-domain policy classification enums (Jurisdiction, RiskTier).
    scorer:    ScorerOutput — ATO fast ML scorer result. Internal domain type;
               not a framework contract.
    registry:  ATO_REGISTRATION — the policy gate binding and the two score
               bands, registered as bypass rules (DR-30).
    assembler: build_submission — the handoff to the framework.
"""
