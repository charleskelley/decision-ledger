"""ATO domain query builder for the framework retriever.

``build_ato_query()`` translates a ``LoginEvent`` plus the top SHAP signals
from the scorer into a natural-language retrieval query string. Crossing
the framework boundary as a *string* keeps the framework retriever
domain-agnostic: the framework knows nothing about login outcomes,
authentication methods, or velocity feature names.

Owned by the ATO reasoner. The reasoner pipeline calls
``build_ato_query(event, scorer_output)`` and forwards the result to
``PolicyRetriever.retrieve(query, ...)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from reasoner.account_takeover.events import AuthOutcome

if TYPE_CHECKING:
    from reasoner.account_takeover.events import LoginEvent
    from reasoner.account_takeover.scorer import ScorerOutput


# ---------------------------------------------------------------------------
# Signal → retrieval term mappings
# ---------------------------------------------------------------------------

_SIGNAL_TERMS: dict[str, str] = {
    "impossible_travel": "impossible travel geographic anomaly mandatory block",
    "travel_speed_kmh": "impossible travel speed geographic block",
    "velocity_1min": "velocity rate limiting authentication burst",
    "velocity_5min": "high velocity credential stuffing rate limit",
    "velocity_60min": "sustained velocity authentication rate limiting",
    "device_novelty": "new unrecognized device fingerprint challenge hold",
    "device_consistency_score": "device fingerprint consistency mismatch anomaly",
    "geo_novelty": "geographic anomaly new country location access",
    "ip_novelty": "new IP address network risk authentication",
    "sparse_history": "novel entity new account insufficient history hold",
    "user_agent_consistency": "user agent consistency browser anomaly",
}

_AUTH_METHOD_TERMS: dict[str, str] = {
    "PASSWORD": "password credential memorized secret authentication",
    "MFA_TOTP": "MFA TOTP multi-factor authenticator app",
    "MFA_PUSH": "MFA push notification multi-factor authentication",
    "SSO": "SSO single sign-on federated identity enterprise authentication",
    "PASSKEY": "FIDO2 passkey WebAuthn phishing-resistant authenticator",
    "MAGIC_LINK": "magic link email one-time authentication",
}


def build_ato_query(event: LoginEvent, scorer_output: ScorerOutput) -> str:
    """Construct a natural-language retrieval query from event + scorer signals.

    Maps the top-3 SHAP signals to policy-relevant terminology, adds an
    authentication-method context phrase, and (for failed/blocked outcomes)
    a credential-failure phrase. Falls back to a generic authentication-risk
    query when no signal mapping is available.

    Args:
        event: Validated login event from the ingestion layer.
        scorer_output: Scorer output carrying top SHAP signals.

    Returns:
        Natural-language query string suitable for dense + sparse retrieval.
    """
    terms: list[str] = []

    for signal in scorer_output.top_signals[:3]:
        term = _SIGNAL_TERMS.get(signal.feature_name)
        if term:
            terms.append(term)

    auth_term = _AUTH_METHOD_TERMS.get(event.auth_method.value, "")
    if auth_term:
        terms.append(auth_term)

    if event.outcome in (AuthOutcome.FAILURE, AuthOutcome.BLOCKED):
        terms.append("authentication failure blocked credential")

    return " ".join(terms) if terms else "authentication risk controls policy"
