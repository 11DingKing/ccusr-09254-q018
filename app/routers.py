"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    SnapshotOut,
    StageFreezeOut,
    StageRuleOut,
    StageRulesIn,
    StageWarningsOut,
    StudentProgressOut,
    StudentStageProgressOut,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 分阶段培养：规则配置 / 学生查询 / 批量预警 / 阶段冻结
# ---------------------------------------------------------------------------


@router.put(
    "/plans/{plan_version}/stage-rules",
    response_model=StageRuleOut,
    status_code=status.HTTP_201_CREATED,
)
def put_stage_rules(
    plan_version: str, body: StageRulesIn, db: Session = Depends(get_db)
) -> Any:
    """发布一个不可变的新阶段规则版本（版本号自动递增）。"""
    try:
        return services.publish_stage_rules(
            db,
            plan_version=plan_version,
            stages=[s.model_dump(mode="json") for s in body.stages],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.StageRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/plans/{plan_version}/stage-rules", response_model=list[StageRuleOut])
def list_stage_rules(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.list_stage_rule_versions_plain(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/stage-progress",
    response_model=StudentStageProgressOut,
)
def get_student_stage_progress(
    plan_version: str,
    student_id: str,
    rules_version: int | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.student_stage_progress(
            db, plan_version, student_id, rules_version=rules_version
        )
    except (
        services.PlanNotFoundError,
        services.StageRulesNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/stage-warnings",
    response_model=StageWarningsOut,
)
def get_stage_warnings(
    plan_version: str,
    stage_id: str | None = None,
    rules_version: int | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.stage_warnings(
            db, plan_version, stage_id=stage_id, rules_version=rules_version
        )
    except (
        services.PlanNotFoundError,
        services.StageRulesNotFoundError,
        services.StageNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/stage-freezes/{stage_id}",
    response_model=StageFreezeOut,
    status_code=status.HTTP_201_CREATED,
)
def post_stage_freeze(
    plan_version: str,
    stage_id: str,
    db: Session = Depends(get_db),
) -> Any:
    """顺序关账某阶段；已关账时幂等返回原凭证（created=false）。"""
    try:
        result, created = services.freeze_stage(
            db, plan_version=plan_version, stage_id=stage_id
        )
    except (
        services.PlanNotFoundError,
        services.StageRulesNotFoundError,
        services.StageNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.StageOrderError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result["created"] = created
    return result
