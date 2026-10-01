"""Cost per ticket from the usage each engine reports.

Defaults are the list prices of gpt-4o (2024-11-20) per million tokens, in USD.
Azure bills in the subscription's currency and region: put your own prices in
a JSON file and pass it with --prices.

    {"currency": "EUR", "input_per_million": 2.30, "output_per_million": 9.20, "per_search_call": 0.0}

Search calls cost 0 by default: the search service is a fixed monthly price.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Prices:
    currency: str = "USD"
    input_per_million: float = 2.50
    output_per_million: float = 10.00
    per_search_call: float = 0.0
    source: str = "default list prices for gpt-4o 2024-11-20; check your Azure region and currency"

    def cost(self, usage) -> float:
        if hasattr(usage, "to_dict"):
            usage = usage.to_dict()
        usage = usage or {}
        return (
            int(usage.get("input_tokens") or 0) * self.input_per_million / 1_000_000
            + int(usage.get("output_tokens") or 0) * self.output_per_million / 1_000_000
            + int(usage.get("search_calls") or 0) * self.per_search_call
        )


def load_prices(path: str | Path | None) -> Prices:
    if not path:
        return Prices()
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    allowed = {f.name for f in fields(Prices)}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"unknown price setting(s): {', '.join(unknown)}")
    data.setdefault("source", str(path))
    return Prices(**data)
