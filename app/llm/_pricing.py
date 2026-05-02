"""Per-model pricing table for ``cost_usd`` computation.

Prices are dollars per 1 million tokens (input, output). Adapters call
``compute_cost_usd`` to populate ``TokenUsage.cost_usd``; an unknown
model id returns ``None`` rather than raising — the audit layer can
detect missing cost data and decide how to handle it.

Update this table when adding a new model. Pricing changes are not
tracked here historically — that belongs in a deployed config or a
separate pricing-history record outside the codebase.
"""

from __future__ import annotations

# (input $/M, output $/M) per concrete model id.
_PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    # Anthropic — Claude 4 family
    "claude-opus-4-7": (15.00, 75.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


def compute_cost_usd(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float | None:
    """Return the dollar cost for a call, or ``None`` for an unknown model.

    Args:
        model: Concrete model id (e.g., ``"gpt-4o-2024-08-06"``).
        prompt_tokens: Input tokens consumed.
        completion_tokens: Output tokens generated.

    Returns:
        Cost in USD when ``model`` is in the pricing table, else
        ``None``. Adapters set ``TokenUsage.cost_usd`` directly from
        this return.
    """
    pricing = _PRICING.get(model)
    if pricing is None:
        return None
    in_per_m, out_per_m = pricing
    return (prompt_tokens / 1_000_000) * in_per_m + (
        completion_tokens / 1_000_000
    ) * out_per_m
