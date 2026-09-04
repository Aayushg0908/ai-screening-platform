# AI Screening Platform

Assignment build for Visl AI Labs. An AI-powered candidate screening platform that
ingests a candidate dataset, evaluates candidates against a job description using an
LLM, analyzes their GitHub at repository level, ranks them, emails a test link to the
shortlist, ingests test results, and schedules interviews on Google Calendar with Meet
links.

**Deadline: 12:00 noon, 6 Sept 2026.** Prefer working end-to-end flow over polish in
any single component.

## Stack

- **Backend:** FastAPI + Uvicorn. All business logic lives here.
- **Orchestration:** LangGraph (per-candidate evaluation graph), LangChain for LLM I/O.
- **Frontend:** Streamlit. Thin UI layer only — it calls the FastAPI HTTP API and
  contains **no business logic**. Never import from `backend/` inside `frontend/`.
- **DB:** Postgres (Neon free tier) via SQLModel. Never SQLite — hosts have ephemeral
  filesystems and the DB would vanish on redeploy.
- **LLM:** Groq `openai/gpt-oss-20b` (free, open-weights). Google Gemini via
  `langchain-google-genai` as fallback when Groq rate-limits. Both behind one factory
  in `backend/services/llm.py` so the provider is swappable in one place.
- **Python 3.11** exactly. Not 3.12+.

Everything must run on free tiers. No paid services, no credit card.

## Layout

```
backend/
  main.py              FastAPI app + router registration
  api/routes/          HTTP endpoints, thin — delegate to services
  core/config.py       pydantic-settings, reads .env
  core/db.py           engine + session dependency
  models/tables.py     SQLModel ORM tables
  models/schemas.py    Pydantic request/response + LLM structured-output models
  services/            ingest, resume, github, scoring, mailer, calendar, llm
  graph/               LangGraph state, nodes, pipeline
frontend/
  app.py               Streamlit entry
  pages/               one file per workflow stage
  lib/api_client.py    the ONLY place that talks HTTP to the backend
scripts/               one-off setup scripts (google oauth, seeding)
docs/architecture.md   deliverable
data/                  sample dataset
```

## Dataset traps — these are real, verified against the provided file

The sample workbook has two sheets, `Response` (10 rows) and `Test Result` (8 rows).

1. **Every candidate shares one email address.** All 10 rows in `Response` use
   `rishabh.choudhary+arnav@mynachiketa.com`. `Test Result` uses a *different* plus-tag,
   `...+assignment@...`. Therefore:
   - **Never** use email as a primary key, unique constraint, or dedupe field.
   - **Never** join the two sheets on email — zero rows would match.
   - Join on `s_no`. Use an internal surrogate `candidate_id` as the real PK.
2. **5 of 10 candidates have no GitHub URL** (s_no 3, 5, 6, 7, 10). The GitHub node must
   degrade gracefully: return a null evaluation with an explicit reason, never crash,
   never score 0 silently. Candidate 7 has a GitHub URL buried in their `research_work`
   free text — implement a regex fallback that scrapes `github.com/<user>` out of text
   fields when the `github` column is empty.
3. **The `Response` sheet's `test_la`/`test_code` columns are stale and contradict the
   `Test Result` sheet** for 6 of 8 overlapping candidates. Drop those two columns on
   ingest of the candidate dataset. The uploaded Test Result file is the sole authority
   for test scores.
4. **`Test Result` is missing s_no 4 and 10.** Merge must be a LEFT join with an explicit
   `NO_RESULT` state. Do not drop those candidates and do not impute zero.
5. `research_work` is null for 3 candidates. Optional field.
6. CGPA carries float noise (`8.2200000000000006`) — round to 2dp on ingest.
7. College names have trailing whitespace and inconsistent casing
   (`netaji subhas university of technology`) — normalize (strip + title-case) but keep
   the raw value in a separate column.
8. All resume links are Google Drive `/file/d/{ID}/view` URLs. Convert to
   `https://drive.google.com/uc?export=download&id={ID}`. Drive sometimes returns an
   HTML interstitial instead of the PDF — **check `Content-Type` is a PDF before handing
   bytes to pypdf**, and surface a clear per-candidate error if not.

## Design rules

- **Scoring is deterministic Python arithmetic, never LLM-computed.** The LLM emits
  per-dimension scores with reasoning; `services/scoring.py` does the weighted sum.
  Weights live in config so they are tunable and auditable.
- **All LLM output is structured via `llm.with_structured_output(PydanticModel)`.**
  Never parse free text. Every dimension carries `score`, `reasoning`, and `evidence`
  so the UI can explain any ranking (assignment bonus: explainable AI scoring).
- **Every graph node wraps its work in try/except and appends to `state["errors"]`
  rather than raising.** One dead resume link must never kill a batch run.
- **Persist incrementally.** Write each candidate's result to the DB as it completes,
  not at the end of the batch.
- GitHub analysis must be **repository-level**, not profile stats. Fetch repos, drop
  forks, rank by recency/stars/substance, then for the top 3 pull README + language
  breakdown and send those to the LLM for qualitative assessment. Cache per username in
  the DB — batches get re-run constantly during development.
- Always use an authenticated GitHub PAT. Unauthenticated is 60 req/hour.
- Long-running work: the frontend polls a `jobs` table via the API. Streamlit reruns its
  script on every interaction, so it cannot hold background state.

## Conventions

- Type hints everywhere. Pydantic v2 syntax (`model_config`, `Field`), not v1.
- Config only via `backend/core/config.py` — no `os.getenv` scattered in modules.
- No secrets in code or committed files. `.env`, `credentials.json`, `token.json` are
  gitignored and must stay that way.
- Log with the stdlib `logging` module, not `print`.
- Keep route handlers thin; put logic in `services/`.
- DB engine must use pool_pre_ping=True and pool_recycle=300. Neon free tier
  scales to zero when idle; without pre-ping the first query after a pause
  fails on a stale connection. Wrap external calls (DB, GitHub, LLM, SMTP)
  in tenacity retry with exponential backoff — transient failures are normal,
  not exceptional.

## Commands

```bash
# backend  (http://localhost:8000, docs at /docs)
uvicorn backend.main:app --reload --port 8000

# frontend (http://localhost:8501)
streamlit run frontend/app.py
```
