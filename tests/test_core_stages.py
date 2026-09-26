"""分阶段重放的纯领域测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.stages import (
    StageRuleError,
    build_rule_set,
    classify_event_stages,
    replay_stages,
)
from app.core.replay import Event, EventType


def _event(
    event_id: str,
    event_type: EventType,
    student_id: str,
    payload: dict,
    plan_version: str = "P1",
) -> Event:
    return Event(
        event_id=event_id,
        plan_version=plan_version,
        event_type=event_type,
        student_id=student_id,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )


def _checkin(
    eid: str,
    student: str,
    start: str,
    end: str,
    *,
    activity_type: str = "regular",
    plan_version: str = "P1",
) -> Event:
    return _event(
        eid,
        EventType.CHECKIN,
        student,
        {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
        plan_version,
    )


def _correction(eid, student, seconds, business_date, *, category="regular"):
    return _event(
        eid,
        EventType.LEAVE_CORRECTION,
        student,
        {
            "adjustment_seconds": seconds,
            "business_date": business_date,
            "category": category,
            "reason": "adjustment",
        },
    )


def _two_stage_rules(**overrides):
    stages = [
        {
            "stage_id": "ST1",
            "end_date": "2024-03-31",
            "required_seconds": 3600,
            "carryover_cap_seconds": 1800,
        },
        {
            "stage_id": "ST2",
            "end_date": "2024-06-30",
            "required_seconds": 7200,
            "carryover_cap_seconds": 0,
        },
    ]
    stages[0].update(overrides.get("st1", {}))
    stages[1].update(overrides.get("st2", {}))
    return build_rule_set(
        plan_version="P1",
        timezone="Asia/Shanghai",
        rules_version=1,
        stages=stages,
    )


def _stage(progress, stage_id):
    return next(s for s in progress.stages if s.stage_id == stage_id)


def test_cross_stage_checkin_is_split_by_business_time():
    # 2024-03-31 23:00 -> 2024-04-01 01:00 Shanghai：两个学术日各 1 小时，
    # 即使事件入库时间（created_at）很晚，也按业务时间分配阶段。
    rules = _two_stage_rules()
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-31T23:00:00+08:00",
            "2024-04-01T01:00:00+08:00",
        )
    ]
    state = replay_stages(events, rules)
    progress = state.students["S1"]
    assert _stage(progress, "ST1").earned_seconds == 3600
    assert _stage(progress, "ST2").earned_seconds == 3600
    assert _stage(progress, "ST1").met is True
    # ST2 要求 7200 且没有结转来源（ST1 恰好达标）。
    assert _stage(progress, "ST2").total_gap_seconds == 3600
    assert _stage(progress, "ST2").met is False
    assert progress.meets_all is False

    # 事件归类辅助函数同样识别两个阶段。
    touched = classify_event_stages(events[0], rules)
    assert touched == {"ST1", "ST2"}


def test_carryover_is_capped_and_chains_through_stages():
    rules = _two_stage_rules()
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-30T08:00:00+08:00",
            "2024-03-30T12:00:00+08:00",
        ),  # 4h in ST1
        _checkin(
            "E-02",
            "S1",
            "2024-05-01T08:00:00+08:00",
            "2024-05-01T09:00:00+08:00",
        ),  # 1h in ST2
    ]
    progress = replay_stages(events, rules).students["S1"]
    st1 = _stage(progress, "ST1")
    st2 = _stage(progress, "ST2")
    assert st1.earned_seconds == 4 * 3600
    assert st1.surplus_seconds == 3 * 3600
    assert st1.carry_out_seconds == 1800  # 封顶
    assert st1.spillover_lost_seconds == 3 * 3600 - 1800
    assert st2.carry_in_seconds == 1800
    assert st2.carry_in_source_stage == "ST1"
    # ST2: 自身 1h + 结转 0.5h = 1.5h，缺口 0.5h。
    assert st2.total_gap_seconds == 1800


def test_category_compensation_covers_gap_at_configured_rate():
    rules = _two_stage_rules(
        st1={
            "category_requirements": [
                {"category": "internship", "required_seconds": 3600}
            ],
            "compensation": [
                {
                    "from_category": "regular",
                    "to_category": "internship",
                    "rate_milli": 500,
                }
            ],
        }
    )
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-30T08:00:00+08:00",
            "2024-03-30T10:00:00+08:00",
            activity_type="regular",
        ),  # 2h regular，本阶段没有任何 internship 学时
    ]
    st1 = _stage(replay_stages(events, rules).students["S1"], "ST1")
    # internship 缺口 1h，按 0.5 折算率需要 2h regular 盈余，刚好补齐。
    gap = next(g for g in st1.category_gaps if g.category == "internship")
    assert gap.earned_seconds == 0
    assert gap.compensated_seconds == 3600
    assert gap.met is True
    applied = st1.compensation_applied[0]
    assert applied.rate_milli == 500
    assert applied.applied_seconds == 3600


def test_progress_reports_gap_and_carryable_sources():
    rules = _two_stage_rules(
        st1={"required_seconds": 3600, "carryover_cap_seconds": None}
    )
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-30T08:00:00+08:00",
            "2024-03-30T12:00:00+08:00",
        ),  # 4h ST1
    ]
    progress = replay_stages(events, rules).students["S1"]
    st1 = _stage(progress, "ST1")
    st2 = _stage(progress, "ST2")
    sources = {s.category: s.available_seconds for s in st1.carryable_sources}
    assert sources == {"regular": 4 * 3600}
    assert st1.carry_out_seconds == 3 * 3600
    # ST2 没有任何学时，但 ST1 的全额结转恰好补上？ST2 要求 7200，结转 10800。
    assert st2.earned_seconds == 0
    assert st2.carry_in_seconds == 3 * 3600
    assert st2.total_gap_seconds == 0
    assert st2.met is True


def test_negative_adjustment_in_open_stage_reopens_gap():
    rules = _two_stage_rules()
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-30T08:00:00+08:00",
            "2024-03-30T10:00:00+08:00",
        ),  # 2h ST1 -> met
        _correction("E-02", "S1", -5400, "2024-03-30"),  # -1.5h
    ]
    st1 = _stage(replay_stages(events, rules).students["S1"], "ST1")
    assert st1.earned_seconds == 1800
    assert st1.adjustment_seconds == -5400
    assert st1.total_gap_seconds == 1800
    assert st1.met is False
    assert st1.carry_out_seconds == 0


def test_late_event_cannot_penetrate_closed_stage_voucher():
    rules = _two_stage_rules()
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-30T08:00:00+08:00",
            "2024-03-30T12:00:00+08:00",
        ),
        _checkin(
            "E-02",
            "S1",
            "2024-05-01T08:00:00+08:00",
            "2024-05-01T09:00:00+08:00",
        ),
    ]
    freeze_state = replay_stages(events, rules)
    voucher = {
        s.stage_id: s.to_dict()
        for s in freeze_state.students["S1"].stages
        if s.stage_id == "ST1"
    }
    # 迟到的负向修正完全落在已关账 ST1。
    events.append(_correction("E-99", "S1", -7200, "2024-03-30"))
    state = replay_stages(
        events,
        rules,
        frozen_stage_ids={"ST1"},
        closed_stage_progress={"S1": voucher},
    )
    st1 = _stage(state.students["S1"], "ST1")
    st2 = _stage(state.students["S1"], "ST2")
    assert st1.frozen is True
    assert st1.earned_seconds == 4 * 3600  # 关账凭证值，修正被剪枝
    assert st1.carry_out_seconds == 1800
    assert st2.carry_in_seconds == 1800  # 下游结转同样不可穿透


def test_late_cross_boundary_checkin_updates_open_stage_only():
    rules = _two_stage_rules()
    voucher_st1 = {
        "earned_seconds": 3600,
        "total_gap_seconds": 0,
        "surplus_seconds": 0,
        "carry_out_seconds": 0,
        "met": True,
    }
    # 迟到签到横跨 ST1（已关账）/ST2：ST1 的 1 小时剪掉，ST2 得到 1 小时。
    events = [
        _checkin(
            "E-88",
            "S1",
            "2024-03-31T23:00:00+08:00",
            "2024-04-01T01:00:00+08:00",
        )
    ]
    state = replay_stages(
        events,
        rules,
        frozen_stage_ids={"ST1"},
        closed_stage_progress={"S1": {"ST1": voucher_st1}},
    )
    progress = state.students["S1"]
    assert _stage(progress, "ST1").earned_seconds == 3600
    assert _stage(progress, "ST2").earned_seconds == 3600


def test_mentor_confirm_follows_checkin_business_time():
    rules = _two_stage_rules()
    checkin = _checkin(
        "E-01",
        "S1",
        "2024-05-01T08:00:00+08:00",
        "2024-05-01T12:00:00+08:00",
        activity_type="internship",
    )
    confirm = _event(
        "E-02",
        EventType.MENTOR_CONFIRM,
        "S1",
        {"checkin_event_id": "E-01"},
    )
    before = _stage(replay_stages([checkin], rules).students["S1"], "ST2")
    assert before.pending_seconds == 4 * 3600
    assert before.earned_seconds == 0
    after = _stage(
        replay_stages([checkin, confirm], rules).students["S1"], "ST2"
    )
    assert after.pending_seconds == 0
    assert after.earned_seconds == 4 * 3600


def test_rule_set_validation_rejects_bad_config():
    with pytest.raises(StageRuleError):
        build_rule_set(
            plan_version="P1",
            timezone="Asia/Shanghai",
            rules_version=1,
            stages=[],
        )
    with pytest.raises(StageRuleError):
        build_rule_set(
            plan_version="P1",
            timezone="Asia/Shanghai",
            rules_version=1,
            stages=[
                {"stage_id": "A", "end_date": "2024-06-30", "required_seconds": 0},
                {"stage_id": "B", "end_date": "2024-03-31", "required_seconds": 0},
            ],
        )
    with pytest.raises(StageRuleError):
        build_rule_set(
            plan_version="P1",
            timezone="Asia/Shanghai",
            rules_version=1,
            stages=[
                {
                    "stage_id": "A",
                    "end_date": "2024-03-31",
                    "required_seconds": 0,
                    "compensation": [
                        {
                            "from_category": "x",
                            "to_category": "x",
                            "rate_milli": 1000,
                        }
                    ],
                }
            ],
        )
