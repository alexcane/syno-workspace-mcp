"""Environment-driven settings, validated once at startup."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from syno_workspace_mcp import persisted

Module = Literal["drive", "spreadsheet"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

MIN_TOKEN_LENGTH = 32
DEFAULT_DATA_DIR = Path("/data")

# Placeholders shipped in deploy/.env.example and the Container Manager compose file.
_PLACEHOLDER = re.compile(r"change[-_]me", re.IGNORECASE)


def _split_csv(value: object) -> object:
    """Accept `a,b,c` from the environment in addition to a real list."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def data_dir_from_env() -> Path:
    for name, value in os.environ.items():
        if name.upper() == "DATA_DIR" and value:
            return Path(value)
    return DEFAULT_DATA_DIR


class PersistedSettingsSource(PydanticBaseSettingsSource):
    """Values saved by the `/setup` page, below environment variables in priority."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._values = persisted.load(data_dir_from_env())

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return {
            name: self._values[name]
            for name in self.settings_cls.model_fields
            if name in self._values and self._values[name] is not None
        }


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    """Server configuration.

    Field names map to environment variables case-insensitively
    (`syno_dsm_url` <- `SYNO_DSM_URL`).
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # Synology DSM
    syno_dsm_url: AnyHttpUrl
    syno_verify_tls: bool = True
    syno_username: str = Field(min_length=1)
    syno_password: SecretStr

    # Synology Spreadsheet API container
    syno_ss_api_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:3000")
    syno_ss_dsm_url: AnyHttpUrl | None = None
    syno_modules: Annotated[list[Module], NoDecode] = ["drive", "spreadsheet"]

    # MCP HTTP transport
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=3100, ge=1, le=65535)
    mcp_auth_token: SecretStr
    mcp_allowed_hosts: Annotated[list[str], NoDecode] = []

    data_dir: Path = DEFAULT_DATA_DIR
    log_level: LogLevel = "INFO"
    # Serve the /setup page (and not /mcp) even though a configuration exists.
    setup_mode: bool = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Environment > files written by /setup > defaults.
        return (init_settings, env_settings, PersistedSettingsSource(settings_cls))

    @field_validator("*", mode="before")
    @classmethod
    def _reject_placeholders(cls, value: object) -> object:
        """Fail fast on a value copied from the templates but never filled in."""
        items = value if isinstance(value, list) else [value]
        if any(isinstance(item, str) and _PLACEHOLDER.search(item) for item in items):
            raise ValueError("still holds a CHANGE_ME placeholder: replace it with your own value")
        return value

    @field_validator("syno_modules", "mcp_allowed_hosts", mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("syno_modules")
    @classmethod
    def _dedupe_modules(cls, value: list[Module]) -> list[Module]:
        if not value:
            raise ValueError("at least one module must be enabled")
        return list(dict.fromkeys(value))

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("mcp_auth_token")
    @classmethod
    def _check_token_strength(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"must be at least {MIN_TOKEN_LENGTH} characters (use `openssl rand -hex 32`)"
            )
        return value

    @model_validator(mode="after")
    def _default_ss_dsm_url(self) -> Settings:
        if self.syno_ss_dsm_url is None:
            self.syno_ss_dsm_url = self.syno_dsm_url
        return self

    @property
    def listens_on_loopback(self) -> bool:
        return is_loopback(self.mcp_host)

    def secret_values(self) -> list[str]:
        """Raw secret values the log redaction filter must never let through."""
        return [
            self.syno_password.get_secret_value(),
            self.mcp_auth_token.get_secret_value(),
        ]


class BootSettings(BaseSettings):
    """The few settings needed to serve the /setup page before anything is configured."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=3100, ge=1, le=65535)
    data_dir: Path = DEFAULT_DATA_DIR
    log_level: LogLevel = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value
