"""Environment-driven settings (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass
class Settings:
    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "development"))
    service_api_key: str = field(default_factory=lambda: os.getenv("SERVICE_API_KEY", ""))

    hubspot_base_url: str = field(default_factory=lambda: os.getenv("HUBSPOT_BASE_URL", "https://api.hubapi.com"))
    hubspot_timeout: int = field(default_factory=lambda: _int("HUBSPOT_TIMEOUT_SECONDS", 30))
    hubspot_page_size: int = field(default_factory=lambda: min(_int("HUBSPOT_PAGE_SIZE", 100), 100))
    hubspot_max_retries: int = field(default_factory=lambda: _int("HUBSPOT_MAX_RETRIES", 5))
    rate_limit_requests: int = field(default_factory=lambda: _int("HUBSPOT_RATE_LIMIT_REQUESTS", 150))
    rate_limit_window: int = field(default_factory=lambda: _int("HUBSPOT_RATE_LIMIT_WINDOW_SECONDS", 10))

    pipeline_name: str = field(default_factory=lambda: os.getenv("PIPELINE_NAME", "hubspot_deals"))
    dataset_schema: str = field(default_factory=lambda: os.getenv("DATASET_SCHEMA", "hubspot_deals"))
    checkpoint_every_n_pages: int = field(default_factory=lambda: _int("CHECKPOINT_EVERY_N_PAGES", 5))
    pipelines_dir: str = field(default_factory=lambda: os.getenv("PIPELINES_DIR", ".pipelines"))

    destination: str = field(default_factory=lambda: os.getenv("DESTINATION", "duckdb"))
    postgres_user: str = field(default_factory=lambda: os.getenv("POSTGRES_USER", "dlt_user"))
    postgres_password: str = field(default_factory=lambda: os.getenv("POSTGRES_PASSWORD", "dlt_password"))
    postgres_db: str = field(default_factory=lambda: os.getenv("POSTGRES_DB", "hubspot_etl"))
    postgres_host: str = field(default_factory=lambda: os.getenv("POSTGRES_HOST", "localhost"))
    postgres_port: int = field(default_factory=lambda: _int("POSTGRES_PORT", 5432))
    duckdb_path: str = field(default_factory=lambda: os.getenv("DUCKDB_PATH", "data/hubspot.duckdb"))

    @property
    def postgres_url(self) -> str:
        return (f"postgresql://{self.postgres_user}:{self.postgres_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}")

    @property
    def jobs_database_url(self) -> str:
        """Job metadata lives in the same Postgres (or a local SQLite file for duckdb mode)."""
        if self.destination == "postgres":
            return self.postgres_url.replace("postgresql://", "postgresql+psycopg2://")
        return f"sqlite:///{self.duckdb_path}.jobs.sqlite"
