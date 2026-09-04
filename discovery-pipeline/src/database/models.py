"""
SQLAlchemy ORM models for the Discovery Pipeline.
Maps to schema.sql — PostgreSQL database for Web3 protocol 
lead scoring, enrichment, outreach, and discovery call tracking.
"""

import os
from datetime import datetime, date
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    Text, DateTime, Date, ForeignKey, CheckConstraint
)
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.sql import func
try:
    from dotenv import load_dotenv
    load_dotenv("config/.env.example")
except ImportError:
    pass

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/discovery_pipeline"
)

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


class Protocol(Base):
    """A Web3 protocol we're evaluating as a potential Discovery Pipeline client."""
    __tablename__ = "protocols"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True)
    website = Column(String(500))
    github_org = Column(String(255))
    category = Column(String(100))          # dex, lending, yield, bridge, l2, etc.
    chain = Column(String(100))             # ethereum, solana, arbitrum, multi
    token = Column(String(50))
    tvl_current = Column(Float)
    tvl_30d_change = Column(Float)
    status = Column(String(50), default="active")
    team_type = Column(String(50))
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    signals = relationship("Signal", back_populates="protocol", cascade="all, delete-orphan")
    enrichment = relationship("Enrichment", back_populates="protocol", uselist=False)
    scores = relationship("LeadScore", back_populates="protocol")
    outreach_messages = relationship("Outreach", back_populates="protocol")

    def __repr__(self):
        return f"<Protocol(name='{self.name}', chain='{self.chain}', tvl={self.tvl_current})>"


class Signal(Base):
    """Raw signal ingested from a Web3 data source."""
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True)
    protocol_id = Column(Integer, ForeignKey("protocols.id", ondelete="CASCADE"))
    signal_type = Column(String(50), nullable=False)
    source = Column(String(100))
    source_url = Column(Text)
    raw_content = Column(Text)
    extracted_data = Column(JSONB)
    relevance_score = Column(Float, default=0)
    ingested_at = Column(DateTime, default=func.now())

    protocol = relationship("Protocol", back_populates="signals")


class Enrichment(Base):
    """Enriched profile for a protocol — one row per protocol."""
    __tablename__ = "enrichment"

    id = Column(Integer, primary_key=True)
    protocol_id = Column(Integer, ForeignKey("protocols.id", ondelete="CASCADE"), unique=True)

    # TVL & Risk
    tvl_usd = Column(Float)
    tvl_category = Column(String(20))
    chains_deployed = Column(ARRAY(Text))
    contract_count = Column(Integer)

    # Audit Status
    has_been_audited = Column(Boolean, default=False)
    audit_providers = Column(ARRAY(Text))
    last_audit_date = Column(Date)
    has_bug_bounty = Column(Boolean, default=False)
    bounty_platform = Column(String(100))
    bounty_amount_usd = Column(Float)
    unaudited_new_code = Column(Boolean, default=False)

    # Development Velocity
    github_commits_30d = Column(Integer)
    github_contributors = Column(Integer)
    languages = Column(ARRAY(Text))
    deploys_last_30d = Column(Integer)
    ai_tool_signals = Column(ARRAY(Text))
    shipping_velocity = Column(String(20))

    # Funding
    total_raised_usd = Column(Float)
    last_funding_date = Column(Date)
    last_funding_amount = Column(Float)
    investors = Column(ARRAY(Text))

    # Team & Reachability
    team_members = Column(JSONB)
    team_type = Column(String(50))
    discord_url = Column(String(500))
    telegram_url = Column(String(500))
    twitter_handle = Column(String(100))
    warm_intro_available = Column(Boolean, default=False)
    warm_intro_path = Column(Text)

    enriched_at = Column(DateTime, default=func.now())

    protocol = relationship("Protocol", back_populates="enrichment")


class LeadScore(Base):
    """Computed lead score for a protocol."""
    __tablename__ = "lead_scores"

    id = Column(Integer, primary_key=True)
    protocol_id = Column(Integer, ForeignKey("protocols.id", ondelete="CASCADE"))

    tvl_score = Column(Float, nullable=False)
    audit_status_score = Column(Float, nullable=False)
    velocity_score = Column(Float, nullable=False)
    funding_score = Column(Float, nullable=False)
    reachability_score = Column(Float, nullable=False)

    composite_score = Column(Float, nullable=False)
    score_tier = Column(String(10))
    scoring_rationale = Column(Text)

    model_version = Column(String(20), default="1.0")
    weights_snapshot = Column(JSONB)

    scored_at = Column(DateTime, default=func.now())

    protocol = relationship("Protocol", back_populates="scores")

    def compute_composite(self):
        """Sum all factor scores into composite."""
        self.composite_score = (
            self.tvl_score +
            self.audit_status_score +
            self.velocity_score +
            self.funding_score +
            self.reachability_score
        )
        # Assign tier
        if self.composite_score >= 90:
            self.score_tier = "hot"
        elif self.composite_score >= 75:
            self.score_tier = "warm"
        else:
            self.score_tier = "cool"


class Outreach(Base):
    """Generated outreach message for a protocol."""
    __tablename__ = "outreach"

    id = Column(Integer, primary_key=True)
    protocol_id = Column(Integer, ForeignKey("protocols.id", ondelete="CASCADE"))
    lead_score_id = Column(Integer, ForeignKey("lead_scores.id"))

    persona_name = Column(String(255))
    persona_role = Column(String(255))
    persona_twitter = Column(String(255))
    persona_email = Column(String(255))

    channel = Column(String(50))
    sequence_step = Column(Integer, default=1)
    subject_line = Column(Text)
    message_body = Column(Text)
    signals_used = Column(JSONB)

    llm_model = Column(String(100))
    llm_prompt = Column(Text)

    status = Column(String(20), default="draft")
    sent_at = Column(DateTime)
    replied_at = Column(DateTime)

    hubspot_contact_id = Column(String(100))

    created_at = Column(DateTime, default=func.now())

    protocol = relationship("Protocol", back_populates="outreach_messages")


class MarketEvent(Base):
    """Market event that can trigger contextual outreach."""
    __tablename__ = "market_events"

    id = Column(Integer, primary_key=True)
    event_type = Column(String(50), nullable=False)
    title = Column(String(500))
    description = Column(Text)
    source_url = Column(Text)
    source = Column(String(100))

    affected_protocols = Column(ARRAY(Integer))
    relevance_tags = Column(ARRAY(Text))

    outreach_triggered = Column(Boolean, default=False)
    triggered_at = Column(DateTime)

    detected_at = Column(DateTime, default=func.now())


class DiscoveryCall(Base):
    """Post-call feedback for hypothesis validation (instrumentation)."""
    __tablename__ = "discovery_calls"

    id = Column(Integer, primary_key=True)
    protocol_id = Column(Integer, ForeignKey("protocols.id"))
    outreach_id = Column(Integer, ForeignKey("outreach.id"))

    call_date = Column(Date, nullable=False)
    prospect_name = Column(String(255))
    prospect_role = Column(String(255))

    pain_confirmed = Column(Boolean)
    pain_severity = Column(Integer)
    using_ai_for_contracts = Column(Boolean)
    ai_tools_named = Column(ARRAY(Text))
    current_audit_provider = Column(ARRAY(Text))
    audit_keeping_pace = Column(Boolean)
    had_exploit_or_near_miss = Column(Boolean)

    icp_signal_accurate = Column(Boolean)
    icp_mismatch_notes = Column(Text)

    interested_in = Column(ARRAY(Text))
    current_bounty_platform = Column(String(100))
    willing_to_switch = Column(Boolean)

    tool_consolidation_mentioned = Column(Boolean, default=False)
    tool_consolidation_notes = Column(Text)

    top_objection = Column(Text)
    next_step = Column(String(50))
    deal_potential = Column(String(20))
    notes = Column(Text)

    created_at = Column(DateTime, default=func.now())


class ScoringModelVersion(Base):
    """Tracks scoring weight changes over time."""
    __tablename__ = "scoring_model_versions"

    id = Column(Integer, primary_key=True)
    version = Column(String(20), nullable=False, unique=True)
    context = Column(String(20), default="web3")  # 'web3' or 'web2' — modular!
    weights = Column(JSONB, nullable=False)
    reason = Column(Text)
    performance = Column(JSONB)
    active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())


# ── Database utilities ──────────────────────────────────────────────────────

def init_db():
    """Create all tables."""
    Base.metadata.create_all(bind=engine)
    print("✓ Database tables created")


def get_session():
    """Get a new database session."""
    return SessionLocal()


if __name__ == "__main__":
    init_db()
