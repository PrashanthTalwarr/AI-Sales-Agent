"""
EMAIL SENDER — Sends drafted outreach emails via Resend.

Two safety gates sit in front of every send, both fail-closed:

  1. TEST RECIPIENT. Every email is redirected to a single test inbox. If no
     test recipient resolves (neither the per-run argument nor RESEND_TEST_EMAIL),
     nothing is sent at all — we never fall through to the real discovered
     contacts. Sending a cold email to a real prospect by accident is not a
     recoverable mistake.

  2. MAX_EMAILS. A hard ceiling on how many emails one run may deliver,
     enforced here in the send loop rather than at qualification, so no caller
     can route around it. Only real sends count against the budget; drafts
     skipped for other reasons do not consume it.

Drafts held back by the cap are reported as skipped with an explicit reason —
never silently dropped — and are NOT written to the send ledger, so a later run
can still deliver them.
"""

import os
import logging
import time

from src.utils.config import load_config

logger = logging.getLogger(__name__)

_DEFAULT_MAX_EMAILS = 5


def _resolve_max_emails(explicit: int = None) -> int:
    """Precedence: explicit argument > MAX_EMAILS env var > config JSON > 5."""
    if explicit is not None:
        return int(explicit)

    env = os.getenv("MAX_EMAILS", "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            logger.warning("MAX_EMAILS=%r is not an integer — ignoring", env)

    cfg = load_config().get("discovery", {})
    return int(cfg.get("max_emails_per_run", _DEFAULT_MAX_EMAILS))


def _resolve_test_recipient(explicit: str = None) -> str:
    """Precedence: explicit (per-run, from the UI) > RESEND_TEST_EMAIL. May be empty."""
    if (explicit or "").strip():
        return explicit.strip()
    return os.getenv("RESEND_TEST_EMAIL", "").strip()


def _already_sent(protocol_name: str, persona_name: str) -> bool:
    """Check the on-disk send ledger — True if this person was emailed in a previous run."""
    try:
        from src.store.json_store import already_sent
        return already_sent(protocol_name, persona_name)
    except Exception as e:
        # Fail closed: if the ledger can't be read we do not know whether this
        # person was already emailed, and a duplicate cold email is worse than
        # a skipped one.
        logger.error("Send ledger unreadable (%s) — skipping send to be safe", e)
        return True


def _get_resend_client():
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        import resend
        resend.api_key = api_key
        return resend
    except ImportError:
        logger.error("resend package not installed — run: pip install resend")
        return None


def _test_mode_body(draft, real_email: str) -> str:
    """
    Prepend a banner naming the person this email was actually written for.

    The subject carries the same information, but mail clients truncate subjects
    and the body always survives — so the intended recipient stays visible.
    """
    who = draft.persona_name or "unknown"
    role = f" — {draft.persona_role}" if draft.persona_role else ""
    addr = real_email or "no email address found"
    return (
        "[TEST MODE] This email would have been sent to:\n"
        f"  {who} <{addr}>{role} at {draft.protocol_name}\n"
        + "-" * 58
        + "\n\n"
        + draft.message_body
    )


def send_outreach_emails(drafts: list, test_email: str = None, max_emails: int = None,
                         allow_resend: bool = False) -> dict:
    """
    Send drafted emails via Resend, redirected to a single test recipient.

    Args:
        drafts:      OutreachDraft objects to send.
        test_email:  Per-run override for the test inbox (from the UI). When
                     omitted, RESEND_TEST_EMAIL is used. If neither resolves,
                     NOTHING is sent.
        max_emails:  Hard ceiling for this run. Defaults to MAX_EMAILS env var,
                     then discovery.max_emails_per_run in config, then 5.
        allow_resend: Skip the already-sent ledger check. Only for the manual
                     "send test" button in the UI, where the operator has
                     explicitly asked to re-send one specific draft. Everything
                     still goes to the test inbox, so a repeat is harmless. The
                     automated pipeline never sets this.

    Returns a summary dict:
      {
        "sent": int,
        "skipped_no_email": int,
        "capped": int,
        "failed": int,
        "status": "ok" | "no_test_recipient" | "resend_unavailable",
        "test_recipient": str,
        "max_emails": int,
        "results": [{"protocol", "persona", "to", "status", "reason"|"id", ...}]
      }
    """
    recipient = _resolve_test_recipient(test_email)
    limit = _resolve_max_emails(max_emails)

    summary = {
        "sent": 0,
        "skipped_no_email": 0,
        "capped": 0,
        "failed": 0,
        "status": "ok",
        "test_recipient": recipient,
        "max_emails": limit,
        "results": [],
    }

    print("\n" + "=" * 60, flush=True)
    print("EMAIL SEND — Sending via Resend", flush=True)
    print("=" * 60 + "\n", flush=True)

    # ── Gate 1: fail closed when there is no test recipient ───────────────────
    if not recipient:
        summary["status"] = "no_test_recipient"
        for draft in drafts:
            summary["results"].append({
                "protocol": draft.protocol_name,
                "persona": draft.persona_name,
                "to": None,
                "status": "skipped",
                "reason": "no test recipient set",
            })
        print("  !! NO TEST RECIPIENT SET — nothing sent.", flush=True)
        print("     Set the \"Send test emails to\" field in the UI, or RESEND_TEST_EMAIL", flush=True)
        print("     in config/.env. Emails are never sent to real discovered contacts.", flush=True)
        print(f"\n  Sent: 0 | Held back: {len(drafts)} (no test recipient)", flush=True)
        logger.warning("No test recipient resolved — %d draft(s) not sent", len(drafts))
        return summary

    resend = _get_resend_client()
    if not resend:
        summary["status"] = "resend_unavailable"
        print("  x Resend unavailable — RESEND_API_KEY missing or package not installed", flush=True)
        logger.warning("Resend skipped — API key missing or package not installed")
        return summary

    print(f"  [TEST MODE] All emails redirected to {recipient}", flush=True)
    print(f"  [CAP] Max {limit} email(s) this run\n", flush=True)
    logger.info("Resend test mode — all emails -> %s (cap %d)", recipient, limit)

    skipped_already_sent = 0
    for draft in drafts:
        # ── Gate 2: hard cap. Only real sends consume the budget. ─────────────
        if summary["sent"] >= limit:
            summary["capped"] += 1
            summary["results"].append({
                "protocol": draft.protocol_name,
                "persona": draft.persona_name,
                "to": None,
                "status": "skipped",
                "reason": "max_emails cap reached",
            })
            print(f"  ~ CAP reached ({limit}) — holding back {draft.persona_name} @ {draft.protocol_name}", flush=True)
            logger.info("Cap %d reached — not sending to %s / %s",
                        limit, draft.protocol_name, draft.persona_name)
            continue

        # Skip if already sent to this person in a previous run
        if not allow_resend and _already_sent(draft.protocol_name, draft.persona_name):
            skipped_already_sent += 1
            summary["results"].append({
                "protocol": draft.protocol_name,
                "persona": draft.persona_name,
                "to": None,
                "status": "skipped",
                "reason": "already sent in previous run",
            })
            print(f"  ~ Already sent to {draft.persona_name} @ {draft.protocol_name} — skipping", flush=True)
            logger.info("Skipped %s (%s) — already sent in previous run", draft.protocol_name, draft.persona_name)
            continue

        real_email = (getattr(draft, "contact_email", "") or "").strip()

        # Everything goes to the test inbox, including drafts with no real address
        to_email = recipient

        if real_email:
            subject = f"[TEST -> {real_email}] {draft.subject_line}"
        else:
            subject = f"[TEST -> no real email] {draft.subject_line}"
        body = _test_mode_body(draft, real_email)

        try:
            response = resend.Emails.send({
                "from": os.getenv("RESEND_FROM_EMAIL", "outreach@example.com").strip(),
                "to": [to_email],
                "subject": subject,
                "text": body,
            })

            email_id = response.get("id", "unknown") if isinstance(response, dict) else getattr(response, "id", "unknown")
            summary["sent"] += 1
            summary["results"].append({
                "protocol": draft.protocol_name,
                "persona": draft.persona_name,
                "role": draft.persona_role,
                "to": to_email,
                "real_email": real_email,
                "subject": draft.subject_line,
                "body": body,
                "channel": draft.channel,
                "status": "sent",
                "id": email_id,
            })
            # Record immediately — an interrupted run must not re-send on retry
            try:
                from src.store.json_store import record_sent
                record_sent(draft.protocol_name, draft.persona_name, to_email, str(email_id))
            except Exception as le:
                logger.error("Failed to write send ledger for %s / %s: %s",
                             draft.protocol_name, draft.persona_name, le)

            dest = f"{to_email} (real: {real_email})" if real_email else to_email
            print(f"  OK Sent -> {dest} ({draft.protocol_name} / {draft.persona_name})", flush=True)
            logger.info("Email sent: %s -> %s | id=%s | subject=%s",
                        draft.protocol_name, to_email, email_id, draft.subject_line)

            # Small delay to stay within Resend rate limits
            time.sleep(0.3)

        except Exception as e:
            summary["failed"] += 1
            summary["results"].append({
                "protocol": draft.protocol_name,
                "persona": draft.persona_name,
                "to": to_email,
                "status": "failed",
                "error": str(e),
            })
            print(f"  FAIL -> {to_email} ({draft.protocol_name}): {e}", flush=True)
            logger.error("Email failed: %s -> %s | %s", draft.protocol_name, to_email, e)

    print(
        f"\n  Sent: {summary['sent']}/{limit} | Already sent: {skipped_already_sent} | "
        f"Capped: {summary['capped']} | Skipped (no email): {summary['skipped_no_email']} | "
        f"Failed: {summary['failed']}",
        flush=True
    )
    if summary["capped"]:
        print(f"  NOTE: {summary['capped']} draft(s) held back by the {limit}-email cap "
              f"— they were not recorded as sent and can go out on a later run.", flush=True)
    return summary
