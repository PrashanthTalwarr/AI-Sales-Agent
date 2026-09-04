"""
Shared Anthropic client factory — lazy-initialized, used across agents and integrations.
"""

import os
import logging

logger = logging.getLogger(__name__)


def get_anthropic_client():
    """
    Return an Anthropic client or None if key is missing / package not installed.
    Called inside functions so dotenv is loaded before os.getenv() runs.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
        return Anthropic(api_key=api_key)
    except ImportError:
        logger.error("anthropic package not installed — run: pip install anthropic")
        return None


def get_anthropic_model() -> str:
    return os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
