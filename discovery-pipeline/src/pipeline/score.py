"""
SCORE — Weighted composite lead scoring engine.

Reads weights from config/scoring_weights.json (configurable without code changes).
Scores each enriched protocol on 5 factors:
  1. TVL & Funds at Risk (30 pts)
  2. Audit Status (25 pts) 
  3. Shipping Velocity (20 pts)
  4. Funding Recency (15 pts)
  5. Reachability (10 pts)

Composite = sum of all factors (0-100).
Tier: hot (90+), warm (75-89), cool (<75).

After 10 discovery calls, recalibrate weights based on what predicted conversion.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Optional

from src.utils.config import load_config

logger = logging.getLogger(__name__)


@dataclass
class ScoredLead:
    """Output of the scoring engine."""
    protocol_name: str
    tvl_score: float
    audit_status_score: float
    velocity_score: float
    funding_score: float
    reachability_score: float
    composite_score: float
    score_tier: str           # 'hot', 'warm', 'cool'
    scoring_rationale: str    # LLM-generated or rule-based explanation
    model_version: str



def score_tvl(tvl_usd: float, tvl_category: str) -> float:
    """Score based on TVL — higher TVL = more funds at risk = more urgency."""
    scoring_map = {
        "mega": 30,       # >$1B
        "large": 25,      # $100M-$1B
        "mid": 20,        # $10M-$100M
        "small": 14,      # $1M-$10M
        "prelaunch": 8,   # <$1M or pre-launch
    }
    return scoring_map.get(tvl_category, 0)


def score_audit_status(
    has_been_audited: bool,
    last_audit_date: Optional[str],
    has_bug_bounty: bool,
    bounty_platform: str,
    unaudited_new_code: bool
) -> float:
    """
    Score based on audit status — no audit or stale audit = highest need.
    Protocols already using our platform score 0 (they are already clients).
    """
    if bounty_platform == "our_platform":
        return 0  # Already a client

    if not has_been_audited:
        return 25  # Never audited — maximum need

    # Has been audited but...
    if unaudited_new_code:
        return 20  # Shipping new code without review

    if last_audit_date:
        try:
            audit_date = datetime.strptime(last_audit_date, "%Y-%m-%d").date()
            months_since = (date.today() - audit_date).days / 30
            if months_since > 6:
                return 22  # Stale audit
        except (ValueError, TypeError):
            pass

    if not has_bug_bounty:
        return 16  # Audited but no continuous program

    if bounty_platform in ["immunefi", "code4rena", "sherlock"]:
        return 8   # Active bounty on competitor — still a target

    return 10  # Multiple audits, no continuous program


def score_velocity(shipping_velocity: str, ai_tool_signals: list) -> float:
    """
    Score based on shipping speed — faster = more unreviewed code.
    AI tool signals are a bonus (directly relevant to Hypothesis A).
    """
    base_scores = {
        "very_high": 16,
        "high": 13,
        "moderate": 10,
        "low": 5,
        "inactive": 0,
    }
    base = base_scores.get(shipping_velocity, 0)

    # Bonus for AI tool adoption signals (max 4 pts)
    ai_bonus = min(len(ai_tool_signals) * 2, 4)

    return min(base + ai_bonus, 20)  # Cap at max 20


def score_funding(
    total_raised: float,
    last_funding_date: Optional[str]
) -> float:
    """Score based on funding recency — recent funding = budget for security."""
    if not last_funding_date or last_funding_date in ["N/A", ""]:
        return 3  # No data or DAO

    try:
        fund_date = datetime.strptime(last_funding_date, "%Y-%m").date()
        months_ago = (date.today() - fund_date).days / 30

        if months_ago <= 3:
            return 15
        elif months_ago <= 6:
            return 12
        elif months_ago <= 12:
            return 9
        else:
            return 5
    except (ValueError, TypeError):
        return 3


def score_reachability(
    team_type: str,
    warm_intro_available: bool,
    twitter_handle: str
) -> float:
    """Score based on how reachable the decision maker is."""
    if warm_intro_available:
        return 10
    elif team_type == "doxxed" and twitter_handle:
        return 8
    elif team_type == "doxxed":
        return 6
    elif team_type == "partially_doxxed":
        return 4
    elif team_type == "anonymous":
        return 2
    return 3


def generate_rationale(protocol_name: str, scores: dict, profile) -> str:
    """
    Generate a human-readable scoring rationale.
    
    In production, this calls Claude API for a richer explanation.
    For the demo, we use rule-based generation.
    """
    parts = []

    # TVL
    tvl_str = f"${profile.tvl_usd:,.0f}" if profile.tvl_usd else "unknown"
    parts.append(f"TVL of {tvl_str} ({profile.tvl_category} tier)")

    # Audit
    if not profile.has_been_audited:
        parts.append("NO audit history — highest security need")
    elif profile.unaudited_new_code:
        parts.append("audited previously but shipping new unreviewed code")
    elif profile.has_bug_bounty:
        parts.append(f"active bounty on {profile.bounty_platform}")
    else:
        parts.append("audited but no continuous security program")

    # Velocity
    parts.append(f"shipping velocity: {profile.shipping_velocity}")
    if profile.ai_tool_signals:
        parts.append(f"AI tool signals detected: {', '.join(profile.ai_tool_signals[:2])}")

    # Funding
    if profile.total_raised_usd:
        parts.append(f"raised ${profile.total_raised_usd:,.0f}")

    return f"{protocol_name}: {'. '.join(parts)}."


def score_protocol(profile, config: dict) -> ScoredLead:
    """
    Score a single enriched protocol using the weighted model.
    
    Args:
        profile: EnrichedProfile from the Enrich stage
        config: Scoring config loaded from JSON
        
    Returns:
        ScoredLead with all factor scores and composite
    """
    tvl = score_tvl(profile.tvl_usd, profile.tvl_category)
    audit = score_audit_status(
        profile.has_been_audited,
        profile.last_audit_date,
        profile.has_bug_bounty,
        profile.bounty_platform,
        profile.unaudited_new_code
    )
    velocity = score_velocity(profile.shipping_velocity, profile.ai_tool_signals)
    funding = score_funding(profile.total_raised_usd, profile.last_funding_date)
    reachability = score_reachability(
        profile.team_type,
        profile.warm_intro_available,
        profile.twitter_handle
    )

    composite = tvl + audit + velocity + funding + reachability

    # Determine tier
    thresholds = config.get("tier_thresholds", {"hot": 90, "warm": 75})
    if composite >= thresholds["hot"]:
        tier = "hot"
    elif composite >= thresholds["warm"]:
        tier = "warm"
    else:
        tier = "cool"

    scores = {
        "tvl": tvl, "audit": audit, "velocity": velocity,
        "funding": funding, "reachability": reachability
    }
    rationale = generate_rationale(profile.protocol_name, scores, profile)

    return ScoredLead(
        protocol_name=profile.protocol_name,
        tvl_score=tvl,
        audit_status_score=audit,
        velocity_score=velocity,
        funding_score=funding,
        reachability_score=reachability,
        composite_score=composite,
        score_tier=tier,
        scoring_rationale=rationale,
        model_version=config.get("model_version", "1.0")
    )


def run_scoring(profiles: list, config_path: str = "config/scoring_weights.json") -> list[ScoredLead]:
    """
    Score all enriched profiles.
    
    Args:
        profiles: List of EnrichedProfile objects
        config_path: Path to scoring weights JSON
        
    Returns:
        List of ScoredLead objects, sorted by composite score descending
    """
    print("\n" + "=" * 60, flush=True)
    print("SCORE STAGE — Weighted composite lead scoring", flush=True)
    print("=" * 60 + "\n", flush=True)

    config = load_config(config_path)
    logger.info(f"Scoring config loaded: version={config.get('model_version', '?')}, thresholds={config.get('tier_thresholds', {})}")
    scored = []

    for profile in profiles:
        lead = score_protocol(profile, config)
        scored.append(lead)

        logger.debug(
            f"Scored {lead.protocol_name}: {lead.composite_score:.1f} ({lead.score_tier}) | "
            f"tvl={lead.tvl_score} audit={lead.audit_status_score} vel={lead.velocity_score} "
            f"fund={lead.funding_score} reach={lead.reachability_score}"
        )

        tier_icon = {"hot": "🔥", "warm": "🟡", "cool": "⚪"}.get(lead.score_tier, "?")
        print(
            f"  {tier_icon} {lead.protocol_name:20s} | "
            f"Score: {lead.composite_score:5.1f} ({lead.score_tier:4s}) | "
            f"TVL:{lead.tvl_score:.0f} Audit:{lead.audit_status_score:.0f} "
            f"Vel:{lead.velocity_score:.0f} Fund:{lead.funding_score:.0f} "
            f"Reach:{lead.reachability_score:.0f}",
            flush=True
        )

    # Sort by composite score descending
    scored.sort(key=lambda x: x.composite_score, reverse=True)

    # Summary
    hot = [s for s in scored if s.score_tier == "hot"]
    warm = [s for s in scored if s.score_tier == "warm"]
    logger.info(f"Scoring complete: {len(scored)} total, {len(hot)} hot, {len(warm)} warm")
    print(f"\n✓ Scored {len(scored)} protocols: {len(hot)} hot, {len(warm)} warm", flush=True)

    return scored


if __name__ == "__main__":
    # Quick test with mock data
    from enrich import EnrichedProfile
    test = EnrichedProfile(
        protocol_name="TestDEX",
        tvl_usd=250_000_000,
        tvl_category="large",
        has_been_audited=False,
        shipping_velocity="high",
        ai_tool_signals=[".cursorrules found"],
        total_raised_usd=20_000_000,
        last_funding_date="2025-09",
        team_type="doxxed",
        twitter_handle="@testdex",
    )
    config = load_config()
    result = score_protocol(test, config)
    print(f"\nTest: {result.protocol_name} = {result.composite_score} ({result.score_tier})")
    print(f"Rationale: {result.scoring_rationale}")
