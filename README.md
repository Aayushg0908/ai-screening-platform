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
