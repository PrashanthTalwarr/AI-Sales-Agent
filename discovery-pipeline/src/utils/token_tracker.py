"""
TOKEN TRACKER — Accumulates Claude API token usage across all pipeline calls.

Pricing (claude-sonnet-4-20250514):
  Input:  $3.00 / 1M tokens
  Output: $15.00 / 1M tokens
"""

from threading import Lock
from dataclasses import dataclass, field

# Pricing per million tokens
_INPUT_COST_PER_M  = 3.00
_OUTPUT_COST_PER_M = 15.00


@dataclass
class _Usage:
    input_tokens:  int = 0
    output_tokens: int = 0
    calls:         int = 0


_lock  = Lock()
_usage = _Usage()


def record(input_tokens: int, output_tokens: int):
    """Add token counts from one API call."""
    with _lock:
        _usage.input_tokens  += input_tokens
        _usage.output_tokens += output_tokens
        _usage.calls         += 1


def get() -> dict:
    """Return current cumulative usage."""
    with _lock:
        cost = (
            _usage.input_tokens  * _INPUT_COST_PER_M  / 1_000_000 +
            _usage.output_tokens * _OUTPUT_COST_PER_M / 1_000_000
        )
        return {
            "input_tokens":       _usage.input_tokens,
            "output_tokens":      _usage.output_tokens,
            "total_tokens":       _usage.input_tokens + _usage.output_tokens,
            "calls":              _usage.calls,
            "estimated_cost_usd": round(cost, 5),
        }


def reset():
    with _lock:
        _usage.input_tokens  = 0
        _usage.output_tokens = 0
        _usage.calls         = 0
