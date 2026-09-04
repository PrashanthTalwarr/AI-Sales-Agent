"""
Shared GitHub API helpers — used by ingest and contacts modules.
"""

import os
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


def github_headers() -> dict:
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_get(url: str, params: dict = None) -> Optional[dict | list]:
    try:
        resp = requests.get(url, params=params, headers=github_headers(), timeout=12)
        if resp.status_code == 403:
            logger.warning("GitHub rate limit or auth error at %s", url)
            return None
        if resp.status_code != 200:
            return None
        return resp.json()
    except requests.RequestException as e:
        logger.debug("GitHub request failed %s: %s", url, e)
        return None
