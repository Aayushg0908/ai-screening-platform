# AI Screening Platform — Visl AI Labs

AI-powered candidate screening platform that automates the recruitment workflow:
ingest a candidate dataset, evaluate candidates against a job description with an
LLM, analyze their GitHub at repository level, score and rank them, email a test
link to the shortlist, ingest test results, and schedule interviews on Google
Calendar with auto-generated Meet links.

**Deadline: 12:00 noon, 6 Sept 2026.** A working end-to-end flow beats any single
polished component. If time runs short, cut UI polish and test coverage — never
cut Google Calendar or the hosted deployment, both are explicit constraints.

---

## Assignment requirements — the definition of done

Every item below is graded. Nothing here is optional.

### Functional requirements (§4)

| # | Requirement | Where it lives |
|---|---|---|
| 4.1 | Recruiter uploads candidate CSV | `services/ingest.py`, `POST /candidates/upload` |
| 4.2 | Download resumes from links, extract info | `services/resume.py` |
| 4.3 | AI evaluation against a job description | `graph/nodes.py::evaluate_vs_jd` |
| 4.4 | Fetch + analyze GitHub repositories | `services/github.py` |
| 4.5 | Score and rank candidates | `services/scoring.py` |
| 4.6 | Automated email with test link, own email service | `services/mailer.py` (Gmail SMTP) |
| 4.7 | Recruiter uploads test results CSV | `services/results.py`, `POST /results/upload` |
| 4.8 | Auto-schedule interviews, Google Calendar + Meet | `services/calendar.py` |

The system must **support uploading similar CSV datasets dynamically** (§2) — do
not hardcode column positions or assume the sample file's exact schema. Match
columns case-insensitively with a synonym map, and fail with a clear error naming
the missing column rather than raising a KeyError.

### Constraints (§5)

- **Publicly hosted.** Both backend and frontend must be reachable by URL.
- **Real Google Calendar integration.** No mocks, no fake links. Events must
  actually appear on a calendar with a working Meet link.
- **GitHub analysis must be repository-level.** Profile stats (followers, total
  stars) alone do not satisfy this. Fetch individual repos and evaluate contents.
- **Open-source LLM preferred.** Using `openai/gpt-oss-120b` (open-weights).

### Deliverables (§6)

1. Hosted application — public URL
2. GitHub repository — source + setup instructions
3. Architecture document — `docs/architecture.md`, system design + AI evaluation approach
4. Demo video, 5–10 min — full workflow

### Evaluation criteria (§7) — what to optimise for

System design quality · AI reasoning and evaluation approach · Automation of the
workflow · Code quality and engineering practices · GitHub analysis methodology ·
Scalability considerations.

### Bonus (§8) — all four are cheap if designed in from the start

- **Explainable AI scoring** — every score carries reasoning + evidence, surfaced in the UI
- **Recruiter dashboard** — the Streamlit frontend
- **Intelligent candidate ranking** — multi-dimensional weighted scoring, not one LLM number
- **Scalable architecture** — stateless backend, DB-backed state, caching, provider abstraction

Anything not on these lists is optional polish. Do not add features that aren't
required — the deadline is the binding constraint.

---

## Stack

- **Backend:** FastAPI + Uvicorn. All business logic lives here.
- **Orchestration:** LangGraph (per-candidate evaluation graph), LangChain for LLM I/O.
- **Frontend:** Streamlit. Thin UI layer only — calls the FastAPI HTTP API,
  contains **no business logic**. Never import from `backend/` inside `frontend/`.
- **DB:** Postgres (Neon free tier) via SQLModel. Never SQLite — hosts have
  ephemeral filesystems and the DB would vanish on redeploy.
- **LLM:** Groq `openai/gpt-oss-120b` (open-weights, free tier). Google Gemini via
  `langchain-google-genai` as fallback on rate limit. Both behind `services/llm.py`
  so the provider swaps in one place.
  **Note:** Groq deprecated all Llama models in 2026. Never use
  `llama-3.3-70b-versatile` or any `llama-*` id — they return 404. Model ids come
  from config, never hardcoded.
- **Python 3.11** exactly.

Everything runs on free tiers. No paid services.

---

## Layout

```
backend/
  main.py              FastAPI app, CORS, /health, router registration
  api/routes/          HTTP endpoints — thin, delegate to services
  core/config.py       pydantic-settings, single source of config truth
  core/db.py           engine + session dependency
  core/logging.py      stdlib logging
  models/tables.py     SQLModel ORM tables
  models/schemas.py    Pydantic request/response + LLM structured-output models
  services/            ingest, resume, github, scoring, mailer, calendar, llm,
                       jobs, evaluation, outreach, results, interviews
  graph/               LangGraph state, nodes, pipeline
frontend/
  app.py               Streamlit entry
  pages/               one file per workflow stage
  lib/api_client.py    the ONLY module that talks HTTP to the backend
scripts/
  preflight.py         credential smoke test — DO NOT MODIFY
  google_oauth_setup.py  one-time refresh token — DO NOT MODIFY
docs/architecture.md   deliverable §6.3
data/                  sample dataset
tests/
```

---

## Dataset traps — verified against the provided workbook

Two sheets: `Response` (10 rows) and `Test Result` (8 rows).

1. **Every candidate shares one email address.** All 10 rows in `Response` use
   `rishabh.choudhary+arnav@mynachiketa.com`. `Test Result` uses a *different*
   plus-tag (`...+assignment@...`). Therefore:
   - **Never** use email as a primary key, unique constraint, or dedupe field.
   - **Never** join the two datasets on email — zero rows would match.
   - Join on `s_no`. Internal PK is a surrogate `candidate_id`.
2. **5 of 10 candidates have no GitHub URL** (s_no 3, 5, 6, 7, 10). The GitHub node
   must degrade gracefully: return an explicit "no profile" state with a reason,
   never crash, never silently score 0. Candidate 7 has a GitHub URL buried in the
   `research_work` free text — implement a regex fallback that scrapes
   `github.com/<user>` from text fields when the `github` column is empty.
3. **The `Response` sheet's `test_la`/`test_code` columns are stale** and
   contradict the `Test Result` sheet for 6 of 8 overlapping candidates. Drop them
   on candidate ingest. The uploaded test-results file is the sole authority.
4. **`Test Result` is missing s_no 4 and 10.** Merge is a LEFT join with an
   explicit `NO_RESULT` state. Do not drop those candidates, do not impute zero.
5. `research_work` is null for 3 candidates — optional field.
6. CGPA carries float noise (`8.2200000000000006`) — round to 2dp on ingest.
7. College names have trailing whitespace and inconsistent casing
   (`netaji subhas university of technology`) — normalize, but keep the raw value
   in a separate column.
8. All resume links are Google Drive `/file/d/{ID}/view` URLs. Convert to
   `https://drive.google.com/uc?export=download&id={ID}`. Drive sometimes returns
   an HTML interstitial instead of the PDF — **check `Content-Type` before handing
   bytes to pypdf** and record a per-candidate error if it isn't a PDF.

---

## Design rules

**Scoring is deterministic Python arithmetic, never LLM-computed.** The LLM emits
per-dimension scores with reasoning; `services/scoring.py` does the weighted sum.
Weights live in config so they're tunable and auditable.

```
pre_test = 0.40*resume_jd + 0.25*github + 0.20*projects + 0.15*academics
final    = 0.60*pre_test  + 0.20*test_la + 0.20*test_code
```

**All LLM output is structured** via `llm.with_structured_output(PydanticModel)`.
Never parse free text. Every dimension carries `score`, `reasoning`, and
`evidence` so any ranking can be explained in the UI (§8 explainable scoring).

**Every graph node wraps work in try/except and appends to `state["errors"]`
rather than raising.** One dead resume link must never kill a batch.

**Persist incrementally** — write each candidate's result as it completes, not at
the end of the batch.

**GitHub analysis is repository-level** (§5, hard constraint). Fetch repos, drop
forks, rank by recency/stars/substance, then for the top 3 pull README + language
breakdown and send those to the LLM for qualitative assessment. Combine with
deterministic signals (commit recency, original-repo count, language diversity).
Cache per username in the DB — batches get re-run constantly during development.
Always use the authenticated PAT; unauthenticated is 60 req/hour.

**Resilience.** DB engine uses `pool_pre_ping=True` and `pool_recycle=300` — Neon's
free tier scales to zero when idle and the first query after a pause fails on a
stale connection. Wrap every external call (DB, GitHub, LLM, SMTP, Calendar) in
tenacity retry with exponential backoff. Transient failures are normal.

**Calendar.** `services/calendar.py` MUST pass `conferenceDataVersion=1` on
`events().insert()`. Without it Google silently drops the `conferenceData` block
and returns a valid event with no Meet link and no error.

**Long-running work.** Streamlit reruns its script on every interaction and cannot
hold background state. The frontend polls a `PipelineRun` row via the API for
progress.

---

## Conventions

- Type hints everywhere. Pydantic v2 syntax (`model_config`, `Field`), not v1.
- Config only through `backend/core/config.py` — no `os.getenv` scattered in
  backend modules. (`frontend/lib/api_client.py` reading `API_BASE_URL` directly
  is the one intentional exception, since frontend cannot import backend.)
- No secrets in code or committed files. `.env`, `credentials.json`, `token.json`
  are gitignored and must stay that way.
- Log with stdlib `logging`, never `print`.
- Route handlers stay thin; logic goes in `services/`.
- Commit at every phase boundary with a real message — commit history is evidence
  for the "engineering practices" criterion (§7).

---

## Build phases

Work one phase at a time. Each ends with something runnable, tests passing, and a
commit. Do not start the next phase on a broken foundation.

| Phase | Scope | Done when |
|---|---|---|
| 1 | Ingest (§4.1) | Uploading the workbook puts 10 correct rows in Postgres; re-upload doesn't duplicate |
| 2 | Resume processing (§4.2) | Text extracted for most candidates, explicit logged reason for any failure |
| 3 | LLM evaluation (§4.3) | One candidate yields valid per-dimension scores with reasoning + evidence |
| 4 | GitHub analysis (§4.4) | All 10 return either an evaluation or an explicit no-profile state |
| 5 | Graph + scoring (§4.5) | One API call evaluates all 10 and returns them ranked with full breakdown |
| 6 | Email (§4.6) | Shortlisted candidates receive a real email with the test link |
| 7 | Test results (§4.7) | Second upload left-joins on s_no, s_no 4 and 10 marked NO_RESULT, finals recomputed |
| 8 | Calendar (§4.8) | Real events on a real calendar with working Meet links |
| 9 | Frontend (§8 dashboard) | All six pages working against the live backend |
| 10 | Deploy + docs + video (§6) | All four deliverables complete |

Phase 5 is the milestone that matters — everything before it is a component,
that's the first working system.

---

## Testing

After each phase, write `tests/test_<phase>.py`:

- **Unit tests for pure functions** — Drive URL conversion, CGPA rounding, college
  normalization, s_no join logic, GitHub URL regex fallback, scoring arithmetic.
  Real assertions on real values, no mocks.
- **One integration test per phase** hitting live services, marked
  `@pytest.mark.integration` so it can be deselected.
- **Use `data/candidate_dataset.xlsx` as the fixture.** It contains the real edge
  cases. Do not invent clean fixture data — the messiness is the point.

Do NOT mock the thing under test. Do NOT assert on LLM output *quality* — assert
the response parses into the Pydantic model and scores fall in range. Do NOT write
tests for `NotImplementedError` stubs.

Run `pytest tests/ -v` and report failures. **Never modify source to make a test
pass without stating what changed and why.**

---

## Commands

```bash
# backend  (http://localhost:8000, docs at /docs)
uvicorn backend.main:app --reload --port 8000

# frontend (http://localhost:8501)
streamlit run frontend/app.py

# credential smoke test
python scripts/preflight.py --full

# tests
pytest tests/ -v -m "not integration"
```