"""
Shared JSON extraction utilities — parse JSON from LLM text responses.
"""

import json
from typing import Optional


def extract_json(text: str) -> Optional[str]:
    """
    Extract a JSON array or object from a text response that may contain
    markdown code fences or surrounding prose.
    """
    # Try bare JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # Try bare JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # Try markdown code block (```json or ```)
    for marker in ("```json", "```"):
        if marker in text:
            after = text.split(marker, 1)[1]
            chunk = after.split("```")[0].strip()
            try:
                json.loads(chunk)
                return chunk
            except json.JSONDecodeError:
                pass

    return None
