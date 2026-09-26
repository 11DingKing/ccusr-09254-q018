"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int
    active_rule_version: str | None = None


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""
    business_time: datetime | None = None

    @field_validator("business_time")
    @classmethod
    def _ensure_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("business_time must be timezone-aware (RFC 3339)")
        return v


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]
    rule_version: str | None = None
    stages: list["StageProgressOut"] = Field(default_factory=list)


class CarrySourceOut(BaseModel):
    from_stage_id: str
    seconds: int


class CompensationSourceOut(BaseModel):
    from_category: str
    seconds: int


class CategoryProgressOut(BaseModel):
    category: str
    confirmed_seconds: int
    required_seconds: int
    compensated_seconds: int
    gap_seconds: int
    compensation_sources: list[CompensationSourceOut]


class StageProgressOut(BaseModel):
    stage_id: str
    frozen: bool
    start_at: str
    end_at: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    own_seconds: int
    carry_in_seconds: int
    carry_in_sources: list[CarrySourceOut]
    effective_seconds: int
    required_seconds: int
    gap_seconds: int
    carry_out_available_seconds: int
    categories: list[CategoryProgressOut]
    meets_requirement: bool


class CompensationRuleIn(BaseModel):
    from_category: str = Field(..., min_length=1, max_length=64)
    to_category: str = Field(..., min_length=1, max_length=64)
    max_seconds: int = Field(..., ge=0)

    @model_validator(mode="after")
    def _distinct_categories(self) -> "CompensationRuleIn":
        if self.from_category == self.to_category:
            raise ValueError("compensation categories must differ")
        return self


class CompensationRuleOut(BaseModel):
    from_category: str
    to_category: str
    max_seconds: int


class StageRuleIn(BaseModel):
    stage_id: str = Field(..., min_length=1, max_length=128)
    start_at: datetime
    end_at: datetime
    required_seconds: int = Field(0, ge=0)
    category_requirements: dict[str, int] = Field(default_factory=dict)
    carry_in_cap_seconds: int = Field(0, ge=0)
    carry_out_cap_seconds: int = Field(0, ge=0)
    compensations: list[CompensationRuleIn] = Field(default_factory=list)

    @field_validator("start_at", "end_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("stage boundaries must be timezone-aware (RFC 3339)")
        return v

    @field_validator("category_requirements")
    @classmethod
    def _requirements_non_negative(
        cls, v: dict[str, int]
    ) -> dict[str, int]:
        for name, required in v.items():
            if not name.strip():
                raise ValueError("category names must not be blank")
            if required < 0:
                raise ValueError("category requirements must be >= 0")
        return v

    @model_validator(mode="after")
    def _check_order(self) -> "StageRuleIn":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class RuleSetIn(BaseModel):
    stages: list[StageRuleIn] = Field(..., min_length=1)


class StageRuleOut(BaseModel):
    stage_id: str
    start_at: str
    end_at: str
    required_seconds: int
    category_requirements: dict[str, int]
    carry_in_cap_seconds: int
    carry_out_cap_seconds: int
    compensations: list[CompensationRuleOut]


class RuleSetOut(BaseModel):
    plan_version: str
    rule_version: str
    active: bool
    frozen_stage_ids: list[str]
    stages: list[StageRuleOut]
    created_at: str


class StageWarningOut(BaseModel):
    student_id: str
    stage_id: str
    frozen: bool
    required_seconds: int
    effective_seconds: int
    gap_seconds: int
    category_gaps: dict[str, int]


class WarningsOut(BaseModel):
    plan_version: str
    rule_version: str | None
    students_evaluated: int
    warnings: list[StageWarningOut]


class StageFreezeIn(BaseModel):
    pass


class StageFreezeOut(BaseModel):
    plan_version: str
    rule_version: str
    stage_id: str
    event_cutoff_id: str | None
    generated_at: str
    stage: dict[str, Any]
    students: list[dict[str, Any]]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int
