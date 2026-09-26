"""服务端业务模块。"""

from __future__ import annotations

from datetime import date, datetime
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


# ---------------------------------------------------------------------------
# 分阶段培养规则
# ---------------------------------------------------------------------------


class CategoryRequirementIn(BaseModel):
    category: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(..., ge=0)


class CompensationIn(BaseModel):
    from_category: str = Field(..., min_length=1, max_length=64)
    to_category: str = Field(..., min_length=1, max_length=64)
    rate_milli: int = Field(..., gt=0, le=1_000_000)

    @model_validator(mode="after")
    def _check_categories(self) -> "CompensationIn":
        if self.from_category == self.to_category:
            raise ValueError("compensation must link different categories")
        return self


class StageRuleIn(BaseModel):
    stage_id: str = Field(..., min_length=1, max_length=128)
    end_date: date
    required_seconds: int = Field(0, ge=0)
    carryover_cap_seconds: int | None = Field(None, ge=0)
    category_requirements: list[CategoryRequirementIn] = Field(default_factory=list)
    compensation: list[CompensationIn] = Field(default_factory=list)


class StageRulesIn(BaseModel):
    stages: list[StageRuleIn] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _check_ordering(self) -> "StageRulesIn":
        seen: set[str] = set()
        prev: date | None = None
        for stage in self.stages:
            if stage.stage_id in seen:
                raise ValueError(f"duplicate stage_id '{stage.stage_id}'")
            seen.add(stage.stage_id)
            if prev is not None and stage.end_date <= prev:
                raise ValueError(
                    f"stage '{stage.stage_id}' end_date must be after {prev}"
                )
            prev = stage.end_date
        return self


class StageRuleOut(BaseModel):
    plan_version: str
    rules_version: int
    iana_timezone: str
    stages: list[dict[str, Any]]


class CategoryGapOut(BaseModel):
    category: str
    required_seconds: int
    earned_seconds: int
    compensated_seconds: int
    gap_seconds: int
    met: bool


class CompensationOut(BaseModel):
    from_category: str
    to_category: str
    rate_milli: int
    offered_seconds: int
    applied_seconds: int


class CarryableSourceOut(BaseModel):
    category: str
    available_seconds: int


class StageProgressOut(BaseModel):
    stage_id: str
    end_date: str
    required_seconds: int
    carryover_cap_seconds: int | None
    earned_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    carry_in_seconds: int
    carry_in_source_stage: str | None
    total_gap_seconds: int
    category_gaps: list[CategoryGapOut]
    compensation_applied: list[CompensationOut]
    surplus_seconds: int
    carry_out_seconds: int
    spillover_lost_seconds: int
    carryable_sources: list[CarryableSourceOut]
    frozen: bool
    met: bool


class StudentStageProgressOut(BaseModel):
    plan_version: str
    rules_version: int
    timezone: str
    student_id: str
    meets_all: bool
    stages: list[StageProgressOut]


class StageWarningOut(BaseModel):
    student_id: str
    stage_id: str
    total_gap_seconds: int
    category_gaps: list[CategoryGapOut]
    carry_in_seconds: int
    carry_in_source_stage: str | None
    carry_out_seconds: int
    pending_seconds: int
    carryable_sources: list[CarryableSourceOut]


class StageWarningsOut(BaseModel):
    plan_version: str
    rules_version: int
    stage_id: str | None
    warnings: list[StageWarningOut]
    students_below: int


class StageFreezeStudentOut(BaseModel):
    student_id: str
    stage: StageProgressOut


class StageFreezeOut(BaseModel):
    plan_version: str
    stage_id: str
    rules_version: int
    event_cutoff_id: str | None
    created: bool
    created_at: str | None = None
    students: list[StageFreezeStudentOut]
