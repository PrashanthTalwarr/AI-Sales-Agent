"""
Shared config loader — reads config/scoring_weights.json once.
"""

import json
import logging

logger = logging.getLogger(__name__)


def load_config(config_path: str = "config/scoring_weights.json") -> dict:
    """Load pipeline/scoring config from JSON. Returns {} on error."""
    try:
        with open(config_path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("Config load failed (%s): %s", config_path, e)
        return {}
