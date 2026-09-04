# AI Screening Platform

AI-powered candidate screening built for the Visl AI Labs assignment. It ingests
a candidate dataset, evaluates each candidate against a job description with an
LLM, analyses their GitHub at the repository level, ranks the field, emails a
test link to the shortlist, ingests test results, and schedules interviews on
Google Calendar with Meet links.

See [`CLAUDE.md`](CLAUDE.md) for the full design brief and
[`docs/architecture.md`](docs/architecture.md) for the architecture write-up.

## Stack

| Layer          | Choice                                                        |
| -------------- | ------------------------------------------------------------ |
| Backend        | FastAPI + Uvicorn (all business logic)                      |
| Orchestration  | LangGraph per-candidate graph, LangChain for LLM I/O        |
| Frontend       | Streamlit (thin UI; talks only to the HTTP API)            |
| Database       | Postgres (Neon free tier) via SQLModel                      |
| LLM            | Groq primary, Google Gemini fallback, one factory           |
| Python         | 3.11 exactly                                                |

Everything runs on free tiers.

## Prerequisites

- Python 3.11
- A Neon Postgres database
- API credentials: Groq, Google AI Studio (Gemini), GitHub PAT, Gmail app
  password, Google Calendar OAuth client

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
  the frontend's Render URL: `https://my-frontend.onrender.com,http://localhost:8501`
  (keeping `localhost:8501` in the list means local dev still works).
- `API_BASE_URL` on the **frontend** service - the backend service's own
  Render URL, e.g. `https://my-backend.onrender.com` (locally this is
  `http://localhost:8000`).

`runtime.txt` pins `python-3.11.9` - Render defaults to a newer Python
otherwise, and some wheels in this dependency set don't build cleanly on
3.12+.

## Configuration

Every environment variable is declared once in
[`backend/core/config.py`](backend/core/config.py) and documented in
[`env.example`](env.example). No other module reads the environment directly.
Scoring weights and shortlisting thresholds live there too, so they are tunable
and auditable.

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
  app.py               Streamlit entry + health indicator
  pages/               one file per workflow stage
  lib/api_client.py    the only module that talks HTTP to the backend
scripts/               preflight + Google OAuth setup (do not modify)
docs/architecture.md   architecture deliverable
tests/                 test suite
```

## Tests

```bash
pytest
```

## Status

Skeleton: imports cleanly and the API runs. Service and graph-node bodies raise
`NotImplementedError` pending implementation.
