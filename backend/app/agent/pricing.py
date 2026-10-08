"""Cost estimation. Prices are USD per 1M tokens: (input, output, cache_read)."""
from __future__ import annotations

from typing import Any

PRICES: dict[str, tuple[float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
}
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute ephemeral cache
WEB_SEARCH_USD_PER_REQUEST = 0.01  # estimate: $10 per 1,000 searches

# Unknown models are priced at the most expensive known rate so the budget cap errs on the safe side.
_FALLBACK = max(PRICES.values(), key=lambda p: p[1])


def estimate_cost(model: str, usage: Any) -> float:
    price_in, price_out, price_cache_read = PRICES.get(model, _FALLBACK)
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    server = getattr(usage, "server_tool_use", None)
    searches = (getattr(server, "web_search_requests", 0) or 0) if server else 0
    return (
        inp * price_in
        + out * price_out
        + cache_read * price_cache_read
        + cache_write * price_in * CACHE_WRITE_MULTIPLIER
    ) / 1_000_000 + searches * WEB_SEARCH_USD_PER_REQUEST
