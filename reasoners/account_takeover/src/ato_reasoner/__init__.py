"""ATO Reasoner domain types — account takeover / identity risk implementation.

This package contains the ATO-specific domain types that satisfy the
framework protocols defined in core/decision/ and core/policy/. It depends
on the framework contracts and may import from them freely. Nothing in the
framework layer imports from this package.

Dependency rule: core/decision/, core/policy/, and core/eval/ must not
import from reasoner/. The arrow points inward only.

Submodules:
    events:   LoginEvent and authentication domain enums (AuthMethod,
              AuthOutcome, GeoData). LoginEvent satisfies the framework
              Observation protocol.
    features: AtoFeatureVector and WindowSpec.
    policy:   ATO-domain policy classification enums (Jurisdiction, RiskTier).
    scorer:   ScorerOutput — ATO fast ML scorer result. Internal domain type;
              not a framework contract.
"""
