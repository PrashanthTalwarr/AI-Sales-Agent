"""
FastAPI backend — Discovery Pipeline Agent REST API.

Start: uvicorn scripts.api:app --port 8000 --reload
"""

import sys
import os
import json
import asyncio
import logging
import importlib.util
import queue as q_module
import threading
import warnings
from datetime import datetime
from collections import defaultdict

warnings.filterwarnings("ignore", module="langchain_anthropic")
warnings.filterwarnings("ignore", module="langchain_core")
warnings.filterwarnings("ignore", message=".*Tool use is not yet supported.*")
warnings.filterwarnings("ignore", message=".*beta.*")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(__file__), "..", "config", ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
except ImportError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from langchain_anthropic import ChatAnthropic
from langchain.agents import create_tool_calling_agent, AgentExecutor
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage

from src.pipeline.score import ScoredLead
from src.agents.outreach_agent import OutreachDraft
from src.monitoring.event_monitor import run_event_monitor
from src.store.json_store import (
    load_leads_and_contacts,
    load_drafts,
    derive_factor_scores,
    list_outreach,
    mark_replied as store_mark_replied,
)
from src.utils import token_tracker

# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    log_path = os.path.join(os.path.dirname(__file__), "..", "app.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    ))
    root.addHandler(fh)

    # Console shows WARNING+ only (keeps uvicorn output clean)
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    root.addHandler(ch)

    # Silence third-party noise in the log file too
    for noisy in ("urllib3", "httpcore", "httpx", "anthropic", "anthropic._base_client",
                  "langchain", "langchain_core", "langchain_anthropic",
                  "watchfiles", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

_setup_logging()
logger = logging.getLogger(__name__)
logger.info("=" * 60)
logger.info("Discovery Pipeline API — starting up")

# ── State ─────────────────────────────────────────────────────────────────────

class AgentState:
    def __init__(self):
        self.scored_leads: list[ScoredLead] = []
        self.outreach_drafts: list[OutreachDraft] = []
        self.enrichment_map: dict = {}
        self.persona_map: dict = {}
        self.last_run: str = None

    def find_lead(self, name: str):
        name_l = name.lower()
        for lead in self.scored_leads:
            if lead.protocol_name.lower() == name_l:
                return lead
        for lead in self.scored_leads:
            if name_l in lead.protocol_name.lower():
                return lead
        return None

    def find_draft(self, name: str):
        name_l = name.lower()
        for d in self.outreach_drafts:
            if d.protocol_name.lower() == name_l:
                return d
        for d in self.outreach_drafts:
            if name_l in d.protocol_name.lower():
                return d
        return None


_state = AgentState()
_chat_history: list = []


def _rehydrate_drafts() -> list:
    """
    Rebuild OutreachDraft objects from data/state.json.

    Drafts previously lived only in this process's memory, so any uvicorn reload
    left the draft drawer and its Send button with nothing to work with. Reading
    them back from disk makes both survive a restart.
    """
    drafts = [
        OutreachDraft(
            protocol_name=d.get("protocol_name", ""), persona_name=d.get("persona_name", ""),
            persona_role=d.get("persona_role", ""), channel=d.get("channel", "email"),
            sequence_step=d.get("sequence_step", 1) or 1,
            subject_line=d.get("subject_line", ""), message_body=d.get("message_body", ""),
            signals_used=d.get("signals_used") or {}, llm_model=d.get("llm_model", ""),
            contact_email=d.get("contact_email") or "", contact_twitter=d.get("contact_twitter") or "",
            contact_github=d.get("contact_github") or "", contact_source=d.get("contact_source") or "",
        )
        for d in load_drafts()
    ]
    if drafts:
        logger.info("Rehydrated %d drafts from data/state.json", len(drafts))
    return drafts


# ── LangChain tools ───────────────────────────────────────────────────────────

@tool
def get_pipeline_results(tier_filter: str = "all") -> str:
    """Get scored leads. Use tier_filter='warm' or 'hot' to narrow results."""
    logger.info("tool:get_pipeline_results tier=%s leads_in_state=%d", tier_filter, len(_state.scored_leads))
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
        contacts = _state.enrichment_map.get(l.protocol_name, {}).get("contacts", [])
        contact_note = f" | {len(contacts)} contacts" if contacts else ""
        lines.append(
            f"{icon} {l.protocol_name:<23} {l.composite_score:>6.0f} {l.score_tier:<5} "
            f"{l.tvl_score:>4.0f} {l.audit_status_score:>5.0f} {l.velocity_score:>4.0f} "
            f"{l.funding_score:>5.0f} {l.reachability_score:>5.0f}{contact_note}"
        )
    lines.append(f"\n{len(leads)} leads")
    return "\n".join(lines)


@tool
def get_outreach_draft(protocol_name: str) -> str:
    """Get all personalized outreach emails for a specific protocol (one per person found)."""
    logger.info("tool:get_outreach_draft protocol=%s", protocol_name)
    name_l = protocol_name.lower()
    drafts = [d for d in _state.outreach_drafts if d.protocol_name.lower() == name_l]
    if not drafts:
        drafts = [d for d in _state.outreach_drafts if name_l in d.protocol_name.lower()]
    if not drafts:
        logger.warning("tool:get_outreach_draft — no drafts found for '%s'", protocol_name)
        return f"No drafts found for '{protocol_name}'."
    logger.info("tool:get_outreach_draft — %d drafts for %s", len(drafts), protocol_name)
    lines = [f"{len(drafts)} personalized email(s) for {drafts[0].protocol_name}:\n"]
    for i, d in enumerate(drafts, 1):
        email_str = f" | {d.contact_email}" if d.contact_email else ""
        github_str = f" | github.com/{d.contact_github}" if d.contact_github else ""
        lines.append(f"── {i}. {d.persona_name} ({d.persona_role}){email_str}{github_str} ──")
        lines.append(f"Subject: {d.subject_line}\n")
        lines.append(d.message_body)
        lines.append("")
    return "\n".join(lines)



@tool
def get_pipeline_summary() -> str:
    """High-level summary: counts, top leads, last run time."""
    logger.info("tool:get_pipeline_summary — %d leads in state", len(_state.scored_leads))
    if not _state.scored_leads:
        return "No pipeline results loaded."
    hot  = [l for l in _state.scored_leads if l.score_tier == "hot"]
    warm = [l for l in _state.scored_leads if l.score_tier == "warm"]
    top5 = sorted(_state.scored_leads, key=lambda x: x.composite_score, reverse=True)[:5]
    lines = [
        f"Last run: {_state.last_run or 'unknown'}",
        f"Total scored: {len(_state.scored_leads)} | Hot: {len(hot)} | Warm: {len(warm)}",
        f"Outreach drafted: {len(_state.outreach_drafts)}",
        "\nTop 5:",
    ]
    for l in top5:
        lines.append(f"  {l.protocol_name}: {l.composite_score:.0f} ({l.score_tier})")
    return "\n".join(lines)


@tool
def run_market_monitor() -> str:
    """Check DeFiLlama for exploits, funding rounds, and governance proposals."""
    logger.info("tool:run_market_monitor — scanning for %d pipeline protocols", len(_state.scored_leads))
    events = run_event_monitor([l.protocol_name for l in _state.scored_leads])
    logger.info("tool:run_market_monitor — %d events detected", len(events))
    if not events:
        return "No market events detected."
    lines = []
    for e in events:
        rel = f" → {', '.join(e.affected_protocols)}" if e.affected_protocols else ""
        lines.append(f"[{e.event_type}] {e.title}{rel}")
    return f"{len(events)} events:\n" + "\n".join(lines)


@tool
def get_contacts(protocol_name: str) -> str:
    """Get the security-sale-relevant contacts found for a protocol."""
    logger.info("tool:get_contacts protocol=%s", protocol_name)
    enrichment = _state.enrichment_map.get(protocol_name)
    if not enrichment:
        # fuzzy match
        name_l = protocol_name.lower()
        for key in _state.enrichment_map:
            if name_l in key.lower():
                enrichment = _state.enrichment_map[key]
                protocol_name = key
                break
    if not enrichment:
        return f"No data found for '{protocol_name}'. Load results or run the pipeline first."
    contacts = enrichment.get("contacts", [])
    if not contacts:
        return f"No contacts found for {protocol_name}."
    lines = [f"Contacts for {protocol_name} ({len(contacts)} found):"]
    for c in contacts:
        email_str = f" | {c['email']}" if c.get("email") else ""
        linkedin_str = f" | {c['linkedin_url']}" if c.get("linkedin_url") else ""
        twitter_str = f" | {c.get('twitter_handle') or c.get('twitter', '')}" if (c.get("twitter_handle") or c.get("twitter")) else ""
        lines.append(f"  • {c['name']} — {c.get('role', c.get('title', ''))}{email_str}{linkedin_str}{twitter_str}")
    return "\n".join(lines)


AGENT_TOOLS = [
    get_pipeline_results, get_outreach_draft,
    get_pipeline_summary, run_market_monitor,
    get_contacts,
]

# ── LangChain agent setup ─────────────────────────────────────────────────────

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
SYSTEM_PROMPT = """You are the Discovery Pipeline Agent — GTM AI assistant for Web3 security sales.
We sell smart contract security: reviews, competitions, bug bounties, and monitoring.
Be concise. Show full outreach messages when asked. Use tools proactively."""


def _build_executor() -> AgentExecutor:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    llm = ChatAnthropic(model=MODEL, api_key=api_key, temperature=0, streaming=True)
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        MessagesPlaceholder("chat_history"),
        ("human", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])
    agent = create_tool_calling_agent(llm, AGENT_TOOLS, prompt)
    return AgentExecutor(agent=agent, tools=AGENT_TOOLS, verbose=False, return_intermediate_steps=True)


_executor: AgentExecutor = None

def get_executor() -> AgentExecutor:
    global _executor
    if _executor is None:
        _executor = _build_executor()
    return _executor


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Discovery Pipeline API")

# ── Deployment config ─────────────────────────────────────────────────────────
# CORS_ORIGINS is a comma-separated allowlist. Local dev needs nothing; a hosted
# deploy sets it to the Vercel URL (plus preview domains if you use them).
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
CORS_ORIGINS = _origins or ["http://localhost:3000"]

# A pipeline run costs real money (Claude calls) and can send email, so it is off
# by default on a public deploy. Set ALLOW_PIPELINE_RUN=true to enable it, and
# API_SECRET to require a shared secret header on top.
ALLOW_PIPELINE_RUN = os.getenv("ALLOW_PIPELINE_RUN", "true").strip().lower() in ("1", "true", "yes")
API_SECRET = os.getenv("API_SECRET", "").strip()

logger.info("CORS origins: %s", CORS_ORIGINS)
logger.info("Pipeline runs allowed: %s | shared secret required: %s",
            ALLOW_PIPELINE_RUN, bool(API_SECRET))

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _check_secret(provided: str):
    """Reject a request when API_SECRET is configured and the caller did not match it."""
    if API_SECRET and (provided or "").strip() != API_SECRET:
        raise HTTPException(
            status_code=401,
            detail="This endpoint requires the API secret. The hosted demo serves saved "
                   "results; running the pipeline live is restricted.",
        )


@app.on_event("startup")
async def _hydrate_on_boot():
    """
    Populate in-memory state from disk (or the committed seed) at startup.

    A hosted deploy restarts on every push and, on a free tier, whenever it wakes
    from sleep. Without this the first visitor would see an empty dashboard until
    they thought to press "Load Last Results".
    """
    try:
        data = load_leads_and_contacts()
        if not data["leads"]:
            logger.info("Startup: no saved leads to hydrate")
            return
        for lead in data["leads"]:
            f = derive_factor_scores(lead)
            _state.scored_leads.append(ScoredLead(
                protocol_name=lead["protocol_name"],
                tvl_score=f["tvl_score"],
                audit_status_score=f["audit_status_score"],
                velocity_score=f["velocity_score"],
                funding_score=f["funding_score"],
                reachability_score=f["reachability_score"],
                composite_score=lead["composite_score"],
                score_tier=lead["score_tier"],
                scoring_rationale=lead["scoring_rationale"],
                model_version="stored",
            ))
            _state.enrichment_map[lead["protocol_name"]] = {
                "tvl_usd":           lead.get("tvl_usd", 0),
                "category":          lead.get("category", ""),
                "shipping_velocity": lead.get("shipping_velocity", ""),
                "ai_tool_signals":   [s for s in (lead.get("ai_signals") or "").split(", ") if s],
                "contacts":          data["contacts"].get(lead["protocol_name"], []),
            }
        _state.outreach_drafts = _rehydrate_drafts()
        _state.last_run = data["last_run"]
        logger.info("Startup: hydrated %d leads and %d drafts",
                    len(_state.scored_leads), len(_state.outreach_drafts))
    except Exception as e:
        logger.error("Startup hydration failed: %s", e)


@app.get("/api/health")
async def health():
    """Liveness probe, and a quick way to see how a deploy is configured."""
    from src.store.json_store import load_state
    state = load_state()
    return {
        "status": "ok",
        "leads": len(state["leads"]),
        "drafts": len(state["drafts"]),
        "last_run": state["last_run"],
        "pipeline_runs_allowed": ALLOW_PIPELINE_RUN,
        "secret_required": bool(API_SECRET),
        "claude_configured": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        "email_configured": bool(os.getenv("RESEND_API_KEY", "").strip()),
    }


# ── Request/response models ───────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """Stream chat response token by token via SSE."""
    logger.info("POST /api/chat/stream — message: %s", req.message[:120])

    async def generate():
        executor = get_executor()
        full_response = ""
        tool_calls_result = []

        try:
            async for event in executor.astream_events(
                {"input": req.message, "chat_history": _chat_history},
                version="v2",
            ):
                kind = event["event"]

                if kind == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    content = chunk.content
                    token = ""
                    if isinstance(content, str):
                        token = content
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                token += block.get("text", "")
                    if token:
                        token = token.replace("**", "")
                        full_response += token
                        yield f"data: {json.dumps({'type': 'token', 'text': token})}\n\n"

                elif kind == "on_llm_end":
                    try:
                        output = event["data"].get("output", {})
                        usage = getattr(output, "usage_metadata", None) or {}
                        if not usage:
                            # also check generations list
                            for gens in getattr(output, "generations", []):
                                for g in gens:
                                    usage = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                                    if usage:
                                        break
                                if usage:
                                    break
                        if usage:
                            inp_t = usage.get("input_tokens", 0)
                            out_t = usage.get("output_tokens", 0)
                            token_tracker.record(inp_t, out_t)
                            logger.debug("chat_stream tokens: in=%d out=%d", inp_t, out_t)
                    except Exception as te:
                        logger.debug("chat_stream token parse failed: %s", te)

                elif kind == "on_tool_end":
                    name = event.get("name", "")
                    inp = event["data"].get("input", {})
                    tool_calls_result.append({"tool": name, "input": inp})

        except Exception as e:
            logger.error("chat_stream error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"
            return

        _chat_history.append(HumanMessage(content=req.message))
        _chat_history.append(AIMessage(content=full_response))
        logger.info("POST /api/chat/stream — tools: %s | chars: %d",
                    [tc["tool"] for tc in tool_calls_result], len(full_response))

        refresh_tools = {"run_pipeline", "get_pipeline_results"}
        should_refresh = any(tc["tool"] in refresh_tools for tc in tool_calls_result)
        yield f"data: {json.dumps({'type': 'done', 'tool_calls': tool_calls_result, 'refresh': should_refresh})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/leads")
async def get_leads():
    logger.info("GET /api/leads — returning %d leads", len(_state.scored_leads))
    return {
        "leads": [
            {
                "protocol":   l.protocol_name,
                "score":      l.composite_score,
                "tier":       l.score_tier,
                "tvl_score":  l.tvl_score,
                "audit_score": l.audit_status_score,
                "vel_score":  l.velocity_score,
                "fund_score": l.funding_score,
                "reach_score": l.reachability_score,
                "rationale":  l.scoring_rationale,
                "contacts":   _state.enrichment_map.get(l.protocol_name, {}).get("contacts", []),
            }
            for l in sorted(_state.scored_leads, key=lambda x: x.composite_score, reverse=True)
        ],
        "last_run": _state.last_run,
    }


@app.get("/api/summary")
async def summary():
    logger.info("GET /api/summary")
    hot  = len([l for l in _state.scored_leads if l.score_tier == "hot"])
    warm = len([l for l in _state.scored_leads if l.score_tier == "warm"])
    return {
        "total":    len(_state.scored_leads),
        "hot":      hot,
        "warm":     warm,
        "drafts":   len(_state.outreach_drafts),
        "last_run": _state.last_run,
    }


def _draft_to_dict(d: OutreachDraft) -> dict:
    return {
        "protocol":       d.protocol_name,
        "persona":        d.persona_name,
        "role":           d.persona_role,
        "channel":        d.channel,
        "step":           d.sequence_step,
        "subject":        d.subject_line,
        "body":           d.message_body,
        "model":          d.llm_model,
        "contact_email":  d.contact_email,
        "contact_twitter": d.contact_twitter,
        "contact_github": d.contact_github,
        "contact_source": d.contact_source,
    }


@app.get("/api/leads/{protocol}/draft")
async def get_draft(protocol: str):
    """Returns the first draft for a protocol (legacy compat)."""
    logger.info("GET /api/leads/%s/draft", protocol)
    draft = _state.find_draft(protocol)
    if not draft:
        logger.warning("GET /api/leads/%s/draft — not found", protocol)
        raise HTTPException(status_code=404, detail=f"No draft for '{protocol}'")
    return _draft_to_dict(draft)


@app.get("/api/leads/{protocol}/drafts")
async def get_all_drafts(protocol: str):
    """Returns ALL per-person drafts for a protocol."""
    logger.info("GET /api/leads/%s/drafts", protocol)
    name_l = protocol.lower()
    # Fall back to disk so drafts survive a server reload
    pool = _state.outreach_drafts or _rehydrate_drafts()
    drafts = [d for d in pool if d.protocol_name.lower() == name_l]
    if not drafts:
        # fuzzy match
        drafts = [d for d in pool if name_l in d.protocol_name.lower()]
    if not drafts:
        raise HTTPException(status_code=404, detail=f"No drafts for '{protocol}'")
    logger.info("GET /api/leads/%s/drafts — returning %d drafts", protocol, len(drafts))
    return {"protocol": protocol, "drafts": [_draft_to_dict(d) for d in drafts]}


@app.post("/api/pipeline/load")
async def pipeline_load():
    logger.info("POST /api/pipeline/load")
    db_data = load_leads_and_contacts()

    if not db_data["leads"]:
        logger.info("POST /api/pipeline/load — data/state.json is empty")
        return {"loaded": False, "total": 0, "hot": 0, "warm": 0, "drafts": 0, "last_run": None}

    # Rebuild _state from the JSON store
    _state.scored_leads.clear()
    _state.enrichment_map.clear()

    for lead in db_data["leads"]:
        f = derive_factor_scores(lead)
        _state.scored_leads.append(ScoredLead(
            protocol_name=lead["protocol_name"],
            tvl_score=f["tvl_score"],
            audit_status_score=f["audit_status_score"],
            velocity_score=f["velocity_score"],
            funding_score=f["funding_score"],
            reachability_score=f["reachability_score"],
            composite_score=lead["composite_score"],
            score_tier=lead["score_tier"],
            scoring_rationale=lead["scoring_rationale"],
            model_version="db",
        ))
        _state.enrichment_map[lead["protocol_name"]] = {
            "tvl_usd":          lead["tvl_usd"],
            "category":         lead["category"],
            "shipping_velocity":lead["shipping_velocity"],
            "ai_tool_signals":  [s for s in lead["ai_signals"].split(", ") if s] if lead["ai_signals"] else [],
            "contacts":         db_data["contacts"].get(lead["protocol_name"], []),
        }

    # Restore drafts too, so the drawer and its Send button survive a restart
    _state.outreach_drafts = _rehydrate_drafts()

    _state.last_run = db_data["last_run"]
    hot  = len([l for l in _state.scored_leads if l.score_tier == "hot"])
    warm = len([l for l in _state.scored_leads if l.score_tier == "warm"])
    logger.info("POST /api/pipeline/load — loaded total=%d hot=%d warm=%d from DB", len(_state.scored_leads), hot, warm)
    return {
        "loaded":   True,
        "total":    len(_state.scored_leads),
        "hot":      hot,
        "warm":     warm,
        "drafts":   len(_state.outreach_drafts),
        "last_run": _state.last_run,
    }


@app.get("/api/pipeline/run")
async def pipeline_run(test_email: str = "", secret: str = ""):
    """
    SSE endpoint — streams pipeline stdout line by line.

    test_email overrides RESEND_TEST_EMAIL for this run. It arrives as a query
    param because EventSource cannot send a request body. If neither resolves,
    the send step delivers nothing (see email_sender.send_outreach_emails).
    """
    if not ALLOW_PIPELINE_RUN:
        raise HTTPException(
            status_code=403,
            detail="Live pipeline runs are disabled on this deployment. The saved results "
                   "from the last real run are loaded — use Load Last Results.",
        )
    _check_secret(secret)

    logger.info("GET /api/pipeline/run — starting pipeline via SSE stream (test_email=%s)",
                test_email or "<falling back to RESEND_TEST_EMAIL>")
    output_queue: q_module.Queue = q_module.Queue()

    class _QueueWriter:
        def write(self, text):
            sys.__stdout__.write(text)
            if text.strip():
                output_queue.put(text.rstrip())
        def flush(self):
            sys.__stdout__.flush()

    def _run():
        old_stdout = sys.stdout
        sys.stdout = _QueueWriter()
        try:
            _do_pipeline_run(test_email=test_email)
            logger.info("GET /api/pipeline/run — pipeline completed successfully")
        except Exception as e:
            logger.exception("GET /api/pipeline/run — pipeline raised an exception")
            output_queue.put(f"ERROR: {e}")
        finally:
            sys.stdout = old_stdout
            output_queue.put(None)  # sentinel

    threading.Thread(target=_run, daemon=True).start()

    async def event_stream():
        while True:
            try:
                line = output_queue.get_nowait()
                if line is None:
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    break
                yield f"data: {json.dumps({'type': 'log', 'text': line})}\n\n"
            except q_module.Empty:
                await asyncio.sleep(0.05)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class SendDraftRequest(BaseModel):
    protocol_name: str
    persona_name: str = ""
    test_email: str = ""


@app.post("/api/outreach/send")
async def send_one_draft(req: SendDraftRequest):
    """
    Send a single drafted email on demand, from the draft drawer's Send button.

    Goes to the test recipient like every other send — if none resolves, nothing
    is delivered. Unlike a pipeline run this bypasses the already-sent ledger,
    because the operator explicitly asked to send this one draft and it lands in
    the test inbox either way.
    """
    from src.integrations.email_sender import send_outreach_emails

    logger.info("POST /api/outreach/send — %s / %s -> %s",
                req.protocol_name, req.persona_name or "<first>", req.test_email or "<env>")

    name_l = req.protocol_name.lower()
    pool = _state.outreach_drafts or _rehydrate_drafts()
    candidates = [d for d in pool if d.protocol_name.lower() == name_l]
    if not candidates:
        candidates = [d for d in pool if name_l in d.protocol_name.lower()]
    if req.persona_name:
        exact = [d for d in candidates if d.persona_name == req.persona_name]
        candidates = exact or candidates
    if not candidates:
        raise HTTPException(
            status_code=404,
            detail=f"No draft found for '{req.protocol_name}'. Run the pipeline to generate one.",
        )

    draft = candidates[0]
    result = send_outreach_emails([draft], test_email=req.test_email, allow_resend=True)

    if result["status"] == "no_test_recipient":
        raise HTTPException(
            status_code=400,
            detail="No test recipient set. Enter an address in \"Send test emails to:\" "
                   "or set RESEND_TEST_EMAIL in config/.env.",
        )
    if result["status"] == "resend_unavailable":
        raise HTTPException(
            status_code=503,
            detail="Resend not configured — set RESEND_API_KEY in config/.env.",
        )

    row = result["results"][0] if result["results"] else {}
    if row.get("status") != "sent":
        raise HTTPException(status_code=502, detail=row.get("error", "Send failed."))

    # Record it so the outreach history reflects what actually went out
    try:
        from src.store.json_store import save_outreach
        save_outreach(result)
    except Exception as e:
        logger.error("send_one_draft: could not persist outreach record: %s", e)

    return {
        "sent": True,
        "to": row.get("to"),
        "real_recipient": row.get("real_email") or "",
        "protocol": draft.protocol_name,
        "persona": draft.persona_name,
        "id": row.get("id"),
    }


@app.get("/api/outreach/sent")
async def get_sent_outreach():
    """Returns all sent/replied outreach records from data/state.json."""
    try:
        results = list_outreach()
        logger.info("GET /api/outreach/sent — %d records", len(results))
        return {"results": results}
    except Exception as e:
        logger.error("GET /api/outreach/sent failed: %s", e)
        return {"results": []}


class MarkRepliedRequest(BaseModel):
    protocol_name: str
    persona_name: str
    reply_body: str = ""


@app.post("/api/outreach/replied")
async def mark_replied(req: MarkRepliedRequest):
    """
    Mark an outreach record as replied in data/state.json.
    Stores the reply body alongside the original message.
    """
    logger.info("POST /api/outreach/replied - %s / %s", req.protocol_name, req.persona_name)
    updated = store_mark_replied(req.protocol_name, req.persona_name, req.reply_body)
    if not updated:
        raise HTTPException(
            status_code=404,
            detail=f"No outreach record for {req.persona_name} at {req.protocol_name}",
        )
    return {
        "updated": updated,
        "protocol": req.protocol_name,
        "persona": req.persona_name,
    }


@app.post("/api/chat/clear")
async def clear_chat():
    logger.info("POST /api/chat/clear — clearing %d messages from history", len(_chat_history))
    _chat_history.clear()
    return {"cleared": True}


@app.get("/api/tokens")
async def get_token_usage():
    return token_tracker.get()


@app.post("/api/tokens/reset")
async def reset_token_usage():
    token_tracker.reset()
    return {"reset": True}


# ── Pipeline run (used by SSE endpoint) ──────────────────────────────────────

def _do_pipeline_run(test_email: str = ""):
    """
    Run the full live pipeline and update _state.

    test_email is the per-run test inbox from the UI; when empty the send step
    falls back to RESEND_TEST_EMAIL, and if that is empty too it sends nothing.
    """
    logger.info("_do_pipeline_run: starting full live pipeline")
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
    from src.integrations.contacts import find_contacts_for_qualified_leads
    from src.integrations.email_sender import send_outreach_emails
    from src.store.json_store import save_leads, save_contacts, save_outreach, save_drafts
    from src.utils.config import load_config

    _cfg = load_config()
    max_qualified = _cfg.get("discovery", {}).get("max_qualified_leads", 3)
    max_contacts  = _cfg.get("discovery", {}).get("max_contacts_per_protocol", 3)

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
    qualified = sorted(
        [s for s in scored if s.score_tier in ("hot", "warm")],
        key=lambda x: x.composite_score, reverse=True
    )[:max_qualified]

    # Contact enrichment — GitHub contributors + Claude web search
    qualified_names = {q.protocol_name for q in qualified}
    contacts_map = find_contacts_for_qualified_leads(profiles, qualified_names)

    enrichment_map, persona_map = {}, {}
    for p in profiles:
        enrichment_map[p.protocol_name] = {
            "tvl_usd": p.tvl_usd, "category": OVERLAYS.get(p.protocol_name, {}).get("category", "protocol"),
            "chains_deployed": p.chains_deployed or ["Ethereum"],
            "has_been_audited": p.has_been_audited, "audit_providers": p.audit_providers or [],
            "last_audit_date": p.last_audit_date, "bounty_platform": p.bounty_platform,
            "bounty_amount_usd": p.bounty_amount_usd, "shipping_velocity": p.shipping_velocity,
            "ai_tool_signals": p.ai_tool_signals or [], "unaudited_new_code": p.unaudited_new_code,
            "total_raised_usd": p.total_raised_usd, "last_funding_date": p.last_funding_date,
            "warm_intro_available": p.warm_intro_available, "warm_intro_path": p.warm_intro_path,
            "contacts": [
                {
                    "name": c.name, "role": c.role, "email": c.email,
                    "twitter": c.twitter_handle, "github": c.github_username,
                    "source": c.source, "confidence": c.confidence,
                }
                for c in contacts_map.get(p.protocol_name, [])
            ],
        }
        if p.protocol_name in OVERLAYS:
            persona_map[p.protocol_name] = OVERLAYS[p.protocol_name]["persona"]

    # For outreach, use only the top 1 contact per protocol to save tokens
    outreach_contacts_map = {proto: contacts[:1] for proto, contacts in contacts_map.items()}
    outreach = run_outreach_generation(
        qualified, enrichment_map, persona_map,
        contacts_map=outreach_contacts_map, use_llm=True
    )

    # Send emails
    send_results = send_outreach_emails(outreach, test_email=test_email)

    # Persist everything to data/state.json
    save_leads(scored, enrichment_map)
    save_contacts(contacts_map)
    save_outreach(send_results)
    save_drafts(outreach)

    _state.scored_leads    = scored
    _state.outreach_drafts = outreach
    _state.enrichment_map  = enrichment_map
    _state.persona_map     = persona_map
    _state.last_run        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    hot  = len([s for s in scored if s.score_tier == "hot"])
    warm = len([s for s in scored if s.score_tier == "warm"])
    logger.info("_do_pipeline_run: complete — scored=%d hot=%d warm=%d outreach=%d last_run=%s",
                len(scored), hot, warm, len(outreach), _state.last_run)
