# AI Screening Platform

AI-powered candidate screening built for the Visl AI Labs assignment. It ingests
a candidate dataset, evaluates each candidate against a job description with an
LLM, analyses their GitHub at the repository level, scores and ranks the field
deterministically, emails a test link to the shortlist, ingests test results,
and schedules interviews on Google Calendar with real Meet links.

See [`CLAUDE.md`](CLAUDE.md) for the full design brief and
[`docs/architecture.md`](docs/architecture.md) for the architecture write-up
(system design, diagrams, and the AI evaluation approach).

## Live deployment

| | |
| --- | --- |
| Frontend (recruiter dashboard) | https://visl-screening-ui.onrender.com |
| Backend API (docs at `/docs`) | https://ai-screening-platform.onrender.com |
| Demo video | _to be added_ |

Both services are on Render's free tier and spin down after ~15 minutes idle —
the first request after a pause can take 30–60s to wake up.

## Stack

| Layer | Choice |
| --- | --- |
| Backend | FastAPI + Uvicorn (all business logic) |
| Orchestration | LangGraph per-candidate graph, LangChain for LLM I/O |
| Frontend | Streamlit (thin UI; talks only to the HTTP API) |
| Database | Postgres (Neon free tier) via SQLModel |
| LLM | Groq `openai/gpt-oss-120b` (pool of keys), Mistral `ministral-8b-latest` fallback |
| Email | Brevo HTTPS API (default transport), SMTP kept as a fallback path |
| Python | 3.11 exactly |

Everything runs on free tiers — see [`docs/architecture.md`](docs/architecture.md)
for why (Groq rate limits, Neon idle scale-to-zero, Render's outbound-SMTP
block, and how each is worked around).

## Prerequisites

- Python 3.11
- A Neon Postgres database
- API credentials: Groq (and optionally Mistral as fallback), a GitHub PAT,
  a Brevo account (API key for email), a Google Cloud OAuth client + refresh
  token for Calendar

## Setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt

cp env.example .env   # then fill in every value
```

Obtain the Google Calendar refresh token:

```bash
python scripts/google_oauth_setup.py
```

Verify every credential actually works:

```bash
python scripts/preflight.py          # read-only checks
python scripts/preflight.py --full   # also sends a test email + calendar event
```

## Run

```bash
# backend — http://localhost:8000  (interactive docs at /docs)
uvicorn backend.main:app --reload --port 8000

# frontend — http://localhost:8501
streamlit run frontend/app.py
```

Check the backend is healthy:

```bash
curl http://localhost:8000/health
# {"status":"ok","db":true}
```

## Recruiter workflow

The frontend is seven pages, walked in order, each picking up state from the
one before it:

1. **Upload Candidates** — CSV/XLSX with any similarly-shaped schema (columns
   matched by synonym, not position); optionally process resumes (Drive link
   → PDF → extracted text).
2. **Job Description** — create or reuse one.
3. **Evaluation** — run the LangGraph pipeline (`fast` or `quality` mode);
   watch live progress.
4. **Rankings** — every score with its reasoning and quoted evidence; retune
   the resume/GitHub weight blend and re-rank instantly with zero new LLM or
   GitHub calls.
5. **Outreach** — preview a shortlist by top-N or score threshold, then send
   test-invite emails.
6. **Test Results** — upload results (LEFT JOIN on `s_no`, never email/name);
   tune the final-score blend.
7. **Interviews** — schedule real Google Calendar events with Meet links by
   top-N or final-score threshold; cancel and re-book.

Every page shows a "what's already happened here" summary (metrics, tables,
a pipeline-progress bar in the sidebar) — not just the result of the last
click.

## Deployment (Render)

Two separate services, backend and frontend, each reading `$PORT` from the
environment Render injects - binding to `127.0.0.1` (uvicorn's default) or a
hardcoded port makes Render mark the service unhealthy, since the platform
can't reach it.

```bash
# backend service - start command
uvicorn backend.main:app --host 0.0.0.0 --port $PORT

# frontend service - start command
streamlit run frontend/app.py --server.port=$PORT --server.address=0.0.0.0 \
  --server.headless=true --server.enableXsrfProtection=false
```

`.streamlit/config.toml` sets `headless`/`enableXsrfProtection` too, so they
apply even if a platform ignores extra CLI flags.

Environment variables specific to deployment (everything else is the same
`.env` values, set directly in Render's dashboard instead of a file):

- `CORS_ORIGINS` on the **backend** service - comma-separated, must include
  the frontend's Render URL: `https://visl-screening-ui.onrender.com,http://localhost:8501`
  (keeping `localhost:8501` in the list means local dev still works).
- `API_BASE_URL` on the **frontend** service - the backend service's own
  Render URL: `https://ai-screening-platform.onrender.com` (locally this is
  `http://localhost:8000`).
- `EMAIL_TRANSPORT=api` on the **backend** service - Render blocks outbound
  SMTP ports, so email goes over Brevo's HTTPS API in production; `smtp` is
  kept as a fallback transport for other hosts. See
  [`docs/architecture.md`](docs/architecture.md) for why.

`runtime.txt` pins `python-3.11.9` - Render defaults to a newer Python
otherwise, and some wheels in this dependency set don't build cleanly on
3.12+.

## Configuration

Every environment variable is declared once in
[`backend/core/config.py`](backend/core/config.py) and documented in
[`env.example`](env.example). No other module reads the environment directly
(the one intentional exception is `frontend/lib/api_client.py` reading
`API_BASE_URL`, since the frontend cannot import backend code). Scoring
weights and shortlisting thresholds live there too, so they are tunable and
auditable without touching code.

## Project layout

```
backend/
  main.py              FastAPI app + router registration
  api/routes/          thin HTTP endpoints, delegate to services
  core/                config, db engine, logging
  models/              SQLModel tables + Pydantic schemas
  services/            ingest, resume, github, scoring, mailer, calendar, llm, ...
  graph/               LangGraph state, nodes, pipeline
frontend/
  app.py               Streamlit entry, health check, pipeline dashboard
  pages/               one file per workflow stage
  lib/api_client.py    the only module that talks HTTP to the backend
  lib/sidebar.py       shared "viewing batch/job/run" + progress-bar widget
scripts/               preflight + Google OAuth setup (do not modify)
docs/architecture.md   architecture deliverable
tests/                 test suite
```

## Tests

```bash
pytest tests/ -v -m "not integration"
```

Unit tests cover the pure functions (Drive URL conversion, CGPA rounding,
college-name normalization, `s_no` join logic, GitHub URL regex fallback,
scoring arithmetic); integration tests hit live services and are marked
`@pytest.mark.integration` so they can be deselected. Given the deadline,
this suite was deprioritised in favour of the end-to-end flow and manual
verification against the live deployed backend (see `docs/architecture.md`
and the commit history for that verification work).

## Status

All eight functional requirements (§4: ingest, resume processing, LLM
evaluation, GitHub analysis, scoring/ranking, email outreach, test-result
ingestion, Calendar scheduling) are implemented, deployed, and verified
against the live services above — not mocked. The frontend covers all seven
workflow stages plus a home dashboard. Remaining before the deadline: the
demo video.
