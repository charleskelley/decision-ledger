"""ATO Observation assembler — translates domain types to framework contracts.

The assembler is the handoff boundary between the ATO Reasoner domain layer
and the DecisionLedger framework. It translates three domain artifacts:

- ``LoginEvent``       — the raw authentication event
- ``AtoFeatureVector`` — computed sliding-window features
- ``ScorerOutput``     — XGBoost risk score, SHAP signals, routing decision

...into a fully-populated ``LoginEvent`` (satisfying ``Observation``) ready
for framework submission. The three domain types are never seen by the
framework — only the assembled observation crosses the boundary.

Translation mapping:
    ScorerOutput → ReasonerContext
        label_type            = NUMERICAL
        label_name            = "risk_score"
        label_value           = scorer_output.risk_score
        feature_set           = AtoFeatureVector fields as a flat dict
        attribution           = AttributionSummary(observation_signals=top_signals)
        model_version         = scorer_output.scorer_version

    LoginEvent + ScorerOutput → GateContext
        prompt_template_id    = "ato-v2"
        jurisdictions         = resolved from event geo (US_FEDERAL + INTERNAL default)
        risk_tier             = resolved from scorer risk score band
        template_vars         = rendered strings for each {placeholder} in ato-v1.yaml

    ScorerOutput → fast_path_rationale  (fast path only)
        Human-readable description of the confidence-band rule that fired,
        e.g. "risk_score=0.930 > 0.85 → FAST_PATH_BLOCK". This is the
        audit trail for why the policy gate was bypassed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.observation import AttributionSummary, GateContext, LabelType, ReasonerContext
from core.routes import GateRoute
from reasoner.account_takeover.policy import Jurisdiction, RiskTier

if TYPE_CHECKING:
    from reasoner.account_takeover.events import LoginEvent
    from reasoner.account_takeover.features import AtoFeatureVector
    from reasoner.account_takeover.scorer import ScorerOutput

# Reasoner registration identity — matches DecisionLedger registration record.
_REASONER_ID = "ato-reasoner"
_REASONER_NAME = "ATO Reasoner"

# Confidence-band thresholds that drive fast-path routing. Must stay in
# sync with FAST_PATH_*_THRESHOLD in reasoner/account_takeover/scorer/scorer.py.
_FAST_PATH_ALLOW_THRESHOLD = 0.20  # risk_score < 0.20 → FAST_PATH_ALLOW
_FAST_PATH_BLOCK_THRESHOLD = 0.95  # risk_score > 0.95 → FAST_PATH_BLOCK

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


def _fast_path_rationale(route: GateRoute, risk_score: float) -> str:
    """Build the fast_path_rationale string for a fast-path route.

    Records the confidence-band rule that fired in a human-readable form.
    This is the audit trail for why the gate was bypassed.

    Args:
        route: The fast-path gate route.
        risk_score: The risk score that crossed the threshold.

    Returns:
        Descriptive rationale string set on the outbound Observation.
    """
    if route == GateRoute.FAST_PATH_ALLOW:
        return (
            f"risk_score={risk_score:.3f} < {_FAST_PATH_ALLOW_THRESHOLD}"
            " → FAST_PATH_ALLOW"
        )
    return (
        f"risk_score={risk_score:.3f} > {_FAST_PATH_BLOCK_THRESHOLD} → FAST_PATH_BLOCK"
    )


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
) -> GateContext:
    """Translate LoginEvent + AtoFeatureVector + ScorerOutput into a GateContext.

    Renders each template variable as a human-readable string suitable for
    LLM prompt injection. Variable names must match the ``{placeholders}``
    in ``app/policy_gate/prompts/ato-v1.yaml``.

    Args:
        event: The login event.
        features: Computed ATO feature snapshot.
        scorer: XGBoost scorer output.

    Returns:
        Populated ``GateContext`` ready for the framework.
    """
    top_signal_strs = [
        f"{s.feature_name}={s.feature_value:.3g} (SHAP {s.value:+.3f})"
        for s in scorer.top_signals[:3]
    ]
    top_signals_rendered = ", ".join(top_signal_strs) if top_signal_strs else "none"

    return GateContext(
        gate_id="policy",
        gate_config={
            "template_id": "ato-v2",
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
            "jurisdictions": _resolve_jurisdictions(event),
            "risk_tier": _resolve_risk_tier(scorer.risk_score),
        },
    )


def build_observation(
    event: LoginEvent,
    features: AtoFeatureVector,
    scorer: ScorerOutput,
) -> LoginEvent:
    """Assemble a fully-populated ATO Observation for DecisionLedger submission.

    Translates three domain artifacts into the framework contracts and returns
    an updated ``LoginEvent`` satisfying the ``Observation`` protocol with all
    framework fields populated. The original ``event`` is not modified —
    ``model_copy`` produces a new frozen instance.

    This is the only function in the ATO domain that should be called by the
    pipeline before handing the observation to the DecisionLedger framework.

    Args:
        event: Validated login event from the ingestion layer. Must have
            ``scenario_tag`` stripped already.
        features: Computed ATO feature snapshot from the features layer.
        scorer: XGBoost scorer output from the scorer layer.

    Returns:
        A new ``LoginEvent`` with ``routing``, ``reasoner_context``,
        ``fast_path_rationale``, and ``gate_context`` fully populated.
        ``fast_path_rationale`` is ``None`` when routing to the gate.
    """
    reasoner_context = _build_reasoner_context(features, scorer)
    gate_context = _build_gate_context(event, features, scorer)

    rationale = (
        _fast_path_rationale(scorer.route, scorer.risk_score)
        if scorer.route != GateRoute.ROUTE_TO_GATE
        else None
    )

    return event.model_copy(
        update={
            "route": scorer.route,
            "reasoner_context": reasoner_context,
            "fast_path_rationale": rationale,
            "gate_context": gate_context,
        }
    )
