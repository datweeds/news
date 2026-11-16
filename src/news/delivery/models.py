"""What a subscription asks for: named sections, each defined by search
phrases, plus places, a schedule and digest rules."""

from __future__ import annotations

import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SectionDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    heading: str = Field(min_length=1, max_length=80)
    phrases: list[str] = Field(min_length=1, max_length=12)
    must_include: list[str] = Field(default_factory=list, max_length=20)
    must_exclude: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("phrases", "must_include", "must_exclude")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for p in v:
            p = re.sub(r"\s+", " ", p).strip()
            if p and p.lower() not in {x.lower() for x in out}:
                if len(p) > 200:
                    raise ValueError("Each phrase must be at most 200 characters")
                out.append(p)
        return out

    @model_validator(mode="after")
    def _has_phrases(self) -> SectionDef:
        if not self.phrases:
            raise ValueError(f"Section '{self.heading}' needs at least one phrase")
        return self


class LocationDef(BaseModel):
    """A place, as returned by GET /v1/locations/resolve: a country, a region
    (country + admin1 code) or a town/city (with a radius)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    kind: Literal["country", "admin1", "place"]
    geonames_id: int | None = None
    country: str = Field(min_length=2, max_length=2)
    admin1: str | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    radius_km: float = Field(25, gt=0, le=500)

    @model_validator(mode="after")
    def _complete(self) -> LocationDef:
        if self.kind == "admin1" and not self.admin1:
            raise ValueError("A region needs its admin1 code")
        if self.kind == "place" and (self.lat is None or self.lon is None):
            raise ValueError("A place needs lat and lon")
        return self


class ScheduleDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cadence: Literal["daily", "weekly"] = "daily"
    time: str = Field("07:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekday: int = Field(0, ge=0, le=6, description="Weekly digests: 0 = Monday … 6 = Sunday")
    tz: str = "Europe/London"

    @field_validator("tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"Unknown time zone {v}") from e
        return v


class SubscriptionDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120, description="Edition name, e.g. 'New Society Daily News'")
    sections: list[SectionDef] = Field(min_length=1, max_length=12)
    locations: list[LocationDef] = Field(default_factory=list, max_length=20)
    min_score: int | None = Field(None, ge=0, le=100)
    max_items: int | None = Field(None, ge=1, le=50)
    max_per_section: int | None = Field(None, ge=1, le=20)
    send_empty: bool = False
    # False: no LLM for this subscription. Editions use the outlets' own
    # headlines, and its phrases don't make items eligible for enrichment.
    use_llm: bool = True

    @model_validator(mode="after")
    def _unique_keys(self) -> SubscriptionDef:
        keys = [s.key for s in self.sections]
        if len(keys) != len(set(keys)):
            raise ValueError("Section keys must be unique")
        return self
