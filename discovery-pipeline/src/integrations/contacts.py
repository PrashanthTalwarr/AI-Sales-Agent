"""
CONTACTS — Find the actual people to reach at each protocol.

Two sources:
  SOURCE 1: GitHub API (free, uses existing PAT)
    - Pulls contributors from the protocol's GitHub org
    - Fetches each contributor's public profile (name, email, bio, company)
    - Filters to those active in the last 30 days
    - Flags solidity/rust contributors

  SOURCE 2: Claude web search
    - Uses the Anthropic web_search tool to find the founding team, CTO,
      and head of security for each protocol from live web sources
    - Returns structured JSON with name, role, twitter, source URL, confidence

Results are merged and deduplicated by name. Up to 3 contacts per protocol.
"""

import os
import json
import logging
import time
import requests
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from src.utils.github import github_get, github_headers, GITHUB_API
from src.utils.claude_client import get_anthropic_client, get_anthropic_model
from src.utils.json_utils import extract_json
from src.utils.config import load_config
from src.utils import token_tracker

_MAX_CONTACTS = load_config().get("discovery", {}).get("max_contacts_per_protocol", 3)

logger = logging.getLogger(__name__)


# ── Contact dataclass ─────────────────────────────────────────────────────────

@dataclass
class Contact:
    name: str
    role: str
    email: str = ""
    twitter_handle: str = ""
    github_username: str = ""
    linkedin_url: str = ""
    source: str = ""               # 'github' | 'web_search' | 'overlay'
    confidence: str = "medium"    # 'high' | 'medium' | 'low'
    source_url: str = ""
    contributor_type: str = ""    # 'solidity_dev' | 'rust_dev' | 'founder' | 'cto' | 'head_of_security' etc.


# ── GitHub helpers ─────────────────────────────────────────────────────────────

def _active_in_last_30d(org: str, repo: str, username: str) -> bool:
    """Return True if this user has commits in the last 30 days."""
    since = (datetime.now() - timedelta(days=30)).isoformat() + "Z"
    data = github_get(
        f"{GITHUB_API}/repos/{org}/{repo}/commits",
        params={"author": username, "since": since, "per_page": 1}
    )
    return bool(data)


def _has_smart_contract_commits(org: str, repo: str, username: str) -> str:
    """
    Check if this contributor's recent commits touch .sol or .rs files.
    Returns 'solidity_dev', 'rust_dev', or ''.
    """
    data = github_get(
        f"{GITHUB_API}/repos/{org}/{repo}/commits",
        params={"author": username, "per_page": 5}
    )
    if not data:
        return ""

    for commit in data:
        sha = commit.get("sha", "")
        if not sha:
            continue
        detail = github_get(f"{GITHUB_API}/repos/{org}/{repo}/commits/{sha}")
        if not detail:
            continue
        files = detail.get("files", [])
        for f in files:
            fname = f.get("filename", "").lower()
            if fname.endswith(".sol"):
                return "solidity_dev"
            if fname.endswith(".rs"):
                return "rust_dev"
    return ""


def find_github_contributors(
    github_orgs: list[str],
    protocol_name: str,
    max_contacts: int = 3,
) -> list[Contact]:
    """
    For each GitHub org, find active contributors and enrich with profile data.
    Returns Contact objects for people active in the last 30 days.
    """
    if not github_orgs:
        logger.debug("No GitHub orgs for %s", protocol_name)
        return []

    contacts: list[Contact] = []
    seen_usernames: set[str] = set()

    for org in github_orgs[:2]:  # check up to 2 orgs
        # Get the most recently pushed repo
        repos = github_get(
            f"{GITHUB_API}/orgs/{org}/repos",
            params={"sort": "pushed", "per_page": 5}
        )
        if repos is None:
            # try as user, not org
            repos = github_get(
                f"{GITHUB_API}/users/{org}/repos",
                params={"sort": "pushed", "per_page": 5}
            )
        if not repos:
            continue

        for repo in repos[:3]:
            repo_name = repo.get("name", "")
            if not repo_name:
                continue

            contributors = github_get(
                f"{GITHUB_API}/repos/{org}/{repo_name}/contributors",
                params={"per_page": 15}
            )
            if not contributors:
                continue

            for contributor in contributors:
                if len(contacts) >= max_contacts:
                    break

                username = contributor.get("login", "")
                if not username or username in seen_usernames:
                    continue
                # skip bots
                if contributor.get("type", "").lower() == "bot" or "[bot]" in username:
                    continue

                # Only keep people active in last 30 days
                if not _active_in_last_30d(org, repo_name, username):
                    continue

                seen_usernames.add(username)

                # Get full profile
                profile = github_get(f"{GITHUB_API}/users/{username}")
                if not profile:
                    continue

                name = profile.get("name") or profile.get("login", username)
                email = profile.get("email") or ""
                bio = profile.get("bio") or ""
                company = profile.get("company") or ""
                blog = profile.get("blog") or ""

                # Infer role from bio/company
                role = _infer_role_from_bio(bio, company, name)

                # Check for smart contract commit types
                ctype = _has_smart_contract_commits(org, repo_name, username)

                # Extract Twitter from bio
                twitter = ""
                for word in bio.split():
                    if word.startswith("@") and len(word) > 1:
                        twitter = word
                        break
                    if "twitter.com/" in word:
                        twitter = "@" + word.split("twitter.com/")[-1].rstrip("/")
                        break

                contact = Contact(
                    name=name,
                    role=role,
                    email=email,
                    twitter_handle=twitter,
                    github_username=username,
                    source="github",
                    confidence="high",  # verified active committer
                    source_url=f"https://github.com/{username}",
                    contributor_type=ctype or "engineering",
                )
                contacts.append(contact)
                logger.debug(
                    "GitHub contributor %s (%s): role=%s email=%s ctype=%s",
                    name, username, role, bool(email), ctype
                )

            if len(contacts) >= max_contacts:
                break

    logger.info("GitHub contributors for %s: %d found", protocol_name, len(contacts))
    return contacts


def _infer_role_from_bio(bio: str, company: str, name: str) -> str:
    """Infer role from GitHub bio/company text."""
    combined = (bio + " " + company).lower()
    if any(w in combined for w in ["founder", "co-founder", "cofounder"]):
        return "founder"
    if any(w in combined for w in ["cto", "chief technology"]):
        return "cto"
    if any(w in combined for w in ["ceo", "chief executive"]):
        return "ceo"
    if any(w in combined for w in ["head of security", "security lead", "chief security", "cso"]):
        return "head_of_security"
    if any(w in combined for w in ["security", "audit"]):
        return "security_engineer"
    if any(w in combined for w in ["solidity", "smart contract"]):
        return "smart_contract_dev"
    if any(w in combined for w in ["head of engineering", "vp engineering", "engineering lead"]):
        return "head_of_engineering"
    if any(w in combined for w in ["protocol", "core dev", "core developer"]):
        return "protocol_engineer"
    return "engineer"


# ── Claude web search ─────────────────────────────────────────────────────────

LEADERSHIP_SEARCH_PROMPT = """Search for the current founding team, CTO, and head of security of the Web3 protocol "{protocol_name}".

Return a JSON array of people found. Each object must have:
- name: full name
- role: one of [founder, co-founder, ceo, cto, cso, head_of_security, head_of_engineering, lead_dev]
- twitter_handle: their Twitter/X handle starting with @ (or empty string)
- source_url: the URL where you found this information
- confidence: high, medium, or low
- last_verified: the year of the source (e.g. "2025")

RULES:
- Only include people confirmed to be CURRENTLY at {protocol_name} in 2025 or 2026
- If you cannot verify someone is still at the company, omit them
- Do not make up names or handles
- Return ONLY the JSON array, no other text

Example output:
[{{"name": "Alice Chen", "role": "founder", "twitter_handle": "@alicechen", "source_url": "https://...", "confidence": "high", "last_verified": "2025"}}]"""


def find_leadership_via_claude(
    protocol_name: str,
    max_contacts: int = 3,
) -> list[Contact]:
    """
    Use Claude with web_search tool to find the leadership team of a protocol.
    Returns Contact objects for founders, CTOs, and security leads.
    """
    client = get_anthropic_client()
    if not client:
        logger.warning("Anthropic client unavailable — skipping web search for %s", protocol_name)
        return []

    prompt = LEADERSHIP_SEARCH_PROMPT.format(protocol_name=protocol_name)

    try:
        logger.debug("Claude web search: leadership for %s", protocol_name)
        response = client.messages.create(
            model=get_anthropic_model(),
            max_tokens=1024,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[
                {
                    "role": "user",
                    "content": f"You are a prospect research agent for a Web3 security platform. "
                               f"Your job is to find current leadership at Web3 protocols. "
                               f"Only return verified information from recent sources (2025-2026). "
                               f"If you cannot verify someone is still at the company, say so.\n\n{prompt}"
                }
            ],
        )

        token_tracker.record(response.usage.input_tokens, response.usage.output_tokens)

        # Extract the final text response (after tool use)
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        if not text.strip():
            logger.debug("Claude returned no text for %s", protocol_name)
            return []

        # Parse JSON from response
        json_str = extract_json(text)
        if not json_str:
            logger.debug("No JSON found in Claude response for %s: %s", protocol_name, text[:200])
            return []

        people = json.loads(json_str)
        if not isinstance(people, list):
            return []

        contacts = []
        for p in people[:max_contacts]:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            contacts.append(Contact(
                name=p.get("name", ""),
                role=p.get("role", "unknown"),
                email="",  # web search won't surface emails
                twitter_handle=p.get("twitter_handle", ""),
                source="web_search",
                confidence=p.get("confidence", "medium"),
                source_url=p.get("source_url", ""),
                contributor_type=p.get("role", ""),
            ))

        logger.info("Claude web search for %s: %d contacts found", protocol_name, len(contacts))
        return contacts

    except Exception as e:
        logger.warning("Claude web search failed for %s: %s", protocol_name, e)
        return []


# ── Main entry point ──────────────────────────────────────────────────────────

def find_contacts_for_protocol(
    protocol_name: str,
    github_orgs: list[str],
    delay: float = 0.3,
) -> list[Contact]:
    """
    Find up to max_contacts_per_protocol security-sale-relevant contacts for a protocol.

    SOURCE 1: GitHub active contributors (verified, with emails when public)
    SOURCE 2: Claude web search for leadership (founders, CTOs, security leads)

    Returns merged, deduplicated list of up to _MAX_CONTACTS contacts.
    """
    logger.info("Finding contacts for %s (github_orgs=%s)", protocol_name, github_orgs)

    # GitHub first — higher confidence, has emails
    github_contacts = find_github_contributors(github_orgs, protocol_name, max_contacts=_MAX_CONTACTS)

    if delay > 0:
        time.sleep(delay)

    # Claude web search for leadership layer
    leadership_contacts = find_leadership_via_claude(protocol_name, max_contacts=_MAX_CONTACTS)

    # Merge: add leadership contacts that aren't already in github list
    seen_names = {c.name.lower() for c in github_contacts}
    merged = list(github_contacts)

    for lc in leadership_contacts:
        if lc.name.lower() not in seen_names:
            merged.append(lc)
            seen_names.add(lc.name.lower())

    # Sort: leadership roles first, then by confidence
    role_priority = {
        "founder": 0, "co-founder": 0, "ceo": 1, "cto": 2,
        "head_of_security": 3, "cso": 3, "head_of_engineering": 4,
        "security_engineer": 5, "smart_contract_dev": 6,
        "protocol_engineer": 7, "engineer": 8, "unknown": 9,
    }
    merged.sort(key=lambda c: (role_priority.get(c.role, 9), c.confidence != "high"))

    result = merged[:_MAX_CONTACTS]
    email_count = sum(1 for c in result if c.email)
    logger.info(
        "Contacts for %s: %d total (%d with email) — github=%d web=%d",
        protocol_name, len(result), email_count, len(github_contacts), len(leadership_contacts)
    )
    return result


def find_contacts_for_qualified_leads(
    profiles: list,
    qualified_protocol_names: set[str],
    delay_between: float = 1.0,
) -> dict[str, list[Contact]]:
    """
    For each qualified protocol, find contacts.
    Only runs on qualified leads (score >= 75) to avoid burning API rate limits.

    Returns: {protocol_name: [Contact, ...]}
    """
    print("\n" + "=" * 60, flush=True)
    print("CONTACT ENRICHMENT — GitHub + Claude web search (in production, ideally would use Apollo.io)", flush=True)
    print("=" * 60 + "\n", flush=True)

    contacts_map: dict[str, list[Contact]] = {}

    for profile in profiles:
        if profile.protocol_name not in qualified_protocol_names:
            continue

        github_orgs = getattr(profile, "github_orgs", [])
        contacts = find_contacts_for_protocol(
            profile.protocol_name,
            github_orgs,
            delay=delay_between,
        )

        # Supplement with any existing team_members (e.g. from RESEARCH_OVERLAYS)
        existing_names = {c.name.lower() for c in contacts}
        for member in profile.team_members:
            m_name = member.get("name", "")
            if m_name and m_name.lower() not in existing_names:
                contacts.append(Contact(
                    name=m_name,
                    role=member.get("role", "unknown"),
                    twitter_handle=member.get("twitter", ""),
                    source="overlay",
                    confidence="high",
                ))
                existing_names.add(m_name.lower())

        contacts = contacts[:_MAX_CONTACTS]
        contacts_map[profile.protocol_name] = contacts

        email_count = sum(1 for c in contacts if c.email)
        print(
            f"  {profile.protocol_name}: {len(contacts)} contacts "
            f"({email_count} with email)",
            flush=True
        )

    print(
        f"\n✓ Contact enrichment complete: {len(contacts_map)} protocols, "
        f"{sum(len(v) for v in contacts_map.values())} total contacts\n",
        flush=True
    )
    return contacts_map
