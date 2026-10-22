"""ATO submission assembler — translates domain types to the framework contract.

The assembler is the handoff boundary between the ATO Reasoner domain layer
and the DecisionLedger framework. It translates three domain artifacts:

- ``LoginEvent``       — the raw authentication event
- ``AtoFeatureVector`` — computed sliding-window features
- ``ScorerOutput``     — XGBoost risk score and SHAP signals

...into one ``Submission`` (DR-31). The three domain types are never seen by
the framework as types — the event crosses only as opaque ``raw_event``
JSON, the features and score only as the ``ReasonerContext``.

Translation mapping:
    ScorerOutput → ReasonerContext
        label_type            = NUMERICAL
        label_name            = "risk_score"
        label_value           = scorer_output.risk_score
        feature_set           = AtoFeatureVector fields as a flat dict
        attribution           = AttributionSummary(feature_contributions=top_signals)
        model_version         = scorer_output.scorer_version

    LoginEvent + ScorerOutput → GateContext (policy gate, gate_id "policy")
        template_id           = AtoSettings.template_id ("ato-v4" by default,
                                DEFAULT_TEMPLATE_ID; "ato-v3" selectable)
        jurisdictions         = resolved from event geo (US_FEDERAL + INTERNAL default)
        risk_tier             = resolved from scorer risk score band
        template_vars         = rendered strings for each {placeholder} in
                                ato_reasoner/prompts/ato-v3.yaml and ato-v4.yaml
                                (the same placeholders)
        retrieval_query       = build_ato_query(event, scorer) — the policy
                                gate runs retrieval itself (DR-28), so the
                                domain-owned query travels in its gate_config

        Which corpus the gate reads is ATO's binding (``ATO_REGISTRATION``:
        ``ato-policy`` at tier 1, DR-34), not the gate context; the gate
        records the state of the corpus itself.

    LoginEvent → subject (DR-32)
        native_subject        = (ato-account, account_id): the account ATO
                                scored, which the ledger resolves to the
                                decision's subject
        entity_id, entity_type = the account's id under ATO's declared
                                namespace, and "account"

    ReasonerContext → mode (DR-30)
        ``matching_bypass_rule`` checks ATO's registered bypass rules
        (``allow-band``, ``block-band``) against the reasoner context. When
        one holds the submission *records* that rule's action and cites it
        (pinned to its version); otherwise it asks the ledger to *gate* the
        login, with no proposal. The policy gate's context goes on every
        submission, recorded ones too, so any recorded login can be shadow-
        evaluated by the gate later.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ato_reasoner.policy import Jurisdiction, RiskTier
from ato_reasoner.registry import ATO_REGISTRATION
from ato_reasoner.retrieval_query import build_ato_query
from core.observation import AttributionSummary, GateContext, LabelType, ReasonerContext
from core.submission import BypassCitation, Submission, matching_bypass_rule

#: The policy gate template ATO's gate context names unless told otherwise
#: (``AtoSettings.template_id``): the conflict/gap protocol of DR-34.
DEFAULT_TEMPLATE_ID = "ato-v4"

if TYPE_CHECKING:
    from pydantic import JsonValue

    from ato_reasoner.events import LoginEvent
    from ato_reasoner.features import AtoFeatureVector
    from ato_reasoner.scorer import ScorerOutput
    from core.observation import ReasonerRegistration

# Reasoner registration identity — matches DecisionLedger registration record.
_REASONER_ID = ATO_REGISTRATION.reasoner_id
_REASONER_NAME = ATO_REGISTRATION.reasoner_name

# Risk score → risk tier mapping for corpus retrieval filtering.
_HIGH_VALUE_TIER_THRESHOLD = 0.60


def _feature_set(features: AtoFeatureVector) -> dict[str, float | int | str | bool]:
    """Flatten AtoFeatureVector into a plain dict for ReasonerContext.feature_set.

    Args:
        features: Computed ATO feature snapshot.

    Returns:
        Flat dict of feature name → raw value at inference time.
    """
    return {
        "velocity_1min": features.velocity_1min,
        "velocity_5min": features.velocity_5min,
        "velocity_60min": features.velocity_60min,
        "velocity_1440min": features.velocity_1440min,
        "ip_novelty": features.ip_novelty,
        "device_novelty": features.device_novelty,
        "geo_novelty": features.geo_novelty,
        "impossible_travel": features.impossible_travel,
        "travel_speed_kmh": (
            features.travel_speed_kmh
            if features.travel_speed_kmh is not None
            else "N/A"
        ),
        "device_consistency_score": features.device_consistency_score,
        "user_agent_consistency": features.user_agent_consistency,
        "sparse_history": features.sparse_history,
    }


def _resolve_risk_tier(risk_score: float) -> str:
    """Map a risk score to a RiskTier string for corpus retrieval filtering.

    Args:
        risk_score: Risk probability in [0.0, 1.0].

    Returns:
        ``RiskTier`` string value.
    """
    if risk_score >= _HIGH_VALUE_TIER_THRESHOLD:
        return RiskTier.HIGH_VALUE
    return RiskTier.STANDARD


def _resolve_jurisdictions(event: LoginEvent) -> list[str]:
    """Resolve retrieval jurisdiction filters from the event's geo context.

    Always includes INTERNAL (org-wide policy). Adds US_FEDERAL for
    US-located events.

    Args:
        event: The login event.

    Returns:
        List of ``Jurisdiction`` string values for corpus filtering.
    """
    jurisdictions = [Jurisdiction.INTERNAL]
    if event.geolocation.country == "US":
        jurisdictions.append(Jurisdiction.US_FEDERAL)
    return [str(j) for j in jurisdictions]


def _build_reasoner_context(
    features: AtoFeatureVector,
    scorer: ScorerOutput,
) -> ReasonerContext:
    """Translate AtoFeatureVector + ScorerOutput into a ReasonerContext.

    Args:
        features: Computed ATO feature snapshot.
        scorer: XGBoost scorer output.

    Returns:
        Populated ``ReasonerContext`` ready for the framework.
    """
    fs = _feature_set(features)
    return ReasonerContext(
        reasoner_id=_REASONER_ID,
        reasoner_name=_REASONER_NAME,
        model_version=scorer.scorer_version,
        model_artifact_sha256=scorer.scorer_artifact_sha256,
        inference_latency_ms=scorer.inference_latency_ms,
        label_type=LabelType.NUMERICAL,
        label_name="risk_score",
        label_value=scorer.risk_score,
        feature_set=fs,
        attribution=AttributionSummary(
            feature_contributions=scorer.top_signals or None,
        ),
    )


def _build_gate_context(
    event: LoginEvent,
    features: AtoFeatureVector,
    scorer: ScorerOutput,
    *,
    template_id: str,
) -> GateContext:
    """Translate LoginEvent + AtoFeatureVector + ScorerOutput into a GateContext.

    Renders each template variable as a human-readable string suitable for
    LLM prompt injection. Variable names must match the ``{placeholders}``
    in ``ato_reasoner/prompts/ato-v3.yaml`` and ``ato-v4.yaml``. Also carries
    the retrieval query the policy gate needs, since the gate runs retrieval
    itself.

    Args:
        event: The login event.
        features: Computed ATO feature snapshot.
        scorer: XGBoost scorer output.
        template_id: The prompt template to render (``ato-v4``, or
            ``ato-v3`` to reproduce the behavior before DR-34).

    Returns:
        Populated ``GateContext`` ready for the framework.
    """
    top_signal_strs = [
        f"{s.feature_name}={s.feature_value:.3g} (SHAP {s.value:+.3f})"
        for s in scorer.top_signals[:3]
    ]
    top_signals_rendered = ", ".join(top_signal_strs) if top_signal_strs else "none"

    gate_config: dict[str, JsonValue] = {
        "template_id": template_id,
        "template_vars": {
            "risk_score": f"{scorer.risk_score:.3f}",
            "risk_tier": _resolve_risk_tier(scorer.risk_score),
            "auth_method": event.auth_method.value,
            "outcome": event.outcome.value,
            "jurisdiction": event.geolocation.country,
            "top_signals": top_signals_rendered,
            "impossible_travel": str(features.impossible_travel),
            "velocity_1min": str(features.velocity_1min),
            "velocity_5min": str(features.velocity_5min),
            "velocity_60min": str(features.velocity_60min),
            "device_novelty": f"{features.device_novelty:.2f}",
            "ip_novelty": f"{features.ip_novelty:.2f}",
            "geo_novelty": f"{features.geo_novelty:.2f}",
            "sparse_history": str(features.sparse_history),
        },
        "jurisdictions": [*_resolve_jurisdictions(event)],
        "risk_tier": _resolve_risk_tier(scorer.risk_score),
        "retrieval_query": build_ato_query(event, scorer),
    }
    return GateContext(gate_id="policy", gate_config=gate_config)


def build_submission(
    event: LoginEvent,
    features: AtoFeatureVector,
    scorer: ScorerOutput,
    *,
    template_id: str = DEFAULT_TEMPLATE_ID,
    registration: ReasonerRegistration = ATO_REGISTRATION,
) -> Submission:
    """Assemble the ATO ``Submission`` for one scored login.

    Translates the three domain artifacts into the framework contract and
    picks the mode with ATO's registered bypass rules: a login whose risk
    score falls in a band is recorded with that band's action, citing the
    rule; any other login asks the ledger to gate it.

    This is the only function in the ATO domain that should be called by the
    pipeline before handing a decision to the DecisionLedger framework.

    Args:
        event: Validated login event from the ingestion layer. Must have
            ``scenario_tag`` stripped already.
        features: Computed ATO feature snapshot from the features layer.
        scorer: XGBoost scorer output from the scorer layer.
        template_id: The policy gate prompt template to name
            (``AtoSettings.template_id``).
        registration: The registration whose bypass rules pick the mode;
            ATO's own by default.

    Returns:
        A ``record`` submission (``proposed_action`` and a version-pinned
        ``bypass`` citation) when a bypass rule holds; otherwise a ``gate``
        submission with no proposal. Both name the account as their native
        subject and carry the policy gate's context and the event as
        ``raw_event``.
    """
    reasoner_context = _build_reasoner_context(features, scorer)
    gate_context = _build_gate_context(event, features, scorer, template_id=template_id)
    rule = matching_bypass_rule(registration, reasoner_context)
    return Submission(
        reasoner_id=registration.reasoner_id,
        event_id=event.event_id,
        timestamp=event.timestamp,
        entity_id=event.entity_id,
        entity_type=event.entity_type,
        native_subject=event.native_subject,
        mode="record" if rule is not None else "gate",
        proposed_action=rule.action if rule is not None else None,
        bypass=(
            BypassCitation(rule_id=rule.rule_id, rule_version=rule.version)
            if rule is not None
            else None
        ),
        reasoner_context=reasoner_context,
        gate_contexts=(gate_context,),
        raw_event=event.model_dump(mode="json"),
    )
