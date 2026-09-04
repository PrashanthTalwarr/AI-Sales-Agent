"""
JSON STORE — File-backed persistence for pipeline results and the send ledger.

Replaces the previous PostgreSQL layer. Two files under data/:

  data/state.json        leads, contacts, and outreach history for the last run(s)
  data/sent_ledger.json  the double-send guard — one entry per (protocol, persona)

Both are written atomically (write to a temp file, then os.replace) so an
interrupted run can never leave a half-written file behind.

The send ledger is deliberately a separate file from the run state: it is the
only thing standing between a re-run and emailing the same person twice, so it
is written immediately after each successful send rather than batched at the
end of a run.
"""

import os
import json
import logging
import tempfile
from datetime import datetime

logger = logging.getLogger(__name__)

# Resolve data/ relative to the project root (this file is src/store/json_store.py)
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(_ROOT, "data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
LEDGER_PATH = os.path.join(DATA_DIR, "sent_ledger.json")
# Committed snapshot used when no run has happened yet (fresh deploy, clean clone)
SEED_PATH = os.path.join(DATA_DIR, "seed_state.json")

_EMPTY_STATE = {"leads": [], "contacts": {}, "outreach": [], "drafts": [], "last_run": None}


# ── Low-level IO ──────────────────────────────────────────────────────────────

def _read_json(path: str, default):
    if not os.path.exists(path):
        return json.loads(json.dumps(default))  # deep copy
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Could not read %s (%s) — treating as empty", path, e)
        return json.loads(json.dumps(default))


def _write_json(path: str, payload) -> bool:
    """Atomic write: temp file in the same dir, then replace."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp, path)
            return True
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except OSError as e:
        logger.error("Could not write %s: %s", path, e)
        return False


def load_state() -> dict:
    """
    Read the whole state file. Always returns the full shape.

    Falls back to the committed seed (data/seed_state.json) when no run has
    happened yet. A hosted deploy starts from an empty, ephemeral disk, so
    without this the API would come up with zero leads until someone triggered
    a run — the seed means a fresh deploy serves real results immediately.
    """
    if not os.path.exists(STATE_PATH) and os.path.exists(SEED_PATH):
        seed = _read_json(SEED_PATH, _EMPTY_STATE)
        for key, empty in _EMPTY_STATE.items():
            seed.setdefault(key, empty)
        logger.info("No state.json — serving seed data (%d leads)", len(seed["leads"]))
        return seed

    state = _read_json(STATE_PATH, _EMPTY_STATE)
    for key, empty in _EMPTY_STATE.items():
        state.setdefault(key, empty)
    return state


def _save_state(state: dict) -> bool:
    return _write_json(STATE_PATH, state)


# ── Leads / contacts / outreach ───────────────────────────────────────────────

def save_leads(scored_leads: list, enrichment_map: dict) -> int:
    """
    Upsert scored leads into state.json, keyed by protocol name.
    Mirrors the old Postgres upsert: existing protocols are updated in place.
    """
    state = load_state()
    existing = {l["protocol_name"]: l for l in state["leads"]}
    now = datetime.now().isoformat(timespec="seconds")

    for lead in scored_leads:
        enr = enrichment_map.get(lead.protocol_name, {})
        ai_signals = enr.get("ai_tool_signals") or []
        existing[lead.protocol_name] = {
            "protocol_name":     lead.protocol_name,
            "tvl_usd":           int(enr.get("tvl_usd", 0) or 0),
            "category":          enr.get("category", ""),
            "chains":            ", ".join(enr.get("chains_deployed", []) or []),
            "composite_score":   float(lead.composite_score),
            "score_tier":        lead.score_tier,
            "audit_status":      "audited" if enr.get("has_been_audited") else "never_audited",
            "audit_providers":   ", ".join(enr.get("audit_providers", []) or []),
            "bounty_platform":   enr.get("bounty_platform", ""),
            "shipping_velocity": enr.get("shipping_velocity", ""),
            "ai_signals":        ", ".join(ai_signals),
            "total_raised_usd":  int(enr.get("total_raised_usd", 0) or 0),
            "last_funding_date": enr.get("last_funding_date") or "",
            "scoring_rationale": lead.scoring_rationale,
            "last_updated":      now,
        }

    state["leads"] = sorted(
        existing.values(), key=lambda l: l["composite_score"], reverse=True
    )
    state["last_run"] = now
    _save_state(state)
    logger.info("state.json: %d leads saved", len(scored_leads))
    print(f"  OK state.json: {len(scored_leads)} leads saved", flush=True)
    return len(scored_leads)


def save_contacts(contacts_map: dict) -> int:
    """Merge contacts into state.json. Existing (protocol, name) pairs are kept."""
    state = load_state()
    saved = 0

    for protocol, contacts in (contacts_map or {}).items():
        bucket = state["contacts"].setdefault(protocol, [])
        seen = {c.get("name", "").lower() for c in bucket}
        for c in contacts:
            name = getattr(c, "name", "") or ""
            if not name or name.lower() in seen:
                continue
            bucket.append({
                "name":            name,
                "role":            getattr(c, "role", "") or "",
                "email":           getattr(c, "email", "") or "",
                "twitter_handle":  getattr(c, "twitter_handle", "") or "",
                "github_username": getattr(c, "github_username", "") or "",
                "source":          getattr(c, "source", "") or "",
                "confidence":      getattr(c, "confidence", "") or "",
            })
            seen.add(name.lower())
            saved += 1

    _save_state(state)
    logger.info("state.json: %d new contacts saved", saved)
    print(f"  OK state.json: {saved} contacts saved", flush=True)
    return saved


def save_outreach(send_results: dict) -> int:
    """
    Append outreach records to state.json. A (protocol, persona) pair that already
    has a record is not overwritten — first outreach history is preserved.
    """
    state = load_state()
    existing = {(o["protocol_name"], o["persona_name"]) for o in state["outreach"]}
    saved = skipped = 0

    for result in (send_results or {}).get("results", []):
        if result.get("status") == "skipped":
            continue
        key = (result.get("protocol", ""), result.get("persona", ""))
        if key in existing:
            skipped += 1
            logger.info("Outreach already recorded for %s / %s — not duplicating", *key)
            continue
        state["outreach"].append({
            "protocol_name": key[0],
            "persona_name":  key[1],
            "persona_role":  result.get("role", ""),
            "to_email":      result.get("to", ""),
            "subject":       result.get("subject", ""),
            "body":          result.get("body", ""),
            "resend_id":     result.get("id", ""),
            "status":        result.get("status", ""),
            "channel":       result.get("channel", "email"),
            "sent_at":       datetime.now().isoformat(timespec="seconds"),
        })
        existing.add(key)
        saved += 1

    _save_state(state)
    logger.info("state.json outreach: %d saved, %d already existed", saved, skipped)
    print(f"  OK state.json: {saved} outreach records saved", flush=True)
    return saved


_DRAFT_FIELDS = (
    "protocol_name", "persona_name", "persona_role", "channel", "sequence_step",
    "subject_line", "message_body", "signals_used", "llm_model",
    "contact_email", "contact_twitter", "contact_github", "contact_source",
)


def save_drafts(drafts: list) -> int:
    """
    Persist generated outreach drafts, replacing any previous set for the same
    protocol. Drafts used to live only in the API's memory, so a server reload
    lost them and the UI's Send button had nothing to send.
    """
    state = load_state()
    touched = {d.protocol_name for d in (drafts or [])}
    kept = [d for d in state["drafts"] if d.get("protocol_name") not in touched]

    for d in drafts or []:
        kept.append({f: getattr(d, f, None) for f in _DRAFT_FIELDS})

    state["drafts"] = kept
    _save_state(state)
    logger.info("state.json: %d drafts saved", len(drafts or []))
    print(f"  OK state.json: {len(drafts or [])} drafts saved", flush=True)
    return len(drafts or [])


def load_drafts() -> list:
    """Return persisted drafts as plain dicts, in save order."""
    return load_state()["drafts"]


def load_leads_and_contacts() -> dict:
    """
    Rehydrate the API's in-memory state from disk.
    Returns {"leads": [...], "contacts": {...}, "last_run": str|None} —
    the same shape the Postgres loader returned.
    """
    state = load_state()
    return {
        "leads":    state["leads"],
        "contacts": state["contacts"],
        "last_run": state["last_run"],
    }


def list_outreach(statuses=("sent", "replied")) -> list:
    """Outreach records filtered by status, newest first, joined with lead score/tier."""
    state = load_state()
    leads = {l["protocol_name"]: l for l in state["leads"]}
    rows = [o for o in state["outreach"] if o.get("status") in statuses]
    rows.sort(key=lambda o: o.get("sent_at") or "", reverse=True)

    out = []
    for o in rows:
        lead = leads.get(o["protocol_name"], {})
        out.append({
            "protocol_name": o["protocol_name"],
            "persona_name":  o["persona_name"],
            "persona_role":  o.get("persona_role", ""),
            "to_email":      o.get("to_email", ""),
            "subject":       o.get("subject", ""),
            "sent_at":       o.get("sent_at"),
            "status":        o.get("status", ""),
            "score":         lead.get("composite_score"),
            "tier":          lead.get("score_tier", ""),
            "tvl_usd":       lead.get("tvl_usd"),
        })
    return out


def mark_replied(protocol_name: str, persona_name: str, reply_body: str = "") -> bool:
    """Flip an outreach record to 'replied' and append the reply body. True if a row matched."""
    state = load_state()
    updated = False

    for o in state["outreach"]:
        if o["protocol_name"] == protocol_name and o["persona_name"] == persona_name:
            o["status"] = "replied"
            o["replied_at"] = datetime.now().isoformat(timespec="seconds")
            if reply_body:
                o["body"] = f"{o.get('body', '')}\n\n--- REPLY ---\n{reply_body}"
                o["reply_body"] = reply_body
            updated = True

    if updated:
        _save_state(state)
        logger.info("state.json: marked %s / %s as replied", protocol_name, persona_name)
    else:
        logger.warning("state.json: no outreach record for %s / %s", protocol_name, persona_name)
    return updated


# ── Send ledger (double-send guard) ───────────────────────────────────────────

def _ledger_key(protocol_name: str, persona_name: str) -> str:
    return f"{protocol_name}||{persona_name}"


def load_ledger() -> dict:
    return _read_json(LEDGER_PATH, {})


def already_sent(protocol_name: str, persona_name: str) -> bool:
    """True if this person at this protocol has been emailed in any previous run."""
    return _ledger_key(protocol_name, persona_name) in load_ledger()


def record_sent(protocol_name: str, persona_name: str, to_email: str, email_id: str = "") -> bool:
    """
    Write one send to the ledger immediately. Called right after a successful
    send so an interrupted run cannot re-send on the next attempt.
    """
    ledger = load_ledger()
    ledger[_ledger_key(protocol_name, persona_name)] = {
        "protocol_name": protocol_name,
        "persona_name":  persona_name,
        "to_email":      to_email,
        "email_id":      email_id,
        "sent_at":       datetime.now().isoformat(timespec="seconds"),
    }
    return _write_json(LEDGER_PATH, ledger)
