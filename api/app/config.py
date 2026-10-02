"""Runtime settings, read from the environment."""

from datetime import date
from functools import cached_property
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    database_url: SecretStr
    clinic_timezone: str = "America/New_York"
    clinic_today: date | None = Field(
        default=None,
        description="Fixed 'today' for reproducible demos and evals; real time when unset.",
    )
    mock_mode: bool = True

    @field_validator("clinic_timezone")
    @classmethod
    def _must_be_iana_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown IANA timezone {value!r}") from error
        return value

    @field_validator("clinic_today", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        return None if value == "" else value

    @cached_property
    def clinic_zone(self) -> ZoneInfo:
        return ZoneInfo(self.clinic_timezone)
