"""Policy corpus metadata schema and document classification enum.

Jurisdiction and RiskTier enums are domain-specific and live in
reasoner/account_takeover/. PolicyDocument uses plain strings for those fields
so that any domain's classification vocabulary is accepted without coupling
the framework to ATO-specific enumerations.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class DocumentType(StrEnum):
    """Classification of a policy document by its authority and purpose.

    Used during retrieval to weight documents by authority and to surface
    conflicting guidance across document types for the policy gate.

    Attributes:
        REGULATION: Legally binding rule with penalties for non-compliance
            (e.g., GDPR, GLBA, CCPA). Highest retrieval authority.
        GUIDANCE: Non-binding best-practice document issued by a regulatory
            body or standards organization (e.g., NIST SP 800-63B, FFIEC).
        INTERNAL_POLICY: Organization-specific policy document. Superseded
            by REGULATION or STANDARD where they conflict.
        STANDARD: Technical or operational standard (e.g., ISO 27001,
            PCI DSS). Treated as binding where contractually adopted.
    """

    REGULATION = "REGULATION"
    GUIDANCE = "GUIDANCE"
    INTERNAL_POLICY = "INTERNAL_POLICY"
    STANDARD = "STANDARD"


class PolicyDocument(BaseModel):
    """Metadata schema for a versioned policy corpus document.

    Each document in the corpus has exactly one metadata record. The retriever
    uses these fields as filters and ranking signals — jurisdiction and
    risk_tier are the primary metadata filters; version and effective_date
    drive version conflict resolution (latest preferred unless a query scopes
    an earlier version explicitly).

    jurisdiction and risk_tier are plain strings so that domain implementations
    can use their own classification enumerations without requiring the framework
    to import domain-specific types.

    Args:
        policy_id: Stable identifier (e.g., "NIST-800-63B", "INTERNAL-RISK-v2.1").
        title: Human-readable document title.
        version: Semantic version string (e.g., "2.1", "4.0").
        jurisdiction: Regulatory jurisdiction this document applies to.
        effective_date: Date from which this version is authoritative.
        supersedes: ``policy_id`` of the document this version replaces.
            None if this is the original version.
        risk_tier: Subject tier this document applies to. None means the
            document applies to all tiers.
        document_type: Authority classification of the document.
    """

    model_config = ConfigDict(strict=True, frozen=True)

    policy_id: str
    title: str
    version: str
    jurisdiction: str
    effective_date: date
    supersedes: str | None
    risk_tier: str | None
    document_type: DocumentType
