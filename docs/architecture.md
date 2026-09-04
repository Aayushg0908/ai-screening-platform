# Architecture

## Overview

## System Context

## Component Diagram

## Backend

### FastAPI Application

### Configuration

### Database Layer

### API Routes

### Services

## Evaluation Pipeline (LangGraph)

### State

### Nodes

### Graph Topology

### Parallelism and Error Handling

## LLM Integration

### Provider Factory

`services/llm.py` exposes a single `get_llm()` factory returning an `LLMClient`
that fronts a **round-robin pool of Groq API keys**, all serving the same
`gpt-oss-120b` model. Groq's 8000 TPM rate limit is org-level, so each key from a
separate account is an independent token bucket on an identical model — scoring
quality is unaffected. Per call, the client rotates its starting key; on a 429 it
moves straight to the next key (no sleep) and puts the throttled key on a ~60s
process-wide cooldown. Mistral (`ministral-8b-latest`) is the last resort,
reached only when every Groq key is cooling down.

**Horizontal scaling:** inference capacity is a config change with no pipeline
modification — add another key to `GROQ_API_KEYS` (comma-separated) and the pool
picks it up on the next `get_llm()`.

### Evaluation modes (`EVALUATION_MODE`)

A batch runs under one of two profiles, surfaced in the API response
(`RunResultsOut.mode`, `RunResultItem.model_used`) so the trade-off is visible:

| | `fast` (deployment default) | `quality` (stored demo run) |
|---|---|---|
| inter-candidate stagger | 5s | 20s |
| per-key 429 cooldown | 60s | 120s |
| pre-emptive wait for a free Groq key | no — drop to Mistral immediately | yes, capped at 90s |
| ~duration, 10 candidates | ~3 min | ~8–10 min |

`fast` keeps a reviewer who uploads their own CSV from watching a progress bar
for ten minutes; it accepts that some candidates are scored by the smaller
`ministral-8b` fallback. `quality` spaces work out and waits for a Groq key so
every candidate is scored by `gpt-oss-120b`.

**This is a deliberate free-tier trade-off.** On a paid Groq tier the per-minute
token ceiling disappears and both modes collapse into one — set
`BATCH_STAGGER_SECONDS` low, raise `BATCH_CONCURRENCY`, and the pipeline runs
fully parallel with no fallback.

The stagger does **not** scale with pool size: each candidate's resume + GitHub
calls fan out in parallel and the pool round-robins them onto *different* keys,
so every candidate loads every key at once. The stagger governs per-key recovery
and must stay fixed regardless of how many keys are configured.

### Known limitation: pre-emptive wait is per-candidate

In `quality` mode, `run_batch` checks the Groq pool before dispatching each
candidate and, if every key is cooling down, sleeps (capped at 90s) for a key to
free rather than dropping to Mistral. But a candidate fires **two** LLM calls in
parallel (resume + GitHub). When exactly one key is hot at dispatch time, the
first call takes it — and usually 429s it — so the second call, a few seconds
later, finds the pool exhausted and falls to Mistral. The batch-level guard
cannot hold a call it has already dispatched.

Measured effect on the stored demo run (`quality`, 10 candidates): 13 of 16 LLM
calls on `gpt-oss-120b`, 3 on the `ministral-8b` fallback (two GitHub calls, one
resume call for the lowest-scoring candidate), 10/10 candidates scored, zero
errors.

**Fix identified, deprioritised for the deadline:** move the wait into
`LLMClient.invoke()` so it applies per call, not per candidate — then a
candidate's second call also waits for a free key. Optionally raise the 429
cooldown 120s → ~150s, since a freshly-freed key is sometimes re-throttled
within a second.

### Cost of the throttling

Actual inference time for 10 candidates is **~90 seconds**. The rest of a
`quality`-mode batch's ~8-minute duration is deliberate throttling to stay under
the free-tier 8000 TPM per key. On a paid tier, `BATCH_STAGGER_SECONDS=0` and
`BATCH_CONCURRENCY=5` bring the batch under two minutes with no code changes.

### Structured Output

### Fallback Strategy

## Scoring Model

### Dimensions and Weights

### Deterministic Aggregation

### Test Result Blending

## GitHub Analysis

### Repository Selection

### Caching

## Data Ingestion

### Candidate Dataset

### Dataset Traps and Mitigations

### Test Results Merge

## Outreach

### Email Delivery

## Interview Scheduling

### Google Calendar and Meet

## Frontend (Streamlit)

### Pages

### API Client

### Polling Model

## Deployment

### Environment Variables

### Free-Tier Constraints

## Sequence: End-to-End Run

## Future Work
Actual inference time for 10 candidates is ~90 seconds. The remaining batch duration is deliberate throttling to stay within free-tier token limits. On a paid tier, BATCH_STAGGER_SECONDS=0 and BATCH_CONCURRENCY=5 reduce the batch to under two minutes with no code changes.
The batch-level pre-emptive guard checks key availability once per candidate, but each candidate dispatches two parallel LLM calls. When exactly one key is hot at dispatch, the first call consumes it and the second finds the pool exhausted, falling back. A per-call guard inside the LLM client would close this — a known limitation documented rather than hidden.