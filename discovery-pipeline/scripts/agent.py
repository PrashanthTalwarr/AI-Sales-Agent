"""
DISCOVERY PIPELINE AGENT — LangChain ReAct agent powered by Claude.

Uses LangChain's create_tool_calling_agent + AgentExecutor so the
framework handles the full ReAct loop (Reason → Act → Observe → Repeat).

Usage:
  python scripts/agent.py

Example prompts:
  "show me the warm leads"
  "show me the outreach message for Spiko"
  "generate a follow-up for Lido"
  "push Pendle to HubSpot"
  "what exploits happened this week?"
  "run the pipeline"
"""

import sys
import os
import json
import glob
import csv
import logging
import importlib.util
from datetime import datetime
from collections import defaultdict

# ── UTF-8 output (Windows) ────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

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

# ── LangChain imports ─────────────────────────────────────────────────────────
from langchain_anthropic import ChatAnthropic
from langchain.agents import create_tool_calling_agent, AgentExecutor
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage

# ── Pipeline imports ──────────────────────────────────────────────────────────
from src.pipeline.score import ScoredLead
from src.agents.outreach_agent import (
    OutreachDraft,
    generate_outreach_with_claude,
    generate_outreach_fallback,
)
from src.integrations.hubspot import (
    push_lead_to_hubspot,
    get_hubspot_client,
    ensure_custom_properties,
)
from src.integrations.slack_alerts import send_slack_alert
from src.monitoring.event_monitor import run_event_monitor


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_agent_logging():
    """Write agent DEBUG+ logs to app.log; suppress third-party library noise."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    log_path = os.path.join(os.path.dirname(__file__), "..", "app.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s"))
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    root.addHandler(ch)

    for noisy in ("urllib3", "httpcore", "httpx", "anthropic", "anthropic._base_client",
                  "langchain", "langchain_core", "langchain_anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


setup_agent_logging()
logger = logging.getLogger(__name__)

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")


# ── Session state (module-level so @tool functions can access it) ─────────────

class AgentState:
    """Holds all pipeline data for the current session."""

    def __init__(self):
        self.scored_leads: list[ScoredLead] = []
        self.outreach_drafts: list[OutreachDraft] = []
        self.enrichment_map: dict = {}
        self.persona_map: dict = {}
        self.last_events: list = []
        self.last_run: str = None

    def find_lead(self, name: str) -> ScoredLead | None:
        name_l = name.lower()
        for lead in self.scored_leads:
            if lead.protocol_name.lower() == name_l:
                return lead
        for lead in self.scored_leads:
            if name_l in lead.protocol_name.lower():
                return lead
        return None

    def find_draft(self, name: str) -> OutreachDraft | None:
        name_l = name.lower()
        for d in self.outreach_drafts:
            if d.protocol_name.lower() == name_l:
                return d
        for d in self.outreach_drafts:
            if name_l in d.protocol_name.lower():
                return d
        return None


_state = AgentState()  # populated in main() before executor is created


# ── Data loader ───────────────────────────────────────────────────────────────

def load_last_results() -> bool:
    """Populate _state from the most recent data/output files."""
    output_dir = "data/output"
    if not os.path.exists(output_dir):
        logger.debug("load_last_results: output dir not found (%s)", output_dir)
        return False

    csvs      = sorted(glob.glob(f"{output_dir}/scored_leads_*.csv"), reverse=True)
    jsons     = sorted(glob.glob(f"{output_dir}/outreach_drafts_*.json"), reverse=True)
    summaries = sorted(glob.glob(f"{output_dir}/pipeline_summary_*.json"), reverse=True)

    logger.debug("load_last_results: %d CSV(s), %d JSON(s), %d summary file(s)",
                 len(csvs), len(jsons), len(summaries))

    if not csvs:
        logger.info("load_last_results: no scored_leads CSV found")
        return False

    logger.info("load_last_results: reading %s", csvs[0])
    with open(csvs[0], newline="", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            _state.scored_leads.append(ScoredLead(
                protocol_name=row["Protocol"],
                tvl_score=float(row["TVL Score"]),
                audit_status_score=float(row["Audit Score"]),
                velocity_score=float(row["Velocity Score"]),
                funding_score=float(row["Funding Score"]),
                reachability_score=float(row["Reachability Score"]),
                composite_score=float(row["Composite Score"]),
                score_tier=row["Tier"],
                scoring_rationale=row["Rationale"],
                model_version="1.0",
            ))
    logger.info("load_last_results: %d scored leads loaded", len(_state.scored_leads))

    if jsons:
        logger.info("load_last_results: reading %s", jsons[0])
        with open(jsons[0], encoding="utf-8") as f:
            for d in json.load(f):
                _state.outreach_drafts.append(OutreachDraft(
                    protocol_name=d["protocol"],
                    persona_name=d["persona"],
                    persona_role=d["role"],
                    channel=d["channel"],
                    sequence_step=d["step"],
                    subject_line=d["subject"],
                    message_body=d["body"],
                    signals_used=d.get("signals_used", {}),
                    llm_model=d.get("model", "unknown"),
                ))
                _state.enrichment_map[d["protocol"]] = d.get("signals_used", {})
                _state.persona_map[d["protocol"]] = {
                    "name": d["persona"],
                    "role": d["role"],
                    "preferred_channel": d["channel"],
                }
        logger.info("load_last_results: %d outreach drafts loaded", len(_state.outreach_drafts))

    if summaries:
        with open(summaries[0], encoding="utf-8") as f:
            _state.last_run = json.load(f).get("run_timestamp")
        logger.info("load_last_results: last_run = %s", _state.last_run)

    return True


# ── LangChain tools ───────────────────────────────────────────────────────────

@tool
def get_pipeline_results(tier_filter: str = "all") -> str:
    """
    Get scored leads from the last pipeline run.
    Shows protocol name, composite score, tier (hot/warm/cool), and factor breakdown.
    Use tier_filter='warm' or 'hot' to narrow results.
    """
    logger.info("tool:get_pipeline_results tier=%s", tier_filter)
    if not _state.scored_leads:
        return "No pipeline results loaded. Run the pipeline first."

    leads = (
        _state.scored_leads if tier_filter == "all"
        else [l for l in _state.scored_leads if l.score_tier == tier_filter]
    )
    if not leads:
        return f"No {tier_filter} leads found."

    lines = [f"{'Protocol':<25} {'Score':>6} {'Tier':<5} {'TVL':>4} {'Audit':>5} {'Vel':>4} {'Fund':>5} {'Reach':>5}"]
    lines.append("─" * 62)
    for l in sorted(leads, key=lambda x: x.composite_score, reverse=True):
        icon = {"hot": "🔥", "warm": "🟡", "cool": "⚪"}.get(l.score_tier, " ")
        lines.append(
            f"{icon} {l.protocol_name:<23} {l.composite_score:>6.0f} {l.score_tier:<5} "
            f"{l.tvl_score:>4.0f} {l.audit_status_score:>5.0f} {l.velocity_score:>4.0f} "
            f"{l.funding_score:>5.0f} {l.reachability_score:>5.0f}"
        )
    lines.append(f"\n{len(leads)} leads")
    return "\n".join(lines)


@tool
def get_outreach_draft(protocol_name: str) -> str:
    """
    Get the full outreach message (subject + body) for a specific protocol.
    Shows who it's addressed to, the channel, and the complete message text.
    """
    logger.info("tool:get_outreach_draft protocol=%s", protocol_name)
    draft = _state.find_draft(protocol_name)
    if not draft:
        logger.info("tool:get_outreach_draft — no draft found for '%s'", protocol_name)
        return f"No draft found for '{protocol_name}'. Try generate_outreach to create one."
    logger.info("tool:get_outreach_draft — found for %s (step=%s model=%s)",
                draft.protocol_name, draft.sequence_step, draft.llm_model)
    return (
        f"Protocol:  {draft.protocol_name}\n"
        f"To:        {draft.persona_name} ({draft.persona_role})\n"
        f"Channel:   {draft.channel}\n"
        f"Step:      {draft.sequence_step}\n"
        f"Model:     {draft.llm_model}\n"
        f"\nSubject: {draft.subject_line}\n"
        f"\n{draft.message_body}"
    )


@tool
def generate_outreach(protocol_name: str, sequence_step: int = 1) -> str:
    """
    Generate a new outreach message using Claude for any protocol.
    sequence_step: 1=first touch, 2=data/urgency follow-up, 3=final/offer.
    Overwrites any existing draft for that protocol.
    """
    logger.info("tool:generate_outreach protocol=%s step=%s", protocol_name, sequence_step)
    lead = _state.find_lead(protocol_name)
    enrichment = _state.enrichment_map.get(protocol_name, {})

    if not enrichment and lead:
        tvl_approx = {30: 2_000_000_000, 25: 500_000_000, 20: 50_000_000, 14: 5_000_000}.get(
            int(lead.tvl_score), 0
        )
        enrichment = {
            "tvl_usd": tvl_approx,
            "category": "protocol",
            "chains_deployed": ["Ethereum"],
            "has_been_audited": lead.audit_status_score < 25,
            "audit_providers": [],
            "bounty_platform": "none",
            "bounty_amount_usd": 0,
            "shipping_velocity": "high" if lead.velocity_score >= 13 else "moderate",
            "ai_tool_signals": [],
            "unaudited_new_code": lead.audit_status_score in (20, 22),
            "total_raised_usd": 0,
            "last_funding_date": None,
            "warm_intro_available": lead.reachability_score == 10,
            "warm_intro_path": "",
        }

    persona = _state.persona_map.get(protocol_name, {
        "name": "team", "role": "Founder/CTO", "preferred_channel": "twitter_dm"
    })
    score_data = {}
    if lead:
        score_data = {
            "composite": lead.composite_score,
            "tier": lead.score_tier,
            "tvl_score": lead.tvl_score,
            "audit_score": lead.audit_status_score,
            "velocity_score": lead.velocity_score,
        }

    draft = None
    if os.getenv("ANTHROPIC_API_KEY"):
        logger.debug("tool:generate_outreach — calling Claude for %s", protocol_name)
        draft = generate_outreach_with_claude(protocol_name, enrichment, score_data, persona, sequence_step)
    if not draft:
        logger.debug("tool:generate_outreach — using fallback for %s", protocol_name)
        draft = generate_outreach_fallback(protocol_name, enrichment, score_data, persona, sequence_step)

    logger.info("tool:generate_outreach — draft created for %s via %s", protocol_name, draft.llm_model)
    _state.outreach_drafts = [d for d in _state.outreach_drafts if d.protocol_name.lower() != protocol_name.lower()]
    _state.outreach_drafts.append(draft)

    return (
        f"Generated (step {sequence_step}, {draft.llm_model}):\n"
        f"\nSubject: {draft.subject_line}\n"
        f"\n{draft.message_body}"
    )


@tool
def get_pipeline_summary() -> str:
    """High-level summary of the last pipeline run: counts, top leads, last run time."""
    logger.info("tool:get_pipeline_summary")
    if not _state.scored_leads:
        return "No pipeline results loaded. Run the pipeline first."

    hot  = [l for l in _state.scored_leads if l.score_tier == "hot"]
    warm = [l for l in _state.scored_leads if l.score_tier == "warm"]
    top5 = sorted(_state.scored_leads, key=lambda x: x.composite_score, reverse=True)[:5]

    lines = [
        f"Last run:         {_state.last_run or 'unknown'}",
        f"Total scored:     {len(_state.scored_leads)}",
        f"Hot leads:        {len(hot)}",
        f"Warm leads:       {len(warm)}",
        f"Outreach drafted: {len(_state.outreach_drafts)}",
        f"\nTop 5:",
    ]
    for l in top5:
        lines.append(f"  {l.protocol_name}: {l.composite_score:.0f} ({l.score_tier})")
    return "\n".join(lines)


@tool
def push_to_hubspot(protocol_name: str) -> str:
    """
    Push a specific lead to HubSpot CRM.
    Creates a contact with all discovery_* custom properties populated.
    """
    logger.info("tool:push_to_hubspot protocol=%s", protocol_name)
    lead = _state.find_lead(protocol_name)
    if not lead:
        logger.warning("tool:push_to_hubspot — no lead found for '%s'", protocol_name)
        return f"No lead found for '{protocol_name}'."

    client = get_hubspot_client()
    if not client:
        logger.warning("tool:push_to_hubspot — HubSpot client not available")
        return "HubSpot not configured — check HUBSPOT_API_KEY in config/.env"

    ensure_custom_properties(client)
    enrichment = _state.enrichment_map.get(lead.protocol_name, {})
    persona    = _state.persona_map.get(lead.protocol_name, {})
    score_data = {"composite": lead.composite_score, "tier": lead.score_tier}

    contact_id = push_lead_to_hubspot(lead.protocol_name, enrichment, score_data, persona)
    if contact_id:
        logger.info("tool:push_to_hubspot — contact created id=%s for %s", contact_id, lead.protocol_name)
        return f"HubSpot contact created: {lead.protocol_name} (id={contact_id})"
    logger.error("tool:push_to_hubspot — failed for %s", lead.protocol_name)
    return f"HubSpot push failed for {lead.protocol_name} — may already exist or check API key"


@tool
def run_market_monitor() -> str:
    """
    Check DeFiLlama for exploits in the last 30 days, plus funding rounds
    and governance proposals. Returns events relevant to pipeline targets.
    """
    logger.info("tool:run_market_monitor — %d pipeline protocols", len(_state.scored_leads))
    protocol_names = [l.protocol_name for l in _state.scored_leads]
    events = run_event_monitor(protocol_names)
    _state.last_events = events
    logger.info("tool:run_market_monitor — %d events detected", len(events))

    if not events:
        return "No market events detected."

    lines = []
    for e in events:
        rel = f" → relevant to: {', '.join(e.affected_protocols)}" if e.affected_protocols else ""
        lines.append(f"[{e.event_type}] {e.title}{rel}")
    return f"{len(events)} events:\n" + "\n".join(lines)


@tool
def run_pipeline(seed_only: bool = False, no_llm: bool = False) -> str:
    """
    Run the full discovery pipeline: DeFiLlama + GitHub ingest, enrich, score,
    qualify, outreach generation, HubSpot push, and Slack alert.
    seed_only=False means live APIs (default). Takes 2-3 minutes.
    """
    logger.info("tool:run_pipeline seed_only=%s no_llm=%s", seed_only, no_llm)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(
        "run_pipeline_mod", os.path.join(script_dir, "run_pipeline.py")
    )
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    OVERLAYS = rp.RESEARCH_OVERLAYS

    from src.pipeline.ingest import run_full_ingest
    from src.pipeline.enrich import run_enrichment, enrich_with_audit_data, enrich_with_team_data, classify_tvl
    from src.pipeline.score import run_scoring
    from src.agents.outreach_agent import run_outreach_generation

    print("\nRunning pipeline...", flush=True)

    if seed_only:
        profiles = rp.build_demo_profiles()
    else:
        all_signals = run_full_ingest()
        sig_map = defaultdict(list)
        for s in all_signals:
            sig_map[s.protocol_name].append(s)
        profiles = run_enrichment(sig_map)
        for profile in profiles:
            if profile.protocol_name in OVERLAYS:
                seed = OVERLAYS[profile.protocol_name]
                profile = enrich_with_audit_data(profile, seed["audit"])
                profile = enrich_with_team_data(profile, seed["team"])
                if seed.get("override_tvl"):
                    profile.tvl_usd = seed["override_tvl"]
                    profile.tvl_category = classify_tvl(profile.tvl_usd)

    scored    = run_scoring(profiles)
    qualified = [s for s in scored if s.score_tier in ("hot", "warm")]

    enrichment_map, persona_map = {}, {}
    for p in profiles:
        enrichment_map[p.protocol_name] = {
            "tvl_usd":              p.tvl_usd,
            "category":             OVERLAYS.get(p.protocol_name, {}).get("category", "protocol"),
            "chains_deployed":      p.chains_deployed or ["Ethereum"],
            "has_been_audited":     p.has_been_audited,
            "audit_providers":      p.audit_providers or [],
            "last_audit_date":      p.last_audit_date,
            "bounty_platform":      p.bounty_platform,
            "bounty_amount_usd":    p.bounty_amount_usd,
            "shipping_velocity":    p.shipping_velocity,
            "ai_tool_signals":      p.ai_tool_signals or [],
            "unaudited_new_code":   p.unaudited_new_code,
            "total_raised_usd":     p.total_raised_usd,
            "last_funding_date":    p.last_funding_date,
            "warm_intro_available": p.warm_intro_available,
            "warm_intro_path":      p.warm_intro_path,
        }
        if p.protocol_name in OVERLAYS:
            persona_map[p.protocol_name] = OVERLAYS[p.protocol_name]["persona"]

    outreach = run_outreach_generation(qualified, enrichment_map, persona_map, use_llm=not no_llm)

    _state.scored_leads    = scored
    _state.outreach_drafts = outreach
    _state.enrichment_map  = enrichment_map
    _state.persona_map     = persona_map
    _state.last_run        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    hot  = [s for s in scored if s.score_tier == "hot"]
    warm = [s for s in scored if s.score_tier == "warm"]
    logger.info("tool:run_pipeline complete — scored=%d hot=%d warm=%d outreach=%d",
                len(scored), len(hot), len(warm), len(outreach))
    return (
        f"Pipeline complete ({_state.last_run})\n"
        f"  Scored: {len(scored)} protocols\n"
        f"  Hot: {len(hot)}, Warm: {len(warm)}\n"
        f"  Outreach drafted: {len(outreach)}\n"
        f"  Qualified: {[q.protocol_name for q in qualified]}"
    )


@tool
def send_slack(text: str) -> str:
    """Send a custom message to the configured Slack channel."""
    logger.info("tool:send_slack — %d chars", len(text))
    sent = send_slack_alert({"text": text})
    if sent:
        logger.info("tool:send_slack — delivered")
    else:
        logger.warning("tool:send_slack — not delivered")
    return "Slack message sent." if sent else "Slack not configured or send failed."


# ── All tools registered with the agent ──────────────────────────────────────

AGENT_TOOLS = [
    get_pipeline_results,
    get_outreach_draft,
    generate_outreach,
    get_pipeline_summary,
    push_to_hubspot,
    run_market_monitor,
    run_pipeline,
    send_slack,
]


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Discovery Pipeline Agent — the GTM team's AI assistant for Web3 security sales.

We sell smart contract security: reviews, competitions, bug bounties, and 24/7 monitoring.

Your job:
- Surface scored leads and explain why they rank where they do
- Show and refine outreach messages — read them in full when asked
- Push leads to HubSpot on request
- Monitor markets for exploits and funding events
- Run the pipeline on demand

Rules:
- Be concise. This is a working tool, not a chat assistant.
- When showing an outreach message, always show the full subject + body.
- When the user asks to push to HubSpot or send Slack, do it and confirm the result.
- If no pipeline results are loaded yet, suggest running the pipeline.
- Use tools proactively — if someone asks "how did Pendle score?", call get_pipeline_results."""


# ── REPL ──────────────────────────────────────────────────────────────────────

def main():
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set in config/.env")
        sys.exit(1)

    # ── Build the LangChain ReAct agent ──────────────────────────────────────
    llm = ChatAnthropic(model=MODEL, api_key=api_key, temperature=0)

    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])

    agent = create_tool_calling_agent(llm, AGENT_TOOLS, prompt)
    executor = AgentExecutor(agent=agent, tools=AGENT_TOOLS, verbose=False)

    logger.info("Agent session started — LangChain ReAct / Claude tool-calling")

    # ── Startup banner ────────────────────────────────────────────────────────
    print("\n" + "=" * 60, flush=True)
    print("  DISCOVERY PIPELINE AGENT  (LangChain + Claude)", flush=True)
    print("=" * 60, flush=True)
    print("", flush=True)
    print("  What would you like to do?", flush=True)
    print("", flush=True)
    print("  1  Run the pipeline", flush=True)
    print("  2  Load last results and start chatting", flush=True)
    print("", flush=True)
    print("  Or just type any question to start.", flush=True)
    print("  Type 'quit' to exit.", flush=True)
    print("=" * 60 + "\n", flush=True)

    _SHORTCUTS = {
        "1": "run the pipeline",
        "2": "load last results",
    }

    chat_history = []

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q", "bye"):
            logger.info("Agent session ended by user")
            print("Goodbye.")
            break

        # Expand numeric shortcuts
        user_input = _SHORTCUTS.get(user_input.strip(), user_input)

        # "load last results" is handled locally — no LLM round-trip needed
        if user_input.lower() in ("load last results", "load results", "load data"):
            loaded = load_last_results()
            if loaded and _state.scored_leads:
                hot  = len([l for l in _state.scored_leads if l.score_tier == "hot"])
                warm = len([l for l in _state.scored_leads if l.score_tier == "warm"])
                print(
                    f"\nLoaded {len(_state.scored_leads)} leads "
                    f"({hot} hot, {warm} warm), "
                    f"{len(_state.outreach_drafts)} outreach drafts. "
                    f"Last run: {_state.last_run or 'unknown'}\n",
                    flush=True,
                )
                logger.info("Manual load: leads=%d drafts=%d last_run=%s",
                            len(_state.scored_leads), len(_state.outreach_drafts), _state.last_run)
            else:
                print("\nNo previous results found — run the pipeline first.\n", flush=True)
            continue

        logger.info("User: %s", user_input[:120])

        try:
            result = executor.invoke({
                "input": user_input,
                "chat_history": chat_history,
            })
            output = result.get("output", "")
            if output:
                print(f"\nAgent: {output}\n", flush=True)
                logger.debug("Agent reply: %d chars", len(output))

            # Keep conversation history for multi-turn context
            chat_history.append(HumanMessage(content=user_input))
            chat_history.append(AIMessage(content=output))

        except Exception as e:
            logger.exception("AgentExecutor error")
            print(f"\nError: {e}\n", flush=True)


if __name__ == "__main__":
    main()
