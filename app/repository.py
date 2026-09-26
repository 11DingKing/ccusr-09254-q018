"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, and_, exists, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import Freeze, Plan, StageFreeze, StageRuleVersion


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


# ---------------------------------------------------------------------------
# 阶段规则版本
# ---------------------------------------------------------------------------


def list_stage_rule_versions(db: Session, plan_version: str) -> list[StageRuleVersion]:
    stmt = (
        select(StageRuleVersion)
        .where(StageRuleVersion.plan_version == plan_version)
        .order_by(StageRuleVersion.rules_version)
    )
    return list(db.execute(stmt).scalars().all())


def get_stage_rule_version(
    db: Session, plan_version: str, rules_version: int
) -> StageRuleVersion | None:
    return db.get(StageRuleVersion, (plan_version, rules_version))


def latest_stage_rule_version(
    db: Session, plan_version: str
) -> StageRuleVersion | None:
    stmt = (
        select(StageRuleVersion)
        .where(StageRuleVersion.plan_version == plan_version)
        .order_by(StageRuleVersion.rules_version.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def insert_stage_rule_version(
    db: Session,
    *,
    plan_version: str,
    rules_version: int,
    iana_timezone: str,
    stages: list[dict[str, Any]],
) -> StageRuleVersion | None:
    """追加不可变规则版本；版本号冲突时返回 None（并发发布决胜）。"""
    stmt = sqlite_insert(StageRuleVersion).values(
        plan_version=plan_version,
        rules_version=rules_version,
        iana_timezone=iana_timezone,
        stages=stages,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "rules_version"]
    ).returning(StageRuleVersion.rules_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(StageRuleVersion, (plan_version, rules_version))
    return None


# ---------------------------------------------------------------------------
# 阶段关账
# ---------------------------------------------------------------------------


def list_stage_freezes(db: Session, plan_version: str) -> list[StageFreeze]:
    stmt = (
        select(StageFreeze)
        .where(StageFreeze.plan_version == plan_version)
        .order_by(StageFreeze.stage_id)
    )
    return list(db.execute(stmt).scalars().all())


def get_stage_freeze(
    db: Session, plan_version: str, rules_version: int, stage_id: str
) -> StageFreeze | None:
    return db.get(StageFreeze, (plan_version, rules_version, stage_id))


def insert_stage_freeze(
    db: Session,
    *,
    plan_version: str,
    stage_id: str,
    rules_version: int,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
    required_predecessors: list[str] | None = None,
) -> StageFreeze | None:
    """插入关账凭证。

    使用 ``INSERT ... SELECT ... WHERE`` 在同一条语句（SQLite 写锁）内原子
    校验“全部前置阶段已关账、且本阶段尚未关账”，从而杜绝 ST1/ST2 并发
    关账时后序阶段穿透顺序边界。任一条件不满足或并发落败时返回 None。
    """
    guard = literal(1)
    for predecessor in required_predecessors or []:
        pred_exists = (
            select(StageFreeze.plan_version)
            .where(StageFreeze.plan_version == plan_version)
            .where(StageFreeze.rules_version == rules_version)
            .where(StageFreeze.stage_id == predecessor)
            .limit(1)
        )
        guard = and_(guard, exists(pred_exists))
    self_exists = (
        select(StageFreeze.plan_version)
        .where(StageFreeze.plan_version == plan_version)
        .where(StageFreeze.rules_version == rules_version)
        .where(StageFreeze.stage_id == stage_id)
        .limit(1)
    )
    guard = and_(guard, ~exists(self_exists))

    base = sqlite_insert(StageFreeze)
    guarded_select = select(
        literal(plan_version),
        literal(stage_id),
        literal(rules_version),
        literal(event_cutoff_id),
        literal(snapshot, type_=JSON),
    ).where(guard)
    stmt = base.from_select(
        ["plan_version", "stage_id", "rules_version", "event_cutoff_id", "snapshot"],
        guarded_select,
    )
    # 唯一键并发冲突时也视为落败。
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "rules_version", "stage_id"]
    ).returning(StageFreeze.stage_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(StageFreeze, (plan_version, rules_version, stage_id))
    return None
