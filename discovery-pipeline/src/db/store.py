"""
DATABASE STORE — Saves all pipeline results to PostgreSQL.

Tables:
  leads    — one row per protocol, updated each run (TVL/score change over time)
  contacts — one row per person per protocol, never overwritten (dedup by protocol+name)
  outreach — one row per person per protocol, never duplicated (dedup by protocol+persona)
"""

import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _get_conn():
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return None
    try:
        import psycopg2
        return psycopg2.connect(url)
    except Exception as e:
        logger.error("PostgreSQL connection failed: %s", e)
        return None


def ensure_schema():
    """Create tables and constraints. Migrates old constraints if needed."""
    conn = _get_conn()
    if not conn:
        logger.warning("PostgreSQL not configured — skipping schema setup")
        return False
    try:
        with conn:
            with conn.cursor() as cur:

                # LEADS — one row per protocol, updated each run
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS leads (
                        id                  SERIAL PRIMARY KEY,
                        first_seen          TIMESTAMP NOT NULL DEFAULT NOW(),
                        last_updated        TIMESTAMP NOT NULL DEFAULT NOW(),
                        protocol_name       TEXT NOT NULL UNIQUE,
                        tvl_usd             BIGINT,
                        category            TEXT,
                        chains              TEXT,
                        composite_score     NUMERIC(5,1),
                        score_tier          TEXT,
                        audit_status        TEXT,
                        audit_providers     TEXT,
                        bounty_platform     TEXT,
                        bounty_amount_usd   BIGINT,
                        shipping_velocity   TEXT,
                        ai_signals          TEXT,
                        total_raised_usd    BIGINT,
                        last_funding_date   TEXT,
                        scoring_rationale   TEXT
                    )
                """)

                # CONTACTS — one row per person per protocol, never overwritten
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS contacts (
                        id              SERIAL PRIMARY KEY,
                        first_seen      TIMESTAMP NOT NULL DEFAULT NOW(),
                        protocol_name   TEXT NOT NULL REFERENCES leads(protocol_name) ON UPDATE CASCADE ON DELETE CASCADE,
                        name            TEXT,
                        role            TEXT,
                        email           TEXT,
                        twitter_handle  TEXT,
                        github_username TEXT,
                        phone           TEXT,
                        source          TEXT,
                        confidence      TEXT,
                        UNIQUE (protocol_name, name)
                    )
                """)

                # OUTREACH — one row per person per protocol, never duplicated
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS outreach (
                        id              SERIAL PRIMARY KEY,
                        sent_at         TIMESTAMP NOT NULL DEFAULT NOW(),
                        protocol_name   TEXT NOT NULL REFERENCES leads(protocol_name) ON UPDATE CASCADE ON DELETE CASCADE,
                        persona_name    TEXT,
                        persona_role    TEXT,
                        to_email        TEXT,
                        subject         TEXT,
                        body            TEXT,
                        resend_id       TEXT,
                        status          TEXT,
                        channel         TEXT,
                        UNIQUE (protocol_name, persona_name)
                    )
                """)

                # Migrate: drop old constraints and add FK if missing
                cur.execute("""
                    DO $$ BEGIN
                        ALTER TABLE leads DROP CONSTRAINT IF EXISTS leads_run_at_protocol_name_key;
                        ALTER TABLE contacts DROP CONSTRAINT IF EXISTS contacts_run_at_protocol_name_name_key;

                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.table_constraints
                            WHERE table_name='contacts' AND constraint_type='FOREIGN KEY'
                            AND constraint_name='contacts_protocol_name_fkey'
                        ) THEN
                            ALTER TABLE contacts
                                ADD CONSTRAINT contacts_protocol_name_fkey
                                FOREIGN KEY (protocol_name) REFERENCES leads(protocol_name)
                                ON UPDATE CASCADE ON DELETE CASCADE;
                        END IF;

                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.table_constraints
                            WHERE table_name='outreach' AND constraint_type='FOREIGN KEY'
                            AND constraint_name='outreach_protocol_name_fkey'
                        ) THEN
                            ALTER TABLE outreach
                                ADD CONSTRAINT outreach_protocol_name_fkey
                                FOREIGN KEY (protocol_name) REFERENCES leads(protocol_name)
                                ON UPDATE CASCADE ON DELETE CASCADE;
                        END IF;
                    EXCEPTION WHEN others THEN NULL;
                    END $$;
                """)

        logger.info("PostgreSQL schema ready")
        return True
    except Exception as e:
        logger.error("Schema creation failed: %s", e)
        return False
    finally:
        conn.close()


def load_leads_from_db() -> dict:
    """
    Read all leads, contacts, and last outreach timestamp from PostgreSQL.
    Returns a dict with keys: leads, contacts, last_run.
    Returns empty structure if DB unavailable or empty.
    """
    conn = _get_conn()
    if not conn:
        return {"leads": [], "contacts": {}, "last_run": None}

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT protocol_name, tvl_usd, category, composite_score,
                           score_tier, audit_status, shipping_velocity,
                           ai_signals, total_raised_usd, scoring_rationale,
                           last_updated
                    FROM leads
                    ORDER BY composite_score DESC NULLS LAST
                """)
                rows = cur.fetchall()
                leads = [
                    {
                        "protocol_name":    r[0],
                        "tvl_usd":          r[1] or 0,
                        "category":         r[2] or "",
                        "composite_score":  float(r[3] or 0),
                        "score_tier":       r[4] or "cool",
                        "audit_status":     r[5] or "",
                        "shipping_velocity":r[6] or "",
                        "ai_signals":       r[7] or "",
                        "total_raised_usd": r[8] or 0,
                        "scoring_rationale":r[9] or "",
                        "last_updated":     r[10].isoformat() if r[10] else None,
                    }
                    for r in rows
                ]

                cur.execute("""
                    SELECT protocol_name, name, role, email, twitter_handle,
                           github_username, source, confidence
                    FROM contacts
                """)
                contacts: dict = {}
                for r in cur.fetchall():
                    proto = r[0]
                    contacts.setdefault(proto, []).append({
                        "name":            r[1] or "",
                        "role":            r[2] or "",
                        "email":           r[3] or "",
                        "twitter_handle":  r[4] or "",
                        "github_username": r[5] or "",
                        "source":          r[6] or "",
                        "confidence":      r[7] or "",
                    })

                cur.execute("SELECT MAX(last_updated) FROM leads")
                last_run_row = cur.fetchone()
                last_run = last_run_row[0].isoformat() if last_run_row and last_run_row[0] else None

        return {"leads": leads, "contacts": contacts, "last_run": last_run}
    except Exception as e:
        logger.error("load_leads_from_db failed: %s", e)
        return {"leads": [], "contacts": {}, "last_run": None}
    finally:
        conn.close()


def save_leads(scored_leads: list, enrichment_map: dict) -> int:
    """
    Upsert all scored leads. Updates TVL/score if protocol already exists.
    Preserves first_seen timestamp.
    """
    conn = _get_conn()
    if not conn:
        return 0

    saved = updated = 0
    try:
        with conn:
            with conn.cursor() as cur:
                for lead in scored_leads:
                    enrichment = enrichment_map.get(lead.protocol_name, {})
                    try:
                        cur.execute("""
                            INSERT INTO leads (
                                protocol_name, tvl_usd, category, chains,
                                composite_score, score_tier, audit_status, audit_providers,
                                bounty_platform, bounty_amount_usd, shipping_velocity,
                                ai_signals, total_raised_usd, last_funding_date, scoring_rationale,
                                last_updated
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                            ON CONFLICT (protocol_name) DO UPDATE SET
                                tvl_usd           = EXCLUDED.tvl_usd,
                                composite_score   = EXCLUDED.composite_score,
                                score_tier        = EXCLUDED.score_tier,
                                shipping_velocity = EXCLUDED.shipping_velocity,
                                ai_signals        = EXCLUDED.ai_signals,
                                total_raised_usd  = EXCLUDED.total_raised_usd,
                                last_updated      = NOW()
                        """, (
                            lead.protocol_name,
                            int(enrichment.get("tvl_usd", 0) or 0),
                            enrichment.get("category", ""),
                            ", ".join(enrichment.get("chains_deployed", []) or []),
                            lead.composite_score,
                            lead.score_tier,
                            "audited" if enrichment.get("has_been_audited") else "not_audited",
                            ", ".join(enrichment.get("audit_providers", []) or []),
                            enrichment.get("bounty_platform", "none"),
                            int(enrichment.get("bounty_amount_usd", 0) or 0),
                            enrichment.get("shipping_velocity", ""),
                            ", ".join(enrichment.get("ai_tool_signals", []) or []),
                            int(enrichment.get("total_raised_usd", 0) or 0),
                            enrichment.get("last_funding_date", ""),
                            getattr(lead, "scoring_rationale", ""),
                        ))
                        saved += 1
                    except Exception as e:
                        logger.warning("Failed to save lead %s: %s", lead.protocol_name, e)
        logger.info("PostgreSQL leads: %d upserted", saved)
        return saved
    except Exception as e:
        logger.error("save_leads failed: %s", e)
        return 0
    finally:
        conn.close()


def save_contacts(contacts_map: dict) -> int:
    """
    Insert contacts. Skips silently if same person at same protocol already exists.
    """
    conn = _get_conn()
    if not conn:
        return 0

    saved = skipped = 0
    try:
        with conn:
            with conn.cursor() as cur:
                for protocol_name, contacts in contacts_map.items():
                    for c in contacts:
                        name = getattr(c, "name", "") or ""
                        try:
                            cur.execute("""
                                INSERT INTO contacts (
                                    protocol_name, name, role,
                                    email, twitter_handle, github_username,
                                    phone, source, confidence
                                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                ON CONFLICT (protocol_name, name) DO NOTHING
                            """, (
                                protocol_name, name,
                                getattr(c, "role", "") or "",
                                getattr(c, "email", "") or "",
                                getattr(c, "twitter_handle", "") or "",
                                getattr(c, "github_username", "") or "",
                                getattr(c, "phone", "") or "",
                                getattr(c, "source", "") or "",
                                getattr(c, "confidence", "") or "",
                            ))
                            if cur.rowcount:
                                saved += 1
                            else:
                                skipped += 1
                                logger.debug("Contact already exists: %s / %s — skipped", protocol_name, name)
                        except Exception as e:
                            logger.warning("Failed to save contact %s/%s: %s", protocol_name, name, e)
        logger.info("PostgreSQL contacts: %d saved, %d already existed", saved, skipped)
        return saved
    except Exception as e:
        logger.error("save_contacts failed: %s", e)
        return 0
    finally:
        conn.close()


def save_outreach(send_results: dict) -> int:
    """
    Insert outreach records. If same person at same protocol already has a record,
    logs it and skips — we don't overwrite first outreach history.
    """
    conn = _get_conn()
    if not conn:
        return 0

    saved = skipped = 0
    try:
        with conn:
            with conn.cursor() as cur:
                for result in send_results.get("results", []):
                    if result.get("status") == "skipped":
                        continue
                    protocol = result.get("protocol", "")
                    persona  = result.get("persona", "")
                    try:
                        cur.execute("""
                            INSERT INTO outreach (
                                protocol_name, persona_name, persona_role,
                                to_email, subject, body,
                                resend_id, status, channel
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (protocol_name, persona_name) DO NOTHING
                        """, (
                            protocol, persona,
                            result.get("role", ""),
                            result.get("to", ""),
                            result.get("subject", ""),
                            result.get("body", ""),
                            result.get("id", ""),
                            result.get("status", ""),
                            result.get("channel", "email"),
                        ))
                        if cur.rowcount:
                            saved += 1
                        else:
                            skipped += 1
                            logger.info(
                                "Outreach already sent to %s / %s — not duplicating",
                                protocol, persona
                            )
                    except Exception as e:
                        logger.warning("Failed to save outreach row: %s", e)
        logger.info("PostgreSQL outreach: %d saved, %d already existed", saved, skipped)
        return saved
    except Exception as e:
        logger.error("save_outreach failed: %s", e)
        return 0
    finally:
        conn.close()
