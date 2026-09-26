"""阶段规则、结转与补偿的核心域测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.replay import Event, EventType
from app.core.stages import (
    CompensationRule,
    RuleError,
    StageRule,
    build_rule_set,
    evaluate_stages,
    replay_staged,
)


def _stage(stage_id: str, start: str, end: str, **kwargs) -> StageRule:
    return StageRule(
        stage_id=stage_id,
        start_utc=datetime.fromisoformat(start).astimezone(timezone.utc),
        end_utc=datetime.fromisoformat(end).astimezone(timezone.utc),
        **kwargs,
    )


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
        plan_version=plan_version,
    )


def _correction(
    eid: str,
    student: str,
    seconds: int,
    *,
    business_time: str | None = None,
    plan_version: str = "P1",
) -> Event:
    payload: dict = {"adjustment_seconds": seconds, "reason": "adjustment"}
    if business_time is not None:
        payload["business_time"] = business_time
    return _event(
        eid, EventType.LEAVE_CORRECTION, student, payload, plan_version=plan_version
    )


def test_cross_stage_checkin_is_split_and_carried_forward():
    stages = (
        _stage(
            "ST-1",
            "2024-03-01T00:00:00+08:00",
            "2024-03-15T00:00:00+08:00",
            required_seconds=3600,
            carry_out_cap_seconds=4 * 3600,
        ),
        _stage(
            "ST-2",
            "2024-03-15T00:00:00+08:00",
            "2024-04-01T00:00:00+08:00",
            required_seconds=3 * 3600,
            carry_in_cap_seconds=4 * 3600,
        ),
    )
    rule_set = build_rule_set("P1", "R1", stages)
    # 22:00-02:00 crosses the stage boundary at local midnight of 03-15.
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-14T22:00:00+08:00",
            "2024-03-15T02:00:00+08:00",
        )
    ]
    buckets = replay_staged(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        stages=rule_set.stages,
    )
    assert buckets["S1"]["ST-1"].confirmed_seconds == 2 * 3600
    assert buckets["S1"]["ST-2"].confirmed_seconds == 2 * 3600

    evals = {ev.stage_id: ev for ev in evaluate_stages(rule_set, buckets["S1"])}
    # ST-1: 2h own vs 1h required -> 1h surplus offered downstream.
    assert evals["ST-1"].own_seconds == 2 * 3600
    assert evals["ST-1"].gap_seconds == 0
    assert evals["ST-1"].carry_out_available_seconds == 3600
    # ST-2: 2h own vs 3h required -> 1h carried in from ST-1.
    assert evals["ST-2"].carry_in_seconds == 3600
    assert evals["ST-2"].carry_in_sources[0].from_stage_id == "ST-1"
    assert evals["ST-2"].carry_in_sources[0].seconds == 3600
    assert evals["ST-2"].effective_seconds == 3 * 3600
    assert evals["ST-2"].gap_seconds == 0
    assert evals["ST-2"].meets_requirement is True


def test_carry_in_and_carry_out_caps_limit_transfer():
    stages = (
        _stage(
            "ST-1",
            "2024-03-01T00:00:00+08:00",
            "2024-03-15T00:00:00+08:00",
            required_seconds=0,
            carry_out_cap_seconds=2 * 3600,
        ),
        _stage(
            "ST-2",
            "2024-03-15T00:00:00+08:00",
            "2024-04-01T00:00:00+08:00",
            required_seconds=4 * 3600,
            carry_in_cap_seconds=3600,
        ),
    )
    rule_set = build_rule_set("P1", "R1", stages)
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-02T08:00:00+08:00",
            "2024-03-02T13:00:00+08:00",
        ),  # 5h in ST-1
        _checkin(
            "E-02",
            "S1",
            "2024-03-20T08:00:00+08:00",
            "2024-03-20T09:00:00+08:00",
        ),  # 1h in ST-2
    ]
    buckets = replay_staged(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        stages=rule_set.stages,
    )
    evals = {ev.stage_id: ev for ev in evaluate_stages(rule_set, buckets["S1"])}
    # Surplus is 5h but the carry-out cap only exposes 2h.
    assert evals["ST-1"].carry_out_available_seconds == 2 * 3600
    # Deficit is 3h but the carry-in cap only accepts 1h.
    assert evals["ST-2"].carry_in_seconds == 3600
    assert evals["ST-2"].gap_seconds == 2 * 3600
    assert evals["ST-2"].meets_requirement is False


def test_compensation_covers_category_gap_up_to_cap():
    stages = (
        _stage(
            "ST-1",
            "2024-03-01T00:00:00+08:00",
            "2024-04-01T00:00:00+08:00",
            required_seconds=0,
            category_requirements={"lecture": 2 * 3600},
            compensations=(
                CompensationRule(
                    from_category="regular",
                    to_category="lecture",
                    max_seconds=3600,
                ),
            ),
        ),
    )
    rule_set = build_rule_set("P1", "R1", stages)
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-02T08:00:00+08:00",
            "2024-03-02T12:00:00+08:00",
            activity_type="regular",
        ),  # 4h regular
        _checkin(
            "E-02",
            "S1",
            "2024-03-03T08:00:00+08:00",
            "2024-03-03T09:00:00+08:00",
            activity_type="lecture",
        ),  # 1h lecture
    ]
    buckets = replay_staged(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        stages=rule_set.stages,
    )
    (ev,) = evaluate_stages(rule_set, buckets["S1"])
    categories = {c.category: c for c in ev.categories}
    lecture = categories["lecture"]
    assert lecture.confirmed_seconds == 3600
    assert lecture.required_seconds == 2 * 3600
    # The 1h gap is fully covered by regular surplus, capped at 1h.
    assert lecture.compensated_seconds == 3600
    assert lecture.gap_seconds == 0
    assert lecture.compensation_sources[0].from_category == "regular"
    assert lecture.compensation_sources[0].seconds == 3600
    assert categories["regular"].confirmed_seconds == 4 * 3600
    assert ev.meets_requirement is True


def test_compensation_cap_leaves_residual_gap():
    stages = (
        _stage(
            "ST-1",
            "2024-03-01T00:00:00+08:00",
            "2024-04-01T00:00:00+08:00",
            required_seconds=0,
            category_requirements={"lecture": 3 * 3600},
            compensations=(
                CompensationRule(
                    from_category="regular",
                    to_category="lecture",
                    max_seconds=3600,
                ),
            ),
        ),
    )
    rule_set = build_rule_set("P1", "R1", stages)
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-02T08:00:00+08:00",
            "2024-03-02T12:00:00+08:00",
            activity_type="regular",
        ),
    ]
    buckets = replay_staged(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        stages=rule_set.stages,
    )
    (ev,) = evaluate_stages(rule_set, buckets["S1"])
    lecture = {c.category: c for c in ev.categories}["lecture"]
    assert lecture.compensated_seconds == 3600
    assert lecture.gap_seconds == 2 * 3600
    assert ev.meets_requirement is False


def test_negative_adjustment_reduces_stage_and_clamps_at_zero():
    stages = (
        _stage(
            "ST-1",
            "2024-03-01T00:00:00+08:00",
            "2024-03-15T00:00:00+08:00",
            required_seconds=3600,
        ),
    )
    rule_set = build_rule_set("P1", "R1", stages)
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-02T08:00:00+08:00",
            "2024-03-02T11:00:00+08:00",
        ),  # 3h
        _correction(
            "E-02", "S1", -2 * 3600, business_time="2024-03-02T10:00:00+08:00"
        ),
    ]
    buckets = replay_staged(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        stages=rule_set.stages,
    )
    (ev,) = evaluate_stages(rule_set, buckets["S1"])
    assert ev.adjustment_seconds == -2 * 3600
    assert ev.own_seconds == 3600
    assert ev.meets_requirement is True

    # A further correction pushes the stage below zero -> clamped to zero.
    events.append(
        _correction(
            "E-03", "S1", -5 * 3600, business_time="2024-03-03T10:00:00+08:00"
        )
    )
    buckets = replay_staged(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        stages=rule_set.stages,
    )
    (ev,) = evaluate_stages(rule_set, buckets["S1"])
    assert ev.own_seconds == 0
    assert ev.gap_seconds == 3600
    assert ev.meets_requirement is False


def test_correction_without_business_time_is_not_staged():
    stages = (
        _stage(
            "ST-1",
            "2024-03-01T00:00:00+08:00",
            "2024-04-01T00:00:00+08:00",
            required_seconds=3600,
        ),
    )
    rule_set = build_rule_set("P1", "R1", stages)
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-02T08:00:00+08:00",
            "2024-03-02T10:00:00+08:00",
        ),
        _correction("E-02", "S1", -3600),
    ]
    buckets = replay_staged(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        stages=rule_set.stages,
    )
    (ev,) = evaluate_stages(rule_set, buckets["S1"])
    assert ev.adjustment_seconds == 0
    assert ev.own_seconds == 2 * 3600


def test_rule_set_validation_rejects_bad_specs():
    with pytest.raises(RuleError):
        build_rule_set("P1", "R1", [])

    with pytest.raises(RuleError):
        # Overlapping windows.
        build_rule_set(
            "P1",
            "R1",
            [
                _stage("ST-1", "2024-03-01T00:00:00+08:00", "2024-03-15T00:00:00+08:00"),
                _stage("ST-2", "2024-03-14T00:00:00+08:00", "2024-04-01T00:00:00+08:00"),
            ],
        )

    with pytest.raises(RuleError):
        # Duplicate stage ids.
        build_rule_set(
            "P1",
            "R1",
            [
                _stage("ST-1", "2024-03-01T00:00:00+08:00", "2024-03-10T00:00:00+08:00"),
                _stage("ST-1", "2024-03-10T00:00:00+08:00", "2024-04-01T00:00:00+08:00"),
            ],
        )

    with pytest.raises(RuleError):
        # Negative carry cap.
        build_rule_set(
            "P1",
            "R1",
            [
                _stage(
                    "ST-1",
                    "2024-03-01T00:00:00+08:00",
                    "2024-04-01T00:00:00+08:00",
                    carry_out_cap_seconds=-1,
                )
            ],
        )

    with pytest.raises(RuleError):
        # Self-compensation.
        build_rule_set(
            "P1",
            "R1",
            [
                _stage(
                    "ST-1",
                    "2024-03-01T00:00:00+08:00",
                    "2024-04-01T00:00:00+08:00",
                    compensations=(
                        CompensationRule("regular", "regular", 100),
                    ),
                )
            ],
        )


def test_frozen_stage_keeps_locked_values_against_late_events():
    stages = (
        _stage(
            "ST-1",
            "2024-03-01T00:00:00+08:00",
            "2024-03-15T00:00:00+08:00",
            required_seconds=3600,
            carry_out_cap_seconds=4 * 3600,
        ),
        _stage(
            "ST-2",
            "2024-03-15T00:00:00+08:00",
            "2024-04-01T00:00:00+08:00",
            required_seconds=2 * 3600,
            carry_in_cap_seconds=4 * 3600,
        ),
    )
    rule_set = build_rule_set("P1", "R1", stages)
    early = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-02T08:00:00+08:00",
            "2024-03-02T11:00:00+08:00",
        ),  # 3h in ST-1
    ]
    buckets = replay_staged(
        early,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        stages=rule_set.stages,
    )
    locked = {ev.stage_id: ev for ev in evaluate_stages(rule_set, buckets["S1"])}
    locked_st1 = locked["ST-1"]
    locked_st1.frozen = True
    assert locked_st1.own_seconds == 3 * 3600
    assert locked_st1.carry_out_available_seconds == 2 * 3600

    # A late event arrives inside the frozen stage's window.
    late = early + [
        _checkin(
            "E-02",
            "S1",
            "2024-03-03T08:00:00+08:00",
            "2024-03-03T12:00:00+08:00",
        ),  # 4h more in ST-1
        _checkin(
            "E-03",
            "S1",
            "2024-03-20T08:00:00+08:00",
            "2024-03-20T09:00:00+08:00",
        ),  # 1h in ST-2
    ]
    buckets = replay_staged(
        late,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        stages=rule_set.stages,
    )
    assert buckets["S1"]["ST-1"].confirmed_seconds == 7 * 3600  # raw replay sees all

    evals = {
        ev.stage_id: ev
        for ev in evaluate_stages(rule_set, buckets["S1"], {"ST-1": locked_st1})
    }
    # The frozen stage is untouched by the late event.
    assert evals["ST-1"].own_seconds == 3 * 3600
    assert evals["ST-1"].frozen is True
    # ST-2 is live: it sees its own late event and the locked carry-out.
    assert evals["ST-2"].own_seconds == 3600
    assert evals["ST-2"].carry_in_seconds == 3600
    assert evals["ST-2"].carry_in_sources[0].from_stage_id == "ST-1"
    assert evals["ST-2"].gap_seconds == 0
