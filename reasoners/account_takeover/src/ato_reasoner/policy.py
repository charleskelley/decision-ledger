"""ATO-domain policy classification enums.

These enums classify policy documents within the ATO Reasoner corpus. They are
used as metadata filters during retrieval and as risk-tier routing signals
in the enforcement layer. The framework PolicyDocument uses plain strings for
these fields; domain code uses these enums for type-safe construction and
filtering.
"""

from __future__ import annotations

from enum import StrEnum


class Jurisdiction(StrEnum):
    """Regulatory jurisdiction of a policy document in the ATO Reasoner corpus.

    Used as a metadata filter during retrieval to prevent cross-jurisdiction
    contamination — for example, applying EU GDPR data minimisation rules to
    a US-only decision context.

    Attributes:
        US_FEDERAL: United States federal regulation and guidance (GLBA,
            FFIEC, OCC guidance, FinCEN rules).
        US_STATE: United States state-level regulation (CCPA, NYDFS
            23 NYCRR 500, state data breach notification laws).
        EU_GDPR: European Union General Data Protection Regulation (GDPR)
            and associated supervisory authority guidance.
        INTERNAL: Organisation-internal policy with no external regulatory
            authority. Applies universally unless overridden by a
            jurisdiction-specific document.
    """

    US_FEDERAL = "US_FEDERAL"
    US_STATE = "US_STATE"
    EU_GDPR = "EU_GDPR"
    INTERNAL = "INTERNAL"


class RiskTier(StrEnum):
    """Account risk tier that a policy document applies to in ATO Reasoner.

    Tier-specific documents override base policy for accounts matched to
    that tier. Documents with no tier assignment apply to all accounts.

    Attributes:
        STANDARD: Default tier. Applies to all accounts not matched by a
            more specific tier.
        HIGH_VALUE: Accounts above a configurable value threshold. Stricter
            risk controls and a lower BLOCK threshold per the
            HIGH_VALUE_ACCOUNT_ADDENDUM.
        ENTERPRISE: Enterprise customer accounts with contractual SLA
            obligations. May carry custom friction thresholds negotiated in
            the service agreement.
    """

    STANDARD = "STANDARD"
    HIGH_VALUE = "HIGH_VALUE"
    ENTERPRISE = "ENTERPRISE"
