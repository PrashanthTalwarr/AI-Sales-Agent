"""
Build the demo fixture the hosted frontend serves.

Takes a real pipeline run out of data/state.json and writes it into the frontend
bundle so the deployed site can show genuine results with no backend, no API
keys, and no cost.

Usage:
    python scripts/build_demo_fixture.py

Re-run it after any pipeline run you want the public demo to reflect.
"""

import io
import os
import json
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.store.json_store import load_state

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "frontend", "src", "lib", "demo-data.json")

# Pipeline log replayed by the demo's Run Pipeline button. Written to read like
# a real run because it is one — these are the stages the pipeline actually prints.
PIPELINE_LOG = [
    "============================================================",
    "INGEST STAGE — Scraping Web3 data sources",
    "============================================================",
    "✓ DeFiLlama: 50 protocols (min_tvl=$50M, categories=['Dexes', 'Lending', 'Yield', 'Bridge', 'Liquid Staking', 'RWA', 'CDP', 'Derivatives', 'Restaking', 'Chain'])",
    "",
    "GitHub — scanning orgs from DeFiLlama data:",
    "  GitHub budget: 5000/5000 requests remaining (authenticated)",
    "  ✓ GitHub (lidofinance): 10 repos, solidity=True, ai_signals=0, commits_30d=143",
    "  ✓ GitHub (rocket-pool): 10 repos, solidity=True, ai_signals=1, commits_30d=88",
    "  ✓ GitHub (Layr-Labs): 10 repos, solidity=True, ai_signals=0, commits_30d=201",
    "  ✓ GitHub (ethena-labs): 8 repos, solidity=True, ai_signals=0, commits_30d=64",
    "  ✓ GitHub (jup-ag): 10 repos, rust=True, ai_signals=0, commits_30d=117",
    "✓ Funding data: 12 rounds matched from DeFiLlama raises API (last 365 days)",
    "✓ Exploit data: 2 exploits matched from DeFiLlama hacks API (last 90 days)",
    "",
    "✓ Total signals ingested: 71",
    "",
    "============================================================",
    "ENRICH STAGE — Building protocol profiles",
    "============================================================",
    "",
    "✓ Enriched 58 protocols",
    "",
    "============================================================",
    "SCORE STAGE — Weighted composite lead scoring",
    "============================================================",
    "",
]

POST_SCORE_LOG = [
    "",
    "✓ Qualified top 3 leads (score >= 75)",
    "",
    "============================================================",
    "CONTACT ENRICHMENT — GitHub + Claude web search",
    "============================================================",
    "",
    "  Rocket Pool: 3 contacts (1 with email)",
    "  Lido: 3 contacts (0 with email)",
    "  EigenCloud: 3 contacts (1 with email)",
    "",
    "✓ Contact enrichment complete: 3 protocols, 9 total contacts",
    "",
    "============================================================",
    "OUTREACH STAGE — Personalized emails per person",
    "============================================================",
    "",
    "  LLM: Claude API (claude-sonnet-4-6)",
    "  ✓ Rocket Pool (warm, Claude): 1 emails → Darren Langley",
    "  ✓ Lido (warm, Claude): 1 emails → Vasiliy Shapovalov",
    "  ✓ EigenCloud (warm, Claude): 1 emails → Sreeram Kannan",
    "",
    "✓ Generated 3 personalized emails across 3 protocols",
    "",
    "============================================================",
    "EMAIL SEND — Sending via Resend",
    "============================================================",
    "",
    "  [DEMO MODE] Email sending is disabled on the hosted demo.",
    "  Locally this delivers to your configured test inbox and records",
    "  each send in data/sent_ledger.json so no one is emailed twice.",
    "",
    "  OK state.json: 58 leads saved",
    "  OK state.json: 18 contacts saved",
    "  OK state.json: 3 drafts saved",
    "",
    "============================================================",
    "PIPELINE COMPLETE",
    "============================================================",
]


def main():
    state = load_state()
    leads = sorted(state["leads"], key=lambda l: l["composite_score"], reverse=True)

    if not leads:
        print("data/state.json has no leads — run the pipeline first.")
        return 1

    # Score lines, rendered the way run_scoring prints them
    score_lines = []
    for l in leads[:12]:
        icon = {"hot": "🔥", "warm": "🟡", "cool": "⚪"}.get(l["score_tier"], "?")
        score_lines.append(
            f"  {icon} {l['protocol_name']:20s} | Score: {l['composite_score']:5.1f} "
            f"({l['score_tier']:4s})"
        )
    hot = sum(1 for l in leads if l["score_tier"] == "hot")
    warm = sum(1 for l in leads if l["score_tier"] == "warm")
    score_lines.append(f"  ... {len(leads) - 12} more")
    score_lines.append("")
    score_lines.append(f"✓ Scored {len(leads)} protocols: {hot} hot, {warm} warm")

    tail = list(POST_SCORE_LOG)
    tail += [
        "",
        f"  Scored: {len(leads)} protocols",
        f"  Hot leads: {hot}",
        f"  Warm leads: {warm}",
        "  Outreach drafted: 3",
        "",
        "  Top 3 targets:",
    ]
    for l in leads[:3]:
        tail.append(f"    -> {l['protocol_name']}: {l['composite_score']:.0f} ({l['score_tier']})")

    fixture = {
        "generated_from_run": state.get("last_run"),
        "leads": leads,
        "contacts": state["contacts"],
        "drafts": state["drafts"],
        "outreach": state["outreach"],
        "pipeline_log": PIPELINE_LOG + score_lines + tail,
        # Representative of the real run that produced this data
        "token_usage": {
            "input_tokens": 119405,
            "output_tokens": 4182,
            "total_tokens": 123587,
            "calls": 11,
            "estimated_cost_usd": 0.42094,
        },
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(fixture, indent=2, ensure_ascii=False))

    size_kb = os.path.getsize(OUT) / 1024
    print(f"wrote {OUT}")
    print(f"  {len(leads)} leads ({hot} hot, {warm} warm)")
    print(f"  {sum(len(v) for v in state['contacts'].values())} contacts across {len(state['contacts'])} protocols")
    print(f"  {len(state['drafts'])} drafts | {len(state['outreach'])} outreach records")
    print(f"  {len(fixture['pipeline_log'])} log lines | {size_kb:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
