# CLAUDE.md — Discovery Pipeline

AI-powered GTM system for Web3 security sales. Discovers protocols from live data,
scores them against an ICP, finds the people to contact, generates personalized cold
email with Claude, sends it, and syncs the result to a CRM.

---

## Architecture

```
discovery-pipeline/
├── scripts/
│   ├── api.py            FastAPI app: REST + SSE endpoints, LangChain chat agent, _do_pipeline_run()
│   ├── run_pipeline.py   CLI orchestrator + RESEARCH_OVERLAYS / SEED_FUNDING seed data
│   └── agent.py          [DEAD] CLI-only duplicate of the api.py chat agent
├── src/
│   ├── pipeline/         ingest.py → enrich.py → score.py
│   ├── agents/
│   │   ├── outreach_agent.py   Claude email generation + template fallback
│   │   └── signal_agent.py     [DEAD] not imported anywhere
│   ├── integrations/     contacts.py, email_sender.py
│   ├── monitoring/       event_monitor.py (DeFiLlama exploit/funding detection)
│   ├── store/json_store.py   JSON persistence (data/state.json) + send ledger
│   └── utils/            claude_client, config, github, json_utils, token_tracker
├── config/
│   ├── scoring_weights.json   ICP: discovery filters, weights, tier thresholds
│   └── .env.example           secrets template
└── frontend/             Next.js 14 App Router + Tailwind; chat, lead table, draft drawer
```

### Pipeline flow

1. **Ingest** (`src/pipeline/ingest.py`) — DeFiLlama `/protocols` filtered by TVL + category;
   GitHub org scan (repos, commits/30d, `.cursorrules`/Copilot/Windsurf config detection);
   DeFiLlama `/raises` and `/hacks`, matched only to protocols already discovered.
2. **Enrich** (`enrich.py`) — groups signals per protocol into one `EnrichedProfile`.
   Audit/team data is layered on from `RESEARCH_OVERLAYS`, not scraped.
3. **Score** (`score.py`) — 5 factors → 0-100 composite → hot/warm/cool tier.
4. **Qualify** — top `max_qualified_leads` (default 3) with tier hot|warm.
5. **Contacts** (`integrations/contacts.py`) — GitHub active contributors + Claude
   `web_search` for leadership; merged, deduped by name, sorted by role priority.
6. **Outreach** (`agents/outreach_agent.py`) — one Claude call per contact
   (callers currently slice `contacts[:1]`, so one email per protocol).
7. **Send** (`email_sender.py`) — Resend, behind three fail-closed gates: a test
   recipient must resolve (else nothing is sent, never to real contacts), a hard
   `MAX_EMAILS` cap (default 5), and the on-disk send ledger
   (`data/sent_ledger.json`), written immediately after each successful send.
8. **Persist** — `data/state.json` via `store/json_store.py` (atomic writes).

Two entry points run this: `run_pipeline.main()` (CLI) and `_do_pipeline_run()` in
`api.py` (UI). **They are near-duplicates and have drifted** — the API version omits
the event monitor and CSV/JSON export.

---

## Running it

```bash
# Backend
cd discovery-pipeline
pip install -r requirements.txt
cp config/.env.example config/.env      # then fill in keys
uvicorn scripts.api:app --port 8000 --reload

# Frontend
cd frontend && npm install && npm run dev

# CLI pipeline
python scripts/run_pipeline.py             # live
python scripts/run_pipeline.py --seed-only # no live API calls
python scripts/run_pipeline.py --no-llm    # template outreach, no Claude
```

- UI: `http://localhost:3000` · API docs: `http://localhost:8000/docs`
- Persistence is `data/state.json` plus `data/sent_ledger.json`; both are created on
  first write and are gitignored. No database to provision.
- There is **no test suite** and no linter config. Verification today means running
  the pipeline or hitting endpoints.

---

## External services

| Service | Auth | Where it lives |
|---|---|---|
| DeFiLlama (`/protocols`, `/raises`, `/hacks`) | none | `pipeline/ingest.py`, `monitoring/event_monitor.py` |
| GitHub REST | `GITHUB_TOKEN` (optional; 60→5000 req/hr) | `utils/github.py`, `pipeline/ingest.py`, `integrations/contacts.py` |
| Claude API (Anthropic SDK) | `ANTHROPIC_API_KEY` | `utils/claude_client.py` → `agents/outreach_agent.py`, `integrations/contacts.py` (web_search tool) |
| Claude via LangChain (`ChatAnthropic`) | same key | `scripts/api.py` chat agent |
| Resend | `RESEND_API_KEY`, `RESEND_FROM_EMAIL`, `RESEND_TEST_EMAIL` | `integrations/email_sender.py` |

Every integration degrades to a no-op when its key is missing — keep that property.

---

## Conventions observed

- **Module docstrings**: every file opens with a block explaining purpose, data sources, and
  what is real vs. seeded. Match this style.
- **Dual output**: `print(..., flush=True)` for user-facing pipeline progress (the SSE endpoint
  captures stdout), `logger.*` for detail. Don't drop the `flush=True`.
- **Lazy client init**: Anthropic and Resend clients are created inside functions, never at
  import time, so `dotenv` loads first. Preserve this.
- **Dataclasses** for pipeline records (`RawSignal`, `EnrichedProfile`, `ScoredLead`,
  `OutreachDraft`, `Contact`). Plain dicts for enrichment/persona maps.
- **Config over constants**: pipeline knobs belong in `config/scoring_weights.json`.
- **Fallbacks over failures**: Claude unavailable → template outreach; DB absent → skip and log.
- Frontend uses a `discovery.*` Tailwind color namespace and `@/lib/api.ts` as the single
  typed client for every backend call.

---

## Known weak spots (context for future work)

- **Config is decorative.** `score.py` reads only `tier_thresholds` and `model_version`; every
  weight in `scoring_weights.json` is re-hardcoded in Python, and the two have already drifted.
- **No output validation on LLM results.** Claude's outreach text is parsed by string-splitting
  on `Subject:` / `Signals used:`; web-search contacts are `json.loads`'d with no schema check and
  no corroboration before a real email is sent.
- **No retries, no backoff.** Every `requests` call is a single attempt with a timeout. GitHub
  rate-limit headers are ignored; a 403 just returns `None`.
- **No tests, no evals.** Nothing verifies scoring, tool selection, or outreach quality.
- **Shared mutable globals.** `_state` and `_chat_history` in `api.py` are unguarded, and
  `/api/pipeline/run` swaps `sys.stdout` process-wide from a worker thread.
- **Dead code**: `src/agents/signal_agent.py`, `scripts/agent.py` — the latter still imports
  the removed HubSpot/Slack modules and no longer runs. Both are slated for deletion.
- **Stale Anthropic surface**: default model `claude-sonnet-4-20250514`; `token_tracker` hardcodes
  $3/$15 per MTok; `contacts.py` uses the `web_search_20250305` tool variant.
- **Committed artifact**: `app.log` is tracked in git.

---

## Working rules for our sessions

- **Plan first.** Propose a short plan before any change touching more than ~3 files, and wait
  for approval before applying it.
- **One concern per change.** Keep diffs small and reviewable; don't fold cleanups into features.
- **Never hardcode or commit secrets.** Keys live in `config/.env` (gitignored); update
  `config/.env.example` with placeholders only.
- **Verify, then report.** After changes, run what can be run and state explicitly what was
  verified versus what was not. Don't claim a thing works if it wasn't exercised.
