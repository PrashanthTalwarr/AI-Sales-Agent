-- ============================================================================
-- DISCOVERY PIPELINE — PostgreSQL Schema
-- Hypothesis A (Web3): "Your audit process wasn't built for AI-generated 
-- smart contracts"
-- ============================================================================

-- Protocols we're tracking as potential discovery call targets
CREATE TABLE IF NOT EXISTS protocols (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(255) NOT NULL UNIQUE,
    website         VARCHAR(500),
    github_org      VARCHAR(255),          -- GitHub organization name
    category        VARCHAR(100),          -- 'dex', 'lending', 'yield', 'bridge', 'l2', 'stablecoin', 'nft', 'infra'
    chain           VARCHAR(100),          -- 'ethereum', 'solana', 'arbitrum', 'base', 'optimism', 'multi'
    token           VARCHAR(50),           -- governance/native token ticker
    tvl_current     FLOAT,                 -- current TVL in USD (from DeFiLlama)
    tvl_30d_change  FLOAT,                 -- % change in TVL over 30 days
    status          VARCHAR(50) DEFAULT 'active',  -- 'active', 'prelaunch', 'inactive'
    team_type       VARCHAR(50),           -- 'doxxed', 'partially_doxxed', 'anonymous'
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- Raw signals ingested from various Web3 data sources
CREATE TABLE IF NOT EXISTS signals (
    id              SERIAL PRIMARY KEY,
    protocol_id     INTEGER REFERENCES protocols(id) ON DELETE CASCADE,
    signal_type     VARCHAR(50) NOT NULL,  -- 'tvl_data', 'github_activity', 'governance', 'exploit', 'funding', 'contract_deploy', 'social', 'audit_listing'
    source          VARCHAR(100),          -- 'defillama', 'etherscan', 'github', 'snapshot', 'twitter', 'rekt_news', 'crunchbase'
    source_url      TEXT,
    raw_content     TEXT,                  -- the scraped content
    extracted_data  JSONB,                 -- LLM-extracted structured signals
    relevance_score FLOAT DEFAULT 0,       -- 0-1, how relevant to Hypothesis A
    ingested_at     TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_signals_protocol ON signals(protocol_id);
CREATE INDEX idx_signals_type ON signals(signal_type);

-- Enriched profile per protocol (combines all signal data into one picture)
CREATE TABLE IF NOT EXISTS enrichment (
    id              SERIAL PRIMARY KEY,
    protocol_id     INTEGER REFERENCES protocols(id) ON DELETE CASCADE UNIQUE,

    -- TVL & Risk
    tvl_usd         FLOAT,
    tvl_category    VARCHAR(20),           -- 'mega' (>1B), 'large' (100M-1B), 'mid' (10M-100M), 'small' (<10M)
    chains_deployed TEXT[],                -- ['ethereum', 'arbitrum', 'base']
    contract_count  INTEGER,               -- number of deployed contracts

    -- Audit Status
    has_been_audited       BOOLEAN DEFAULT FALSE,
    audit_providers        TEXT[],          -- ['spearbit', 'openzeppelin', 'trail_of_bits']
    last_audit_date        DATE,
    has_bug_bounty         BOOLEAN DEFAULT FALSE,
    bounty_platform        VARCHAR(100),   -- 'discovery', 'immunefi', 'code4rena', 'sherlock', 'none'
    bounty_amount_usd      FLOAT,
    unaudited_new_code     BOOLEAN DEFAULT FALSE,  -- shipping new code since last audit?

    -- Development Velocity
    github_commits_30d     INTEGER,
    github_contributors    INTEGER,
    languages              TEXT[],          -- ['solidity', 'rust', 'typescript']
    deploys_last_30d       INTEGER,         -- new contract deployments
    ai_tool_signals        TEXT[],          -- ['.cursor_rules found', 'copilot_config', 'ai_mentioned_in_commits']
    shipping_velocity      VARCHAR(20),     -- 'very_high', 'high', 'moderate', 'low'

    -- Funding
    total_raised_usd       FLOAT,
    last_funding_date      DATE,
    last_funding_amount    FLOAT,
    investors              TEXT[],          -- ['a16z', 'paradigm', 'polychain']

    -- Team & Reachability
    team_members           JSONB,           -- [{name, role, twitter, email, linkedin}]
    team_type              VARCHAR(50),     -- 'doxxed', 'partially_doxxed', 'anonymous'
    discord_url            VARCHAR(500),
    telegram_url           VARCHAR(500),
    twitter_handle         VARCHAR(100),
    warm_intro_available   BOOLEAN DEFAULT FALSE,
    warm_intro_path        TEXT,            -- 'researcher X knows their lead dev'

    enriched_at            TIMESTAMP DEFAULT NOW()
);

-- Lead scores computed by the scoring engine
CREATE TABLE IF NOT EXISTS lead_scores (
    id                  SERIAL PRIMARY KEY,
    protocol_id         INTEGER REFERENCES protocols(id) ON DELETE CASCADE,

    -- Individual factor scores (match scoring_weights.json)
    tvl_score           FLOAT NOT NULL,       -- 0-30
    audit_status_score  FLOAT NOT NULL,       -- 0-25
    velocity_score      FLOAT NOT NULL,       -- 0-20
    funding_score       FLOAT NOT NULL,       -- 0-15
    reachability_score  FLOAT NOT NULL,       -- 0-10

    -- Composite
    composite_score     FLOAT NOT NULL,       -- sum (0-100)
    score_tier          VARCHAR(10),          -- 'hot' (90+), 'warm' (75-89), 'cool' (<75)

    -- LLM-generated rationale explaining the score
    scoring_rationale   TEXT,

    -- Model tracking (for weight recalibration)
    model_version       VARCHAR(20) DEFAULT '1.0',
    weights_snapshot    JSONB,                -- snapshot of weights used

    scored_at           TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_scores_composite ON lead_scores(composite_score DESC);
CREATE INDEX idx_scores_tier ON lead_scores(score_tier);

-- Outreach messages (generated by LLM agent)
CREATE TABLE IF NOT EXISTS outreach (
    id              SERIAL PRIMARY KEY,
    protocol_id     INTEGER REFERENCES protocols(id) ON DELETE CASCADE,
    lead_score_id   INTEGER REFERENCES lead_scores(id),

    -- Target persona
    persona_name    VARCHAR(255),
    persona_role    VARCHAR(255),          -- 'founder', 'cto', 'lead_solidity_dev', 'head_of_security'
    persona_twitter VARCHAR(255),
    persona_email   VARCHAR(255),

    -- Message
    channel         VARCHAR(50),           -- 'email', 'twitter_dm', 'telegram', 'discord', 'warm_intro'
    sequence_step   INTEGER DEFAULT 1,     -- 1=first touch, 2=follow-up, 3=break-up
    subject_line    TEXT,
    message_body    TEXT,
    signals_used    JSONB,                 -- which enrichment data informed this message

    -- LLM metadata
    llm_model       VARCHAR(100),          -- 'claude-sonnet-4-20250514'
    llm_prompt      TEXT,

    -- Status
    status          VARCHAR(20) DEFAULT 'draft',  -- 'draft','approved','sent','opened','replied','booked'
    sent_at         TIMESTAMP,
    replied_at      TIMESTAMP,

    -- CRM sync
    hubspot_contact_id  VARCHAR(100),

    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_outreach_status ON outreach(status);

-- Market events that trigger contextual outreach
CREATE TABLE IF NOT EXISTS market_events (
    id              SERIAL PRIMARY KEY,
    event_type      VARCHAR(50) NOT NULL,  -- 'exploit', 'funding_round', 'mainnet_launch', 'governance_vote', 'new_audit_listing', 'research_report'
    title           VARCHAR(500),
    description     TEXT,
    source_url      TEXT,
    source          VARCHAR(100),          -- 'rekt_news', 'defillama', 'twitter', 'crunchbase'

    -- Which protocols does this affect?
    affected_protocols  INTEGER[],         -- protocol IDs
    relevance_tags      TEXT[],            -- ['exploit', 'reentrancy', 'bridge', 'solana']

    -- Trigger tracking
    outreach_triggered  BOOLEAN DEFAULT FALSE,
    triggered_at        TIMESTAMP,

    detected_at         TIMESTAMP DEFAULT NOW()
);

-- Post-discovery-call feedback (instrumentation layer)
CREATE TABLE IF NOT EXISTS discovery_calls (
    id              SERIAL PRIMARY KEY,
    protocol_id     INTEGER REFERENCES protocols(id),
    outreach_id     INTEGER REFERENCES outreach(id),

    -- Call info
    call_date       DATE NOT NULL,
    prospect_name   VARCHAR(255),
    prospect_role   VARCHAR(255),

    -- Hypothesis A validation
    pain_confirmed          BOOLEAN,       -- do they confirm AI code security is a problem?
    pain_severity           INTEGER CHECK (pain_severity BETWEEN 1 AND 10),
    using_ai_for_contracts  BOOLEAN,       -- confirmed using Copilot/Cursor/Claude for Solidity/Rust?
    ai_tools_named          TEXT[],        -- ['copilot', 'cursor', 'claude_code', 'remix_ai']
    current_audit_provider  TEXT[],        -- ['openzeppelin', 'trail_of_bits', 'none']
    audit_keeping_pace      BOOLEAN,       -- is their audit process keeping up with shipping speed?
    had_exploit_or_near_miss BOOLEAN,

    -- ICP validation
    icp_signal_accurate     BOOLEAN,       -- did our enrichment match reality?
    icp_mismatch_notes      TEXT,

    -- Discovery Pipeline-specific
    interested_in           TEXT[],        -- ['security_review', 'bug_bounty', 'competition', 'mdr', 'ai_analyzer']
    current_bounty_platform VARCHAR(100),  -- 'immunefi', 'code4rena', 'sherlock', 'none'
    willing_to_switch       BOOLEAN,

    -- Hypothesis B signal (track organically)
    tool_consolidation_mentioned BOOLEAN DEFAULT FALSE,
    tool_consolidation_notes     TEXT,

    -- Outcome
    top_objection           TEXT,
    next_step               VARCHAR(50),   -- 'security_review_scoping', 'bounty_setup', 'demo', 'ciso_intro', 'no_next_step'
    deal_potential           VARCHAR(20),  -- 'high', 'medium', 'low', 'none'
    notes                   TEXT,

    created_at              TIMESTAMP DEFAULT NOW()
);

-- Scoring model versions (tracks weight recalibrations over time)
CREATE TABLE IF NOT EXISTS scoring_model_versions (
    id          SERIAL PRIMARY KEY,
    version     VARCHAR(20) NOT NULL UNIQUE,
    context     VARCHAR(20) DEFAULT 'web3',  -- 'web3' or 'web2' (modular!)
    weights     JSONB NOT NULL,
    reason      TEXT,
    performance JSONB,                       -- {booking_rate, confirmation_rate, ...}
    active      BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMP DEFAULT NOW()
);

-- Insert default Web3 scoring model
INSERT INTO scoring_model_versions (version, context, weights, reason, active) VALUES (
    '1.0',
    'web3',
    '{"tvl_and_funds_at_risk": 30, "audit_status": 25, "shipping_velocity": 20, "funding_recency": 15, "reachability": 10}',
    'Initial weights for Hypothesis A (Web3). TVL weighted highest because funds at risk = urgency.',
    TRUE
);

-- ============================================================================
-- VIEWS
-- ============================================================================

-- Qualified leads (score >= 75)
CREATE OR REPLACE VIEW qualified_leads AS
SELECT
    p.name, p.category, p.chain, p.tvl_current,
    ls.composite_score, ls.score_tier,
    ls.tvl_score, ls.audit_status_score, ls.velocity_score,
    ls.funding_score, ls.reachability_score,
    ls.scoring_rationale,
    e.has_been_audited, e.bounty_platform, e.shipping_velocity,
    e.github_commits_30d, e.ai_tool_signals
FROM lead_scores ls
JOIN protocols p ON p.id = ls.protocol_id
LEFT JOIN enrichment e ON e.protocol_id = p.id
WHERE ls.composite_score >= 75
ORDER BY ls.composite_score DESC;

-- Discovery call metrics (instrumentation dashboard)
CREATE OR REPLACE VIEW discovery_metrics AS
SELECT
    COUNT(*) AS total_calls,
    COUNT(*) FILTER (WHERE pain_confirmed) AS pain_confirmed_count,
    ROUND(AVG(pain_severity)::numeric, 1) AS avg_severity,
    COUNT(*) FILTER (WHERE using_ai_for_contracts) AS using_ai_count,
    COUNT(*) FILTER (WHERE audit_keeping_pace = FALSE) AS audit_bottleneck_count,
    COUNT(*) FILTER (WHERE next_step != 'no_next_step') AS advancing_count,
    COUNT(*) FILTER (WHERE tool_consolidation_mentioned) AS hyp_b_mentions,
    ROUND(
        COUNT(*) FILTER (WHERE pain_confirmed)::numeric / NULLIF(COUNT(*), 0) * 100, 1
    ) AS hypothesis_confirmation_pct
FROM discovery_calls;
