# Deploying

**Frontend on Vercel, Python API on Render.** Two services, one environment
variable connecting them.

```
Vercel (Next.js)  ──NEXT_PUBLIC_API_BASE──▶  Render (FastAPI + uvicorn)
                                              serves data/seed_state.json
                                              real Claude chat agent
```

If `NEXT_PUBLIC_API_BASE` is ever unset or the API is unreachable, the frontend
falls back to its own `/api/*` routes, which serve a bundled snapshot — so the
link never shows an empty page.

---

## 1. Deploy the API to Render

1. render.io → **New → Blueprint**, point it at this repo. It reads `render.yaml`.
   *(Or **New → Web Service** by hand with the settings below.)*

| Setting | Value |
|---|---|
| Root Directory | `discovery-pipeline` |
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn scripts.api:app --host 0.0.0.0 --port $PORT` |
| Health Check Path | `/api/health` |

2. Environment variables (Render dashboard → Environment):

| Key | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your key — powers the live chat agent |
| `ALLOW_PIPELINE_RUN` | `false` |
| `CORS_ORIGINS` | your Vercel URL, added after step 2 |
| `GITHUB_TOKEN` | optional, only needed for live runs |

**Leave `RESEND_API_KEY` and `RESEND_TEST_EMAIL` unset.** With no test recipient
the send step fails closed and delivers nothing, so the public API cannot email
anyone even if a run were triggered.

3. Deploy, then check `https://<your-service>.onrender.com/api/health`:

```json
{"status":"ok","leads":58,"drafts":3,"pipeline_runs_allowed":false,"claude_configured":true}
```

`leads: 58` on a first boot means the seed loaded correctly.

---

## 2. Deploy the frontend to Vercel

1. vercel.com → **Add New → Project**, import the same repo
2. **Root Directory: `discovery-pipeline/frontend`** ← the repo root is not the app
3. Framework preset: Next.js (auto-detected)
4. Environment variable:

| Key | Value |
|---|---|
| `NEXT_PUBLIC_API_BASE` | `https://<your-service>.onrender.com` — no trailing slash |

5. Deploy

---

## 3. Connect them

Go back to Render and set `CORS_ORIGINS` to your Vercel URL:

```
https://your-project.vercel.app
```

Add preview domains as a comma-separated list if you use them. Render restarts
automatically. Without this the browser blocks every API call with a CORS error.

---

## Verify

- `/api/health` returns `leads: 58`
- The Vercel URL loads 58 protocols with no clicking — the API hydrates its state on startup
- Opening **Rocket Pool**, **Lido**, or **EigenCloud** shows a real Claude-written email
- The chat is **live Claude with real tool calls** — ask *"show me the warm leads"*
- **Run Pipeline** returns a clear 403; it is disabled on purpose

---

## Things that will bite you

**Free instances sleep.** Render's free tier sleeps after 15 minutes idle and
takes ~50s to wake. The frontend polls `/api/health` and shows a "waking the API"
banner instead of a blank screen, but the first visitor after a quiet period
still waits. Options: upgrade to Render Starter (~$7/mo, no sleep), or ping
`/api/health` every 10 minutes from a free cron service like cron-job.org.

**The filesystem is ephemeral.** Render free has no persistent disk, so
`data/state.json` and `data/sent_ledger.json` reset on every deploy and every
wake. Reads are unaffected — `data/seed_state.json` is committed and loads as the
fallback. But **the send ledger resets**, which is why live runs and email are
both off in this configuration. If you ever enable them, add a Render disk
mounted at `discovery-pipeline/data` first, or the double-send guard is not real.

**CORS is the usual failure.** A dashboard that loads but stays empty almost always
means `CORS_ORIGINS` does not match the Vercel origin. Use `https://`, not `http://`.
Trailing slashes are stripped automatically — browsers send `Origin: https://site.app`
with no trailing slash, so a configured `https://site.app/` would otherwise match
nothing and block every request while the server logged no error at all.

To check from a terminal, ask for the header a browser would need:

```bash
curl -s -D - -o /dev/null -H "Origin: https://your-site.vercel.app"   https://your-api.onrender.com/api/leads | grep -i access-control-allow-origin
```

No output means the origin is not allowed.

**Cold starts affect the chat too.** The first message after a sleep waits for the
wake plus the Claude call.

---

## Demoing a live run in an interview

Live runs are off by default because they spend Claude credits and can send
email. To turn one on for a demo:

1. Render → set `ALLOW_PIPELINE_RUN=true` and `API_SECRET=<something long>`
2. Add a Render disk mounted at `discovery-pipeline/data` so the send ledger persists
3. Set `RESEND_API_KEY` and `RESEND_TEST_EMAIL` only if you want it to actually send
4. Trigger it with the secret:
   `curl "https://<service>.onrender.com/api/pipeline/run?secret=<API_SECRET>"`
5. Turn `ALLOW_PIPELINE_RUN` back to `false` afterwards

---

## Running locally

Unchanged. Backend:

```bash
cd discovery-pipeline
uvicorn scripts.api:app --port 8000 --reload
```

Frontend:

```bash
cd discovery-pipeline/frontend
cp .env.local.example .env.local     # NEXT_PUBLIC_API_BASE=http://localhost:8000
npm run dev
```

`.env.local` is gitignored deliberately — Next.js loads it on Vercel too, so
committing it would point the deployed site at `localhost:8000`.

---

## Refreshing the deployed data

The API serves `data/seed_state.json`, which is committed. After a local run you
want the world to see:

```bash
cd discovery-pipeline
python scripts/run_pipeline.py --test-email you@example.com
cp data/state.json data/seed_state.json
python scripts/build_demo_fixture.py    # also refresh the frontend fallback
git add -A && git commit -m "chore: refresh seed data" && git push
```

Both Render and Vercel redeploy on push.
