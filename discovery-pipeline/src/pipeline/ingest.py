"""
INGEST — Signal scraping from Web3 data sources.

Data sources:
  - DeFiLlama API: TVL, protocol metadata, chain deployments, GitHub orgs
  - GitHub API: Solidity/Rust repo activity, AI tool config files
  - DeFiLlama Hacks API: Real exploit signals matched to pipeline protocols

All thresholds and category filters come from config/scoring_weights.json.
GitHub orgs are extracted dynamically from DeFiLlama protocol data — no hardcoded lists.
"""

import os
import json
import logging
import requests
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass

from src.utils.config import load_config
from src.utils.github import github_headers, GITHUB_API

logger = logging.getLogger(__name__)


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class RawSignal:
    """Standardized signal format — all ingesters output this."""
    protocol_name: str
    signal_type: str        # 'tvl_data', 'github_activity', 'funding', 'exploit'
    source: str             # 'defillama', 'github', 'etherscan', 'crunchbase'
    source_url: str
    raw_content: str
    extracted_data: dict
    relevance_score: float  # 0-1


# ── DeFiLlama Ingester (real API — no key needed) ────────────────────────────

DEFILLAMA_BASE = os.getenv("DEFILLAMA_BASE_URL", "https://api.llama.fi")


def ingest_defillama_protocols(config: dict = None) -> list[RawSignal]:
    """
    Fetch protocols from DeFiLlama filtered by TVL and category.

    All thresholds come from config/scoring_weights.json:discovery.
    GitHub orgs are extracted from the DeFiLlama response itself.
    No hardcoded protocol lists.
    """
    discovery = (config or {}).get("discovery", {})
    min_tvl = discovery.get("min_tvl_usd", 50_000_000)
    target_categories = discovery.get("target_categories", [])
    exclude_categories = discovery.get("exclude_categories", [])
    max_protocols = discovery.get("max_protocols_per_run", 50)

    logger.info(f"DeFiLlama request: min_tvl=${min_tvl/1e6:.0f}M, categories={target_categories or 'all'}, max={max_protocols}")
    signals = []
    try:
        resp = requests.get(f"{DEFILLAMA_BASE}/protocols", timeout=30)
        resp.raise_for_status()
        protocols = resp.json()
        logger.debug(f"DeFiLlama returned {len(protocols)} total protocols before filtering")

        for p in protocols:
            tvl = p.get("tvl", 0) or 0
            category = p.get("category", "") or ""

            if tvl < min_tvl:
                continue
            if target_categories and category not in target_categories:
                continue
            if category in exclude_categories:
                continue

            signal = RawSignal(
                protocol_name=p.get("name", "Unknown"),
                signal_type="tvl_data",
                source="defillama",
                source_url=f"https://defillama.com/protocol/{p.get('slug', '')}",
                raw_content=json.dumps({
                    "name": p.get("name"),
                    "tvl": tvl,
                    "category": category,
                    "chains": p.get("chains", []),
                    "change_1m": p.get("change_1m"),
                }),
                extracted_data={
                    "tvl_usd": tvl,
                    "category": category,
                    "chains": p.get("chains", []),
                    "tvl_change_30d": p.get("change_1m"),
                    "slug": p.get("slug"),
                    "symbol": p.get("symbol"),
                    "url": p.get("url"),
                    "github_orgs": p.get("github", []),   # dynamic — from DeFiLlama
                    "twitter": p.get("twitter", ""),
                },
                relevance_score=_score_tvl_relevance(tvl)
            )
            signals.append(signal)
            logger.debug(f"Accepted: {signal.protocol_name} (TVL=${tvl:,.0f}, category={category}, github_orgs={p.get('github', [])})")

            if len(signals) >= max_protocols:
                break

        logger.info(f"DeFiLlama: {len(signals)} protocols accepted after filtering")
        print(f"✓ DeFiLlama: {len(signals)} protocols (min_tvl=${min_tvl/1e6:.0f}M, categories={target_categories or 'all'})", flush=True)

    except requests.RequestException as e:
        logger.error(f"DeFiLlama API error: {e}")
        print(f"✗ DeFiLlama API error: {e}", flush=True)

    return signals


def _score_tvl_relevance(tvl: float) -> float:
    if tvl > 1_000_000_000:
        return 1.0
    elif tvl > 100_000_000:
        return 0.85
    elif tvl > 10_000_000:
        return 0.65
    elif tvl > 1_000_000:
        return 0.4
    return 0.2


# ── GitHub Ingester (real API — public, rate limited) ────────────────────────


def ingest_github_activity(org_name: str) -> Optional[RawSignal]:
    """
    Check a GitHub org for Solidity/Rust activity and AI tool signals.
    Looks for: commit frequency, .cursorrules, copilot config, languages.

    Real API call. Set GITHUB_TOKEN in .env for 5000 req/hr (vs 60 unauthenticated).
    """
    logger.debug(f"GitHub scan: {org_name}")
    headers = github_headers()

    try:
        resp = requests.get(
            f"{GITHUB_API}/orgs/{org_name}/repos",
            params={"sort": "pushed", "per_page": 10},
            headers=headers,
            timeout=15
        )
        if resp.status_code == 404:
            logger.debug(f"GitHub org not found, trying user endpoint: {org_name}")
            resp = requests.get(
                f"{GITHUB_API}/users/{org_name}/repos",
                params={"sort": "pushed", "per_page": 10},
                headers=headers,
                timeout=15
            )

        if resp.status_code == 403:
            logger.warning(f"GitHub rate limit hit for {org_name} (403). Set GITHUB_TOKEN in .env for 5000 req/hr")
            return None
        if resp.status_code != 200:
            logger.warning(f"GitHub {org_name}: unexpected status {resp.status_code}")
            return None

        repos = resp.json()
        if not repos:
            return None

        total_commits_30d = 0
        languages_found = set()
        has_solidity = False
        has_rust = False
        ai_signals = []

        for repo in repos[:5]:
            repo_name = repo.get("full_name", "")
            lang = repo.get("language", "")

            if lang:
                languages_found.add(lang.lower())
            if lang and lang.lower() == "solidity":
                has_solidity = True
            if lang and lang.lower() == "rust":
                has_rust = True

            # Check for AI tool config files
            for branch in ("main", "master"):
                try:
                    tree_resp = requests.get(
                        f"{GITHUB_API}/repos/{repo_name}/git/trees/{branch}",
                        params={"recursive": "1"},
                        headers=headers,
                        timeout=10
                    )
                    if tree_resp.status_code == 200:
                        for item in tree_resp.json().get("tree", []):
                            path = item.get("path", "").lower()
                            if ".cursorrules" in path or ".cursor/" in path:
                                ai_signals.append(f"cursor_config:{repo_name}")
                            elif "copilot" in path and path.endswith((".json", ".yml", ".yaml")):
                                ai_signals.append(f"copilot_config:{repo_name}")
                            elif path.endswith(".windsurfrules"):
                                ai_signals.append(f"windsurf_config:{repo_name}")
                        break
                except requests.RequestException:
                    pass

            since = (datetime.now() - timedelta(days=30)).isoformat() + "Z"
            try:
                commits_resp = requests.get(
                    f"{GITHUB_API}/repos/{repo_name}/commits",
                    params={"since": since, "per_page": 100},
                    headers=headers,
                    timeout=10
                )
                if commits_resp.status_code == 200:
                    total_commits_30d += len(commits_resp.json())
            except requests.RequestException:
                pass

        signal = RawSignal(
            protocol_name=org_name,
            signal_type="github_activity",
            source="github",
            source_url=f"https://github.com/{org_name}",
            raw_content=json.dumps({
                "repos_count": len(repos),
                "languages": list(languages_found),
                "has_solidity": has_solidity,
                "has_rust": has_rust,
                "ai_signals": ai_signals,
                "commits_30d": total_commits_30d,
            }),
            extracted_data={
                "repos_count": len(repos),
                "languages": list(languages_found),
                "has_solidity": has_solidity,
                "has_rust": has_rust,
                "ai_tool_signals": ai_signals,
                "commits_30d": total_commits_30d,
                "contributors": repos[0].get("watchers_count", 0) if repos else 0,
            },
            relevance_score=0.8 if (has_solidity or has_rust) else 0.3
        )

        logger.info(f"GitHub {org_name}: {len(repos)} repos, solidity={has_solidity}, rust={has_rust}, ai_signals={len(ai_signals)}, commits_30d={total_commits_30d}")
        if ai_signals:
            logger.info(f"GitHub {org_name}: AI tool signals detected — {ai_signals}")
        print(f"  ✓ GitHub ({org_name}): {len(repos)} repos, solidity={has_solidity}, ai_signals={len(ai_signals)}, commits_30d={total_commits_30d}", flush=True)
        return signal

    except requests.RequestException as e:
        logger.error(f"GitHub {org_name}: request failed — {e}")
        print(f"  ✗ GitHub ({org_name}): {e}", flush=True)
        return None


# ── Real exploit ingest via DeFiLlama hacks API ───────────────────────────────

def ingest_funding_rounds(known_protocols: set[str], days_back: int = 365) -> list[RawSignal]:
    """
    Fetches real funding round data from DeFiLlama raises endpoint (free, no key).
    Only creates signals for protocols already discovered by DeFiLlama TVL ingest.
    """
    signals = []
    try:
        resp = requests.get("https://api.llama.fi/raises", timeout=15)
        if resp.status_code != 200:
            logger.warning("DeFiLlama raises API returned %s", resp.status_code)
            print(f"  ! DeFiLlama raises API: status {resp.status_code}", flush=True)
            return signals

        raises = resp.json().get("raises", [])
        cutoff = datetime.now() - timedelta(days=days_back)
        logger.debug("DeFiLlama raises: %d total records", len(raises))

        for r in raises:
            raise_date = datetime.fromtimestamp(r.get("date", 0) or 0)
            if raise_date < cutoff:
                continue

            name   = r.get("name") or ""
            amount = (r.get("amount") or 0) * 1_000_000  # API returns amount in $M

            # Only match protocols already in our pipeline
            matched = next(
                (p for p in known_protocols
                 if p.lower() == name.lower()
                 or name.lower() in p.lower()
                 or p.lower() in name.lower()),
                None
            )
            if not matched:
                continue

            lead_investors   = r.get("leadInvestors") or []
            other_investors  = r.get("otherInvestors") or []
            all_investors    = lead_investors + other_investors

            signals.append(RawSignal(
                protocol_name=matched,
                signal_type="funding",
                source="defillama_raises",
                source_url=r.get("source") or "",
                raw_content=json.dumps(r),
                extracted_data={
                    "amount_usd": amount,
                    "round":      r.get("round") or "unknown",
                    "date":       raise_date.strftime("%Y-%m"),
                    "investors":  all_investors[:10],
                },
                relevance_score=0.9 if amount >= 10_000_000 else 0.6,
            ))
            logger.info("Funding signal: %s raised $%s (%s) on %s",
                        matched, f"{amount:,.0f}", r.get("round"), raise_date.strftime("%Y-%m"))

        logger.info("DeFiLlama raises: %d funding signals matched pipeline protocols", len(signals))
        print(f"✓ Funding data: {len(signals)} rounds matched from DeFiLlama raises API (last {days_back} days)", flush=True)

    except requests.RequestException as e:
        logger.error("DeFiLlama raises API error: %s", e)
        print(f"  ! DeFiLlama raises API error: {e}", flush=True)

    return signals


def ingest_recent_exploits(known_protocols: set[str], days_back: int = 90) -> list[RawSignal]:
    """
    Fetches real exploit data from the DeFiLlama hacks endpoint.
    Only creates signals for protocols already discovered by DeFiLlama TVL ingest
    so no phantom companies are injected into the pipeline.
    """
    signals = []
    try:
        resp = requests.get("https://api.llama.fi/hacks", timeout=15)
        if resp.status_code != 200:
            logger.warning("DeFiLlama hacks API returned %s", resp.status_code)
            print(f"  ! DeFiLlama hacks API: status {resp.status_code}", flush=True)
            return signals

        hacks = resp.json()
        cutoff = datetime.now() - timedelta(days=days_back)

        for hack in hacks:
            hack_date = datetime.fromtimestamp(hack.get("date", 0) or 0)
            if hack_date < cutoff:
                continue

            name   = hack.get("name") or ""
            amount = hack.get("amount") or 0

            # Only include if this protocol is already in our DeFiLlama pipeline
            matched = next(
                (p for p in known_protocols if p.lower() == name.lower() or name.lower() in p.lower()),
                None
            )
            if not matched:
                continue

            signals.append(RawSignal(
                protocol_name=matched,
                signal_type="exploit",
                source="defillama_hacks",
                source_url=hack.get("link") or "",
                raw_content=json.dumps(hack),
                extracted_data={
                    "amount_lost_usd": amount,
                    "exploit_type":    hack.get("technique") or hack.get("classification") or "unknown",
                    "date":            hack_date.strftime("%Y-%m"),
                    "chain":           hack.get("chain") or "unknown",
                },
                relevance_score=1.0,
            ))
            logger.info("Exploit signal: %s lost $%s on %s", matched, f"{amount:,.0f}", hack_date.date())

        logger.info("DeFiLlama hacks: %d exploit signals matched pipeline protocols", len(signals))
        print(f"✓ Exploit data: {len(signals)} exploits matched from DeFiLlama hacks API (last {days_back} days)", flush=True)

    except requests.RequestException as e:
        logger.error("DeFiLlama hacks API error: %s", e)
        print(f"  ! DeFiLlama hacks API error: {e}", flush=True)

    return signals


# ── Main orchestrator ─────────────────────────────────────────────────────────

def run_full_ingest(config_path: str = "config/scoring_weights.json") -> list[RawSignal]:
    """
    Run all ingesters and return combined signal list.

    GitHub orgs come from DeFiLlama data — no hardcoded lists.
    All thresholds come from config_path.
    """
    print("\n" + "=" * 60, flush=True)
    print("INGEST STAGE — Scraping Web3 data sources", flush=True)
    print("=" * 60 + "\n", flush=True)

    config = load_config(config_path)
    all_signals = []

    # 1. DeFiLlama — real API, config-filtered
    tvl_signals = ingest_defillama_protocols(config)
    all_signals.extend(tvl_signals)

    # 2. GitHub — orgs extracted dynamically from DeFiLlama results
    print("\nGitHub — scanning orgs from DeFiLlama data:", flush=True)
    max_orgs = config.get("discovery", {}).get("github_orgs_per_protocol", 3)
    seen_orgs: set[str] = set()

    for sig in tvl_signals:
        for org in sig.extracted_data.get("github_orgs", [])[:max_orgs]:
            if org and org not in seen_orgs:
                seen_orgs.add(org)
                gh_signal = ingest_github_activity(org)
                if gh_signal:
                    gh_signal.protocol_name = sig.protocol_name
                    all_signals.append(gh_signal)

    known_protocols = {sig.protocol_name for sig in tvl_signals}

    # 3. Funding rounds — real DeFiLlama raises API, matched to pipeline protocols only
    all_signals.extend(ingest_funding_rounds(known_protocols))

    # 4. Recent exploits — real DeFiLlama hacks API, matched to pipeline protocols only
    all_signals.extend(ingest_recent_exploits(known_protocols))

    print(f"\n✓ Total signals ingested: {len(all_signals)}", flush=True)
    return all_signals


if __name__ == "__main__":
    signals = run_full_ingest()
    print(f"\nSample: {signals[0]}" if signals else "No signals")
