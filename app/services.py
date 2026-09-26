"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .core.stages import (
    StageRuleError,
    StageRuleSet,
    build_rule_set,
    classify_event_stages,
    replay_stages,
)
from .repository import (
    get_freeze,
    get_plan,
    get_stage_freeze,
    insert_events,
    insert_freeze,
    insert_stage_freeze,
    insert_stage_rule_version,
    latest_stage_rule_version,
    list_stage_freezes,
    list_stage_rule_versions,
    load_events,
    load_events_up_to,
    max_event_id,
    get_stage_rule_version,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class StageRulesNotFoundError(Exception):
    pass


class StageNotFoundError(Exception):
    pass


class StageOrderError(Exception):
    """前置阶段尚未关账。"""


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    allowed, rejected = _filter_closed_stage_events(db, plan_version, events)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=allowed
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": rejected,
    }


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


# ---------------------------------------------------------------------------
# 分阶段培养：规则配置 / 查询 / 预警 / 关账
# ---------------------------------------------------------------------------


def _core_event_from_payload(
    plan_version: str, raw: dict[str, Any]
) -> CoreEvent:
    return CoreEvent(
        event_id=raw["event_id"],
        plan_version=plan_version,
        event_type=EventType(raw["event_type"]),
        student_id=raw["student_id"],
        payload=dict(raw["payload"]),
        created_at=datetime.now(timezone.utc),
    )


def _active_rule_row(db: Session, plan_version: str, rules_version: int | None):
    if rules_version is not None:
        row = get_stage_rule_version(db, plan_version, rules_version)
        if row is None:
            raise StageRulesNotFoundError(
                f"stage rules version {rules_version} not found for plan "
                f"'{plan_version}'"
            )
        return row
    row = latest_stage_rule_version(db, plan_version)
    if row is None:
        raise StageRulesNotFoundError(
            f"no stage rules published for plan '{plan_version}'"
        )
    return row


def _rule_set_from_row(row) -> StageRuleSet:
    return build_rule_set(
        plan_version=row.plan_version,
        timezone=row.iana_timezone,
        rules_version=row.rules_version,
        stages=list(row.stages),
    )


def publish_stage_rules(
    db: Session, *, plan_version: str, stages: list[dict[str, Any]]
) -> dict[str, Any]:
    """发布一个不可变的新阶段规则版本（版本号单调递增）。"""
    plan = _require_plan(db, plan_version)
    latest = latest_stage_rule_version(db, plan_version)
    next_version = 1 if latest is None else latest.rules_version + 1
    # 先构造校验，非法配置直接拒绝，不落库。
    rules = build_rule_set(
        plan_version=plan_version,
        timezone=plan.iana_timezone,
        rules_version=next_version,
        stages=stages,
    )
    row = insert_stage_rule_version(
        db,
        plan_version=plan_version,
        rules_version=next_version,
        iana_timezone=plan.iana_timezone,
        stages=[s.to_rule_dict() for s in rules.stages],
    )
    if row is None:
        # 并发发布：版本号被抢占，返回已经落库的那个版本。
        winner = latest_stage_rule_version(db, plan_version)
        assert winner is not None
        return _rule_row_to_dict(winner)
    return _rule_row_to_dict(row)


def _rule_row_to_dict(row) -> dict[str, Any]:
    return {
        "plan_version": row.plan_version,
        "rules_version": row.rules_version,
        "iana_timezone": row.iana_timezone,
        "stages": list(row.stages),
    }


def list_stage_rule_versions_plain(db: Session, plan_version: str) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    return [_rule_row_to_dict(r) for r in list_stage_rule_versions(db, plan_version)]


def _closed_state(
    db: Session, rules: StageRuleSet
) -> tuple[set[str], dict[str, dict[str, dict[str, Any]]]]:
    """读取所有已关账阶段及其按学生保存的凭证。"""
    frozen_ids: set[str] = set()
    closed: dict[str, dict[str, dict[str, Any]]] = {}
    for freeze in list_stage_freezes(db, rules.plan_version):
        if freeze.rules_version != rules.rules_version:
            # 旧规则版本下的关账在当前版本中不直接生效；新版本需要重新关账。
            continue
        frozen_ids.add(freeze.stage_id)
        for entry in freeze.snapshot.get("students", []):
            closed.setdefault(entry["student_id"], {})[freeze.stage_id] = entry["stage"]
    return frozen_ids, closed


def _stage_replay(db: Session, rules: StageRuleSet):
    frozen_ids, closed = _closed_state(db, rules)
    events = load_events(db, rules.plan_version)
    state = replay_stages(
        events,
        rules,
        frozen_stage_ids=frozen_ids,
        closed_stage_progress=closed,
    )
    return state, frozen_ids


def student_stage_progress(
    db: Session,
    plan_version: str,
    student_id: str,
    *,
    rules_version: int | None = None,
) -> dict[str, Any] | None:
    row = _active_rule_row(db, plan_version, rules_version)
    rules = _rule_set_from_row(row)
    state, _ = _stage_replay(db, rules)
    progress = state.students.get(student_id)
    if progress is None:
        return None
    result = progress.to_dict()
    result["plan_version"] = plan_version
    result["rules_version"] = rules.rules_version
    result["timezone"] = rules.timezone
    return result


def stage_warnings(
    db: Session,
    plan_version: str,
    *,
    stage_id: str | None = None,
    rules_version: int | None = None,
) -> dict[str, Any]:
    row = _active_rule_row(db, plan_version, rules_version)
    rules = _rule_set_from_row(row)
    if stage_id is not None and rules.get(stage_id) is None:
        raise StageNotFoundError(
            f"stage '{stage_id}' is not defined in rules version "
            f"{rules.rules_version}"
        )
    state, _ = _stage_replay(db, rules)
    if stage_id is not None:
        warnings = state.stage_warnings(stage_id)
    else:
        warnings = []
        for stage in rules.stages:
            warnings.extend(state.stage_warnings(stage.stage_id))
    return {
        "plan_version": plan_version,
        "rules_version": rules.rules_version,
        "stage_id": stage_id,
        "warnings": warnings,
        "students_below": len(warnings),
    }


def freeze_stage(
    db: Session, *, plan_version: str, stage_id: str
) -> tuple[dict[str, Any], bool]:
    """顺序关账：前置阶段必须已关账；同一阶段重复关账幂等。"""
    row = _active_rule_row(db, plan_version, None)
    rules = _rule_set_from_row(row)
    stage = rules.get(stage_id)
    if stage is None:
        raise StageNotFoundError(
            f"stage '{stage_id}' is not defined in rules version "
            f"{rules.rules_version}"
        )

    existing = get_stage_freeze(db, plan_version, rules.rules_version, stage_id)
    if existing is not None:
        return _stage_freeze_to_dict(existing), False

    state, frozen_ids = _stage_replay(db, rules)
    predecessors = [s.stage_id for s in rules.predecessors(stage_id)]
    missing = [pid for pid in predecessors if pid not in frozen_ids]
    if missing:
        raise StageOrderError(
            f"stage '{stage_id}' cannot be frozen before predecessor stage(s): "
            + ", ".join(missing)
        )

    cutoff = max_event_id(db, plan_version)
    students_payload = []
    for sid in sorted(state.students):
        stage_dict = next(
            s.to_dict()
            for s in state.students[sid].stages
            if s.stage_id == stage_id
        )
        # 凭证一旦写入即代表已关账。
        stage_dict["frozen"] = True
        students_payload.append({"student_id": sid, "stage": stage_dict})
    snapshot = {
        "plan_version": plan_version,
        "stage_id": stage_id,
        "rules_version": rules.rules_version,
        "event_cutoff_id": cutoff,
        "students": students_payload,
    }
    stored = insert_stage_freeze(
        db,
        plan_version=plan_version,
        stage_id=stage_id,
        rules_version=rules.rules_version,
        snapshot=snapshot,
        event_cutoff_id=cutoff,
        required_predecessors=predecessors,
    )
    if stored is None:
        # 原子守卫落败：要么并发下已有凭证（幂等），要么前置在并发中尚未就绪。
        winner = get_stage_freeze(
            db, plan_version, rules.rules_version, stage_id
        )
        if winner is not None:
            return _stage_freeze_to_dict(winner), False
        raise StageOrderError(
            f"stage '{stage_id}' cannot be frozen before predecessor stage(s) "
            "are committed"
        )
    return _stage_freeze_to_dict(stored), True


def _stage_freeze_to_dict(row) -> dict[str, Any]:
    return {
        "plan_version": row.plan_version,
        "stage_id": row.stage_id,
        "rules_version": row.rules_version,
        "event_cutoff_id": row.event_cutoff_id,
        "created": True,
        "created_at": row.created_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "students": list(row.snapshot.get("students", [])),
    }


def _filter_closed_stage_events(
    db: Session, plan_version: str, events: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """阶段规则生效后，事件必须能按业务时间归属到具体阶段。

    可归属的事件（包括完全落在已关账阶段、或横跨关账边界的迟到事件）一律
    追加入库以保留审计轨迹；重放时剪掉关账段，从而“可更新未冻结阶段但不
    能穿透已关账边界”。无法归属的事件（请假修正缺少 business_date、负载
    非法、业务时间落在全部阶段窗口之外）直接拒绝。
    """
    latest = latest_stage_rule_version(db, plan_version)
    if latest is None:
        return events, []
    rules = _rule_set_from_row(latest)

    index_events = load_events(db, plan_version)
    checkin_index = {
        e.event_id: e
        for e in index_events
        if e.event_type == EventType.CHECKIN
    }
    allowed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in events:
        event = _core_event_from_payload(plan_version, raw)
        if event.event_type == EventType.CHECKIN:
            checkin_index[event.event_id] = event
    for raw in events:
        event = _core_event_from_payload(plan_version, raw)
        if event.event_type == EventType.LEAVE_CORRECTION:
            has_business_date = bool(
                event.payload.get("business_date")
                or event.payload.get("occurred_at")
            )
            if not has_business_date:
                rejected.append(
                    {
                        "event_id": event.event_id,
                        "reason": "leave_correction requires business_date "
                        "when stage rules are active",
                        "stage_ids": [],
                    }
                )
                continue
        try:
            touched = classify_event_stages(event, rules, checkin_index)
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append(
                {
                    "event_id": event.event_id,
                    "reason": f"invalid event payload: {exc}",
                    "stage_ids": [],
                }
            )
            continue
        if not touched:
            rejected.append(
                {
                    "event_id": event.event_id,
                    "reason": "event business time is outside every defined "
                    "stage boundary",
                    "stage_ids": [],
                }
            )
            continue
        # 触碰已关账阶段的事件仍然入库；重放层负责剪枝，保证不穿透关账边界。
        allowed.append(raw)
    return allowed, rejected
