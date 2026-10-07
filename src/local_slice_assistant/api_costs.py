"""Approximate DeepSeek Flash API cost before sending; never an invoice."""
from __future__ import annotations

from math import ceil
import re


_RATES = {
    "deepseek-flash": (1.0, 2.0, 4.0, 8.0),
    # As of 2026-10-07, official docs say deepseek-v4-pro requests are routed
    # to V4.1-Flash until V4.1-Pro launches and billed at Flash rates.
    "deepseek-v4-pro": (1.0, 2.0, 4.0, 8.0),
}


def estimate_input_tokens(text: str) -> int:
    """Very rough local estimate based on DeepSeek's published character ratios."""
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
    ascii_count = sum(char.isascii() for char in text)
    other = len(text) - cjk - ascii_count
    return max(1, ceil(cjk * 0.6 + ascii_count * 0.3 + other * 0.6))


def estimate_cost(input_tokens: int, output_tokens: int, model: str = "deepseek-flash") -> tuple[float, float]:
    """Return (idle, peak) RMB, assuming all input tokens miss cache."""
    idle_input, peak_input, idle_output, peak_output = _RATES.get(model, _RATES["deepseek-flash"])
    million = 1_000_000
    idle = (input_tokens * idle_input + output_tokens * idle_output) / million
    peak = (input_tokens * peak_input + output_tokens * peak_output) / million
    return idle, peak
