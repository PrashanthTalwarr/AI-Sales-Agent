"""
EMAIL SENDER — Sends drafted outreach emails via Resend.

Only sends to drafts that have a contact_email set.
Skips drafts without an email (Twitter DM candidates).
Logs every send attempt — success and failure.
"""

import os
import logging
import time

logger = logging.getLogger(__name__)


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


def send_outreach_emails(drafts: list) -> dict:
    """
    Send drafted emails via Resend for any draft that has a contact_email.

    Returns a summary dict:
      {
        "sent": int,
        "skipped_no_email": int,
        "failed": int,
        "results": [{"protocol": str, "to": str, "status": "sent"|"failed"|"skipped", "id": str}]
      }
    """
    resend = _get_resend_client()
    from_email = os.getenv("RESEND_FROM_EMAIL", "outreach@example.com").strip()
    test_email = os.getenv("RESEND_TEST_EMAIL", "").strip()

    if test_email:
        print(f"\n  [TEST MODE] All emails redirected to {test_email}", flush=True)
        logger.info("Resend test mode — all emails → %s", test_email)

    summary = {"sent": 0, "skipped_no_email": 0, "failed": 0, "results": []}

    print("\n" + "=" * 60, flush=True)
    print("EMAIL SEND — Sending via Resend", flush=True)
    print("=" * 60 + "\n", flush=True)

    if not resend:
        print("  ✗ Resend unavailable — RESEND_API_KEY missing or package not installed", flush=True)
        logger.warning("Resend skipped — API key missing or package not installed")
        return summary

    skipped_already_sent = 0
    for draft in drafts:
        # Skip if already sent to this person in a previous run
        if _already_sent(draft.protocol_name, draft.persona_name):
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

        # In test mode, send to override even if no real contact email exists
        to_email = test_email if test_email else real_email

        if not to_email:
            summary["skipped_no_email"] += 1
            summary["results"].append({
                "protocol": draft.protocol_name,
                "persona": draft.persona_name,
                "to": None,
                "status": "skipped",
                "reason": "no email address",
            })
            logger.info("Skipped %s (%s) — no email address", draft.protocol_name, draft.persona_name)
            continue

        subject = draft.subject_line
        if test_email and real_email:
            subject = f"[TEST → {real_email}] {draft.subject_line}"
        elif test_email:
            subject = f"[TEST → no real email] {draft.subject_line}"

        try:
            response = resend.Emails.send({
                "from": from_email,
                "to": [to_email],
                "subject": subject,
                "text": draft.message_body,
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
                "body": draft.message_body,
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

            dest = f"{to_email} (real: {real_email})" if test_email and real_email else to_email
            print(f"  OK Sent -> {dest} ({draft.protocol_name} / {draft.persona_name})", flush=True)
            logger.info("Email sent: %s → %s | id=%s | subject=%s", draft.protocol_name, to_email, email_id, draft.subject_line)

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
            logger.error("Email failed: %s → %s | %s", draft.protocol_name, to_email, e)

    print(
        f"\n  Sent: {summary['sent']} | Already sent: {skipped_already_sent} | Skipped (no email): {summary['skipped_no_email']} | Failed: {summary['failed']}",
        flush=True
    )
    return summary
