"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .core.stages import RuleError
from .db import get_db
from .schemas import (
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    RuleSetIn,
    RuleSetOut,
    SnapshotOut,
    StageFreezeIn,
    StageFreezeOut,
    StudentProgressOut,
    WarningsOut,
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


@router.put(
    "/plans/{plan_version}/rules/{rule_version}",
    response_model=RuleSetOut,
    status_code=status.HTTP_201_CREATED,
)
def put_rule_set(
    plan_version: str,
    rule_version: str,
    body: RuleSetIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.put_rule_set(
            db,
            plan_version=plan_version,
            rule_version=rule_version,
            stages=[s.model_dump() for s in body.stages],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.RuleSetFrozenError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/plans/{plan_version}/rules", response_model=list[RuleSetOut])
def list_rule_sets(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.list_rule_set_views(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/rules/{rule_version}",
    response_model=RuleSetOut,
)
def get_rule_set(
    plan_version: str, rule_version: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.get_rule_set_view(db, plan_version, rule_version)
    except (
        services.PlanNotFoundError,
        services.RuleSetNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/rules/{rule_version}/activate",
    response_model=PlanOut,
)
def activate_rule_set(
    plan_version: str, rule_version: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.activate_rule_set(db, plan_version, rule_version)
    except (
        services.PlanNotFoundError,
        services.RuleSetNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/warnings",
    response_model=WarningsOut,
)
def get_warnings(
    plan_version: str,
    stage_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.batch_warnings(db, plan_version, stage_id=stage_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/stages/{stage_id}/freeze",
    response_model=StageFreezeOut,
    status_code=status.HTTP_201_CREATED,
)
def post_stage_freeze(
    plan_version: str,
    stage_id: str,
    body: StageFreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snapshot, _ = services.freeze_stage(
            db, plan_version=plan_version, stage_id=stage_id
        )
        return snapshot
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.StageNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.NoActiveRuleSetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/stages/{stage_id}/freeze",
    response_model=StageFreezeOut,
)
def get_stage_freeze(
    plan_version: str,
    stage_id: str,
    rule_version: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.get_stage_freeze_view(
            db, plan_version, stage_id, rule_version=rule_version
        )
    except (
        services.PlanNotFoundError,
        services.StageFreezeNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.NoActiveRuleSetError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
