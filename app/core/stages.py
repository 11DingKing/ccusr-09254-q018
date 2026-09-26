"""分阶段培养进度的纯领域逻辑。

培养方案把总学时要求拆成多个有序阶段：每个阶段有独立的业务时间边界
（``end_date``，按培养方案时区解释的学术日）、总学时门槛、类别学时门槛，
以及结转到下一阶段的学时上限（``carryover_cap_seconds``）。类别之间可以
定义补偿关系，例如“实习”学时可以按折算率抵补“实训”缺口。

重放严格按事件的业务时间（而不是入库时间）把学时分配到阶段；跨阶段边界
的签到会像跨学术日一样被切段。已关账（冻结）阶段的事件段在重放时一律
剔除，从而保证迟到事件只能更新未冻结阶段，无法穿透关账边界。

本模块不依赖数据库或 Web 框架，全部输出均可 JSON 序列化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from .clock import (
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)
from .replay import (
    CheckinStatus,
    Event,
    EventType,
    _parse_checkin,
)

DEFAULT_CATEGORY = "regular"
CARRY_FROM_PLAN_OVERALL = None  # 早于/晚于所有阶段的事件不计入任何阶段


class StageRuleError(ValueError):
    """阶段规则配置不合法。"""


# ---------------------------------------------------------------------------
# 规则模型（不可变）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryRequirement:
    category: str
    required_seconds: int


@dataclass(frozen=True)
class CompensationRule:
    """补偿关系：``from_category`` 的 1 秒可抵 ``to_category`` 缺口
    ``rate_milli / 1000`` 秒（rate_milli 为千分率，避免浮点）。"""

    from_category: str
    to_category: str
    rate_milli: int


@dataclass(frozen=True)
class StageRule:
    stage_id: str
    end_date: date
    required_seconds: int
    carryover_cap_seconds: int | None
    category_requirements: tuple[CategoryRequirement, ...] = ()
    compensation: tuple[CompensationRule, ...] = ()

    def category_required(self, category: str) -> int:
        for req in self.category_requirements:
            if req.category == category:
                return req.required_seconds
        return 0

    def to_rule_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "end_date": self.end_date.isoformat(),
            "required_seconds": self.required_seconds,
            "carryover_cap_seconds": self.carryover_cap_seconds,
            "category_requirements": [
                {"category": r.category, "required_seconds": r.required_seconds}
                for r in self.category_requirements
            ],
            "compensation": [
                {
                    "from_category": c.from_category,
                    "to_category": c.to_category,
                    "rate_milli": c.rate_milli,
                }
                for c in self.compensation
            ],
        }


@dataclass(frozen=True)
class StageRuleSet:
    plan_version: str
    timezone: str
    rules_version: int
    stages: tuple[StageRule, ...]

    def get(self, stage_id: str) -> StageRule | None:
        for stage in self.stages:
            if stage.stage_id == stage_id:
                return stage
        return None

    def stage_for_date(self, day: date) -> StageRule | None:
        """业务日期归属的阶段：end_date 不早于该日期的第一个阶段。"""
        for stage in self.stages:
            if day <= stage.end_date:
                return stage
        return None

    def predecessors(self, stage_id: str) -> list[StageRule]:
        """按顺序关账时，冻结某阶段前必须已经冻结的前置阶段。"""
        ordered = list(self.stages)
        for index, stage in enumerate(ordered):
            if stage.stage_id == stage_id:
                return ordered[:index]
        raise StageRuleError(f"stage '{stage_id}' is not defined in this rule set")


def build_rule_set(
    *,
    plan_version: str,
    timezone: str,
    rules_version: int,
    stages: Sequence[dict[str, Any]],
) -> StageRuleSet:
    """从可 JSON 序列化的配置构造并校验规则集。"""
    if not stages:
        raise StageRuleError("at least one stage is required")
    built: list[StageRule] = []
    seen_ids: set[str] = set()
    prev_end: date | None = None
    for order, raw in enumerate(stages):
        stage_id = str(raw.get("stage_id", "")).strip()
        if not stage_id:
            raise StageRuleError(f"stage at position {order} is missing stage_id")
        if stage_id in seen_ids:
            raise StageRuleError(f"duplicate stage_id '{stage_id}'")
        seen_ids.add(stage_id)

        end_raw = raw.get("end_date")
        if isinstance(end_raw, date) and not isinstance(end_raw, datetime):
            end_day = end_raw
        else:
            try:
                end_day = date.fromisoformat(str(end_raw))
            except (TypeError, ValueError) as exc:
                raise StageRuleError(
                    f"stage '{stage_id}' has invalid end_date: {end_raw!r}"
                ) from exc
        if prev_end is not None and end_day <= prev_end:
            raise StageRuleError(
                f"stage '{stage_id}' end_date {end_day} must be after {prev_end}"
            )
        prev_end = end_day

        required = int(raw.get("required_seconds", 0))
        if required < 0:
            raise StageRuleError(f"stage '{stage_id}' required_seconds must be >= 0")

        cap_raw = raw.get("carryover_cap_seconds")
        cap = None if cap_raw is None else int(cap_raw)
        if cap is not None and cap < 0:
            raise StageRuleError(
                f"stage '{stage_id}' carryover_cap_seconds must be >= 0"
            )

        cat_reqs: list[CategoryRequirement] = []
        seen_cats: set[str] = set()
        for req_raw in raw.get("category_requirements", []) or []:
            category = str(req_raw["category"]).strip()
            req_seconds = int(req_raw["required_seconds"])
            if not category:
                raise StageRuleError(f"stage '{stage_id}' has an empty category")
            if category in seen_cats:
                raise StageRuleError(
                    f"stage '{stage_id}' declares category '{category}' twice"
                )
            seen_cats.add(category)
            if req_seconds < 0:
                raise StageRuleError(
                    f"category '{category}' required_seconds must be >= 0"
                )
            cat_reqs.append(
                CategoryRequirement(category=category, required_seconds=req_seconds)
            )

        comps: list[CompensationRule] = []
        for comp_raw in raw.get("compensation", []) or []:
            from_cat = str(comp_raw["from_category"]).strip()
            to_cat = str(comp_raw["to_category"]).strip()
            rate = int(comp_raw["rate_milli"])
            if not from_cat or not to_cat:
                raise StageRuleError(
                    f"stage '{stage_id}' compensation categories must be non-empty"
                )
            if from_cat == to_cat:
                raise StageRuleError(
                    f"stage '{stage_id}' compensation must link different categories"
                )
            if not 0 < rate <= 1_000_000:
                raise StageRuleError(
                    f"stage '{stage_id}' compensation rate_milli must be in (0, 1000000]"
                )
            comps.append(
                CompensationRule(
                    from_category=from_cat, to_category=to_cat, rate_milli=rate
                )
            )

        built.append(
            StageRule(
                stage_id=stage_id,
                end_date=end_day,
                required_seconds=required,
                carryover_cap_seconds=cap,
                category_requirements=tuple(cat_reqs),
                compensation=tuple(comps),
            )
        )

    return StageRuleSet(
        plan_version=plan_version,
        timezone=timezone,
        rules_version=rules_version,
        stages=tuple(built),
    )


# ---------------------------------------------------------------------------
# 重放结果
# ---------------------------------------------------------------------------


@dataclass
class CompensationApplication:
    from_category: str
    to_category: str
    rate_milli: int
    offered_seconds: int
    applied_seconds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_category": self.from_category,
            "to_category": self.to_category,
            "rate_milli": self.rate_milli,
            "offered_seconds": self.offered_seconds,
            "applied_seconds": self.applied_seconds,
        }


@dataclass
class CategoryGap:
    category: str
    required_seconds: int
    earned_seconds: int
    compensated_seconds: int
    gap_seconds: int
    met: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "required_seconds": self.required_seconds,
            "earned_seconds": self.earned_seconds,
            "compensated_seconds": self.compensated_seconds,
            "gap_seconds": self.gap_seconds,
            "met": self.met,
        }


@dataclass
class CarryableSource:
    """可结转来源：某类别的净盈余（信息性，帮助解释缺口从哪里补）。"""

    category: str
    available_seconds: int

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category, "available_seconds": self.available_seconds}


@dataclass
class StageProgress:
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
    category_gaps: list[CategoryGap]
    compensation_applied: list[CompensationApplication]
    surplus_seconds: int
    carry_out_seconds: int
    spillover_lost_seconds: int
    carryable_sources: list[CarryableSource]
    frozen: bool
    met: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "end_date": self.end_date,
            "required_seconds": self.required_seconds,
            "carryover_cap_seconds": self.carryover_cap_seconds,
            "earned_seconds": self.earned_seconds,
            "pending_seconds": self.pending_seconds,
            "adjustment_seconds": self.adjustment_seconds,
            "carry_in_seconds": self.carry_in_seconds,
            "carry_in_source_stage": self.carry_in_source_stage,
            "total_gap_seconds": self.total_gap_seconds,
            "category_gaps": [g.to_dict() for g in self.category_gaps],
            "compensation_applied": [
                c.to_dict() for c in self.compensation_applied
            ],
            "surplus_seconds": self.surplus_seconds,
            "carry_out_seconds": self.carry_out_seconds,
            "spillover_lost_seconds": self.spillover_lost_seconds,
            "carryable_sources": [s.to_dict() for s in self.carryable_sources],
            "frozen": self.frozen,
            "met": self.met,
        }


@dataclass
class StudentStageProgress:
    student_id: str
    stages: list[StageProgress] = field(default_factory=list)

    @property
    def meets_all(self) -> bool:
        return all(s.met for s in self.stages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "student_id": self.student_id,
            "meets_all": self.meets_all,
            "stages": [s.to_dict() for s in self.stages],
        }


@dataclass
class StageReplayState:
    plan_version: str
    rules_version: int
    timezone: str
    stages: list[StageRule]
    students: dict[str, StudentStageProgress]

    def stage_warnings(self, stage_id: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for sid in sorted(self.students):
            progress = self.students[sid]
            stage = next((s for s in progress.stages if s.stage_id == stage_id), None)
            if stage is None or stage.frozen or stage.met:
                continue
            rows.append(
                {
                    "student_id": sid,
                    "stage_id": stage_id,
                    "total_gap_seconds": stage.total_gap_seconds,
                    "category_gaps": [
                        g.to_dict() for g in stage.category_gaps if not g.met
                    ],
                    "carry_in_seconds": stage.carry_in_seconds,
                    "carry_in_source_stage": stage.carry_in_source_stage,
                    "carry_out_seconds": stage.carry_out_seconds,
                    "pending_seconds": stage.pending_seconds,
                    "carryable_sources": [
                        s.to_dict() for s in stage.carryable_sources
                    ],
                }
            )
        return rows


# ---------------------------------------------------------------------------
# 业务时间归属
# ---------------------------------------------------------------------------


def event_business_date(
    event: Event,
    timezone_name: str,
    target_checkin: Event | None = None,
) -> date | None:
    """事件的业务日期（按业务发生时间，而非入库时间）。

    - checkin：check_in_at 在培养方案时区下的学术日；
    - leave_correction：payload.business_date（学术日）或 payload.occurred_at；
    - mentor_confirm：跟随其确认的目标签到，因为确认效果归属于签到所在阶段。
    """
    if event.event_type == EventType.CHECKIN:
        return _date_from_payload(event.payload["check_in_at"], timezone_name)
    if event.event_type == EventType.LEAVE_CORRECTION:
        raw = event.payload.get("business_date") or event.payload.get("occurred_at")
        if raw is None:
            return None
        return _date_from_payload(raw, timezone_name)
    if event.event_type == EventType.MENTOR_CONFIRM:
        if target_checkin is None:
            return None
        return _date_from_payload(
            target_checkin.payload["check_in_at"], timezone_name
        )
    return None


def _date_from_payload(raw: Any, timezone_name: str | None = None) -> date:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    text = str(raw)
    if "T" in text or " " in text:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if timezone_name is not None:
            from .clock import academic_day

            return academic_day(moment, timezone_name)
        return moment.date()
    return date.fromisoformat(text[:10])


def classify_event_stages(
    event: Event,
    rules: StageRuleSet,
    checkin_index: dict[str, Event] | None = None,
) -> set[str]:
    """判断一个事件按业务时间触碰哪些阶段。

    - checkin 按学术日切段后可能横跨阶段边界（如 3 月 31 日深夜至 4 月 1 日）；
    - leave_correction 归属于 ``business_date`` 单个阶段；
    - mentor_confirm 跟随目标签到；
    - 缺少业务日期的事件返回空集合，由调用方决定如何处理。
    ``checkin_index`` 用于 mentor_confirm 查找目标签到。
    """
    checkin_index = checkin_index or {}
    if event.event_type == EventType.CHECKIN:
        from .replay import _parse_checkin

        record = _parse_checkin(event, rules.timezone)
        return {
            stage_id
            for stage_id, _, _ in _split_intervals_by_stage(
                record.start_utc, record.end_utc, rules
            )
        }
    if event.event_type == EventType.LEAVE_CORRECTION:
        day = event_business_date(event, rules.timezone)
        stage = rules.stage_for_date(day) if day is not None else None
        return {stage.stage_id} if stage is not None else set()
    if event.event_type == EventType.MENTOR_CONFIRM:
        target = checkin_index.get(event.payload.get("checkin_event_id"))
        if target is None:
            return set()
        from .replay import _parse_checkin

        record = _parse_checkin(target, rules.timezone)
        return {
            stage_id
            for stage_id, _, _ in _split_intervals_by_stage(
                record.start_utc, record.end_utc, rules
            )
        }
    return set()


# ---------------------------------------------------------------------------
# 重放
# ---------------------------------------------------------------------------


@dataclass
class _StageSegments:
    """单个学生单个阶段在重放过程中的中间聚合。"""

    confirmed_intervals: dict[str, list[tuple[datetime, datetime]]] = field(
        default_factory=dict
    )
    pending_intervals: dict[str, list[tuple[datetime, datetime]]] = field(
        default_factory=dict
    )
    adjustment_seconds: dict[str, int] = field(default_factory=dict)

    def add_interval(
        self,
        category: str,
        start: datetime,
        end: datetime,
        status: CheckinStatus,
    ) -> None:
        bucket = (
            self.confirmed_intervals
            if status == CheckinStatus.CONFIRMED
            else self.pending_intervals
        )
        bucket.setdefault(category, []).append((start, end))

    def add_adjustment(self, category: str, seconds: int) -> None:
        self.adjustment_seconds[category] = (
            self.adjustment_seconds.get(category, 0) + seconds
        )


def _checkin_category(event: Event) -> str:
    return str(event.payload.get("category") or event.payload.get("activity_type") or DEFAULT_CATEGORY)


def _correction_category(event: Event) -> str:
    return str(event.payload.get("category") or DEFAULT_CATEGORY)


def replay_stages(
    events: Iterable[Event],
    rules: StageRuleSet,
    *,
    frozen_stage_ids: Iterable[str] = (),
    closed_stage_progress: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> StageReplayState:
    """按业务时间把事件重放到各阶段，求解补偿与逐阶段结转链。

    ``frozen_stage_ids`` 中的阶段视为已关账：其事件段一律剔除；若同时在
    ``closed_stage_progress``（student_id -> stage_id -> 关账时的阶段进度
    快照）中提供了关账凭证，则该阶段直接回放凭证值，并沿凭证中的
    ``carry_out_seconds`` 把结转量注入下一阶段，保证迟到事件无法改动已关账
    阶段及其向下游的结转。
    """
    frozen = set(frozen_stage_ids)
    closed = closed_stage_progress or {}
    sorted_events = sorted(
        (e for e in events if e.plan_version == rules.plan_version),
        key=lambda e: e.event_id,
    )

    checkin_index: dict[str, Event] = {}
    records: list[tuple[Event, Any]] = []
    corrections: list[Event] = []
    for event in sorted_events:
        if event.event_type == EventType.CHECKIN:
            record = _parse_checkin(event, rules.timezone)
            checkin_index[event.event_id] = event
            records.append((event, record))
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target_id = event.payload.get("checkin_event_id")
            target_event = checkin_index.get(target_id)
            target_record = next(
                (r for e, r in records if e.event_id == target_id), None
            )
            if (
                target_record is not None
                and target_event is not None
                and target_event.student_id == event.student_id
            ):
                target_record.status = CheckinStatus.CONFIRMED
        elif event.event_type == EventType.LEAVE_CORRECTION:
            corrections.append(event)

    # student -> stage_id -> segments
    per_student: dict[str, dict[str, _StageSegments]] = {}
    touched_students: set[str] = set()

    for event, record in records:
        category = _checkin_category(event)
        stage_segments = _split_intervals_by_stage(
            record.start_utc, record.end_utc, rules
        )
        for stage_id, start, end in stage_segments:
            if stage_id in frozen:
                # 已关账段：迟到事件不能穿透关账边界。
                continue
            touched_students.add(event.student_id)
            stages_map = per_student.setdefault(event.student_id, {})
            bucket = stages_map.setdefault(stage_id, _StageSegments())
            bucket.add_interval(category, start, end, record.status)

    for event in corrections:
        day = event_business_date(event, rules.timezone)
        if day is None:
            continue
        stage = rules.stage_for_date(day)
        if stage is None or stage.stage_id in frozen:
            continue
        touched_students.add(event.student_id)
        seconds = int(event.payload.get("adjustment_seconds", 0))
        stages_map = per_student.setdefault(event.student_id, {})
        bucket = stages_map.setdefault(stage.stage_id, _StageSegments())
        bucket.add_adjustment(_correction_category(event), seconds)

    # 关账凭证里出现过、但当前没有任何未冻结事件的学生也要出现在结果中。
    for student_id in closed:
        touched_students.add(student_id)

    students: dict[str, StudentStageProgress] = {}
    for student_id in touched_students:
        stages_map = per_student.get(student_id, {})
        progress = _settle_student(
            rules, stages_map, frozen, closed.get(student_id, {})
        )
        students[student_id] = StudentStageProgress(
            student_id=student_id, stages=progress
        )

    return StageReplayState(
        plan_version=rules.plan_version,
        rules_version=rules.rules_version,
        timezone=rules.timezone,
        stages=list(rules.stages),
        students=students,
    )


def _split_intervals_by_stage(
    start_utc: datetime,
    end_utc: datetime,
    rules: StageRuleSet,
) -> list[tuple[str, datetime, datetime]]:
    """先按学术日切段，再归并到阶段；不属于任何阶段的段丢弃。"""
    grouped: dict[str, list[tuple[datetime, datetime]]] = {}
    for day, seg_start, seg_end in split_by_academic_day(
        to_utc(start_utc), to_utc(end_utc), rules.timezone
    ):
        stage = rules.stage_for_date(day)
        if stage is None:
            continue
        grouped.setdefault(stage.stage_id, []).append((seg_start, seg_end))

    result: list[tuple[str, datetime, datetime]] = []
    stage_order = {s.stage_id: i for i, s in enumerate(rules.stages)}
    for stage_id, intervals in grouped.items():
        for seg_start, seg_end in merge_intervals(intervals):
            result.append((stage_id, seg_start, seg_end))
    result.sort(key=lambda item: (stage_order[item[0]], item[1]))
    return result


def _stage_progress_from_closed(stage: StageRule, data: dict[str, Any]) -> StageProgress:
    """从关账时保存的 JSON 凭证重建阶段进度。"""
    category_gaps = [
        CategoryGap(
            category=g["category"],
            required_seconds=g["required_seconds"],
            earned_seconds=g["earned_seconds"],
            compensated_seconds=g.get("compensated_seconds", 0),
            gap_seconds=g["gap_seconds"],
            met=g["met"],
        )
        for g in data.get("category_gaps", [])
    ]
    applications = [
        CompensationApplication(
            from_category=c["from_category"],
            to_category=c["to_category"],
            rate_milli=c["rate_milli"],
            offered_seconds=c["offered_seconds"],
            applied_seconds=c["applied_seconds"],
        )
        for c in data.get("compensation_applied", [])
    ]
    sources = [
        CarryableSource(
            category=s["category"], available_seconds=s["available_seconds"]
        )
        for s in data.get("carryable_sources", [])
    ]
    return StageProgress(
        stage_id=stage.stage_id,
        end_date=stage.end_date.isoformat(),
        required_seconds=stage.required_seconds,
        carryover_cap_seconds=stage.carryover_cap_seconds,
        earned_seconds=int(data.get("earned_seconds", 0)),
        pending_seconds=int(data.get("pending_seconds", 0)),
        adjustment_seconds=int(data.get("adjustment_seconds", 0)),
        carry_in_seconds=int(data.get("carry_in_seconds", 0)),
        carry_in_source_stage=data.get("carry_in_source_stage"),
        total_gap_seconds=int(data.get("total_gap_seconds", 0)),
        category_gaps=category_gaps,
        compensation_applied=applications,
        surplus_seconds=int(data.get("surplus_seconds", 0)),
        carry_out_seconds=int(data.get("carry_out_seconds", 0)),
        spillover_lost_seconds=int(data.get("spillover_lost_seconds", 0)),
        carryable_sources=sources,
        frozen=True,
        met=bool(data.get("met", False)),
    )


def _settle_student(
    rules: StageRuleSet,
    stages_map: dict[str, _StageSegments],
    frozen: set[str],
    closed: dict[str, dict[str, Any]] | None = None,
) -> list[StageProgress]:
    closed = closed or {}
    progress: list[StageProgress] = []
    carry_in = 0
    carry_source: str | None = None

    for stage in rules.stages:
        if stage.stage_id in frozen and stage.stage_id in closed:
            # 已关账阶段：回放关账凭证，迟到事件既不能改其结果，
            # 也不能改其注入下一阶段的结转量。
            sealed = _stage_progress_from_closed(stage, closed[stage.stage_id])
            progress.append(sealed)
            carry_in = sealed.carry_out_seconds
            carry_source = stage.stage_id
            continue

        bucket = stages_map.get(stage.stage_id, _StageSegments())

        earned_by_category: dict[str, int] = {}
        for category, intervals in bucket.confirmed_intervals.items():
            earned_by_category[category] = union_seconds(intervals)
        for category, seconds in bucket.adjustment_seconds.items():
            earned_by_category[category] = (
                earned_by_category.get(category, 0) + seconds
            )

        pending_seconds = 0
        for intervals in bucket.pending_intervals.values():
            pending_seconds += union_seconds(intervals)

        adjustment_total = sum(bucket.adjustment_seconds.values())
        own_total = sum(earned_by_category.values())

        effective_total = max(0, own_total) + carry_in

        # ---- 类别门槛 + 补偿求解 --------------------------------------
        required_by_category = {
            req.category: req.required_seconds for req in stage.category_requirements
        }
        deficits: dict[str, int] = {}
        surplus: dict[str, int] = {}
        category_gaps: list[CategoryGap] = []
        for category, earned in earned_by_category.items():
            required = required_by_category.get(category, 0)
            if required == 0:
                # 未设类别门槛：全部学时都是可补偿/可结转盈余。
                if earned > 0:
                    surplus[category] = earned
                continue
            if earned < required:
                deficits[category] = required - earned
            elif earned > required:
                surplus[category] = earned - required
            category_gaps.append(
                CategoryGap(
                    category=category,
                    required_seconds=required,
                    earned_seconds=earned,
                    compensated_seconds=0,
                    gap_seconds=max(0, required - earned),
                    met=earned >= required,
                )
            )
        # 声明了门槛但本阶段完全没有该类别学时的情况也要报缺口。
        for req in stage.category_requirements:
            if req.category not in earned_by_category:
                deficits.setdefault(req.category, req.required_seconds)
                category_gaps.append(
                    CategoryGap(
                        category=req.category,
                        required_seconds=req.required_seconds,
                        earned_seconds=0,
                        compensated_seconds=0,
                        gap_seconds=req.required_seconds,
                        met=req.required_seconds == 0,
                    )
                )
        category_gaps.sort(key=lambda g: g.category)
        gap_index = {g.category: g for g in category_gaps}
        applications: list[CompensationApplication] = []
        for comp in stage.compensation:
            need = deficits.get(comp.to_category, 0)
            offer = surplus.get(comp.from_category, 0)
            if need <= 0 or offer <= 0:
                continue
            applied = min(need, (offer * comp.rate_milli) // 1000)
            if applied <= 0:
                continue
            consumed = (applied * 1000 + comp.rate_milli - 1) // comp.rate_milli
            consumed = min(consumed, offer)
            applications.append(
                CompensationApplication(
                    from_category=comp.from_category,
                    to_category=comp.to_category,
                    rate_milli=comp.rate_milli,
                    offered_seconds=offer,
                    applied_seconds=applied,
                )
            )
            surplus[comp.from_category] = offer - consumed
            deficits[comp.to_category] = need - applied
            gap = gap_index[comp.to_category]
            gap.compensated_seconds += applied
            gap.gap_seconds = deficits[comp.to_category]
            gap.met = gap.gap_seconds == 0
        for gap in category_gaps:
            if gap.category in deficits:
                gap.gap_seconds = deficits[gap.category]
                gap.met = gap.gap_seconds == 0

        total_gap = max(0, stage.required_seconds - effective_total)
        categories_met = all(g.met for g in category_gaps)
        met = total_gap == 0 and categories_met

        surplus_total = max(0, effective_total - stage.required_seconds)
        if stage.carryover_cap_seconds is None:
            carry_out = surplus_total
        else:
            carry_out = min(surplus_total, stage.carryover_cap_seconds)
        spillover_lost = surplus_total - carry_out

        carryable_sources = [
            CarryableSource(category=cat, available_seconds=secs)
            for cat, secs in sorted(surplus.items())
            if secs > 0
        ]

        progress.append(
            StageProgress(
                stage_id=stage.stage_id,
                end_date=stage.end_date.isoformat(),
                required_seconds=stage.required_seconds,
                carryover_cap_seconds=stage.carryover_cap_seconds,
                earned_seconds=own_total,
                pending_seconds=pending_seconds,
                adjustment_seconds=adjustment_total,
                carry_in_seconds=carry_in,
                carry_in_source_stage=carry_source,
                total_gap_seconds=total_gap,
                category_gaps=category_gaps,
                compensation_applied=applications,
                surplus_seconds=surplus_total,
                carry_out_seconds=carry_out,
                spillover_lost_seconds=spillover_lost,
                carryable_sources=carryable_sources,
                frozen=stage.stage_id in frozen,
                met=met,
            )
        )
        carry_in = carry_out
        carry_source = stage.stage_id

    return progress
