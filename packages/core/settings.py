"""Application configuration.

All settings come from the environment (``CA_`` prefix) or ``.env``. Nothing here
reads ``os.environ`` directly elsewhere in the codebase, so configuration is
inspectable in one place and testable by constructing ``Settings`` explicitly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProviderName = Literal["stub", "anthropic"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Runtime ---
    app_env: Literal["local", "test", "prod"] = "local"
    log_level: str = "INFO"

    # --- Storage ---
    database_url: str = "sqlite:///./data/career_agent.db"
    data_dir: Path = Path("./data")

    # --- LLM ---
    llm_provider: LLMProviderName = "stub"
    anthropic_api_key: SecretStr | None = None
    # Default to the most capable model; reasoning quality matters more than
    # per-token cost at this volume. Overridable per deployment.
    llm_model: str = "claude-opus-5"
    llm_max_retries: int = 2
    llm_timeout_seconds: int = 120

    # --- Job sources ---
    #: Greenhouse board tokens, comma-separated in the environment. Empty means the
    #: adapter stays unregistered rather than making pointless requests.
    greenhouse_boards: list[str] = Field(default_factory=list)
    #: Lever site slugs (the segment in jobs.lever.co/<slug>).
    lever_sites: list[str] = Field(default_factory=list)
    #: Ashby job board names (the segment in jobs.ashbyhq.com/<name>).
    ashby_boards: list[str] = Field(default_factory=list)
    #: SmartRecruiters company identifiers (the segment in jobs.smartrecruiters.com/<id>).
    smartrecruiters_companies: list[str] = Field(default_factory=list)

    @field_validator(
        "greenhouse_boards",
        "lever_sites",
        "ashby_boards",
        "smartrecruiters_companies",
        mode="before",
    )
    @classmethod
    def _split_boards(cls, value: object) -> object:
        """Accept "a,b,c" from the environment as well as a JSON list."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    #: Adzuna is an aggregator, not a per-company board: one free key covers every
    #: search. Both must be set for the adapter to register.
    adzuna_app_id: str | None = None
    adzuna_app_key: SecretStr | None = None
    #: Adzuna country code, e.g. "in", "gb", "us" — see developer.adzuna.com/docs.
    adzuna_country: str = "in"

    #: Arbeitnow needs no credentials at all, so an explicit opt-in avoids a
    #: discovery run silently reaching a third party when nobody configured it.
    arbeitnow_enabled: bool = False

    #: Company career page URLs to read via embedded schema.org JobPosting data
    #: (see services/jobs/sources.py:CareerPageSource). Comma-separated.
    career_pages: list[str] = Field(default_factory=list)

    @field_validator("career_pages", mode="before")
    @classmethod
    def _split_career_pages(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def adzuna_configured(self) -> bool:
        return (
            bool(self.adzuna_app_id)
            and self.adzuna_app_key is not None
            and bool(self.adzuna_app_key.get_secret_value())
        )

    # --- LaTeX ---
    latex_engine: str = "tectonic"
    latex_bin: Path = Path(".tooling/bin/tectonic")
    latex_timeout_seconds: int = 120

    # --- Safety ---
    # Master kill-switch. Even when true, every submission still requires an
    # explicit per-application human approval (PRD FR-54).
    allow_browser_submit: bool = False

    # --- Derived data locations (single source of truth for on-disk layout) ---
    @computed_field  # type: ignore[prop-decorator]
    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resumes_dir(self) -> Path:
        return self.data_dir / "resumes"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def applications_dir(self) -> Path:
        return self.data_dir / "applications"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def browser_dir(self) -> Path:
        return self.data_dir / "browser"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        """Create the local data tree. Safe to call repeatedly."""
        for path in (
            self.data_dir,
            self.raw_dir,
            self.processed_dir,
            self.resumes_dir,
            self.applications_dir,
            self.browser_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def llm_configured(self) -> bool:
        """True when the selected provider can actually service a request."""
        if self.llm_provider == "stub":
            return True
        return self.anthropic_api_key is not None and bool(
            self.anthropic_api_key.get_secret_value()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
