"""Application configuration.

Every environment variable is declared here exactly once. No other module may
call ``os.getenv`` — import :func:`get_settings` instead. Values are loaded from
the process environment and, if present, a local ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Batch evaluation profiles (§4.5). A deliberate free-tier trade-off:
#:  - "quality": produce the stored demo run — spaced out, long 429 cooldown,
#:    and a pre-emptive wait for a free Groq key (capped) so it stays on
#:    gpt-oss-120b. ~8-10 min for 10 candidates.
#:  - "fast": the DEPLOYMENT default — a reviewer uploading their own CSV falls
#:    straight to the Mistral fallback rather than waiting. ~3 min.
#: On a paid Groq tier the rate ceiling disappears and both collapse into one.
_EVAL_PROFILES: dict[str, dict] = {
    "fast": {
        "stagger": 5.0,
        "cooldown": 60.0,
        "preemptive_wait": False,
        "preemptive_cap": 0.0,
    },
    "quality": {
        "stagger": 20.0,
        "cooldown": 120.0,
        "preemptive_wait": True,
        "preemptive_cap": 90.0,
    },
}


class Settings(BaseSettings):
    """Typed view over the ``.env`` file (see ``env.example``)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Database: Neon Postgres --------------------------------------------
    database_url: str = Field(
        ...,
        validation_alias="DATABASE_URL",
        description="Neon pooled connection string; must include sslmode=require.",
    )

    # ---- LLM primary: a POOL of Groq API keys, all on the same gpt-oss-120b --
    # Groq's rate limits (8000 TPM) are org-level, so each key from a separate
    # account is an independent bucket on the SAME model — horizontal inference
    # capacity with zero effect on scoring quality. GROQ_API_KEYS is a
    # comma-separated list; GROQ_API_KEY (singular) still works as a one-key pool.
    groq_api_keys: str = Field("", validation_alias="GROQ_API_KEYS")
    groq_api_key: str = Field("", validation_alias="GROQ_API_KEY")
    groq_model: str = Field(
        "openai/gpt-oss-120b",
        validation_alias="GROQ_MODEL",
        description="Groq model id; the LLM factory reads this, never a literal.",
    )

    # ---- LLM fallback: Mistral (only when every Groq key is cooling down) ---
    mistral_api_key: str = Field("", validation_alias="MISTRAL_API_KEY")
    mistral_model: str = Field(
        "ministral-8b-latest", validation_alias="MISTRAL_MODEL"
    )

    @property
    def groq_key_pool(self) -> list[str]:
        """Ordered, de-duplicated Groq API keys.

        ``GROQ_API_KEYS`` (comma-separated) first, then the singular
        ``GROQ_API_KEY`` if it is not already in the list.
        """
        keys: list[str] = []
        for raw in (self.groq_api_keys or "").split(","):
            key = raw.strip()
            if key and key not in keys:
                keys.append(key)
        if self.groq_api_key and self.groq_api_key not in keys:
            keys.append(self.groq_api_key)
        return keys

    # ---- GitHub -----------------------------------------------------------------
    github_token: str = Field(
        "",
        validation_alias=AliasChoices("GITHUB_TOKEN", "GITHUB_PAT"),
        description="Fine-grained PAT; unauthenticated GitHub is 60 req/hr.",
    )

    # ---- Email: Gmail SMTP over STARTTLS ------------------------------------
    smtp_host: str = Field("smtp.gmail.com", validation_alias="SMTP_HOST")
    smtp_port: int = Field(587, validation_alias="SMTP_PORT")
    smtp_user: str = Field("", validation_alias="SMTP_USER")
    smtp_password: str = Field("", validation_alias="SMTP_PASSWORD")
    smtp_from_name: str = Field(
        "Visl AI Labs Recruitment", validation_alias="SMTP_FROM_NAME"
    )

    # ---- Assessment link mailed to the shortlist -------------------------------
    test_link_url: str = Field("", validation_alias="TEST_LINK_URL")

    # ---- Google Calendar OAuth (Desktop-app client + refresh token) ---------
    google_client_id: str = Field("", validation_alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field("", validation_alias="GOOGLE_CLIENT_SECRET")
    google_refresh_token: str = Field("", validation_alias="GOOGLE_REFRESH_TOKEN")
    google_calendar_id: str = Field("primary", validation_alias="GOOGLE_CALENDAR_ID")

    # ---- App config ---------------------------------------------------------
    api_base_url: str = Field(
        "http://localhost:8000", validation_alias="API_BASE_URL"
    )
    environment: str = Field("development", validation_alias="ENVIRONMENT")
    log_level: str = Field("INFO", validation_alias="LOG_LEVEL")

    # ---- Resume / JD scoring weights (deterministic aggregation) -------------
    w_resume_jd: float = Field(0.40, validation_alias="W_RESUME_JD")
    w_github: float = Field(0.25, validation_alias="W_GITHUB")
    w_projects: float = Field(0.20, validation_alias="W_PROJECTS")
    w_academics: float = Field(0.15, validation_alias="W_ACADEMICS")

    # ---- Final score weights (pre-test blended with test results) -----------
    w_pre_test: float = Field(0.60, validation_alias="W_PRE_TEST")
    w_test_la: float = Field(0.20, validation_alias="W_TEST_LA")
    w_test_code: float = Field(0.20, validation_alias="W_TEST_CODE")

    # ---- Pre-test blend: resume score vs GitHub score (§4.5) ---------------
    pretest_resume_weight: float = Field(
        0.60, validation_alias="PRETEST_RESUME_WEIGHT"
    )
    pretest_github_weight: float = Field(
        0.40, validation_alias="PRETEST_GITHUB_WEIGHT"
    )

    # ---- Batch evaluation throughput vs Groq's 8000 tokens/min free tier --
    # 8000 TPM is a token-RATE ceiling per key. The EVALUATION_MODE profile
    # (below) sets the inter-candidate stagger and the per-key 429 cooldown.
    #
    # The stagger does NOT scale with pool size: each candidate's resume +
    # GitHub calls fan out in PARALLEL and the key pool round-robins them onto
    # *different* keys, so every candidate loads every key at once. The stagger
    # therefore governs per-key recovery time and must stay fixed regardless of
    # how many keys are in the pool.
    batch_concurrency: int = Field(1, validation_alias="BATCH_CONCURRENCY", ge=1)
    #: EVALUATION_MODE: "fast" (default, for deployment) or "quality" (for the
    #: stored demo run). See _EVAL_PROFILES / eval_profile().
    evaluation_mode: str = Field("fast", validation_alias="EVALUATION_MODE")
    #: >0 hard-overrides the active profile's stagger; 0 => use the profile.
    batch_stagger_seconds: float = Field(
        0.0, validation_alias="BATCH_STAGGER_SECONDS", ge=0
    )
    #: >0 hard-overrides the active profile's LLM 429 cooldown; 0 => use it.
    llm_cooldown_seconds: float = Field(
        0.0, validation_alias="LLM_COOLDOWN_SECONDS", ge=0
    )
    resume_max_chars: int = Field(
        6000, validation_alias="RESUME_MAX_CHARS", ge=500,
        description="Resume text is truncated to this before the LLM call. Real "
        "resumes run 2000-3700 chars; this only caps outliers.",
    )
    github_readme_max_chars: int = Field(
        1500, validation_alias="GITHUB_README_MAX_CHARS", ge=200,
        description="Each analysed repo's README is truncated to this.",
    )

    # ---- Shortlisting -----------------------------------------------------------
    shortlist_top_n: int = Field(5, validation_alias="SHORTLIST_TOP_N")
    test_pass_threshold: float = Field(70.0, validation_alias="TEST_PASS_THRESHOLD")

    def eval_profile(self, mode: str | None = None) -> dict:
        """Resolve a batch to a profile of {mode, stagger, cooldown,
        preemptive_wait, preemptive_cap}.

        ``mode`` (per-request) wins over ``EVALUATION_MODE`` (env). Explicit
        ``BATCH_STAGGER_SECONDS`` / ``LLM_COOLDOWN_SECONDS`` (>0) hard-override
        the chosen profile's values.

        Deliberate free-tier trade-off: on a paid Groq tier the rate ceiling
        disappears and the two profiles collapse into one.
        """
        name = (mode or self.evaluation_mode or "fast").strip().lower()
        profile = dict(_EVAL_PROFILES.get(name, _EVAL_PROFILES["fast"]))
        profile["mode"] = name if name in _EVAL_PROFILES else "fast"
        if self.batch_stagger_seconds > 0:
            profile["stagger"] = self.batch_stagger_seconds
        if self.llm_cooldown_seconds > 0:
            profile["cooldown"] = self.llm_cooldown_seconds
        return profile

    @property
    def pretest_blend(self) -> dict[str, float]:
        """Resume/GitHub blend weights for :func:`scoring.compute_pre_test_score`."""
        return {
            "resume": self.pretest_resume_weight,
            "github": self.pretest_github_weight,
        }

    @property
    def evaluation_weights(self) -> dict[str, float]:
        """Pre-test dimension weights, keyed by :class:`FinalScore` field name."""
        return {
            "resume_jd": self.w_resume_jd,
            "github": self.w_github,
            "projects": self.w_projects,
            "academics": self.w_academics,
        }

    @property
    def final_weights(self) -> dict[str, float]:
        """Weights blending the pre-test score with the two test sub-scores."""
        return {
            "pre_test": self.w_pre_test,
            "test_la": self.w_test_la,
            "test_code": self.w_test_code,
        }


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached :class:`Settings` instance."""
    return Settings()  # type: ignore[call-arg]
