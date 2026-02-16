"""Application settings loaded from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """SpaceTraders configuration from .env file."""

    token: str = ""
    account_token: str = ""
    callsign: str = ""
    faction: str = "COSMIC"
    base_url: str = "https://api.spacetraders.io/v2"
    data_dir: Path = Path("data")

    model_config = {"env_prefix": "SPACETRADERS_", "env_file": ".env"}


def load_settings() -> Settings:
    """Load and return application settings."""
    return Settings()
