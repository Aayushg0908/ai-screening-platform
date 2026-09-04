"""Application configuration.

Every environment variable is declared here exactly once. No other module may
call ``os.getenv`` — import :func:`get_settings` instead. Values are loaded from
the process environment and, if present, a local ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # ---- LLM: Groq (primary) ---------------------------------------------------
    groq_api_key: str = Field("", validation_alias="GROQ_API_KEY")
    groq_model: str = Field(
        "openai/gpt-oss-120b",
        validation_alias="GROQ_MODEL",
        description="Groq model id; the LLM factory reads this, never a literal.",
    )

    # ---- LLM: Google Gemini (fallback on Groq rate-limit) --------------------
    google_api_key: str = Field(
        "",
        validation_alias=AliasChoices("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    )
    gemini_model: str = Field("gemini-2.0-flash", validation_alias="GEMINI_MODEL")

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

    # ---- Shortlisting -----------------------------------------------------------
    shortlist_top_n: int = Field(5, validation_alias="SHORTLIST_TOP_N")
    test_pass_threshold: float = Field(70.0, validation_alias="TEST_PASS_THRESHOLD")

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
