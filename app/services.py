"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .core.stages import (
    RuleSet,
    StageEval,
    build_rule_set,
    evaluate_stages,
    replay_staged,
    rule_set_from_spec,
    rule_set_to_spec,
    stage_eval_from_dict,
    stage_eval_to_dict,
    stage_rule_from_dict,
    stage_rule_to_dict,
    zero_stage_eval,
)
from .models import Plan
from .repository import (
    get_freeze,
    get_plan,
    get_rule_set,
    get_stage_freeze,
    insert_events,
    insert_freeze,
    insert_stage_freeze,
    list_frozen_stage_ids,
    list_rule_sets,
    list_stage_freezes,
    load_events,
    load_events_up_to,
    max_event_id,
    set_active_rule_version,
    upsert_plan,
    upsert_rule_set,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class RuleSetNotFoundError(Exception):
    pass


class RuleSetFrozenError(Exception):
    pass


class StageNotFoundError(Exception):
    pass


class StageFreezeNotFoundError(Exception):
    pass


class NoActiveRuleSetError(Exception):
    pass


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _plan_out(plan: Plan) -> dict[str, Any]:
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
        "active_rule_version": plan.active_rule_version,
    }


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return _plan_out(plan)


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
    return _plan_out(plan)


def _require_plan(db: Session, plan_version: str) -> Plan:
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
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


def _rule_set_out(db: Session, plan: Plan, row) -> dict[str, Any]:
    return {
        "plan_version": row.plan_version,
        "rule_version": row.rule_version,
        "active": plan.active_rule_version == row.rule_version,
        "frozen_stage_ids": list_frozen_stage_ids(
            db, row.plan_version, row.rule_version
        ),
        "stages": list(row.spec.get("stages", [])),
        "created_at": _iso_z(row.created_at),
    }


def put_rule_set(
    db: Session,
    *,
    plan_version: str,
    rule_version: str,
    stages: list[dict[str, Any]],
) -> dict[str, Any]:
    """创建或替换一个规则版本；已有阶段关账的版本不可再改。"""
    plan = _require_plan(db, plan_version)
    frozen_ids = list_frozen_stage_ids(db, plan_version, rule_version)
    if frozen_ids:
        raise RuleSetFrozenError(
            f"rule version '{rule_version}' already has frozen stages: {frozen_ids}"
        )
    rule_set = build_rule_set(
        plan_version,
        rule_version,
        [stage_rule_from_dict(s) for s in stages],
    )
    row = upsert_rule_set(
        db,
        plan_version=plan_version,
        rule_version=rule_version,
        spec=rule_set_to_spec(rule_set),
    )
    return _rule_set_out(db, plan, row)


def list_rule_set_views(db: Session, plan_version: str) -> list[dict[str, Any]]:
    plan = _require_plan(db, plan_version)
    return [_rule_set_out(db, plan, row) for row in list_rule_sets(db, plan_version)]


def get_rule_set_view(
    db: Session, plan_version: str, rule_version: str
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    row = get_rule_set(db, plan_version, rule_version)
    if row is None:
        raise RuleSetNotFoundError(
            f"rule version '{rule_version}' for plan '{plan_version}' does not exist"
        )
    return _rule_set_out(db, plan, row)


def activate_rule_set(
    db: Session, plan_version: str, rule_version: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    row = get_rule_set(db, plan_version, rule_version)
    if row is None:
        raise RuleSetNotFoundError(
            f"rule version '{rule_version}' for plan '{plan_version}' does not exist"
        )
    plan = set_active_rule_version(db, plan_version, rule_version)
    return _plan_out(plan)


def _load_active_rule_set(db: Session, plan: Plan) -> RuleSet | None:
    if plan.active_rule_version is None:
        return None
    row = get_rule_set(db, plan.plan_version, plan.active_rule_version)
    if row is None:
        return None
    return rule_set_from_spec(
        row.spec,
        plan_version=plan.plan_version,
        rule_version=row.rule_version,
    )


def _frozen_evals_by_stage(
    db: Session, plan_version: str, rule_version: str
) -> dict[str, dict[str, StageEval]]:
    """读取某规则版本下所有已关账阶段的锁定结果。"""
    frozen: dict[str, dict[str, StageEval]] = {}
    for row in list_stage_freezes(db, plan_version, rule_version):
        frozen[row.stage_id] = {
            s["student_id"]: stage_eval_from_dict(s)
            for s in row.snapshot.get("students", [])
        }
    return frozen


def _staged_evaluation(
    db: Session, plan: Plan
) -> tuple[RuleSet | None, dict[str, list[StageEval]]]:
    """在激活规则版本下对全部学生做阶段求值（冻结阶段用锁定结果）。"""
    rule_set = _load_active_rule_set(db, plan)
    if rule_set is None:
        return None, {}
    frozen_by_stage = _frozen_evals_by_stage(
        db, plan.plan_version, rule_set.rule_version
    )
    events = load_events(db, plan.plan_version)
    buckets_by_student = replay_staged(
        events,
        plan_version=plan.plan_version,
        timezone_name=plan.iana_timezone,
        stages=rule_set.stages,
    )
    student_ids = set(buckets_by_student)
    for stage_map in frozen_by_stage.values():
        student_ids.update(stage_map)

    stage_by_id = {s.stage_id: s for s in rule_set.stages}
    result: dict[str, list[StageEval]] = {}
    for student_id in student_ids:
        frozen_for_student: dict[str, StageEval] = {}
        for stage_id, stage_map in frozen_by_stage.items():
            locked = stage_map.get(student_id)
            if locked is None:
                locked = zero_stage_eval(stage_by_id[stage_id], frozen=True)
            frozen_for_student[stage_id] = locked
        result[student_id] = evaluate_stages(
            rule_set,
            buckets_by_student.get(student_id, {}),
            frozen_for_student,
        )
    return rule_set, result


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )
    base = explain_student(snap, student_id)
    if base is None:
        return None
    result = dict(base)
    rule_set, evals_by_student = _staged_evaluation(db, plan)
    if rule_set is None:
        result["rule_version"] = None
        result["stages"] = []
        return result
    stage_by_id = {s.stage_id: s for s in rule_set.stages}
    result["rule_version"] = rule_set.rule_version
    result["stages"] = [
        stage_eval_to_dict(ev, stage_by_id[ev.stage_id])
        for ev in evals_by_student.get(student_id, [])
    ]
    return result


def batch_warnings(
    db: Session, plan_version: str, stage_id: str | None = None
) -> dict[str, Any]:
    """批量预警：列出所有存在学时或类别缺口的学生-阶段组合。"""
    plan = _require_plan(db, plan_version)
    rule_set, evals_by_student = _staged_evaluation(db, plan)
    warnings: list[dict[str, Any]] = []
    if rule_set is not None:
        for stage in rule_set.stages:
            if stage_id is not None and stage.stage_id != stage_id:
                continue
            for student_id in sorted(evals_by_student):
                ev = next(
                    e for e in evals_by_student[student_id]
                    if e.stage_id == stage.stage_id
                )
                category_gaps = {
                    c.category: c.gap_seconds
                    for c in ev.categories
                    if c.gap_seconds > 0
                }
                if ev.gap_seconds > 0 or category_gaps:
                    warnings.append(
                        {
                            "student_id": student_id,
                            "stage_id": ev.stage_id,
                            "frozen": ev.frozen,
                            "required_seconds": ev.required_seconds,
                            "effective_seconds": ev.effective_seconds,
                            "gap_seconds": ev.gap_seconds,
                            "category_gaps": category_gaps,
                        }
                    )
    return {
        "plan_version": plan_version,
        "rule_version": rule_set.rule_version if rule_set is not None else None,
        "students_evaluated": len(evals_by_student),
        "warnings": warnings,
    }


def _evaluate_all_students(
    db: Session,
    plan: Plan,
    rule_set: RuleSet,
    *,
    up_to_event_id: str | None,
) -> dict[str, list[StageEval]]:
    frozen_by_stage = _frozen_evals_by_stage(
        db, plan.plan_version, rule_set.rule_version
    )
    events = load_events(db, plan.plan_version)
    buckets_by_student = replay_staged(
        events,
        plan_version=plan.plan_version,
        timezone_name=plan.iana_timezone,
        stages=rule_set.stages,
        up_to_event_id=up_to_event_id,
    )
    student_ids = set(buckets_by_student)
    for stage_map in frozen_by_stage.values():
        student_ids.update(stage_map)
    stage_by_id = {s.stage_id: s for s in rule_set.stages}
    result: dict[str, list[StageEval]] = {}
    for student_id in student_ids:
        frozen_for_student: dict[str, StageEval] = {}
        for stage_id, stage_map in frozen_by_stage.items():
            locked = stage_map.get(student_id)
            if locked is None:
                locked = zero_stage_eval(stage_by_id[stage_id], frozen=True)
            frozen_for_student[stage_id] = locked
        result[student_id] = evaluate_stages(
            rule_set,
            buckets_by_student.get(student_id, {}),
            frozen_for_student,
        )
    return result


def freeze_stage(
    db: Session, *, plan_version: str, stage_id: str
) -> tuple[dict[str, Any], bool]:
    """对激活规则版本下的某个阶段关账；并发下只有一个写入者成功。"""
    plan = _require_plan(db, plan_version)
    if plan.active_rule_version is None:
        raise NoActiveRuleSetError(
            f"plan '{plan_version}' has no active rule version"
        )
    row = get_rule_set(db, plan_version, plan.active_rule_version)
    if row is None:
        raise NoActiveRuleSetError(
            f"active rule version '{plan.active_rule_version}' for plan "
            f"'{plan_version}' is missing"
        )
    rule_set = rule_set_from_spec(
        row.spec, plan_version=plan_version, rule_version=row.rule_version
    )
    stage = rule_set.stage(stage_id)
    if stage is None:
        raise StageNotFoundError(
            f"stage '{stage_id}' is not part of rule version '{row.rule_version}'"
        )

    existing = get_stage_freeze(db, plan_version, row.rule_version, stage_id)
    if existing is not None:
        return dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    evals_by_student = _evaluate_all_students(
        db, plan, rule_set, up_to_event_id=cutoff
    )
    students = []
    for student_id in sorted(evals_by_student):
        ev = next(
            e for e in evals_by_student[student_id] if e.stage_id == stage_id
        )
        entry = stage_eval_to_dict(ev, stage)
        entry["frozen"] = True
        students.append({"student_id": student_id, **entry})
    snapshot = {
        "plan_version": plan_version,
        "rule_version": row.rule_version,
        "stage_id": stage_id,
        "event_cutoff_id": cutoff,
        "generated_at": _iso_z(datetime.now(timezone.utc)),
        "stage": stage_rule_to_dict(stage),
        "students": students,
    }
    inserted = insert_stage_freeze(
        db,
        plan_version=plan_version,
        rule_version=row.rule_version,
        stage_id=stage_id,
        snapshot=snapshot,
        event_cutoff_id=cutoff,
    )
    if inserted is None:
        winner = get_stage_freeze(db, plan_version, row.rule_version, stage_id)
        assert winner is not None
        return dict(winner.snapshot), False
    return snapshot, True


def get_stage_freeze_view(
    db: Session,
    plan_version: str,
    stage_id: str,
    rule_version: str | None = None,
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    resolved = rule_version or plan.active_rule_version
    if resolved is None:
        raise NoActiveRuleSetError(
            f"plan '{plan_version}' has no active rule version"
        )
    row = get_stage_freeze(db, plan_version, resolved, stage_id)
    if row is None:
        raise StageFreezeNotFoundError(
            f"stage '{stage_id}' of rule version '{resolved}' for plan "
            f"'{plan_version}' is not frozen"
        )
    return dict(row.snapshot)


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
