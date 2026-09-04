"""
EVENT MONITOR — Detects market events and triggers contextual outreach.

Monitors:
  - DeFi exploits (rekt.news, DeFiLlama hacks endpoint)
  - Funding rounds (Crunchbase, Messari)
  - New contract deployments (Etherscan)
  - Governance proposals mentioning security (Snapshot, Tally)

When a relevant event is detected, it:
  1. Checks if any pipeline target protocols are affected
  2. Creates a MarketEvent record
  3. Triggers contextual outreach via the outreach agent
  4. Sends a Slack alert

JD alignment: "monitoring systems that detect market events (exploits, launches, 
governance proposals, funding rounds) and trigger contextual outreach"

In production: runs continuously on AWS Lambda via CloudWatch Events / Step Functions.
"""

import json
import logging
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DetectedEvent:
    """A market event detected by the monitor."""
    event_type: str          # 'exploit', 'funding_round', 'mainnet_launch', 'governance_vote'
    title: str
    description: str
    source: str
    source_url: str
    affected_protocols: list = field(default_factory=list)
    relevance_tags: list = field(default_factory=list)
    detected_at: str = field(default_factory=lambda: datetime.now().isoformat())


# ── DeFiLlama Hacks Monitor (real API) ───────────────────────────────────────

def check_recent_exploits(days_back: int = 7) -> list[DetectedEvent]:
    """
    Check DeFiLlama hacks endpoint for recent exploits.
    This is a REAL API call — DeFiLlama hacks endpoint is free.
    """
    logger.info(f"DeFiLlama hacks API: checking last {days_back} days")
    events = []
    try:
        resp = requests.get("https://api.llama.fi/hacks", timeout=15)
        if resp.status_code != 200:
            logger.warning(f"DeFiLlama hacks API returned {resp.status_code}")
            return events

        hacks = resp.json()
        logger.debug(f"DeFiLlama hacks: {len(hacks)} total records fetched")
        cutoff = datetime.now() - timedelta(days=days_back)

        for hack in hacks:
            hack_date = datetime.fromtimestamp(hack.get("date", 0) or 0)
            if hack_date < cutoff:
                continue

            amount = hack.get("amount") or 0
            name = hack.get("name") or "Unknown"
            event = DetectedEvent(
                event_type="exploit",
                title=f"{name} exploit -- ${amount:,.0f} lost",
                description=(
                    f"Protocol: {name}\n"
                    f"Amount lost: ${amount:,.0f}\n"
                    f"Classification: {hack.get('classification') or 'unknown'}\n"
                    f"Chain: {hack.get('chain') or 'unknown'}\n"
                    f"Technique: {hack.get('technique') or 'unknown'}"
                ),
                source="defillama_hacks",
                source_url=hack.get("link") or "",
                affected_protocols=[name],
                relevance_tags=list(filter(None, [
                    hack.get("classification"),
                    hack.get("technique"),
                    hack.get("chain"),
                    "exploit"
                ]))
            )
            logger.info(f"Exploit detected: {event.title} (chain={hack.get('chain')}, technique={hack.get('technique')})")
            events.append(event)

        logger.info(f"DeFiLlama hacks: {len(events)} exploits in last {days_back} days")
        print(f"  ✓ DeFiLlama hacks: {len(events)} exploits in last {days_back} days", flush=True)

    except requests.RequestException as e:
        logger.error(f"DeFiLlama hacks API error: {e}")
        print(f"  ✗ DeFiLlama hacks API error: {e}", flush=True)

    return events


# ── Simulated Monitors (would be real API calls in production) ────────────────

def check_funding_rounds() -> list[DetectedEvent]:
    """
    Simulated: In production, polls Crunchbase/CryptoRank API.
    Detects new funding rounds for Web3 projects.
    """
    # Would be real API data in production
    recent = [
        {
            "protocol": "New DeFi Protocol",
            "amount": 8_000_000,
            "round": "Seed",
            "investors": ["Paradigm", "Variant"],
            "date": "2026-03",
        }
    ]
    
    events = []
    for r in recent:
        event = DetectedEvent(
            event_type="funding_round",
            title=f"{r['protocol']} raises ${r['amount']:,.0f} ({r['round']})",
            description=f"Investors: {', '.join(r['investors'])}",
            source="crunchbase_simulated",
            source_url="",
            affected_protocols=[r["protocol"]],
            relevance_tags=["funding", r["round"].lower()]
        )
        events.append(event)

    return events


def check_governance_security_proposals() -> list[DetectedEvent]:
    """
    Simulated: In production, polls Snapshot/Tally APIs.
    Detects governance proposals mentioning security, audits, or bug bounties.
    """
    # Would query Snapshot GraphQL API in production
    proposals = [
        {
            "protocol": "Aave",
            "title": "Proposal to increase bug bounty budget to $2M",
            "url": "https://snapshot.org/#/aave.eth/proposal/...",
            "tags": ["security", "bug_bounty", "governance"],
        }
    ]

    events = []
    for p in proposals:
        event = DetectedEvent(
            event_type="governance_vote",
            title=f"{p['protocol']}: {p['title']}",
            description=f"Governance proposal mentioning security budget",
            source="snapshot_simulated",
            source_url=p.get("url", ""),
            affected_protocols=[p["protocol"]],
            relevance_tags=p.get("tags", [])
        )
        events.append(event)

    return events


# ── Event → Outreach trigger logic ───────────────────────────────────────────

def check_event_relevance_to_pipeline(
    event: DetectedEvent,
    pipeline_protocols: list[str]
) -> list[str]:
    """
    Check if a market event is relevant to any protocols in our pipeline.
    
    Relevance rules:
    1. Direct match: the exploited protocol is in our pipeline
    2. Category match: an exploit in a DEX → outreach to other DEXs in pipeline
    3. Chain match: exploit on Solana → outreach to Solana protocols in pipeline
    """
    relevant_to = []

    # Direct match
    for protocol in pipeline_protocols:
        if protocol.lower() in [p.lower() for p in event.affected_protocols]:
            relevant_to.append(protocol)

    # Category/chain match would use enrichment data in production
    # For demo, we flag all protocols when a major exploit happens
    if event.event_type == "exploit" and not relevant_to:
        # A major exploit is relevant to ALL pipeline targets as context
        if any(tag in ["reentrancy", "overflow", "oracle_manipulation", "bridge"] 
               for tag in event.relevance_tags):
            relevant_to = pipeline_protocols[:3]  # Top 3 targets get contextual outreach

    return relevant_to


def run_event_monitor(pipeline_protocols: list[str]) -> list[DetectedEvent]:
    """
    Run all event monitors and return detected events.
    
    In production: runs on AWS Lambda every hour via CloudWatch Events.
    Step Functions orchestrates: detect → check relevance → trigger outreach → alert Slack.
    """
    print("\n" + "=" * 60, flush=True)
    print("MONITOR — Checking for market events", flush=True)
    print("=" * 60 + "\n", flush=True)

    all_events = []

    # 1. Recent exploits (real API)
    exploit_events = check_recent_exploits(days_back=30)
    all_events.extend(exploit_events)

    # 2. Funding rounds (simulated)
    funding_events = check_funding_rounds()
    all_events.extend(funding_events)

    # 3. Governance proposals (simulated)
    gov_events = check_governance_security_proposals()
    all_events.extend(gov_events)

    # Check relevance to pipeline
    triggered_count = 0
    for event in all_events:
        relevant = check_event_relevance_to_pipeline(event, pipeline_protocols)
        if relevant:
            event.affected_protocols = relevant
            triggered_count += 1
            logger.info(f"Event relevant to pipeline: [{event.event_type}] {event.title} → {relevant}")
            print(f"  🔔 {event.event_type}: {event.title}", flush=True)
            print(f"     → Relevant to: {', '.join(relevant)}", flush=True)
        else:
            logger.debug(f"Event not relevant to pipeline: [{event.event_type}] {event.title}")

    logger.info(f"Monitor complete: {len(all_events)} events, {triggered_count} relevant to pipeline")
    print(f"\n✓ Detected {len(all_events)} events, {triggered_count} relevant to pipeline", flush=True)
    return all_events
