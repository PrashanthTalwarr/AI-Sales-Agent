"""
OUTREACH AGENT — Uses Claude API to generate personalized outreach messages.

Takes a scored lead + enrichment data and produces:
  - Personalized cold outreach (email or Twitter DM)
  - Signal-specific messaging (references TVL, audit status, recent events)
  - 3-touch sequence: Signal → Data → Bridge → Ask

Claude client is lazy-initialized inside the call so dotenv loading in
run_pipeline.py takes effect before the API key is read.
"""

import os
import json
import logging
from dataclasses import dataclass
from typing import Optional

from src.utils.claude_client import get_anthropic_client, get_anthropic_model
from src.utils import token_tracker

logger = logging.getLogger(__name__)


@dataclass
class OutreachDraft:
    """Generated outreach message — one per person, not per company."""
    protocol_name: str
    persona_name: str
    persona_role: str
    channel: str            # 'email', 'twitter_dm', 'telegram'
    sequence_step: int      # 1, 2, or 3
    subject_line: str
    message_body: str
    signals_used: dict      # which data points informed the message
    llm_model: str
    contact_email: str = ""     # actual email address if found
    contact_twitter: str = ""   # twitter handle if found
    contact_github: str = ""    # github username if found
    contact_source: str = ""    # 'github' | 'web_search' | 'overlay'


# ── System prompt ─────────────────────────────────────────────────────────────

OUTREACH_SYSTEM_PROMPT = """You are writing a cold outreach email on behalf of a Web3 security platform.

WHAT WE ARE:
We are a security partner for serious Web3 protocols — not a traditional auditing firm.
- Security competitions: 100+ independent researchers review code simultaneously. More coverage, faster, than any 2-3 person audit team.
- Continuous AI analysis: an analyzer that runs between formal reviews, so coverage doesn't stop when the audit ends.
- Managed bug bounties: we own triage and quality — teams get signal, not noise.

Be direct about what we do when you introduce us. Don't bury the lede.

FRAMING — this is the most important thing to get right:
Serious protocols get reviewed before major upgrades. That's what mature teams do — it's how they ship confidently, not cautiously.
Frame what we offer as something that enables their next move, not a safety check before something risky.
A review isn't "make sure nothing goes wrong." It's "how the best teams move fast without slowing down."
NEVER create fear. NEVER imply they need this because something bad might happen. Smart founders see through it and it kills the conversation.

When a protocol hasn't had a formal review yet: don't expose that as a failure or make them feel behind. Many serious teams reach significant scale before their first comprehensive review — that's normal. Frame it as: we'd love to be their first, here's what that looks like. Warm and direct, not a gap to be ashamed of.

SUBJECT LINES — specific rule:
Never write a subject that highlights their gap like a verdict: "$1B TVL, never audited" reads as calling them out. The subject should open a conversation, not pass judgment. Lead with what you'd offer or what you noticed about where they're headed.

TONE:
- Peer-to-peer. Warm, direct, a little casual.
- One specific observation about their actual situation — show you looked.
- Position as: "here's how teams like yours think about this."
- Low-stakes close: "would you be open to a quick call?", "happy to walk you through it", "want to connect?"

NEVER SAY:
- Fear framing: "new attack surface", "historically been the source of major exploits", "we'd hate to see you affected", "before it's too late", "vulnerabilities introduced", "risk exposure"
- Safety-check framing: "make sure nothing goes wrong", "just to be safe", "protect your users"
- Filler praise: "serious momentum", "impressive growth", "great traction", "you've built something remarkable"
- Clichés: "I hope this finds you well", "touching base", "I wanted to reach out", "circling back"
- Buzzwords: "synergy", "ecosystem partner", "deep dive", "robust"
- "curious if that's on your radar"
- Subject lines that just label the situation: "[Protocol] at $XB TVL" tells them nothing. Lead with the observation or the angle.
- Any reference to 2025 as upcoming — it is 2026.

ROLE ADJUSTMENTS:
- founder/CEO → shipping faster with confidence, what serious protocols do at scale
- CTO/engineering lead → velocity vs. coverage gap, technical peer tone
- security person → skip the basics, talk shop, peer-to-peer
- smart contract dev → brief, technical, reference their specific work

FORMATTING:
- Start: Hey {first name},
- End: Best,
Discovery Pipeline Team
- Subject: max 8 words, specific angle — not a label
- Body: under 120 words, no bullets

OUTPUT FORMAT:
Subject: <subject line>

Hey {first name},

<body>

[Book a call]

Best,
Discovery Pipeline Team

Signals used: <comma-separated list>
"""



def build_outreach_prompt(
    protocol_name: str,
    enrichment: dict,
    score_data: dict,
    persona: dict,
    sequence_step: int = 1,
    contact=None,
) -> str:
    """Build the user prompt for Claude outreach generation."""
    step_instructions = {
        1: "First touch. Lead with a specific signal. Don't sell yet — demonstrate you understand their situation better than they expected.",
        2: "Follow-up. Reference a recent exploit or research data for their category. Create urgency with third-party data, not our features.",
        3: "Final touch. Short, direct. Offer something tangible (a free AI code analysis pass, category-specific exploit pattern report) to earn a response.",
    }

    audit_providers = enrichment.get("audit_providers", [])
    bounty_platform = enrichment.get("bounty_platform", "none")
    bounty_amount = enrichment.get("bounty_amount_usd", 0)
    tvl = enrichment.get("tvl_usd", 0)
    chains = ", ".join(enrichment.get("chains_deployed", ["unknown"]))

    ratio_note = ""
    if tvl > 0 and bounty_amount > 0:
        ratio_pct = (bounty_amount / tvl) * 100
        ratio_note = f"Bounty/TVL ratio: {ratio_pct:.3f}% (${bounty_amount:,.0f} bounty for ${tvl:,.0f} TVL)"

    # Role-specific angle
    role = (contact.role if contact else persona.get("role", "")).lower()
    role_note = ""
    if any(r in role for r in ["founder", "ceo"]):
        role_note = "This is a founder/CEO. Frame around business risk and protecting TVL. Security as a growth enabler."
    elif any(r in role for r in ["cto", "head_of_engineering"]):
        role_note = "This is a CTO/engineering lead. Frame around shipping velocity vs. security coverage gap. Technical credibility matters most."
    elif any(r in role for r in ["security", "cso", "audit"]):
        role_note = "This is a security person. They know the problem. Skip the basics — go straight to our specific advantage (continuous AI analysis, competition model). Peer tone."
    elif any(r in role for r in ["solidity_dev", "smart_contract", "protocol_engineer", "rust_dev"]):
        role_note = "This is a core developer. Acknowledge their work. Frame around the specific vuln classes in their language/category. Short and technical."

    name = (contact.name if contact else persona.get("name", "team")).split()[0]
    github_note = f"\nGITHUB: github.com/{contact.github_username} (active contributor, verified)" if contact and contact.github_username else ""
    twitter_note = f"\nTWITTER: {contact.twitter_handle}" if contact and contact.twitter_handle else ""

    prompt = f"""Generate a personalized outreach email for a specific person at a Web3 protocol.

PROTOCOL: {protocol_name}
CATEGORY: {enrichment.get('category', 'unknown')}
CHAIN(S): {chains}
TVL: ${tvl:,.0f}
AUDITED BY: {', '.join(audit_providers) if audit_providers else 'NEVER AUDITED'}
LAST AUDIT DATE: {enrichment.get('last_audit_date', 'unknown')}
BUG BOUNTY: {bounty_platform} / ${bounty_amount:,.0f}
{ratio_note}
SHIPPING VELOCITY: {enrichment.get('shipping_velocity', 'unknown')}
AI TOOL SIGNALS: {', '.join(enrichment.get('ai_tool_signals', [])) or 'none detected'}
UNAUDITED NEW CODE SINCE LAST REVIEW: {enrichment.get('unaudited_new_code', False)}
RECENT FUNDING: ${enrichment.get('total_raised_usd', 0):,.0f} ({enrichment.get('last_funding_date') or 'no recent raise'})
WARM INTRO AVAILABLE: {enrichment.get('warm_intro_available', False)}
{('WARM INTRO PATH: ' + enrichment.get('warm_intro_path', '')) if enrichment.get('warm_intro_available') else ''}

LEAD SCORE: {json.dumps(score_data)}

RECIPIENT:
Name: {name}
Role: {role or persona.get('role', 'team member')}{github_note}{twitter_note}
{f'ROLE-SPECIFIC ANGLE: {role_note}' if role_note else ''}

SEQUENCE STEP: {sequence_step}
INSTRUCTION: {step_instructions.get(sequence_step, step_instructions[1])}

INDUSTRY CONTEXT (use selectively, not all at once — these are past events):
- $3.1B lost in Web3 in 2025
- Balancer: $100M from a single rounding error (November 2025)
- Cetus DEX: $223M from a missing integer overflow check (May 2025)
- 45% of AI-generated code contains OWASP Top 10 vulnerabilities (Veracode research)
- Average time from vulnerability introduction to exploitation: 38 days
- Top audit firms: 2-3 month waitlists while protocols ship daily

CURRENT DATE: 2026. Do not reference planning for 2025 or frame 2025 as upcoming.
"""
    return prompt


def generate_outreach_with_claude(
    protocol_name: str,
    enrichment: dict,
    score_data: dict,
    persona: dict,
    sequence_step: int = 1,
    contact=None,
) -> Optional[OutreachDraft]:
    """Call Claude API to generate personalized outreach."""
    client = get_anthropic_client()
    if not client:
        logger.warning(f"Claude client unavailable for {protocol_name} — ANTHROPIC_API_KEY missing or import failed")
        return None

    prompt = build_outreach_prompt(protocol_name, enrichment, score_data, persona, sequence_step, contact)
    person_name = (contact.name if contact else persona.get("name", "?"))
    logger.info(f"Claude API call: {protocol_name} → {person_name} (step {sequence_step}, model={get_anthropic_model()})")

    try:
        response = client.messages.create(
            model=get_anthropic_model(),
            max_tokens=600,
            system=OUTREACH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )

        token_tracker.record(response.usage.input_tokens, response.usage.output_tokens)
        content = response.content[0].text.strip()
        lines = content.split("\n")

        subject = ""
        body_lines = []
        signals_list = []
        section = "preamble"

        for line in lines:
            stripped = line.strip()
            if not stripped:
                if section == "body":
                    body_lines.append("")
                continue
            if stripped.lower().startswith("subject:"):
                subject = stripped.split(":", 1)[-1].strip().strip('"')
                section = "body"
            elif stripped.lower().startswith("signals used:"):
                section = "signals"
                raw = stripped.split(":", 1)[-1].strip()
                if raw:
                    signals_list = [s.strip() for s in raw.split(",")]
            elif section == "body":
                body_lines.append(line)
            elif section == "signals":
                signals_list.extend([s.strip() for s in stripped.strip("- ").split(",")])

        body = "\n".join(body_lines).strip()
        if not subject:
            subject = lines[0] if lines else f"{protocol_name}: security gap detected"
        if not body:
            body = content

        person_name = contact.name if contact else persona.get("name", "")
        person_role = contact.role if contact else persona.get("role", "")
        channel = "email" if (contact and contact.email) else persona.get("preferred_channel", "email")

        draft = OutreachDraft(
            protocol_name=protocol_name,
            persona_name=person_name,
            persona_role=person_role,
            channel=channel,
            sequence_step=sequence_step,
            subject_line=subject,
            message_body=body,
            signals_used={
                "tvl": enrichment.get("tvl_usd"),
                "audit_providers": enrichment.get("audit_providers"),
                "bounty_platform": enrichment.get("bounty_platform"),
                "velocity": enrichment.get("shipping_velocity"),
                "ai_signals": enrichment.get("ai_tool_signals"),
                "claude_referenced": signals_list,
            },
            llm_model=get_anthropic_model(),
            contact_email=contact.email if contact else "",
            contact_twitter=contact.twitter_handle if contact else persona.get("preferred_channel", ""),
            contact_github=contact.github_username if contact else "",
            contact_source=contact.source if contact else "",
        )
        logger.info(f"Claude outreach generated for {protocol_name} → {person_name}: \"{subject}\"")
        return draft

    except Exception as e:
        logger.error(f"Claude API error for {protocol_name}: {e}")
        print(f"  ✗ Claude API error for {protocol_name}: {e}", flush=True)
        return None


def generate_outreach_fallback(
    protocol_name: str,
    enrichment: dict,
    score_data: dict,
    persona: dict,
    sequence_step: int = 1,
    contact=None,
) -> OutreachDraft:
    """
    Template fallback when Claude API is unavailable.
    Produces differentiated messages using enrichment signals — no two protocols
    get the same subject line or body structure.
    """
    tvl = enrichment.get("tvl_usd", 0)
    category = enrichment.get("category", "protocol")
    chains = enrichment.get("chains_deployed", ["Ethereum"])
    chain_str = chains[0] if len(chains) == 1 else f"{chains[0]} + {len(chains)-1} more"
    has_audit = enrichment.get("has_been_audited", False)
    audit_providers = enrichment.get("audit_providers", [])
    bounty = enrichment.get("bounty_platform", "none")
    bounty_amount = enrichment.get("bounty_amount_usd", 0)
    velocity = enrichment.get("shipping_velocity", "active")
    name = (contact.name if contact else persona.get("name", "team")).split()[0]
    warm_intro = enrichment.get("warm_intro_available", False)
    warm_intro_path = enrichment.get("warm_intro_path", "")

    risk_hooks = {
        "dex":        "MEV extraction and reentrancy vulnerabilities in swap/router logic",
        "lending":    "oracle manipulation and liquidation edge cases",
        "yield":      "vault accounting errors and withdrawal race conditions",
        "bridge":     "cross-chain message validation failures and replay attacks",
        "l2":         "sequencer downtime edge cases and fraud proof gaps",
        "chain":      "sequencer downtime edge cases and fraud proof gaps",
        "stablecoin": "depeg scenarios and minting function access control flaws",
        "restaking":  "slashing condition logic and AVS integration risks",
        "infra":      "composability risks and cross-protocol interaction bugs",
        "cdp":        "oracle manipulation and collateral liquidation edge cases",
    }
    risk = risk_hooks.get(category, "smart contract vulnerabilities introduced at scale")

    tvl_str = f"${tvl/1e9:.1f}B" if tvl >= 1e9 else f"${tvl/1e6:.0f}M"
    bounty_ratio = f"{bounty_amount/tvl*100:.3f}% of TVL" if tvl > 0 and bounty_amount > 0 else None

    if sequence_step == 1:
        if not has_audit:
            subject = f"Would love to work with {protocol_name} on security"
            body = (
                f"Hey {name},\n\n"
                f"Been following {protocol_name}'s growth on {chain_str} — really impressed with what you've built. "
                f"We work with a number of {category} protocols at similar scale and would love to be the team "
                f"that does your first comprehensive security review.\n\n"
                f"We run competitions where 100+ independent researchers review your code simultaneously — "
                f"more coverage than a traditional audit, and most teams find it gives them the confidence to "
                f"ship the next phase faster. Happy to walk you through how it works.\n\n"
                f"[Book a call]\n\nBest,\nDiscovery Pipeline Team"
            )
        elif bounty == "none":
            auditor_str = f"audited by {', '.join(audit_providers)}" if audit_providers else "audited"
            subject = f"Quick thought on {protocol_name}'s bounty setup"
            body = (
                f"Hey {name},\n\n"
                f"{protocol_name} is {auditor_str} — nice. One thing I noticed: no active bounty program. "
                f"Most teams at your scale run one mainly because it keeps researchers engaged between formal reviews "
                f"and surfaces edge cases that automated tools miss.\n\n"
                f"We run managed bounty programs — triage quality is a lot better than what "
                f"most teams deal with directly on Immunefi. Worth a quick chat if it's something you've been thinking about?\n\n"
                f"[Book a call]\n\nBest,\nDiscovery Pipeline Team"
            )
        elif warm_intro:
            subject = f"Intro via {warm_intro_path.split('—')[0].strip()}"
            body = (
                f"Hey {name},\n\n"
                f"{warm_intro_path}\n\n"
                f"Wanted to connect — we work with a few {category} protocols at similar scale and one thing that "
                f"comes up consistently is how to keep security coverage moving as fast as the codebase. "
                f"Your {bounty} bounty is a good start. Curious if continuous review between formal audits is something on your roadmap.\n\n"
                f"Happy to share what's worked for other teams if useful.\n\n"
                f"[Book a call]\n\nBest,\nDiscovery Pipeline Team"
            )
        else:
            auditor_str = f"by {', '.join(audit_providers)}" if audit_providers else ""
            ratio_note = f"One thing — your {bounty} bounty is {bounty_ratio}, which is pretty light relative to scale. " if bounty_ratio else ""
            subject = f"How {protocol_name} keeps security up with shipping pace"
            body = (
                f"Hey {name},\n\n"
                f"Audited {auditor_str}, {bounty} bounty running — solid setup. "
                f"{ratio_note}"
                f"Curious how you handle coverage for {chain_str} at {velocity} velocity. "
                f"The teams we work with at similar scale usually want continuous analysis between formal reviews "
                f"so they can ship confidently without waiting for the next audit cycle.\n\n"
                f"We've built exactly that — happy to walk through how it works if it's relevant.\n\n"
                f"[Book a call]\n\nBest,\nDiscovery Pipeline Team"
            )

    elif sequence_step == 2:
        subject = f"Following up — {category} security patterns in 2026"
        body = (
            f"Hey {name},\n\n"
            f"Wanted to follow up with something concrete. "
            f"The {category} protocols we've worked with this year have mostly been focused on "
            f"keeping review coverage in sync with shipping pace — especially on {chain_str} where {risk} "
            f"shows up most in newer code.\n\n"
            f"Happy to share what approaches are working. No pitch — just useful context if you're thinking about it. 15 min?\n\n"
            f"[Book a call]\n\nBest,\nDiscovery Pipeline Team"
        )

    else:
        subject = f"One last thing on {protocol_name} security"
        body = (
            f"Hey {name},\n\n"
            f"Last note — we're happy to run a free AI analysis pass on {protocol_name}'s contracts. "
            f"You'd get a full report of what it finds in your {category} logic, no strings attached.\n\n"
            f"Takes about 10 minutes to kick off and teams usually find it pretty useful regardless of what they decide after. "
            f"Worth doing?\n\n"
            f"[Book a call]\n\nBest,\nDiscovery Pipeline Team"
        )

    person_name = contact.name if contact else persona.get("name", "")
    person_role = contact.role if contact else persona.get("role", "")
    channel = "email" if (contact and contact.email) else persona.get("preferred_channel", "email")

    return OutreachDraft(
        protocol_name=protocol_name,
        persona_name=person_name,
        persona_role=person_role,
        channel=channel,
        sequence_step=sequence_step,
        subject_line=subject,
        message_body=body,
        signals_used={
            "tvl": tvl,
            "category": category,
            "chain": chain_str,
            "audit_providers": audit_providers,
            "bounty_platform": bounty,
            "bounty_amount": bounty_amount,
            "velocity": velocity,
            "risk_hook": risk,
        },
        llm_model="template_fallback",
        contact_email=contact.email if contact else "",
        contact_twitter=contact.twitter_handle if contact else "",
        contact_github=contact.github_username if contact else "",
        contact_source=contact.source if contact else "",
    )


def run_outreach_generation(
    scored_leads: list,
    enrichment_map: dict,
    persona_map: dict,
    contacts_map: dict = None,
    use_llm: bool = True,
) -> list[OutreachDraft]:
    """
    Generate personalized outreach for every contact found per qualified lead.
    One email per person (up to 3 per protocol), not one per company.

    contacts_map: {protocol_name: [Contact, ...]} from contacts.find_contacts_for_qualified_leads
    """
    print("\n" + "=" * 60, flush=True)
    print("OUTREACH STAGE — Personalized emails per person", flush=True)
    print("=" * 60 + "\n", flush=True)

    has_claude = bool(os.getenv("ANTHROPIC_API_KEY")) and use_llm
    if has_claude:
        logger.info(f"Outreach using Claude API ({get_anthropic_model()})")
        print(f"  LLM: Claude API ({get_anthropic_model()})", flush=True)
    else:
        reason = "ANTHROPIC_API_KEY not set" if not os.getenv("ANTHROPIC_API_KEY") else "--no-llm flag"
        logger.info(f"Outreach using template fallback ({reason})")
        print(f"  LLM: template fallback ({reason})", flush=True)

    drafts = []
    contacts_map = contacts_map or {}

    for lead in scored_leads:
        if lead.score_tier == "cool":
            continue

        enrichment = enrichment_map.get(lead.protocol_name, {})
        score_data = {
            "composite": lead.composite_score,
            "tier": lead.score_tier,
            "tvl_score": lead.tvl_score,
            "audit_score": lead.audit_status_score,
            "velocity_score": lead.velocity_score,
        }

        contacts = contacts_map.get(lead.protocol_name, [])

        # If no contacts found, fall back to persona (legacy behavior)
        if not contacts:
            persona = persona_map.get(lead.protocol_name, {
                "name": "team",
                "role": "Founder/CTO",
                "preferred_channel": "twitter_dm",
            })
            contacts_to_use = [None]  # None signals "use persona fallback"
        else:
            contacts_to_use = contacts
            persona = persona_map.get(lead.protocol_name, {})

        protocol_drafts = []
        for contact in contacts_to_use:
            persona_for_draft = persona if contact is None else {}

            draft = None
            if has_claude:
                draft = generate_outreach_with_claude(
                    lead.protocol_name, enrichment, score_data,
                    persona_for_draft, sequence_step=1, contact=contact
                )

            if not draft:
                draft = generate_outreach_fallback(
                    lead.protocol_name, enrichment, score_data,
                    persona_for_draft, sequence_step=1, contact=contact
                )

            protocol_drafts.append(draft)

        drafts.extend(protocol_drafts)
        model_tag = "Claude" if protocol_drafts[0].llm_model != "template_fallback" else "template"
        names = [d.persona_name for d in protocol_drafts]
        print(
            f"  ✓ {lead.protocol_name} ({lead.score_tier}, {model_tag}): "
            f"{len(protocol_drafts)} emails → {', '.join(names[:3])}{'...' if len(names) > 3 else ''}",
            flush=True
        )
        logger.info(
            "Outreach for %s: %d drafts — %s",
            lead.protocol_name, len(protocol_drafts), names
        )

    print(f"\n✓ Generated {len(drafts)} personalized emails across {len(scored_leads)} protocols", flush=True)
    return drafts
