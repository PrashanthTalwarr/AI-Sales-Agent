"""
SCORE — Weighted composite lead scoring engine.

Every point value comes from config/scoring_weights.json. Editing that file
changes scoring; there are no hardcoded numbers in this module. Each scoring
function takes the relevant `rules` dict from the config and looks its answer
up by key, so a rule renamed or retuned in the JSON takes effect immediately.

Five factors, weighted:
  1. TVL & Funds at Risk    (weights.tvl_and_funds_at_risk)
  2. Audit Status           (weights.audit_status)
  3. Shipping Velocity      (weights.shipping_velocity)
  4. Funding Recency        (weights.funding_recency)
  5. Reachability           (weights.reachability)

Composite = sum of all factors. Tier comes from config tier_thresholds.

After 10 discovery calls, recalibrate the JSON based on what predicted conversion.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, date
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


# ── Config access ─────────────────────────────────────────────────────────────

def _rules(config: dict, factor: str) -> dict:
    """Return the rules dict for one weighted factor."""
    return (config.get("weights", {}).get(factor, {}) or {}).get("rules", {}) or {}


def _factor(config: dict, factor: str) -> dict:
    return config.get("weights", {}).get(factor, {}) or {}


def _points(rules: dict, key: str, factor_name: str = "") -> float:
    """
    Look a rule up by key. A missing key scores 0 and is logged loudly — a typo
    in the config should be visible, not silently absorbed into the score.
    """
    if key not in rules:
        logger.warning("Scoring rule %r missing from config%s — scoring 0",
                       key, f" ({factor_name})" if factor_name else "")
        return 0
    return float(rules[key])


# ── Factor scorers ────────────────────────────────────────────────────────────

_TVL_RULE_BY_CATEGORY = {
    "mega":      "tvl_above_1b",
    "large":     "tvl_100m_to_1b",
    "mid":       "tvl_10m_to_100m",
    "small":     "tvl_1m_to_10m",
    "prelaunch": "tvl_below_1m_or_prelaunch",
}


def score_tvl(tvl_usd: float, tvl_category: str, rules: dict) -> float:
    """Higher TVL = more funds at risk = more urgency."""
    key = _TVL_RULE_BY_CATEGORY.get(tvl_category)
    if key is None:
        return _points(rules, "no_data", "tvl_and_funds_at_risk")
    return _points(rules, key, "tvl_and_funds_at_risk")


def score_audit_status(
    has_been_audited: bool,
    last_audit_date: Optional[str],
    has_bug_bounty: bool,
    bounty_platform: str,
    unaudited_new_code: bool,
    rules: dict,
    competitor_platforms: tuple = ("immunefi", "code4rena", "sherlock"),
) -> float:
    """
    No audit or a stale audit means the highest need. Protocols already on our
    own platform score 0 — they are already clients.
    """
    if bounty_platform == "our_platform":
        return _points(rules, "already_a_client", "audit_status")

    if not has_been_audited:
        return _points(rules, "no_audit_ever", "audit_status")

    if unaudited_new_code:
        return _points(rules, "audited_but_shipping_new_unaudited_code", "audit_status")

    if last_audit_date:
        try:
            audit_date = datetime.strptime(last_audit_date, "%Y-%m-%d").date()
            if (date.today() - audit_date).days / 30 > 6:
                return _points(rules, "single_audit_over_6mo_ago", "audit_status")
        except (ValueError, TypeError):
            pass

    if not has_bug_bounty:
        return _points(rules, "single_recent_audit_no_bounty", "audit_status")

    if bounty_platform in competitor_platforms:
        return _points(rules, "active_bounty_on_competitor_platform", "audit_status")

    return _points(rules, "multiple_audits_no_continuous_program", "audit_status")


_VELOCITY_RULE_BY_LEVEL = {
    "very_high": "daily_commits_new_contracts_deploying_weekly",
    "high":      "active_development_multiple_repos",
    "moderate":  "moderate_activity_monthly_deploys",
    "low":       "slow_development_stable_protocol",
    "inactive":  "no_recent_activity",
}


def score_velocity(shipping_velocity: str, ai_tool_signals: list, factor: dict) -> float:
    """
    Faster shipping = more unreviewed code.

    AI tool signals (.cursorrules, Copilot config, ...) add a bonus on top of the
    base tier — they are the core hypothesis of this pipeline, so they need to
    discriminate between two protocols shipping at the same rate. Both the
    per-signal points and the bonus ceiling come from config, and the total is
    clamped to the factor's max_score.
    """
    rules = factor.get("rules", {}) or {}
    key = _VELOCITY_RULE_BY_LEVEL.get(shipping_velocity)
    base = _points(rules, key, "shipping_velocity") if key else 0

    bonus_cfg = factor.get("ai_tool_signal_bonus", {}) or {}
    per_signal = float(bonus_cfg.get("points_per_signal", 0))
    max_bonus = float(bonus_cfg.get("max_bonus", 0))
    bonus = min(len(ai_tool_signals or []) * per_signal, max_bonus)

    max_score = float(factor.get("max_score", base + bonus))
    return min(base + bonus, max_score)


def score_funding(total_raised: float, last_funding_date: Optional[str], rules: dict) -> float:
    """Recent funding = budget available for security services."""
    if not last_funding_date or last_funding_date in ("N/A", ""):
        return _points(rules, "no_funding_data_or_dao", "funding_recency")

    try:
        fund_date = datetime.strptime(last_funding_date, "%Y-%m").date()
        months_ago = (date.today() - fund_date).days / 30
    except (ValueError, TypeError):
        return _points(rules, "no_funding_data_or_dao", "funding_recency")

    if months_ago <= 3:
        return _points(rules, "raised_in_last_3_months", "funding_recency")
    if months_ago <= 6:
        return _points(rules, "raised_in_last_6_months", "funding_recency")
    if months_ago <= 12:
        return _points(rules, "raised_in_last_12_months", "funding_recency")
    return _points(rules, "raised_over_12_months_ago", "funding_recency")


def score_reachability(
    team_type: str,
    warm_intro_available: bool,
    twitter_handle: str,
    rules: dict,
) -> float:
    """How reachable is the decision maker within two hops?"""
    if warm_intro_available:
        return _points(rules, "warm_intro_via_researcher_network", "reachability")
    if team_type == "doxxed" and twitter_handle:
        return _points(rules, "team_doxxed_active_on_twitter", "reachability")
    if team_type == "doxxed":
        return _points(rules, "team_doxxed_reachable_via_discord_telegram", "reachability")
    if team_type == "partially_doxxed":
        return _points(rules, "team_partially_doxxed", "reachability")
    if team_type == "anonymous":
        return _points(rules, "fully_anonymous_team", "reachability")
    return _points(rules, "team_unknown", "reachability")


# ── Rationale ─────────────────────────────────────────────────────────────────

def generate_rationale(protocol_name: str, scores: dict, profile) -> str:
    """Human-readable explanation of why a protocol scored the way it did."""
    parts = []

    tvl_str = f"${profile.tvl_usd:,.0f}" if profile.tvl_usd else "unknown"
    parts.append(f"TVL of {tvl_str} ({profile.tvl_category} tier)")

    if not profile.has_been_audited:
        parts.append("NO audit history — highest security need")
    elif profile.unaudited_new_code:
        parts.append("audited previously but shipping new unreviewed code")
    elif profile.has_bug_bounty:
        parts.append(f"active bounty on {profile.bounty_platform}")
    else:
        parts.append("audited but no continuous security program")

    parts.append(f"shipping velocity: {profile.shipping_velocity}")
    if profile.ai_tool_signals:
        parts.append(f"AI tool signals detected: {', '.join(profile.ai_tool_signals[:2])}")

    if profile.total_raised_usd:
        parts.append(f"raised ${profile.total_raised_usd:,.0f}")

    return f"{protocol_name}: {'. '.join(parts)}."


# ── Composite ─────────────────────────────────────────────────────────────────

def score_protocol(profile, config: dict) -> ScoredLead:
    """Score one enriched protocol using the weights in config."""
    tvl = score_tvl(
        profile.tvl_usd, profile.tvl_category,
        _rules(config, "tvl_and_funds_at_risk"),
    )
    audit = score_audit_status(
        profile.has_been_audited,
        profile.last_audit_date,
        profile.has_bug_bounty,
        profile.bounty_platform,
        profile.unaudited_new_code,
        _rules(config, "audit_status"),
    )
    velocity = score_velocity(
        profile.shipping_velocity, profile.ai_tool_signals,
        _factor(config, "shipping_velocity"),
    )
    funding = score_funding(
        profile.total_raised_usd, profile.last_funding_date,
        _rules(config, "funding_recency"),
    )
    reachability = score_reachability(
        profile.team_type, profile.warm_intro_available, profile.twitter_handle,
        _rules(config, "reachability"),
    )

    composite = tvl + audit + velocity + funding + reachability

    thresholds = config.get("tier_thresholds", {"hot": 90, "warm": 75})
    if composite >= thresholds.get("hot", 90):
        tier = "hot"
    elif composite >= thresholds.get("warm", 75):
        tier = "warm"
    else:
        tier = "cool"

    scores = {
        "tvl": tvl, "audit": audit, "velocity": velocity,
        "funding": funding, "reachability": reachability,
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
        model_version=config.get("model_version", "1.0"),
    )


def run_scoring(profiles: list, config_path: str = "config/scoring_weights.json") -> list[ScoredLead]:
    """Score all enriched profiles, highest composite first."""
    print("\n" + "=" * 60, flush=True)
    print("SCORE STAGE — Weighted composite lead scoring", flush=True)
    print("=" * 60 + "\n", flush=True)

    config = load_config(config_path)
    if not config.get("weights"):
        logger.error("No weights found in %s — every protocol will score 0", config_path)
        print(f"  !! No weights in {config_path} — scores will all be 0", flush=True)

    logger.info("Scoring config loaded: version=%s, thresholds=%s",
                config.get("model_version", "?"), config.get("tier_thresholds", {}))
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

    scored.sort(key=lambda x: x.composite_score, reverse=True)

    hot = [s for s in scored if s.score_tier == "hot"]
    warm = [s for s in scored if s.score_tier == "warm"]
    logger.info("Scoring complete: %d total, %d hot, %d warm", len(scored), len(hot), len(warm))
    print(f"\n✓ Scored {len(scored)} protocols: {len(hot)} hot, {len(warm)} warm", flush=True)

    return scored


if __name__ == "__main__":
    from src.pipeline.enrich import EnrichedProfile
    test = EnrichedProfile(
        protocol_name="TestDEX",
        tvl_usd=250_000_000,
        tvl_category="large",
        has_been_audited=False,
        shipping_velocity="high",
        ai_tool_signals=[".cursorrules found"],
        total_raised_usd=20_000_000,
        last_funding_date="2026-08",
        team_type="doxxed",
        twitter_handle="@testdex",
    )
    cfg = load_config()
    result = score_protocol(test, cfg)
    print(f"\nTest: {result.protocol_name} = {result.composite_score} ({result.score_tier})")
    print(f"Rationale: {result.scoring_rationale}")
