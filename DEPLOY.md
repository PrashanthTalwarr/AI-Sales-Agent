# Deploying the demo to Vercel

The hosted demo runs the Next.js frontend **only**. There is no Python backend in
production: the app's own `/api/*` routes serve a snapshot of a real pipeline run
from `frontend/src/lib/demo-data.json`.

That means the public link needs no API keys, costs nothing to run, never sleeps,
and cannot send email or spend your Anthropic credits.

---

## Why not deploy the backend too

The FastAPI service does not fit serverless as written, and it is worth knowing why
rather than discovering it mid-deploy:

- A pipeline run takes 3–4 minutes. Vercel functions cap at 60s (Hobby) / 300s (Pro).
- `/api/pipeline/run` spawns a thread and redirects `sys.stdout` to stream logs — that
  model does not survive in a serverless invocation.
- `data/state.json` and `data/sent_ledger.json` live on disk. Serverless filesystems are
  ephemeral, so the **send ledger would reset** and the double-send guard would stop
  working. That is a safety regression, not just lost data.
- The API has no authentication. A public backend would let anyone trigger runs that
  spend your Claude credits and Resend quota.

If you later want a genuinely live deployment, put the backend on a host with a real
process and a persistent disk (Railway, Fly.io, or Render with a paid disk), add an
auth header, and set `NEXT_PUBLIC_API_BASE` on Vercel to point at it. Everything else
already works — the frontend switches targets on that one variable.

---

## Deploy

### 1. Push the repo

```bash
git push origin main
```

### 2. Import into Vercel

1. vercel.com → **Add New → Project** → import the GitHub repo
2. **Root Directory** → `discovery-pipeline/frontend` ← *the important one; the repo root is not the app*
3. Framework preset: **Next.js** (auto-detected)
4. Build/output settings: leave the defaults
5. **Environment variables: add none.** Leaving `NEXT_PUBLIC_API_BASE` unset is what
   turns demo mode on.
6. **Deploy**

That is the whole deployment. The build is a normal `next build` — the same one that
runs locally.

### 3. Check the live site

- The amber **Demo** banner appears under the header
- **Load Last Results** fills the sidebar with 58 scored protocols
- Opening **Rocket Pool**, **Lido**, or **EigenCloud** shows a real Claude-written email
- **Run Pipeline** replays the saved run's log, about 5 seconds
- Asking the chat *"show me the warm leads"* returns the 8 real warm leads

---

## Running locally against the real backend

The same build talks to the live FastAPI service when you point it there:

```bash
cd discovery-pipeline/frontend
cp .env.local.example .env.local     # sets NEXT_PUBLIC_API_BASE=http://localhost:8000
npm run dev
```

`.env.local` is gitignored on purpose. If it were committed, Vercel would load it and
the deployed site would try to reach `localhost:8000`.

With `NEXT_PUBLIC_API_BASE` set, the demo routes are bypassed entirely, the banner
disappears, and the test-recipient field comes back.

---

## Refreshing the demo data

The snapshot is generated from whatever is in `data/state.json`:

```bash
cd discovery-pipeline
python scripts/run_pipeline.py --test-email you@example.com   # optional: a fresh run
python scripts/build_demo_fixture.py                          # regenerate the fixture
cd frontend && npm run build                                  # verify it still builds
git add -A && git commit -m "chore: refresh demo fixture" && git push
```

Vercel redeploys on push.

---

## What the demo does and does not do

| Works | Does not |
|-------|----------|
| 58 scored protocols with real factor breakdowns | Live DeFiLlama / GitHub calls |
| Contacts found by GitHub + Claude web search | Live Claude calls |
| Three real Claude-written outreach emails | Sending email (returns an explicit 501) |
| Run Pipeline log replay | An actual pipeline run |
| Chat answering from the saved run | The real LangChain agent |

The chat answers the questions the real agent's five tools cover, using the same
snapshot the rest of the demo reads, so its numbers are accurate. Anything outside
that returns an honest "this is a static demo" reply rather than a hallucination.
