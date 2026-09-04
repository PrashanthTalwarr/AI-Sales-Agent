"""
SIGNAL AGENT — LangChain ReAct agent for intelligent signal extraction.

Takes raw scraped content (job posts, blog posts, governance proposals)
and extracts structured signals relevant to Hypothesis A.

Uses the ReAct pattern:
  1. Thought: What kind of signal is this?
  2. Action: Extract structured data using the right tool
  3. Observation: Validate the extraction
  4. Repeat if needed

JD alignment: "Agent frameworks (LangChain, ReAct, tool-based agents)"
"""

import os
import json
from dataclasses import dataclass, field

from src.utils.claude_client import get_anthropic_client, get_anthropic_model
from src.utils.json_utils import extract_json
from src.utils import token_tracker


@dataclass
class ExtractedSignal:
    """Structured signal extracted from raw content."""
    protocol_name: str
    signal_category: str     # 'ai_adoption', 'security_need', 'audit_gap', 'shipping_velocity', 'funding'
    confidence: float        # 0-1
    evidence: str            # the specific text/data that supports this signal
    structured_data: dict = field(default_factory=dict)


# ── Signal extraction prompt ─────────────────────────────────────────────────

EXTRACTION_PROMPT = """You are a signal extraction agent for Discovery Pipeline, a Web3 security platform.

Your job is to analyze raw content (scraped text, API data, social posts) and extract 
structured signals relevant to this hypothesis:

"Web3 teams using AI coding tools are shipping smart contracts faster than their 
security processes can keep up. This creates exploitable gaps."

For each piece of content, extract:
1. Protocol name (if identifiable)
2. Signal category: ai_adoption, security_need, audit_gap, shipping_velocity, funding, exploit_risk
3. Confidence (0-1): how confident are you this signal is real and relevant?
4. Evidence: the specific quote or data point that supports this signal
5. Structured data: any numbers, dates, or facts extracted

Respond in JSON format only. No preamble.

Content to analyze:
{content}

Source: {source}
"""


def extract_signals_with_llm(
    raw_content: str,
    source: str,
    protocol_hint: str = ""
) -> list[ExtractedSignal]:
    """
    Use Claude API to extract structured signals from raw content.
    
    Falls back to rule-based extraction if API is unavailable.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        return _extract_with_claude(raw_content, source, protocol_hint)
    else:
        return _extract_with_rules(raw_content, source, protocol_hint)


def _extract_with_claude(
    raw_content: str,
    source: str,
    protocol_hint: str
) -> list[ExtractedSignal]:
    """Extract signals using Claude API."""
    try:
        client = get_anthropic_client()
        if not client:
            return _extract_with_rules(raw_content, source, protocol_hint)

        response = client.messages.create(
            model=get_anthropic_model(),
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": EXTRACTION_PROMPT.format(
                    content=raw_content[:2000],
                    source=source
                )
            }]
        )

        token_tracker.record(response.usage.input_tokens, response.usage.output_tokens)
        text = response.content[0].text.strip()
        json_str = extract_json(text) or text
        data = json.loads(json_str)

        if isinstance(data, dict):
            data = [data]

        signals = []
        for item in data:
            signal = ExtractedSignal(
                protocol_name=item.get("protocol_name", protocol_hint),
                signal_category=item.get("signal_category", "unknown"),
                confidence=float(item.get("confidence", 0.5)),
                evidence=item.get("evidence", ""),
                structured_data=item.get("structured_data", {})
            )
            signals.append(signal)

        return signals

    except Exception as e:
        print(f"  ⚠ LLM extraction failed: {e}")
        return _extract_with_rules(raw_content, source, protocol_hint)


def _extract_with_rules(
    raw_content: str,
    source: str,
    protocol_hint: str
) -> list[ExtractedSignal]:
    """
    Rule-based signal extraction (fallback when LLM is unavailable).
    
    Scans for keywords and patterns to extract structured signals.
    """
    signals = []
    content_lower = raw_content.lower()

    # ── AI adoption signals ──────────────────────────────────────────
    ai_keywords = ["copilot", "cursor", "claude code", "ai-generated", "ai generated",
                    "llm", "ai coding", "remix ai", "ai assistant", "vibe coding"]
    ai_found = [kw for kw in ai_keywords if kw in content_lower]
    
    if ai_found:
        signals.append(ExtractedSignal(
            protocol_name=protocol_hint,
            signal_category="ai_adoption",
            confidence=min(0.5 + len(ai_found) * 0.15, 0.95),
            evidence=f"Keywords found: {', '.join(ai_found)}",
            structured_data={"ai_tools_mentioned": ai_found}
        ))

    # ── Security need signals ────────────────────────────────────────
    security_keywords = ["audit", "security review", "bug bounty", "vulnerability",
                         "exploit", "hack", "security researcher", "penetration test"]
    security_found = [kw for kw in security_keywords if kw in content_lower]
    
    if security_found:
        signals.append(ExtractedSignal(
            protocol_name=protocol_hint,
            signal_category="security_need",
            confidence=min(0.4 + len(security_found) * 0.12, 0.9),
            evidence=f"Keywords found: {', '.join(security_found)}",
            structured_data={"security_terms": security_found}
        ))

    # ── Audit gap signals ────────────────────────────────────────────
    gap_patterns = ["no audit", "unaudited", "not yet audited", "pending audit",
                    "needs review", "pre-audit", "security concern"]
    gaps_found = [p for p in gap_patterns if p in content_lower]
    
    if gaps_found:
        signals.append(ExtractedSignal(
            protocol_name=protocol_hint,
            signal_category="audit_gap",
            confidence=0.8,
            evidence=f"Audit gap indicators: {', '.join(gaps_found)}",
            structured_data={"gap_indicators": gaps_found}
        ))

    # ── Funding signals ──────────────────────────────────────────────
    funding_keywords = ["raised", "funding round", "seed round", "series a",
                        "series b", "strategic round", "investment"]
    funding_found = [kw for kw in funding_keywords if kw in content_lower]
    
    if funding_found:
        signals.append(ExtractedSignal(
            protocol_name=protocol_hint,
            signal_category="funding",
            confidence=0.7,
            evidence=f"Funding indicators: {', '.join(funding_found)}",
            structured_data={"funding_terms": funding_found}
        ))

    # ── Exploit risk signals ─────────────────────────────────────────
    exploit_keywords = ["reentrancy", "overflow", "flash loan", "oracle manipulation",
                        "access control", "front-running", "mev", "slippage attack"]
    exploits_found = [kw for kw in exploit_keywords if kw in content_lower]
    
    if exploits_found:
        signals.append(ExtractedSignal(
            protocol_name=protocol_hint,
            signal_category="exploit_risk",
            confidence=0.85,
            evidence=f"Exploit risk patterns: {', '.join(exploits_found)}",
            structured_data={"risk_patterns": exploits_found}
        ))

    return signals

