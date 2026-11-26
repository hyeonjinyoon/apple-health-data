from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


def _finite(v: float | None) -> float | None:
    if v is not None and not math.isfinite(v):
        raise ValueError("value must be finite")
    return v


Finite = Annotated[float, AfterValidator(_finite)]
OptFinite = Annotated[float | None, AfterValidator(_finite)]
ShortStr = Annotated[str, Field(max_length=512)]
TypeName = Annotated[str, Field(min_length=1, max_length=128)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class _Timed(_Base):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _to_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _order(self):
        if self.end < self.start:
            raise ValueError("end must not be before start")
        return self


class Sample(_Timed):
    uuid: UUID
    type: TypeName
    value: Finite
    unit: ShortStr = ""
    category: ShortStr = ""
    source: ShortStr = ""
    source_bundle: ShortStr = ""
    device: ShortStr = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class Workout(_Timed):
    uuid: UUID
    activity_type: TypeName
    duration_s: Annotated[Finite, Field(ge=0)]
    energy_kcal: OptFinite = None
    distance_m: OptFinite = None
    avg_heart_rate: OptFinite = None
    max_heart_rate: OptFinite = None
    source: ShortStr = ""
    source_bundle: ShortStr = ""
    device: ShortStr = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class Stat(_Timed):
    type: TypeName
    value: Finite
    unit: ShortStr = ""


class IngestRequest(_Base):
    device_id: ShortStr = ""
    device_name: ShortStr = ""
    app_version: ShortStr = ""
    samples: list[Sample] = Field(default_factory=list, max_length=50_000)
    workouts: list[Workout] = Field(default_factory=list, max_length=10_000)
    stats: list[Stat] = Field(default_factory=list, max_length=100_000)
    deleted: list[UUID] = Field(default_factory=list, max_length=50_000)


class IngestResponse(BaseModel):
    samples: int
    workouts: int
    stats: int
    deleted: int
