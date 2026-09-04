"""
ENRICH — Combines raw signals into a structured profile per protocol.

Takes signals from the Ingest stage and builds a complete picture:
  - TVL & chain data (from DeFiLlama signals)
  - Audit status (from competition platforms, known audit providers)
  - Development velocity (from GitHub signals)
  - Funding data (from Crunchbase signals)
  - Team & reachability (from GitHub + Claude web search)

Output: One Enrichment record per protocol in the database.
"""

import json
from datetime import datetime, date, timedelta
from typing import Optional
from dataclasses import dataclass, asdict, field


@dataclass
class EnrichedProfile:
    """Complete enriched profile for a protocol."""
    protocol_name: str

    # TVL & Risk
    tvl_usd: float = 0
    tvl_category: str = "unknown"   # mega, large, mid, small, prelaunch
    chains_deployed: list = field(default_factory=list)
    contract_count: int = 0

    # Audit Status
    has_been_audited: bool = False
    audit_providers: list = field(default_factory=list)
    last_audit_date: Optional[str] = None
    has_bug_bounty: bool = False
    bounty_platform: str = "none"
    bounty_amount_usd: float = 0
    unaudited_new_code: bool = False

    # Development Velocity
    github_commits_30d: int = 0
    github_contributors: int = 0
    languages: list = field(default_factory=list)
    deploys_last_30d: int = 0
    ai_tool_signals: list = field(default_factory=list)
    shipping_velocity: str = "unknown"

    # Funding
    total_raised_usd: float = 0
    last_funding_date: Optional[str] = None
    last_funding_amount: float = 0
    investors: list = field(default_factory=list)

    # Team & Reachability
    team_members: list = field(default_factory=list)
    team_type: str = "unknown"
    twitter_handle: str = ""
    discord_url: str = ""
    warm_intro_available: bool = False
    warm_intro_path: str = ""

    # GitHub orgs (from DeFiLlama) — used for contributor lookup
    github_orgs: list = field(default_factory=list)


def classify_tvl(tvl: float) -> str:
    """Classify TVL into categories matching scoring_weights.json."""
    if tvl >= 1_000_000_000:
        return "mega"
    elif tvl >= 100_000_000:
        return "large"
    elif tvl >= 10_000_000:
        return "mid"
    elif tvl >= 1_000_000:
        return "small"
    return "prelaunch"


def classify_velocity(commits_30d: int, deploys_30d: int) -> str:
    """Classify shipping velocity based on GitHub + deployment activity."""
    activity = commits_30d + (deploys_30d * 5)  # deploys weighted more
    if activity >= 100:
        return "very_high"
    elif activity >= 50:
        return "high"
    elif activity >= 20:
        return "moderate"
    elif activity > 0:
        return "low"
    return "inactive"


def enrich_protocol(protocol_name: str, signals: list) -> EnrichedProfile:
    """
    Take all signals for a single protocol and build an enriched profile.
    
    This combines data from multiple sources into one structured record.
    In production, this also calls contact enrichment APIs.
    """
    profile = EnrichedProfile(protocol_name=protocol_name)

    for signal in signals:
        data = signal.extracted_data if hasattr(signal, 'extracted_data') else signal.get("extracted_data", {})
        sig_type = signal.signal_type if hasattr(signal, 'signal_type') else signal.get("signal_type", "")

        # ── TVL Data (from DeFiLlama) ─────────────────────────────────
        if sig_type == "tvl_data":
            profile.tvl_usd = data.get("tvl_usd", 0)
            profile.tvl_category = classify_tvl(profile.tvl_usd)
            profile.chains_deployed = data.get("chains", [])
            profile.twitter_handle = data.get("twitter", "")
            profile.github_orgs = data.get("github_orgs", [])

        # ── GitHub Activity ───────────────────────────────────────────
        elif sig_type == "github_activity":
            profile.github_commits_30d = data.get("commits_30d", 0)
            profile.github_contributors = data.get("contributors", 0)
            profile.languages = data.get("languages", [])
            profile.ai_tool_signals = data.get("ai_tool_signals", [])
            profile.deploys_last_30d = data.get("deploys_30d", 0)

        # ── Funding Data ──────────────────────────────────────────────
        elif sig_type == "funding":
            profile.total_raised_usd = data.get("amount_usd", 0)
            profile.last_funding_date = data.get("date")
            profile.last_funding_amount = data.get("amount_usd", 0)
            profile.investors = data.get("investors", [])

    # ── Derived fields ────────────────────────────────────────────────
    profile.shipping_velocity = classify_velocity(
        profile.github_commits_30d,
        profile.deploys_last_30d
    )

    return profile


def enrich_with_audit_data(profile: EnrichedProfile, audit_data: dict) -> EnrichedProfile:
    """
    Layer on audit/bounty data.
    
    In production, this scrapes Immunefi, Code4rena, Sherlock
    to check if the protocol has active programs.
    For the demo, we use pre-researched data.
    """
    profile.has_been_audited = audit_data.get("has_been_audited", False)
    profile.audit_providers = audit_data.get("audit_providers", [])
    profile.last_audit_date = audit_data.get("last_audit_date")
    profile.has_bug_bounty = audit_data.get("has_bug_bounty", False)
    profile.bounty_platform = audit_data.get("bounty_platform", "none")
    profile.bounty_amount_usd = audit_data.get("bounty_amount_usd", 0)
    profile.unaudited_new_code = audit_data.get("unaudited_new_code", False)
    return profile


def enrich_with_team_data(profile: EnrichedProfile, team_data: dict) -> EnrichedProfile:
    """
    Layer on team/reachability data.
    
    In production, this calls contact enrichment APIs.
    For the demo, we use pre-researched data.
    """
    profile.team_members = team_data.get("team_members", [])
    profile.team_type = team_data.get("team_type", "unknown")
    profile.twitter_handle = team_data.get("twitter", profile.twitter_handle)
    profile.discord_url = team_data.get("discord", "")
    profile.warm_intro_available = team_data.get("warm_intro_available", False)
    profile.warm_intro_path = team_data.get("warm_intro_path", "")
    return profile


def run_enrichment(protocol_signals_map: dict) -> list[EnrichedProfile]:
    """
    Enrich all protocols from their grouped signals.
    
    Args:
        protocol_signals_map: {protocol_name: [list of RawSignal objects]}
    
    Returns:
        List of EnrichedProfile objects ready for scoring
    """
    print("\n" + "=" * 60, flush=True)
    print("ENRICH STAGE — Building protocol profiles", flush=True)
    print("=" * 60 + "\n", flush=True)

    profiles = []
    for name, signals in protocol_signals_map.items():
        profile = enrich_protocol(name, signals)
        profiles.append(profile)
        print(f"  ✓ {name}: TVL=${profile.tvl_usd:,.0f} | velocity={profile.shipping_velocity} | ai_signals={len(profile.ai_tool_signals)}", flush=True)

    print(f"\n✓ Enriched {len(profiles)} protocols", flush=True)
    return profiles


if __name__ == "__main__":
    # Quick test
    from ingest import RawSignal
    test_signals = [
        RawSignal("TestProtocol", "tvl_data", "defillama", "", "", {"tvl_usd": 500_000_000, "chains": ["ethereum", "arbitrum"]}, 0.8),
        RawSignal("TestProtocol", "github_activity", "github", "", "", {"commits_30d": 85, "languages": ["solidity"], "ai_tool_signals": [".cursorrules found"]}, 0.7),
    ]
    profile = enrich_protocol("TestProtocol", test_signals)
    print(f"Test: {profile.protocol_name} | TVL={profile.tvl_category} | velocity={profile.shipping_velocity}")
