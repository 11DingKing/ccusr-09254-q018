"""阶段规则配置、阶段冻结与批量预警的 API 测试。"""

from __future__ import annotations

import threading

from tests.conftest import TestSessionLocal

PLAN = {
    "plan_version": "P-STG-2024",
    "iana_timezone": "Asia/Shanghai",
    "required_seconds": 0,
}


def _rules_v1() -> dict:
    return {
        "stages": [
            {
                "stage_id": "ST-1",
                "start_at": "2024-03-01T00:00:00+08:00",
                "end_at": "2024-03-15T00:00:00+08:00",
                "required_seconds": 3600,
                "carry_out_cap_seconds": 4 * 3600,
                "category_requirements": {"internship": 1800},
                "compensations": [
                    {
                        "from_category": "regular",
                        "to_category": "internship",
                        "max_seconds": 1800,
                    }
                ],
            },
            {
                "stage_id": "ST-2",
                "start_at": "2024-03-15T00:00:00+08:00",
                "end_at": "2024-04-01T00:00:00+08:00",
                "required_seconds": 3 * 3600,
                "carry_in_cap_seconds": 4 * 3600,
            },
        ]
    }


def _create_plan(client, plan: dict | None = None) -> str:
    body = dict(plan or PLAN)
    resp = client.post("/api/plans", json=body)
    assert resp.status_code == 201, resp.text
    return body["plan_version"]


def _put_rules(client, pv: str, rule_version: str, rules: dict):
    resp = client.put(f"/api/plans/{pv}/rules/{rule_version}", json=rules)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _activate(client, pv: str, rule_version: str):
    resp = client.post(f"/api/plans/{pv}/rules/{rule_version}/activate")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _checkin(
    eid: str,
    student: str,
    start: str,
    end: str,
    activity_type: str = "regular",
) -> dict:
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _correction(
    eid: str, student: str, seconds: int, business_time: str | None = None
) -> dict:
    payload: dict = {"adjustment_seconds": seconds, "reason": "fix"}
    if business_time is not None:
        payload["business_time"] = business_time
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": payload,
    }


def _post_events(client, pv: str, events: list[dict]):
    resp = client.post(f"/api/plans/{pv}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _progress(client, pv: str, student: str) -> dict:
    resp = client.get(f"/api/plans/{pv}/students/{student}/progress")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _stage(progress: dict, stage_id: str) -> dict:
    return next(s for s in progress["stages"] if s["stage_id"] == stage_id)


def test_rule_config_lifecycle_and_validation(client):
    pv = _create_plan(client)

    # Unknown plan -> 404.
    resp = client.put("/api/plans/NOPE/rules/R1", json=_rules_v1())
    assert resp.status_code == 404

    body = _put_rules(client, pv, "R1", _rules_v1())
    assert body["rule_version"] == "R1"
    assert body["active"] is False
    assert body["frozen_stage_ids"] == []
    assert [s["stage_id"] for s in body["stages"]] == ["ST-1", "ST-2"]
    # Stage windows are normalized to UTC.
    assert body["stages"][0]["start_at"] == "2024-02-29T16:00:00Z"

    listed = client.get(f"/api/plans/{pv}/rules").json()
    assert [r["rule_version"] for r in listed] == ["R1"]

    fetched = client.get(f"/api/plans/{pv}/rules/R1").json()
    assert fetched["stages"][0]["compensations"][0]["max_seconds"] == 1800

    resp = client.get(f"/api/plans/{pv}/rules/NOPE")
    assert resp.status_code == 404

    # Overlapping windows are rejected with 422.
    bad = {
        "stages": [
            {
                "stage_id": "A",
                "start_at": "2024-03-01T00:00:00+08:00",
                "end_at": "2024-03-15T00:00:00+08:00",
            },
            {
                "stage_id": "B",
                "start_at": "2024-03-14T00:00:00+08:00",
                "end_at": "2024-04-01T00:00:00+08:00",
            },
        ]
    }
    resp = client.put(f"/api/plans/{pv}/rules/R2", json=bad)
    assert resp.status_code == 422

    # Naive timestamps are rejected as well.
    naive = {
        "stages": [
            {
                "stage_id": "A",
                "start_at": "2024-03-01T00:00:00",
                "end_at": "2024-03-15T00:00:00",
            }
        ]
    }
    resp = client.put(f"/api/plans/{pv}/rules/R2", json=naive)
    assert resp.status_code == 422

    # Activation flips the plan's active rule version.
    resp = client.post(f"/api/plans/{pv}/rules/NOPE/activate")
    assert resp.status_code == 404
    plan = _activate(client, pv, "R1")
    assert plan["active_rule_version"] == "R1"
    assert client.get(f"/api/plans/{pv}").json()["active_rule_version"] == "R1"


def test_progress_reports_stage_gaps_carry_sources_and_compensation(client):
    pv = _create_plan(client)
    _put_rules(client, pv, "R1", _rules_v1())
    _activate(client, pv, "R1")

    # 22:00-02:00 crosses the ST-1/ST-2 boundary (cross-stage check-in).
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-14T22:00:00+08:00",
                "2024-03-15T02:00:00+08:00",
            )
        ],
    )
    progress = _progress(client, pv, "S1")
    assert progress["rule_version"] == "R1"
    assert progress["total_seconds"] == 4 * 3600

    st1 = _stage(progress, "ST-1")
    assert st1["frozen"] is False
    assert st1["confirmed_seconds"] == 2 * 3600
    assert st1["own_seconds"] == 2 * 3600
    assert st1["gap_seconds"] == 0
    assert st1["carry_out_available_seconds"] == 3600
    # The internship category gap (30 min) is compensated by regular surplus.
    categories = {c["category"]: c for c in st1["categories"]}
    assert categories["internship"]["required_seconds"] == 1800
    assert categories["internship"]["compensated_seconds"] == 1800
    assert categories["internship"]["gap_seconds"] == 0
    assert categories["internship"]["compensation_sources"] == [
        {"from_category": "regular", "seconds": 1800}
    ]
    assert st1["meets_requirement"] is True

    st2 = _stage(progress, "ST-2")
    assert st2["own_seconds"] == 2 * 3600
    assert st2["carry_in_seconds"] == 3600
    assert st2["carry_in_sources"] == [{"from_stage_id": "ST-1", "seconds": 3600}]
    assert st2["effective_seconds"] == 3 * 3600
    assert st2["gap_seconds"] == 0
    assert st2["meets_requirement"] is True


def test_rule_versions_switch_evaluation_and_isolate_freezes(client):
    pv = _create_plan(client)
    rules_v1 = _rules_v1()
    rules_v1["stages"][0]["required_seconds"] = 4 * 3600
    rules_v1["stages"][0]["category_requirements"] = {}
    rules_v1["stages"][0]["compensations"] = []
    _put_rules(client, pv, "R1", rules_v1)
    _activate(client, pv, "R1")

    _post_events(
        client,
        pv,
        [_checkin("E-01", "S1", "2024-03-02T08:00:00+08:00", "2024-03-02T10:00:00+08:00")],
    )
    st1 = _stage(_progress(client, pv, "S1"), "ST-1")
    assert st1["gap_seconds"] == 2 * 3600
    assert st1["meets_requirement"] is False

    # Freeze ST-1 under R1, then a late event lands in the frozen window.
    client.post(f"/api/plans/{pv}/stages/ST-1/freeze", json={})
    _post_events(
        client,
        pv,
        [_checkin("E-02", "S1", "2024-03-03T08:00:00+08:00", "2024-03-03T10:00:00+08:00")],
    )
    st1 = _stage(_progress(client, pv, "S1"), "ST-1")
    assert st1["frozen"] is True
    assert st1["own_seconds"] == 2 * 3600  # late event blocked by the boundary

    # A frozen rule version becomes immutable.
    resp = client.put(f"/api/plans/{pv}/rules/R1", json=_rules_v1())
    assert resp.status_code == 409

    # A new rule version re-evaluates live and ignores R1's freeze.
    rules_v2 = _rules_v1()
    rules_v2["stages"][0]["required_seconds"] = 2 * 3600
    rules_v2["stages"][0]["category_requirements"] = {}
    rules_v2["stages"][0]["compensations"] = []
    _put_rules(client, pv, "R2", rules_v2)
    _activate(client, pv, "R2")

    progress = _progress(client, pv, "S1")
    assert progress["rule_version"] == "R2"
    st1 = _stage(progress, "ST-1")
    assert st1["frozen"] is False
    assert st1["own_seconds"] == 4 * 3600  # both events count again
    assert st1["gap_seconds"] == 0
    assert st1["meets_requirement"] is True

    # R1's freeze snapshot is still retrievable by explicit rule version.
    frozen = client.get(
        f"/api/plans/{pv}/stages/ST-1/freeze", params={"rule_version": "R1"}
    ).json()
    assert frozen["rule_version"] == "R1"
    assert frozen["students"][0]["own_seconds"] == 2 * 3600


def test_negative_adjustment_flows_into_stage_evaluation(client):
    pv = _create_plan(client)
    _put_rules(client, pv, "R1", _rules_v1())
    _activate(client, pv, "R1")

    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-02T08:00:00+08:00",
                "2024-03-02T11:00:00+08:00",
            ),  # 3h in ST-1
            _correction(
                "E-02", "S1", -2 * 3600, business_time="2024-03-02T10:00:00+08:00"
            ),
        ],
    )
    progress = _progress(client, pv, "S1")
    st1 = _stage(progress, "ST-1")
    assert st1["adjustment_seconds"] == -2 * 3600
    assert st1["own_seconds"] == 3600
    assert st1["gap_seconds"] == 0
    # Overall totals also reflect the correction.
    assert progress["adjustment_seconds"] == -2 * 3600
    assert progress["total_seconds"] == 3600

    # A correction without business_time only affects the overall total.
    _post_events(client, pv, [_correction("E-03", "S1", -1800)])
    progress = _progress(client, pv, "S1")
    st1 = _stage(progress, "ST-1")
    assert st1["adjustment_seconds"] == -2 * 3600
    assert st1["own_seconds"] == 3600
    assert progress["adjustment_seconds"] == -2 * 3600 - 1800
    assert progress["total_seconds"] == 1800

    # Deep negative adjustments clamp the stage at zero and open a gap.
    _post_events(
        client,
        pv,
        [
            _correction(
                "E-04", "S1", -5 * 3600, business_time="2024-03-03T10:00:00+08:00"
            )
        ],
    )
    st1 = _stage(_progress(client, pv, "S1"), "ST-1")
    assert st1["own_seconds"] == 0
    assert st1["gap_seconds"] == 3600
    assert st1["meets_requirement"] is False


def test_late_events_update_unfrozen_stages_but_not_frozen_ones(client):
    pv = _create_plan(client)
    rules = _rules_v1()
    rules["stages"][0]["category_requirements"] = {}
    rules["stages"][0]["compensations"] = []
    rules["stages"][0]["required_seconds"] = 2 * 3600
    rules["stages"][1]["required_seconds"] = 2 * 3600
    _put_rules(client, pv, "R1", rules)
    _activate(client, pv, "R1")

    _post_events(
        client,
        pv,
        [_checkin("E-01", "S1", "2024-03-02T08:00:00+08:00", "2024-03-02T10:00:00+08:00")],
    )
    frozen = client.post(f"/api/plans/{pv}/stages/ST-1/freeze", json={})
    assert frozen.status_code == 201, frozen.text
    assert frozen.json()["event_cutoff_id"] == "E-01"
    assert frozen.json()["students"][0]["own_seconds"] == 2 * 3600

    # Late events: one inside the frozen window, one in the live stage.
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-02", "S1", "2024-03-03T08:00:00+08:00", "2024-03-03T10:00:00+08:00"
            ),
            _checkin(
                "E-03", "S1", "2024-03-20T08:00:00+08:00", "2024-03-20T09:00:00+08:00"
            ),
        ],
    )
    progress = _progress(client, pv, "S1")
    st1 = _stage(progress, "ST-1")
    assert st1["frozen"] is True
    assert st1["own_seconds"] == 2 * 3600  # E-02 cannot penetrate the boundary
    st2 = _stage(progress, "ST-2")
    assert st2["frozen"] is False
    assert st2["own_seconds"] == 3600  # E-03 updates the unfrozen stage
    assert st2["gap_seconds"] == 3600
    # The overall (non-staged) totals still count every event.
    assert progress["total_seconds"] == 5 * 3600

    # The frozen snapshot itself is immutable.
    again = client.get(f"/api/plans/{pv}/stages/ST-1/freeze").json()
    assert again["students"][0]["own_seconds"] == 2 * 3600
    assert again["event_cutoff_id"] == "E-01"


def test_stage_freeze_is_idempotent_and_validates_scope(client):
    pv = _create_plan(client)

    # No active rule version -> 409.
    resp = client.post(f"/api/plans/{pv}/stages/ST-1/freeze", json={})
    assert resp.status_code == 409

    _put_rules(client, pv, "R1", _rules_v1())
    _activate(client, pv, "R1")

    # Unknown stage -> 404.
    resp = client.post(f"/api/plans/{pv}/stages/NOPE/freeze", json={})
    assert resp.status_code == 404

    # Missing freeze -> 404 on read.
    resp = client.get(f"/api/plans/{pv}/stages/ST-1/freeze")
    assert resp.status_code == 404

    _post_events(
        client,
        pv,
        [_checkin("E-01", "S1", "2024-03-02T08:00:00+08:00", "2024-03-02T10:00:00+08:00")],
    )
    first = client.post(f"/api/plans/{pv}/stages/ST-1/freeze", json={})
    assert first.status_code == 201
    second = client.post(f"/api/plans/{pv}/stages/ST-1/freeze", json={})
    assert second.status_code == 201
    # Re-freezing returns the identical snapshot.
    assert first.json() == second.json()


def test_concurrent_stage_freeze_only_one_wins(client):
    pv = _create_plan(client)
    _put_rules(client, pv, "R1", _rules_v1())
    _activate(client, pv, "R1")
    _post_events(
        client,
        pv,
        [_checkin("E-01", "S1", "2024-03-02T08:00:00+08:00", "2024-03-02T10:00:00+08:00")],
    )

    from app import services

    created_flags: list[bool] = []
    lock = threading.Lock()

    def _freeze():
        session = TestSessionLocal()
        try:
            _, created = services.freeze_stage(
                session, plan_version=pv, stage_id="ST-1"
            )
            with lock:
                created_flags.append(created)
        finally:
            session.close()

    threads = [threading.Thread(target=_freeze) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for c in created_flags if c) == 1
    assert sum(1 for c in created_flags if not c) == 3

    stored = client.get(f"/api/plans/{pv}/stages/ST-1/freeze").json()
    assert stored["stage_id"] == "ST-1"
    assert stored["students"][0]["own_seconds"] == 2 * 3600


def test_batch_warnings_list_students_with_gaps(client):
    pv = _create_plan(client)
    rules = _rules_v1()
    rules["stages"][1]["required_seconds"] = 2 * 3600
    _put_rules(client, pv, "R1", rules)
    _activate(client, pv, "R1")

    _post_events(
        client,
        pv,
        [
            # S1: 2h regular in ST-1 -> total gap 0 but internship category
            # is compensated; ST-2 has nothing -> 3h gap.
            _checkin(
                "E-01", "S1", "2024-03-02T08:00:00+08:00", "2024-03-02T10:00:00+08:00"
            ),
            # S2: 4h internship in ST-1 and 2h in ST-2 -> fully compliant.
            _checkin(
                "E-02",
                "S2",
                "2024-03-02T08:00:00+08:00",
                "2024-03-02T12:00:00+08:00",
                activity_type="internship",
            ),
            _checkin(
                "E-03", "S2", "2024-03-20T08:00:00+08:00", "2024-03-20T10:00:00+08:00"
            ),
        ],
    )

    # S2's internship check-in is pending until the mentor confirms it.
    body = client.get(f"/api/plans/{pv}/warnings").json()
    assert body["rule_version"] == "R1"
    assert body["students_evaluated"] == 2
    by_key = {(w["student_id"], w["stage_id"]): w for w in body["warnings"]}
    # S2 is pending, so it warns as well until confirmation arrives.
    assert ("S2", "ST-1") in by_key
    assert by_key[("S2", "ST-1")]["category_gaps"] == {"internship": 1800}

    _post_events(
        client,
        pv,
        [
            {
                "event_id": "E-04",
                "event_type": "mentor_confirm",
                "student_id": "S2",
                "payload": {"checkin_event_id": "E-02"},
            }
        ],
    )
    body = client.get(f"/api/plans/{pv}/warnings").json()
    by_key = {(w["student_id"], w["stage_id"]): w for w in body["warnings"]}
    assert ("S2", "ST-1") not in by_key
    assert ("S2", "ST-2") not in by_key
    # S1 still misses ST-2 (ST-1 is covered via carry/compensation, and the
    # 1h surplus carried from ST-1 leaves a 1h gap in ST-2).
    assert ("S1", "ST-1") not in by_key
    assert ("S1", "ST-2") in by_key
    assert by_key[("S1", "ST-2")]["gap_seconds"] == 3600
    assert by_key[("S1", "ST-2")]["frozen"] is False

    # The stage_id filter narrows the batch.
    filtered = client.get(
        f"/api/plans/{pv}/warnings", params={"stage_id": "ST-2"}
    ).json()
    assert {(w["student_id"], w["stage_id"]) for w in filtered["warnings"]} == {
        ("S1", "ST-2")
    }


def test_warnings_without_active_rules_are_empty(client):
    pv = _create_plan(client)
    body = client.get(f"/api/plans/{pv}/warnings").json()
    assert body["rule_version"] is None
    assert body["warnings"] == []
    assert body["students_evaluated"] == 0
