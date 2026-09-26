"""阶段化培养方案：阶段规则、结转上限、类别补偿与按业务时间的分桶重放。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .clock import to_utc, union_seconds
from .replay import (
    CheckinRecord,
    CheckinStatus,
    Event,
    EventType,
    parse_checkin,
)


class RuleError(ValueError):
    """阶段规则配置不合法。"""


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CompensationRule:
    """类别补偿关系：from 类别的盈余可抵偿 to 类别的缺口，上限 max_seconds。"""

    from_category: str
    to_category: str
    max_seconds: int


@dataclass(frozen=True)
class StageRule:
    """单个阶段的时间窗、学时与类别门槛、结转上限和补偿关系。"""

    stage_id: str
    start_utc: datetime
    end_utc: datetime
    required_seconds: int = 0
    category_requirements: dict[str, int] = field(default_factory=dict)
    carry_in_cap_seconds: int = 0
    carry_out_cap_seconds: int = 0
    compensations: tuple[CompensationRule, ...] = ()

    def contains(self, moment_utc: datetime) -> bool:
        moment_utc = to_utc(moment_utc)
        return self.start_utc <= moment_utc < self.end_utc


@dataclass(frozen=True)
class RuleSet:
    """一个规则版本：若干互不重叠、按时间排序的阶段。"""

    plan_version: str
    rule_version: str
    stages: tuple[StageRule, ...]

    def stage(self, stage_id: str) -> StageRule | None:
        for stage in self.stages:
            if stage.stage_id == stage_id:
                return stage
        return None


def _validate_stage(stage: StageRule) -> None:
    if not stage.stage_id:
        raise RuleError("阶段标识不能为空")
    if stage.end_utc <= stage.start_utc:
        raise RuleError(f"阶段 {stage.stage_id} 的结束时间必须晚于开始时间")
    if stage.required_seconds < 0:
        raise RuleError(f"阶段 {stage.stage_id} 的学时门槛不能为负")
    for name, required in stage.category_requirements.items():
        if not name:
            raise RuleError(f"阶段 {stage.stage_id} 的类别名不能为空")
        if required < 0:
            raise RuleError(f"阶段 {stage.stage_id} 的类别门槛不能为负")
    if stage.carry_in_cap_seconds < 0 or stage.carry_out_cap_seconds < 0:
        raise RuleError(f"阶段 {stage.stage_id} 的结转上限不能为负")
    for comp in stage.compensations:
        if comp.from_category == comp.to_category:
            raise RuleError(f"阶段 {stage.stage_id} 的补偿来源与目标类别必须不同")
        if comp.max_seconds < 0:
            raise RuleError(f"阶段 {stage.stage_id} 的补偿上限不能为负")


def build_rule_set(
    plan_version: str, rule_version: str, stages: Iterable[StageRule]
) -> RuleSet:
    """校验并组装一个规则版本。"""
    ordered = tuple(sorted(stages, key=lambda s: (s.start_utc, s.stage_id)))
    if not ordered:
        raise RuleError("至少需要一个阶段")
    seen: set[str] = set()
    for stage in ordered:
        _validate_stage(stage)
        if stage.stage_id in seen:
            raise RuleError(f"阶段标识重复: {stage.stage_id}")
        seen.add(stage.stage_id)
    for previous, current in zip(ordered, ordered[1:]):
        if current.start_utc < previous.end_utc:
            raise RuleError(
                f"阶段 {previous.stage_id} 与 {current.stage_id} 的时间窗重叠"
            )
    return RuleSet(
        plan_version=plan_version, rule_version=rule_version, stages=ordered
    )


def stage_rule_from_dict(data: dict[str, Any]) -> StageRule:
    """执行确定性的业务处理。"""
    start = data["start_at"]
    end = data["end_at"]
    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    if isinstance(end, str):
        end = datetime.fromisoformat(end)
    compensations = tuple(
        CompensationRule(
            from_category=str(c["from_category"]),
            to_category=str(c["to_category"]),
            max_seconds=int(c["max_seconds"]),
        )
        for c in data.get("compensations", [])
    )
    return StageRule(
        stage_id=str(data["stage_id"]),
        start_utc=to_utc(start),
        end_utc=to_utc(end),
        required_seconds=int(data.get("required_seconds", 0)),
        category_requirements={
            str(k): int(v)
            for k, v in dict(data.get("category_requirements", {})).items()
        },
        carry_in_cap_seconds=int(data.get("carry_in_cap_seconds", 0)),
        carry_out_cap_seconds=int(data.get("carry_out_cap_seconds", 0)),
        compensations=compensations,
    )


def stage_rule_to_dict(stage: StageRule) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    return {
        "stage_id": stage.stage_id,
        "start_at": _iso_z(stage.start_utc),
        "end_at": _iso_z(stage.end_utc),
        "required_seconds": stage.required_seconds,
        "category_requirements": dict(stage.category_requirements),
        "carry_in_cap_seconds": stage.carry_in_cap_seconds,
        "carry_out_cap_seconds": stage.carry_out_cap_seconds,
        "compensations": [
            {
                "from_category": c.from_category,
                "to_category": c.to_category,
                "max_seconds": c.max_seconds,
            }
            for c in stage.compensations
        ],
    }


def rule_set_from_spec(
    spec: dict[str, Any], *, plan_version: str, rule_version: str
) -> RuleSet:
    """执行确定性的业务处理。"""
    return build_rule_set(
        plan_version,
        rule_version,
        [stage_rule_from_dict(s) for s in spec.get("stages", [])],
    )


def rule_set_to_spec(rule_set: RuleSet) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    return {"stages": [stage_rule_to_dict(s) for s in rule_set.stages]}


def find_stage(stages: Iterable[StageRule], moment_utc: datetime) -> StageRule | None:
    """执行确定性的业务处理。"""
    moment_utc = to_utc(moment_utc)
    for stage in stages:
        if stage.contains(moment_utc):
            return stage
    return None


@dataclass
class StageAdjustment:
    """请假修正：只有显式携带 business_time 时才分配到阶段。"""

    seconds: int
    business_utc: datetime | None


@dataclass
class StageBucket:
    """单个学生在单个阶段内的原始秒数（重放输出）。"""

    stage_id: str
    confirmed_seconds: int = 0
    pending_seconds: int = 0
    adjustment_seconds: int = 0
    category_confirmed: dict[str, int] = field(default_factory=dict)


def _business_time(payload: dict[str, Any]) -> datetime | None:
    raw = payload.get("business_time")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _clip_to_stage(
    record: CheckinRecord, stage: StageRule
) -> tuple[datetime, datetime] | None:
    start = max(record.start_utc, stage.start_utc)
    end = min(record.end_utc, stage.end_utc)
    if start < end:
        return start, end
    return None


def replay_staged(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    stages: Iterable[StageRule],
    up_to_event_id: str | None = None,
) -> dict[str, dict[str, StageBucket]]:
    """按事件业务时间把签到区间与修正分配到阶段桶。

    签到按 [start, end) 与各阶段时间窗求交（跨阶段签到会被切开）；
    请假修正只有携带 business_time 时才落入对应阶段，否则只影响总量。
    """
    stage_list = list(stages)
    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    checkins_by_student: dict[str, list[CheckinRecord]] = {}
    checkin_index: dict[str, CheckinRecord] = {}
    adjustments_by_student: dict[str, list[StageAdjustment]] = {}

    for event in sorted_events:
        if event.event_type == EventType.CHECKIN:
            record = parse_checkin(event, timezone_name)
            checkins_by_student.setdefault(event.student_id, []).append(record)
            checkin_index[event.event_id] = record
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target_id = event.payload.get("checkin_event_id")
            target = checkin_index.get(target_id)
            if target is not None and target.student_id == event.student_id:
                target.status = CheckinStatus.CONFIRMED
        elif event.event_type == EventType.LEAVE_CORRECTION:
            adjustments_by_student.setdefault(event.student_id, []).append(
                StageAdjustment(
                    seconds=int(event.payload.get("adjustment_seconds", 0)),
                    business_utc=_business_time(event.payload),
                )
            )

    result: dict[str, dict[str, StageBucket]] = {}
    for student_id in set(checkins_by_student) | set(adjustments_by_student):
        records = checkins_by_student.get(student_id, [])
        buckets = {
            stage.stage_id: StageBucket(stage_id=stage.stage_id)
            for stage in stage_list
        }
        for stage in stage_list:
            bucket = buckets[stage.stage_id]
            confirmed_intervals: list[tuple[datetime, datetime]] = []
            pending_intervals: list[tuple[datetime, datetime]] = []
            category_intervals: dict[str, list[tuple[datetime, datetime]]] = {}
            for record in records:
                clipped = _clip_to_stage(record, stage)
                if clipped is None:
                    continue
                if record.counts:
                    confirmed_intervals.append(clipped)
                    category_intervals.setdefault(record.activity_type, []).append(
                        clipped
                    )
                elif record.status == CheckinStatus.PENDING:
                    pending_intervals.append(clipped)
            bucket.confirmed_seconds = union_seconds(confirmed_intervals)
            bucket.pending_seconds = union_seconds(pending_intervals)
            bucket.category_confirmed = {
                category: union_seconds(intervals)
                for category, intervals in sorted(category_intervals.items())
            }
        for adjustment in adjustments_by_student.get(student_id, []):
            if adjustment.business_utc is None:
                continue
            stage = find_stage(stage_list, adjustment.business_utc)
            if stage is not None:
                buckets[stage.stage_id].adjustment_seconds += adjustment.seconds
        result[student_id] = buckets
    return result


@dataclass
class CarrySource:
    """一条可结转来源：来自哪个阶段、贡献了多少秒。"""

    from_stage_id: str
    seconds: int


@dataclass
class CompensationSource:
    from_category: str
    seconds: int


@dataclass
class CategoryEval:
    category: str
    confirmed_seconds: int
    required_seconds: int
    compensated_seconds: int
    gap_seconds: int
    compensation_sources: list[CompensationSource] = field(default_factory=list)


@dataclass
class StageEval:
    """单个学生在单个阶段的求值结果（冻结快照也以此结构落库）。"""

    stage_id: str
    frozen: bool
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    own_seconds: int
    carry_in_seconds: int
    carry_in_sources: list[CarrySource]
    effective_seconds: int
    required_seconds: int
    gap_seconds: int
    carry_out_available_seconds: int
    categories: list[CategoryEval]
    meets_requirement: bool


def _evaluate_categories(
    stage: StageRule, category_confirmed: dict[str, int]
) -> list[CategoryEval]:
    """按补偿关系在阶段内部用盈余类别抵偿缺口类别。"""
    names = sorted(set(stage.category_requirements) | set(category_confirmed))
    evals: dict[str, CategoryEval] = {}
    for name in names:
        required = stage.category_requirements.get(name, 0)
        confirmed = category_confirmed.get(name, 0)
        evals[name] = CategoryEval(
            category=name,
            confirmed_seconds=confirmed,
            required_seconds=required,
            compensated_seconds=0,
            gap_seconds=max(0, required - confirmed),
            compensation_sources=[],
        )
    surplus = {
        name: max(0, ev.confirmed_seconds - ev.required_seconds)
        for name, ev in evals.items()
    }
    for comp in stage.compensations:
        target = evals.get(comp.to_category)
        if target is None:
            continue
        need = (
            target.required_seconds
            - target.confirmed_seconds
            - target.compensated_seconds
        )
        if need <= 0:
            continue
        take = min(need, surplus.get(comp.from_category, 0), comp.max_seconds)
        if take <= 0:
            continue
        surplus[comp.from_category] = surplus.get(comp.from_category, 0) - take
        target.compensated_seconds += take
        target.gap_seconds = max(0, target.gap_seconds - take)
        target.compensation_sources.append(
            CompensationSource(from_category=comp.from_category, seconds=take)
        )
    return [evals[name] for name in names]


@dataclass
class _PoolEntry:
    stage_id: str
    remaining: int


def _draw_from(pool: list[_PoolEntry], stage_id: str, amount: int) -> None:
    """从结转池中按指定来源扣减（用于已冻结阶段锁定的结转来源）。"""
    for entry in pool:
        if entry.stage_id != stage_id:
            continue
        take = min(entry.remaining, amount)
        entry.remaining -= take
        amount -= take
        if amount <= 0:
            return


def _evaluate_live(
    stage: StageRule, bucket: StageBucket | None, pool: list[_PoolEntry]
) -> StageEval:
    """对未冻结阶段做实时求值，并按需从结转池中结转。"""
    bucket = bucket or StageBucket(stage_id=stage.stage_id)
    own_seconds = bucket.confirmed_seconds + bucket.adjustment_seconds
    if own_seconds < 0:
        own_seconds = 0
    categories = _evaluate_categories(stage, bucket.category_confirmed)

    carry_in = 0
    sources: list[CarrySource] = []
    deficit = stage.required_seconds - own_seconds
    if deficit > 0 and stage.carry_in_cap_seconds > 0:
        for entry in pool:
            if carry_in >= deficit or carry_in >= stage.carry_in_cap_seconds:
                break
            take = min(
                entry.remaining,
                deficit - carry_in,
                stage.carry_in_cap_seconds - carry_in,
            )
            if take <= 0:
                continue
            entry.remaining -= take
            carry_in += take
            sources.append(CarrySource(from_stage_id=entry.stage_id, seconds=take))

    effective = own_seconds + carry_in
    gap = max(0, stage.required_seconds - effective)
    surplus = max(0, own_seconds - stage.required_seconds)
    carry_out = min(surplus, stage.carry_out_cap_seconds)
    return StageEval(
        stage_id=stage.stage_id,
        frozen=False,
        confirmed_seconds=bucket.confirmed_seconds,
        pending_seconds=bucket.pending_seconds,
        adjustment_seconds=bucket.adjustment_seconds,
        own_seconds=own_seconds,
        carry_in_seconds=carry_in,
        carry_in_sources=sources,
        effective_seconds=effective,
        required_seconds=stage.required_seconds,
        gap_seconds=gap,
        carry_out_available_seconds=carry_out,
        categories=categories,
        meets_requirement=gap == 0 and all(c.gap_seconds == 0 for c in categories),
    )


def evaluate_stages(
    rule_set: RuleSet,
    buckets: dict[str, StageBucket],
    frozen: dict[str, StageEval] | None = None,
) -> list[StageEval]:
    """按阶段顺序求值单个学生的结转链。

    frozen 中的阶段直接使用关账时锁定的结果（含其锁定的结转来源与
    可转出量），迟到事件无法穿透该关账边界；未冻结阶段实时重放。
    """
    frozen = frozen or {}
    pool: list[_PoolEntry] = []
    evals: list[StageEval] = []
    for stage in rule_set.stages:
        locked = frozen.get(stage.stage_id)
        if locked is not None:
            ev = locked
            for src in ev.carry_in_sources:
                _draw_from(pool, src.from_stage_id, src.seconds)
        else:
            ev = _evaluate_live(stage, buckets.get(stage.stage_id), pool)
        evals.append(ev)
        if ev.carry_out_available_seconds > 0:
            pool.append(
                _PoolEntry(
                    stage_id=ev.stage_id,
                    remaining=ev.carry_out_available_seconds,
                )
            )
    return evals


def zero_stage_eval(stage: StageRule, *, frozen: bool) -> StageEval:
    """关账时无任何记录的学生在该阶段的零值结果。"""
    categories = [
        CategoryEval(
            category=name,
            confirmed_seconds=0,
            required_seconds=required,
            compensated_seconds=0,
            gap_seconds=required,
            compensation_sources=[],
        )
        for name, required in sorted(stage.category_requirements.items())
    ]
    return StageEval(
        stage_id=stage.stage_id,
        frozen=frozen,
        confirmed_seconds=0,
        pending_seconds=0,
        adjustment_seconds=0,
        own_seconds=0,
        carry_in_seconds=0,
        carry_in_sources=[],
        effective_seconds=0,
        required_seconds=stage.required_seconds,
        gap_seconds=stage.required_seconds,
        carry_out_available_seconds=0,
        categories=categories,
        meets_requirement=stage.required_seconds == 0
        and all(c.gap_seconds == 0 for c in categories),
    )


def stage_eval_to_dict(ev: StageEval, stage: StageRule) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    return {
        "stage_id": ev.stage_id,
        "frozen": ev.frozen,
        "start_at": _iso_z(stage.start_utc),
        "end_at": _iso_z(stage.end_utc),
        "confirmed_seconds": ev.confirmed_seconds,
        "pending_seconds": ev.pending_seconds,
        "adjustment_seconds": ev.adjustment_seconds,
        "own_seconds": ev.own_seconds,
        "carry_in_seconds": ev.carry_in_seconds,
        "carry_in_sources": [
            {"from_stage_id": s.from_stage_id, "seconds": s.seconds}
            for s in ev.carry_in_sources
        ],
        "effective_seconds": ev.effective_seconds,
        "required_seconds": ev.required_seconds,
        "gap_seconds": ev.gap_seconds,
        "carry_out_available_seconds": ev.carry_out_available_seconds,
        "categories": [
            {
                "category": c.category,
                "confirmed_seconds": c.confirmed_seconds,
                "required_seconds": c.required_seconds,
                "compensated_seconds": c.compensated_seconds,
                "gap_seconds": c.gap_seconds,
                "compensation_sources": [
                    {"from_category": s.from_category, "seconds": s.seconds}
                    for s in c.compensation_sources
                ],
            }
            for c in ev.categories
        ],
        "meets_requirement": ev.meets_requirement,
    }


def stage_eval_from_dict(data: dict[str, Any]) -> StageEval:
    """执行确定性的业务处理。"""
    return StageEval(
        stage_id=str(data["stage_id"]),
        frozen=bool(data.get("frozen", False)),
        confirmed_seconds=int(data.get("confirmed_seconds", 0)),
        pending_seconds=int(data.get("pending_seconds", 0)),
        adjustment_seconds=int(data.get("adjustment_seconds", 0)),
        own_seconds=int(data.get("own_seconds", 0)),
        carry_in_seconds=int(data.get("carry_in_seconds", 0)),
        carry_in_sources=[
            CarrySource(
                from_stage_id=str(s["from_stage_id"]), seconds=int(s["seconds"])
            )
            for s in data.get("carry_in_sources", [])
        ],
        effective_seconds=int(data.get("effective_seconds", 0)),
        required_seconds=int(data.get("required_seconds", 0)),
        gap_seconds=int(data.get("gap_seconds", 0)),
        carry_out_available_seconds=int(data.get("carry_out_available_seconds", 0)),
        categories=[
            CategoryEval(
                category=str(c["category"]),
                confirmed_seconds=int(c.get("confirmed_seconds", 0)),
                required_seconds=int(c.get("required_seconds", 0)),
                compensated_seconds=int(c.get("compensated_seconds", 0)),
                gap_seconds=int(c.get("gap_seconds", 0)),
                compensation_sources=[
                    CompensationSource(
                        from_category=str(s["from_category"]),
                        seconds=int(s["seconds"]),
                    )
                    for s in c.get("compensation_sources", [])
                ],
            )
            for c in data.get("categories", [])
        ],
        meets_requirement=bool(data.get("meets_requirement", False)),
    )
