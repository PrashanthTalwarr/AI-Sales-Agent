"""
SLACK ALERTS — Sends notifications for hot leads and market events.

Sends to a Slack channel when:
  - A new lead scores 90+ (hot)
  - A market event (exploit, funding) is detected that affects a pipeline target
  - Scoring weights are recalibrated

JD alignment: "monitoring systems that detect market events and trigger contextual outreach"
"""

import os
import json
import logging
import requests
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")


def send_slack_alert(message: dict) -> bool:
    """
    Send a message to Slack via webhook.
    
    Args:
        message: Slack Block Kit payload
    Returns:
        True if sent successfully
    """
    if not SLACK_WEBHOOK_URL:
        logger.debug(f"Slack not configured — skipping: {message.get('text', '')[:80]}")
        print(f"  ⏩ Slack not configured — would send: {message.get('text', '')[:80]}", flush=True)
        return False

    text_preview = message.get("text", "")[:80]
    logger.info(f"Slack webhook: sending — {text_preview}")
    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=message, timeout=10)
        if resp.status_code == 200:
            logger.info("Slack webhook: delivered successfully")
        else:
            logger.warning(f"Slack webhook: non-200 response {resp.status_code} — {resp.text[:120]}")
        return resp.status_code == 200
    except requests.RequestException as e:
        logger.error(f"Slack webhook request failed: {e}")
        print(f"  ✗ Slack error: {e}", flush=True)
        return False


def alert_hot_lead(protocol_name: str, score: float, rationale: str, persona: dict):
    """Alert when a lead scores 90+ (hot tier)."""
    message = {
        "text": f"🔥 Hot Lead: {protocol_name} ({score:.0f}/100)",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🔥 Hot Lead: {protocol_name}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Score:* {score:.0f}/100"},
                    {"type": "mrkdwn", "text": f"*Persona:* {persona.get('name', 'Unknown')} ({persona.get('role', '')})"},
                    {"type": "mrkdwn", "text": f"*Channel:* {persona.get('preferred_channel', 'email')}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Rationale:* {rationale}"}
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View in Pipeline"},
                        "url": "https://discovery-pipeline.internal/leads"
                    }
                ]
            }
        ]
    }
    return send_slack_alert(message)



def alert_outreach_sent(send_results: dict):
    """Alert after emails are sent — one section per company with person details."""
    sent = [r for r in send_results.get("results", []) if r.get("status") == "sent"]
    if not sent:
        return False

    # Group by protocol
    by_protocol: dict = {}
    for r in sent:
        by_protocol.setdefault(r["protocol"], []).append(r)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📧 Outreach Sent — {len(sent)} email(s)"}
        }
    ]

    for protocol, people in by_protocol.items():
        lines = []
        for p in people:
            role = p.get("role", "")
            role_str = f" · {role}" if role else ""
            lines.append(f"• *{p.get('persona', 'Unknown')}*{role_str}")
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{protocol}*\n" + "\n".join(lines)
            }
        })
        blocks.append({"type": "divider"})

    message = {
        "text": f"📧 Outreach sent to {len(by_protocol)} company/companies",
        "blocks": blocks,
    }
    return send_slack_alert(message)


def alert_pipeline_complete(total_scored: int, hot: int, warm: int, outreach: int):
    """Summary alert after pipeline run completes."""
    message = {
        "text": f"🪐 Pipeline Run Complete: {total_scored} scored, {hot} hot, {warm} warm",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🪐 Pipeline Run Complete"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Protocols scored:* {total_scored}"},
                    {"type": "mrkdwn", "text": f"*Hot leads:* {hot}"},
                    {"type": "mrkdwn", "text": f"*Warm leads:* {warm}"},
                    {"type": "mrkdwn", "text": f"*Outreach drafted:* {outreach}"},
                    {"type": "mrkdwn", "text": f"*Time:* {datetime.now().strftime('%Y-%m-%d %H:%M')}"},
                ]
            }
        ]
    }
    return send_slack_alert(message)
