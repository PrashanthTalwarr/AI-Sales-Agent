"""
DISCOVERY PIPELINE — Main Orchestrator

Runs the full pipeline end-to-end:
  1. INGEST  — Scrape Web3 data sources for raw signals
  2. ENRICH  — Build structured profiles per protocol
  3. SCORE   — Weighted composite lead scoring
  4. QUALIFY — Filter to score >= 75
  5. OUTREACH — Generate personalized messages via Claude API
  6. SEND    — Deliver emails via Resend
  7. EXPORT  — Save results to data/state.json, CSV + JSON

Usage:
  python scripts/run_pipeline.py              # Full pipeline
  python scripts/run_pipeline.py --no-llm     # Skip Claude API calls (use templates)
  python scripts/run_pipeline.py --seed-only  # Use seed data instead of live APIs
"""

import sys
import os

# ── UTF-8 output (needed on Windows terminals) ────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ── Load .env BEFORE any module imports that call os.getenv at import time ────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(__file__), "..", "config", ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
    elif os.path.exists(".env"):
        load_dotenv(".env")
except ImportError:
    pass

import logging
import json
import csv
import argparse
from datetime import datetime
from collections import defaultdict

from src.pipeline.ingest import run_full_ingest, RawSignal
from src.pipeline.enrich import run_enrichment, enrich_with_audit_data, enrich_with_team_data, EnrichedProfile
from src.pipeline.score import run_scoring
from src.agents.outreach_agent import run_outreach_generation
from src.integrations.contacts import find_contacts_for_qualified_leads
from src.integrations.email_sender import send_outreach_emails
from src.monitoring.event_monitor import run_event_monitor
from src.store.json_store import save_leads, save_contacts, save_outreach, save_drafts
from src.utils.config import load_config

logger = logging.getLogger(__name__)

_cfg = load_config()
MAX_QUALIFIED_LEADS    = _cfg.get("discovery", {}).get("max_qualified_leads", 3)
MAX_CONTACTS_PER_PROTO = _cfg.get("discovery", {}).get("max_contacts_per_protocol", 3)


def setup_logging(log_file: str = "app.log") -> str:
    """
    Configure logging: DEBUG to app.log (overwritten each run), WARNING+ to console.
    app.log always contains only the most recent run — no accumulation across runs.
    Returns the log file path.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # File handler — mode='w' overwrites on every run (no history kept)
    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    # Console handler — WARNING+ only so it doesn't double the print() output
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    root.addHandler(fh)
    root.addHandler(ch)
    return log_file


# ── Human-curated research overlays ──────────────────────────────────────────
# These are NOT the source of protocol discovery — DeFiLlama drives that.
# This dict layers audit/team/persona data onto protocols found by the live pipeline.
# In production, this data comes from contact enrichment APIs + audit platform scrapers.
# Keyed by DeFiLlama protocol name.

RESEARCH_OVERLAYS = {
    "Hyperliquid": {
        "audit": {
            "has_been_audited": True,
            "audit_providers": ["Zellic"],
            "last_audit_date": "2024-06-01",
            "has_bug_bounty": True,
            "bounty_platform": "immunefi",
            "bounty_amount_usd": 1_000_000,
            "unaudited_new_code": True,
        },
        "team": {
            "team_type": "partially_doxxed",
            "twitter": "@HyperliquidX",
            "team_members": [{"name": "Jeff", "role": "founder", "twitter": "@chameleon_jeff"}],
            "warm_intro_available": False,
        },
        "persona": {"name": "Jeff", "role": "Founder", "preferred_channel": "twitter_dm"},
        "category": "dex",
        "chains": ["Hyperliquid L1"],
        "override_tvl": 2_500_000_000,
    },
    "Ethena": {
        "audit": {
            "has_been_audited": True,
            "audit_providers": ["Quantstamp", "Pashov"],
            "last_audit_date": "2025-03-01",
            "has_bug_bounty": True,
            "bounty_platform": "immunefi",
            "bounty_amount_usd": 750_000,
            "unaudited_new_code": True,
        },
        "team": {
            "team_type": "doxxed",
            "twitter": "@ethaboratory",
            "team_members": [{"name": "Guy Young", "role": "founder", "twitter": "@laboratoryguy"}],
            "warm_intro_available": False,
        },
        "persona": {"name": "Guy", "role": "Founder", "preferred_channel": "twitter_dm"},
        "category": "stablecoin",
        "chains": ["Ethereum"],
        "override_tvl": 5_200_000_000,
    },
    "EigenLayer": {
        "audit": {
            "has_been_audited": True,
            "audit_providers": ["Trail of Bits", "Sigma Prime"],
            "last_audit_date": "2025-06-01",
            "has_bug_bounty": True,
            "bounty_platform": "immunefi",
            "bounty_amount_usd": 2_000_000,
            "unaudited_new_code": True,
        },
        "team": {
            "team_type": "doxxed",
            "twitter": "@eigenlayer",
            "team_members": [{"name": "Sreeram Kannan", "role": "founder", "twitter": "@saborados"}],
            "warm_intro_available": True,
            "warm_intro_path": "Discovery Pipeline researcher network — multiple researchers have audited EigenLayer code",
        },
        "persona": {"name": "Sreeram", "role": "Founder/CEO", "preferred_channel": "email"},
        "category": "infra",
        "chains": ["Ethereum"],
        "override_tvl": 8_000_000_000,
    },
    "Pendle": {
        "audit": {
            "has_been_audited": True,
            "audit_providers": ["Dedaub", "Dingbat"],
            "last_audit_date": "2025-01-01",
            "has_bug_bounty": True,
            "bounty_platform": "immunefi",
            "bounty_amount_usd": 200_000,
            "unaudited_new_code": True,
        },
        "team": {
            "team_type": "doxxed",
            "twitter": "@penaboratory",
            "team_members": [{"name": "TN", "role": "founder", "twitter": "@tn_penaboratory"}],
            "warm_intro_available": False,
        },
        "persona": {"name": "TN", "role": "Founder", "preferred_channel": "twitter_dm"},
        "category": "yield",
        "chains": ["Ethereum", "Arbitrum", "BSC"],
        "override_tvl": 3_500_000_000,
    },
    "LayerZero": {
        "audit": {
            "has_been_audited": True,
            "audit_providers": ["Zellic", "Trail of Bits"],
            "last_audit_date": "2025-04-01",
            "has_bug_bounty": True,
            "bounty_platform": "immunefi",
            "bounty_amount_usd": 15_000_000,
            "unaudited_new_code": True,
        },
        "team": {
            "team_type": "doxxed",
            "twitter": "@LayerZero_Labs",
            "team_members": [{"name": "Bryan Pellegrino", "role": "CEO", "twitter": "@PrimordialAA"}],
            "warm_intro_available": False,
        },
        "persona": {"name": "Bryan", "role": "CEO", "preferred_channel": "email"},
        "category": "bridge",
        "chains": ["Ethereum", "Arbitrum", "BNB Chain", "Polygon", "Avalanche", "Solana"],
        "override_tvl": 500_000_000,
    },
    "Kamino Finance": {
        "audit": {
            "has_been_audited": True,
            "audit_providers": ["OtterSec"],
            "last_audit_date": "2024-09-01",
            "has_bug_bounty": False,
            "bounty_platform": "none",
            "bounty_amount_usd": 0,
            "unaudited_new_code": True,
        },
        "team": {
            "team_type": "doxxed",
            "twitter": "@KaminoFinance",
            "team_members": [{"name": "Gabe", "role": "co-founder", "twitter": "@0xgabe_"}],
            "warm_intro_available": False,
        },
        "persona": {"name": "Gabe", "role": "Co-Founder", "preferred_channel": "twitter_dm"},
        "category": "yield",
        "chains": ["Solana"],
        "override_tvl": 1_200_000_000,
    },
    "Jupiter": {
        "audit": {
            "has_been_audited": True,
            "audit_providers": ["OtterSec", "Neodyme"],
            "last_audit_date": "2025-02-01",
            "has_bug_bounty": True,
            "bounty_platform": "immunefi",
            "bounty_amount_usd": 500_000,
            "unaudited_new_code": True,
        },
        "team": {
            "team_type": "doxxed",
            "twitter": "@JupiterExchange",
            "team_members": [{"name": "Meow", "role": "co-founder", "twitter": "@weremeow"}],
            "warm_intro_available": False,
        },
        "persona": {"name": "Meow", "role": "Co-Founder", "preferred_channel": "twitter_dm"},
        "category": "dex",
        "chains": ["Solana"],
        "override_tvl": 2_000_000_000,
    },
    "Blast": {
        "audit": {
            "has_been_audited": True,
            "audit_providers": ["Spearbit"],
            "last_audit_date": "2024-02-01",
            "has_bug_bounty": False,
            "bounty_platform": "none",
            "bounty_amount_usd": 0,
            "unaudited_new_code": True,
        },
        "team": {
            "team_type": "doxxed",
            "twitter": "@blast",
            "team_members": [{"name": "Pacman", "role": "founder", "twitter": "@PacmanBlur"}],
            "warm_intro_available": True,
            "warm_intro_path": "Previously audited by Spearbit — existing relationship",
        },
        "persona": {"name": "Pacman", "role": "Founder", "preferred_channel": "twitter_dm"},
        "category": "l2",
        "chains": ["Blast"],
        "override_tvl": 400_000_000,
    },
}


SEED_FUNDING = {
    "Hyperliquid": {"amount": 0, "date": None, "investors": ["self-funded"]},
    "Ethena": {"amount": 20_000_000, "date": "2025-06", "investors": ["Dragonfly", "Maelstrom"]},
    "EigenLayer": {"amount": 100_000_000, "date": "2025-03", "investors": ["a16z", "Blockchain Capital"]},
    "Pendle": {"amount": 15_000_000, "date": "2025-08", "investors": ["Binance Labs", "Spartan"]},
    "LayerZero": {"amount": 120_000_000, "date": "2024-04", "investors": ["a16z", "Sequoia", "Samsung Next"]},
    "Kamino Finance": {"amount": 6_000_000, "date": "2025-01", "investors": ["Placeholder", "Delphi"]},
    "Jupiter": {"amount": 0, "date": None, "investors": ["community"]},
    "Blast": {"amount": 20_000_000, "date": "2024-11", "investors": ["Paradigm"]},
}


def build_demo_profiles() -> list[EnrichedProfile]:
    """
    Build profiles from the RESEARCH_OVERLAYS dict for demo/offline runs.
    In live mode, DeFiLlama drives protocol discovery and these overlays
    are applied on top of what the pipeline finds.
    """
    from src.pipeline.enrich import classify_tvl

    profiles = []
    for name, data in RESEARCH_OVERLAYS.items():
        profile = EnrichedProfile(protocol_name=name)
        profile.tvl_usd = data.get("override_tvl", 0)
        profile.tvl_category = classify_tvl(profile.tvl_usd)

        profile = enrich_with_audit_data(profile, data["audit"])
        profile = enrich_with_team_data(profile, data["team"])

        profile.chains_deployed = data.get("chains", ["Ethereum"])

        funding = SEED_FUNDING.get(name, {})
        profile.total_raised_usd = funding.get("amount", 0)
        profile.last_funding_date = funding.get("date")
        profile.investors = funding.get("investors", [])

        # Velocity classification: very_high for most active protocols
        high_velocity = {"Hyperliquid", "Ethena", "EigenLayer", "Jupiter"}
        profile.shipping_velocity = "very_high" if name in high_velocity else "high"
        profile.ai_tool_signals = []  # populated by GitHub ingester in live mode

        profiles.append(profile)

    return profiles


def export_results(scored_leads, outreach_drafts, output_dir="data/output"):
    """Export pipeline results to CSV and JSON."""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Scored leads CSV ─────────────────────────────────────────────
    csv_path = os.path.join(output_dir, f"scored_leads_{timestamp}.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Protocol", "Composite Score", "Tier",
            "TVL Score", "Audit Score", "Velocity Score",
            "Funding Score", "Reachability Score", "Rationale"
        ])
        for lead in scored_leads:
            writer.writerow([
                lead.protocol_name, round(lead.composite_score, 1), lead.score_tier,
                round(lead.tvl_score, 1), round(lead.audit_status_score, 1),
                round(lead.velocity_score, 1), round(lead.funding_score, 1),
                round(lead.reachability_score, 1), lead.scoring_rationale
            ])
    print(f"  ✓ Scored leads → {csv_path}", flush=True)

    # ── Outreach drafts JSON ─────────────────────────────────────────
    json_path = os.path.join(output_dir, f"outreach_drafts_{timestamp}.json")
    drafts_data = []
    for d in outreach_drafts:
        drafts_data.append({
            "protocol": d.protocol_name,
            "persona": d.persona_name,
            "role": d.persona_role,
            "channel": d.channel,
            "step": d.sequence_step,
            "subject": d.subject_line,
            "body": d.message_body,
            "signals_used": d.signals_used,
            "model": d.llm_model,
            "contact_email": d.contact_email,
            "contact_twitter": d.contact_twitter,
            "contact_github": d.contact_github,
            "contact_source": d.contact_source,
        })
    with open(json_path, "w") as f:
        json.dump(drafts_data, f, indent=2)
    print(f"  ✓ Outreach drafts → {json_path}", flush=True)

    # ── Summary JSON ─────────────────────────────────────────────────
    summary = {
        "run_timestamp": timestamp,
        "total_scored": len(scored_leads),
        "hot_leads": len([s for s in scored_leads if s.score_tier == "hot"]),
        "warm_leads": len([s for s in scored_leads if s.score_tier == "warm"]),
        "outreach_generated": len(outreach_drafts),
        "top_5": [
            {"name": s.protocol_name, "score": s.composite_score, "tier": s.score_tier}
            for s in scored_leads[:5]
        ]
    }
    summary_path = os.path.join(output_dir, f"pipeline_summary_{timestamp}.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"  ✓ Summary → {summary_path}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Discovery Pipeline")
    parser.add_argument("--no-llm", action="store_true", help="Skip Claude API, use template fallbacks")
    parser.add_argument("--seed-only", action="store_true", help="Use seed data instead of live API calls")
    parser.add_argument("--test-email", default="",
                        help="Send all outreach to this address (overrides RESEND_TEST_EMAIL). "
                             "If neither is set, nothing is sent.")
    parser.add_argument("--max-emails", type=int, default=None,
                        help="Hard cap on emails delivered this run (default: config max_emails_per_run)")
    args = parser.parse_args()

    log_file = setup_logging()

    run_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    mode = "seed-only" if args.seed_only else "live"
    llm_mode = "off (templates)" if args.no_llm else "on (Claude)"

    logger.info(f"Pipeline started — mode={mode}, llm={llm_mode}, log={log_file}")

    print("\n" + "=" * 60, flush=True)
    print("\n  DISCOVERY PIPELINE", flush=True)
    print("  Hypothesis A: Your audit process wasn't built for AI-generated smart contracts", flush=True)
    print(f"  Run: {run_ts}", flush=True)
    print(f"  Mode: {'Demo (research overlays)' if args.seed_only else 'Live APIs'} | LLM: {'Off (templates)' if args.no_llm else 'On (Claude)'}", flush=True)
    print("\n" + "=" * 60, flush=True)

    # ── STEP 1: INGEST ───────────────────────────────────────────────
    logger.info("Step 1: INGEST")
    if args.seed_only:
        print("\nSkipping live ingest -- using research overlays\n", flush=True)
        all_signals = []
        logger.info("Ingest skipped — using RESEARCH_OVERLAYS (seed-only mode)")
    else:
        all_signals = run_full_ingest()
        logger.info(f"Ingest complete: {len(all_signals)} signals collected")

    # ── STEP 2: ENRICH ───────────────────────────────────────────────
    logger.info("Step 2: ENRICH")
    if args.seed_only:
        profiles = build_demo_profiles()
        print(f"\n✓ Built {len(profiles)} profiles from seed data", flush=True)
        logger.info(f"Built {len(profiles)} demo profiles from RESEARCH_OVERLAYS")
    else:
        signal_map = defaultdict(list)
        for s in all_signals:
            signal_map[s.protocol_name].append(s)

        profiles = run_enrichment(signal_map)
        sys.stdout.flush()

        # Layer on seed audit/team data for our target protocols
        overlay_count = 0
        for profile in profiles:
            if profile.protocol_name in RESEARCH_OVERLAYS:
                seed = RESEARCH_OVERLAYS[profile.protocol_name]
                profile = enrich_with_audit_data(profile, seed["audit"])
                profile = enrich_with_team_data(profile, seed["team"])
                if seed.get("chains"):
                    profile.chains_deployed = seed["chains"]
                if seed.get("override_tvl"):
                    profile.tvl_usd = seed["override_tvl"]
                    from src.pipeline.enrich import classify_tvl
                    profile.tvl_category = classify_tvl(profile.tvl_usd)
                overlay_count += 1
        logger.info(f"Enriched {len(profiles)} profiles; applied overlays to {overlay_count}")

    # ── STEP 3: SCORE ────────────────────────────────────────────────
    logger.info("Step 3: SCORE")
    scored = run_scoring(profiles)
    sys.stdout.flush()
    hot_count = len([s for s in scored if s.score_tier == "hot"])
    warm_count = len([s for s in scored if s.score_tier == "warm"])
    logger.info(f"Scoring complete: {len(scored)} protocols — {hot_count} hot, {warm_count} warm")
    for lead in scored:
        logger.debug(
            f"Scored {lead.protocol_name}: composite={lead.composite_score:.1f} ({lead.score_tier}) "
            f"TVL={lead.tvl_score} audit={lead.audit_status_score} vel={lead.velocity_score} "
            f"fund={lead.funding_score} reach={lead.reachability_score}"
        )

    # ── STEP 4: QUALIFY ──────────────────────────────────────────────
    qualified = sorted(
        [s for s in scored if s.score_tier in ("hot", "warm")],
        key=lambda x: x.composite_score, reverse=True
    )[:MAX_QUALIFIED_LEADS]
    print(f"\n✓ Qualified top {len(qualified)} leads (score >= 75)", flush=True)
    logger.info(f"Qualified {len(qualified)} leads: {[q.protocol_name for q in qualified]}")

    # ── STEP 4b: CONTACT ENRICHMENT ──────────────────────────────────
    logger.info("Step 4b: CONTACT ENRICHMENT (GitHub + Claude web search)")
    qualified_names = {q.protocol_name for q in qualified}
    contacts_map = find_contacts_for_qualified_leads(profiles, qualified_names)
    logger.info("Contact enrichment complete: %d protocols, %d total contacts",
                len(contacts_map), sum(len(v) for v in contacts_map.values()))
    sys.stdout.flush()

    # ── STEP 5: OUTREACH ─────────────────────────────────────────────
    logger.info("Step 5: OUTREACH")
    enrichment_map = {}
    persona_map = {}
    for p in profiles:
        enrichment_map[p.protocol_name] = {
            "tvl_usd": p.tvl_usd,
            "tvl_category": p.tvl_category,
            "category": RESEARCH_OVERLAYS.get(p.protocol_name, {}).get("category", "protocol"),
            "chains_deployed": p.chains_deployed or ["Ethereum"],
            "has_been_audited": p.has_been_audited,
            "audit_providers": p.audit_providers or [],
            "last_audit_date": p.last_audit_date,
            "bounty_platform": p.bounty_platform,
            "bounty_amount_usd": p.bounty_amount_usd,
            "shipping_velocity": p.shipping_velocity,
            "ai_tool_signals": p.ai_tool_signals or [],
            "unaudited_new_code": p.unaudited_new_code,
            "total_raised_usd": p.total_raised_usd,
            "last_funding_date": p.last_funding_date,
            "warm_intro_available": p.warm_intro_available,
            "warm_intro_path": p.warm_intro_path,
            "contacts": [
                {
                    "name": c.name, "role": c.role, "email": c.email,
                    "twitter": c.twitter_handle, "github": c.github_username,
                    "source": c.source, "confidence": c.confidence,
                }
                for c in contacts_map.get(p.protocol_name, [])
            ],
        }
        if p.protocol_name in RESEARCH_OVERLAYS:
            persona_map[p.protocol_name] = RESEARCH_OVERLAYS[p.protocol_name]["persona"]

    # For outreach, use only the top 1 contact per protocol to save tokens
    outreach_contacts_map = {proto: contacts[:1] for proto, contacts in contacts_map.items()}
    outreach = run_outreach_generation(
        qualified, enrichment_map, persona_map,
        contacts_map=outreach_contacts_map, use_llm=not args.no_llm
    )
    sys.stdout.flush()
    logger.info(f"Outreach generated: {len(outreach)} drafts")
    for draft in outreach:
        logger.debug(f"Draft [{draft.llm_model}] {draft.protocol_name}: \"{draft.subject_line}\"")

    # ── STEP 6: SEND EMAILS ──────────────────────────────────────────
    logger.info("Step 6: EMAIL SEND")
    send_results = send_outreach_emails(
        outreach, test_email=args.test_email, max_emails=args.max_emails
    )
    sys.stdout.flush()

    # ── STEP 7: PERSIST TO data/state.json ───────────────────────────
    logger.info("Step 7: PERSIST (data/state.json)")
    save_leads(scored, enrichment_map)
    save_contacts(contacts_map)
    save_outreach(send_results)
    save_drafts(outreach)
    sys.stdout.flush()

    # ── STEP 8: MARKET EVENT MONITOR ─────────────────────────────────
    logger.info("Step 8: MONITOR")
    pipeline_protocol_names = [p.protocol_name for p in profiles]
    events = run_event_monitor(pipeline_protocol_names)
    logger.info(f"Monitor: {len(events)} events detected")
    sys.stdout.flush()

    # ── STEP 9: EXPORT ───────────────────────────────────────────────
    logger.info("Step 9: EXPORT")
    print("\n" + "=" * 60, flush=True)
    print("EXPORT — Saving results", flush=True)
    print("=" * 60 + "\n", flush=True)
    export_results(scored, outreach)
    sys.stdout.flush()

    # ── FINAL SUMMARY ────────────────────────────────────────────────
    top_leads = [(s.protocol_name, s.composite_score, s.score_tier) for s in scored[:MAX_QUALIFIED_LEADS]]
    logger.info(f"Pipeline complete — scored={len(scored)}, hot={hot_count}, warm={warm_count}, outreach={len(outreach)}, top={top_leads}")

    print("\n" + "=" * 60, flush=True)
    print("PIPELINE COMPLETE", flush=True)
    print("=" * 60, flush=True)
    print(f"\n  Scored: {len(scored)} protocols", flush=True)
    print(f"  Hot leads: {hot_count}", flush=True)
    print(f"  Warm leads: {warm_count}", flush=True)
    print(f"  Outreach drafted: {len(outreach)}", flush=True)
    print(f"\n  Top {MAX_QUALIFIED_LEADS} targets:", flush=True)
    for s in scored[:MAX_QUALIFIED_LEADS]:
        print(f"    -> {s.protocol_name}: {s.composite_score:.0f} ({s.score_tier})", flush=True)
    print(flush=True)


if __name__ == "__main__":
    main()
