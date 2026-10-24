from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOSTACGIS_", extra="ignore")
    database_url: str = "postgresql://autostacgis:autostacgis@127.0.0.1:5432/autostacgis"
    api_token: SecretStr = SecretStr("")
    data_dir: Path = Path(".data")
    public_url: str = "http://127.0.0.1:8080"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173", "app://autostacgis"]
    job_poll_seconds: float = Field(default=1, gt=0)
    result_collection: str = "autostacgis-results"
    plugin_registry_url: str = "https://autostacgis.github.io/autostacgis-plugins/plugins.json"
    workflow_registry_url: str = "https://autostacgis.github.io/autostacgis-workflows/workflows.json"
    max_inputs: int = Field(default=1000, ge=1)
