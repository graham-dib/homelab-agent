"""Centralised config loaded from .env via pydantic-settings."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8",
        extra="ignore")  # ignore unknown settings in .env rather than erroring)

    # dibo SSH
    dibo_ssh_host: str = "dibo.local"
    dibo_ssh_user: str
    dibo_ssh_key_path: str = "~/.ssh/id_ed25519"
    dibo_ssh_port: int = 22


settings = Settings()