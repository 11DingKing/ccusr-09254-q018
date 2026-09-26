"""分阶段培养 API：规则版本、学生查询、批量预警、阶段冻结、并发关账。"""

from __future__ import annotations

import threading

from tests.conftest import SHANGHAI_PLAN


def _create_plan(client, required=10800, pv="P-STAGE-1"):
    plan = dict(SHANGHAI_PLAN)
    plan["plan_version"] = pv
    plan["required_seconds"] = required
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text
    return plan["plan_version"]


def _rules_payload():
    return {
        "stages": [
            {
                "stage_id": "ST1",
                "end_date": "2024-03-31",
                "required_seconds": 3600,
                "carryover_cap_seconds": 1800,
                "category_requirements": [
                    {"category": "internship", "required_seconds": 1800}
                ],
                "compensation": [
                    {
                        "from_category": "regular",
                        "to_category": "internship",
                        "rate_milli": 1000,
                    }
                ],
            },
            {
                "stage_id": "ST2",
                "end_date": "2024-06-30",
                "required_seconds": 7200,
                "carryover_cap_seconds": 0,
            },
        ]
    }


def _publish_rules(client, pv, payload=None):
    resp = client.put(f"/api/plans/{pv}/stage-rules", json=payload or _rules_payload())
    assert resp.status_code == 201, resp.text
    return resp.json()


def _checkin(eid, student, start, end, activity_type="regular"):
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


def _correction(eid, student, seconds, business_date, category="regular"):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {
            "adjustment_seconds": seconds,
            "business_date": business_date,
            "category": category,
            "reason": "adj",
        },
    }


def _post_events(client, pv, events):
    return client.post(f"/api/plans/{pv}/events", json={"events": events})


def test_publish_and_list_immutable_rule_versions(client):
    pv = _create_plan(client)
    v1 = _publish_rules(client, pv)
    assert v1["rules_version"] == 1
    assert len(v1["stages"]) == 2

    # 发布新版本：版本号递增，旧版本仍可查询。
    payload = _rules_payload()
    payload["stages"][1]["required_seconds"] = 9000
    v2 = _publish_rules(client, pv, payload)
    assert v2["rules_version"] == 2
    assert v2["stages"][1]["required_seconds"] == 9000

    versions = client.get(f"/api/plans/{pv}/stage-rules").json()
    assert [v["rules_version"] for v in versions] == [1, 2]
    assert versions[0]["stages"][1]["required_seconds"] == 7200

    # 非法配置（阶段日期逆序）返回 422。
    bad = _rules_payload()
    bad["stages"][0]["end_date"] = "2024-07-31"
    resp = client.put(f"/api/plans/{pv}/stage-rules", json=bad)
    assert resp.status_code == 422
    # 失败不占用版本号：下一次发布仍是 v3。
    v3 = _publish_rules(client, pv, payload)
    assert v3["rules_version"] == 3


def test_student_stage_progress_reports_gap_and_carryable_sources(client):
    pv = _create_plan(client)
    _publish_rules(client, pv)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-30T08:00:00+08:00",
                "2024-03-30T12:00:00+08:00",
            )  # 4h ST1
        ],
    )
    body = client.get(f"/api/plans/{pv}/students/S1/stage-progress").json()
    assert body["rules_version"] == 1
    st1, st2 = body["stages"]
    assert st1["stage_id"] == "ST1"
    assert st1["earned_seconds"] == 4 * 3600
    assert st1["carry_out_seconds"] == 1800
    assert st1["spillover_lost_seconds"] == 3 * 3600 - 1800
    # regular 盈余补偿了 internship 类别门槛。
    cat = st1["category_gaps"][0]
    assert cat["category"] == "internship"
    assert cat["met"] is True
    assert cat["compensated_seconds"] == 1800
    # ST2 未达标：缺口 = 7200 - 1800（结转）。
    assert st2["carry_in_seconds"] == 1800
    assert st2["carry_in_source_stage"] == "ST1"
    assert st2["total_gap_seconds"] == 7200 - 1800
    assert body["meets_all"] is False

    # 规则尚未配置的培养方案查询返回 404；未知学生也是 404。
    pv2 = _create_plan(client, pv="P-STAGE-2")
    assert (
        client.get(f"/api/plans/{pv2}/students/S1/stage-progress").status_code == 404
    )
    assert (
        client.get(f"/api/plans/{pv}/students/NOBODY/stage-progress").status_code
        == 404
    )


def test_batch_warnings_lists_only_unmet_open_stages(client):
    pv = _create_plan(client)
    _publish_rules(client, pv)
    _post_events(
        client,
        pv,
        [
            # S1 两期都达标（ST1 4h，ST2 自身 2h + 封顶结转 0.5h... 调整为 3h）。
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
                "2024-05-01T11:00:00+08:00",
            ),
            # S2 ST1 完全没有学时。
            _checkin(
                "E-03",
                "S2",
                "2024-05-01T08:00:00+08:00",
                "2024-05-01T09:00:00+08:00",
            ),
        ],
    )
    body = client.get(f"/api/plans/{pv}/stage-warnings").json()
    warned = {(w["student_id"], w["stage_id"]) for w in body["warnings"]}
    # S1 两期都达标（ST1 4h；ST2 自身 3h）；S2 ST1 无学时、ST2 仅 1h。
    assert warned == {("S2", "ST1"), ("S2", "ST2")}
    assert body["students_below"] == 2

    st1_only = client.get(
        f"/api/plans/{pv}/stage-warnings", params={"stage_id": "ST1"}
    ).json()
    assert {w["stage_id"] for w in st1_only["warnings"]} == {"ST1"}
    assert {w["student_id"] for w in st1_only["warnings"]} == {"S2"}

    # 未知阶段 404。
    resp = client.get(
        f"/api/plans/{pv}/stage-warnings", params={"stage_id": "NOPE"}
    )
    assert resp.status_code == 404


def test_stage_freeze_requires_predecessors_and_is_idempotent(client):
    pv = _create_plan(client)
    _publish_rules(client, pv)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-30T08:00:00+08:00",
                "2024-03-30T10:00:00+08:00",
            )
        ],
    )
    # ST2 不能先于 ST1 关账。
    resp = client.post(f"/api/plans/{pv}/stage-freezes/ST2")
    assert resp.status_code == 409

    f1 = client.post(f"/api/plans/{pv}/stage-freezes/ST1")
    assert f1.status_code == 201
    body = f1.json()
    assert body["created"] is True
    assert body["rules_version"] == 1
    assert body["event_cutoff_id"] == "E-01"
    stage = body["students"][0]["stage"]
    assert stage["frozen"] is True
    assert stage["earned_seconds"] == 7200

    # 幂等：再次关账返回原凭证且 created=false。
    again = client.post(f"/api/plans/{pv}/stage-freezes/ST1").json()
    assert again["created"] is False
    assert again["event_cutoff_id"] == "E-01"

    # 前置已关账后 ST2 可以关账。
    f2 = client.post(f"/api/plans/{pv}/stage-freezes/ST2")
    assert f2.status_code == 201


def test_late_negative_adjustment_does_not_penetrate_closed_stage(client):
    pv = _create_plan(client)
    _publish_rules(client, pv)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-30T08:00:00+08:00",
                "2024-03-30T12:00:00+08:00",
            )
        ],
    )
    client.post(f"/api/plans/{pv}/stage-freezes/ST1")

    # 迟到负向修正（事件仍被记录用于审计，但重放剪掉关账段）。
    result = _post_events(
        client, pv, [_correction("E-90", "S1", -7200, "2024-03-30")]
    ).json()
    assert result["accepted"] == 1

    progress = client.get(f"/api/plans/{pv}/students/S1/stage-progress").json()
    st1 = next(s for s in progress["stages"] if s["stage_id"] == "ST1")
    assert st1["frozen"] is True
    assert st1["earned_seconds"] == 4 * 3600
    assert st1["carry_out_seconds"] == 1800

    # 关账凭证本身不可变。
    voucher = client.post(f"/api/plans/{pv}/stage-freezes/ST1").json()
    assert voucher["students"][0]["stage"]["earned_seconds"] == 4 * 3600


def test_late_cross_boundary_checkin_updates_open_stage_only(client):
    pv = _create_plan(client)
    _publish_rules(client, pv)
    # ST1 恰好达标并关账。
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-30T08:00:00+08:00",
                "2024-03-30T09:00:00+08:00",
            )
        ],
    )
    client.post(f"/api/plans/{pv}/stage-freezes/ST1")

    # 迟到签到横跨关账边界（3/31 23:00 -> 4/1 01:00），只有 ST2 的 1 小时生效。
    result = _post_events(
        client,
        pv,
        [
            _checkin(
                "E-91",
                "S1",
                "2024-03-31T23:00:00+08:00",
                "2024-04-01T01:00:00+08:00",
            )
        ],
    ).json()
    assert result["accepted"] == 1
    progress = client.get(f"/api/plans/{pv}/students/S1/stage-progress").json()
    st1 = next(s for s in progress["stages"] if s["stage_id"] == "ST1")
    st2 = next(s for s in progress["stages"] if s["stage_id"] == "ST2")
    assert st1["earned_seconds"] == 3600
    assert st2["earned_seconds"] == 3600


def test_negative_adjustment_in_open_stage_reopens_gap(client):
    pv = _create_plan(client)
    _publish_rules(client, pv)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-30T08:00:00+08:00",
                "2024-03-30T10:00:00+08:00",
            ),
            _correction("E-02", "S1", -5400, "2024-03-30"),
        ],
    )
    progress = client.get(f"/api/plans/{pv}/students/S1/stage-progress").json()
    st1 = next(s for s in progress["stages"] if s["stage_id"] == "ST1")
    assert st1["earned_seconds"] == 1800
    assert st1["total_gap_seconds"] == 1800
    assert st1["met"] is False

    # 阶段规则生效后，缺 business_date 的修正被拒绝。
    result = _post_events(
        client,
        pv,
        [
            {
                "event_id": "E-03",
                "event_type": "leave_correction",
                "student_id": "S1",
                "payload": {"adjustment_seconds": 100, "reason": "no date"},
            }
        ],
    ).json()
    assert result["accepted"] == 0
    assert result["rejected"][0]["event_id"] == "E-03"


def test_new_rule_version_requires_refreeze_and_leaves_old_voucher(client):
    pv = _create_plan(client)
    _publish_rules(client, pv)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-30T08:00:00+08:00",
                "2024-03-30T10:00:00+08:00",
            )
        ],
    )
    client.post(f"/api/plans/{pv}/stage-freezes/ST1")

    # 发布 v2（ST2 门槛提高）；v1 的关账在 v2 下不再生效，ST1 可重新关账。
    payload = _rules_payload()
    payload["stages"][1]["required_seconds"] = 9000
    _publish_rules(client, pv, payload)

    progress = client.get(f"/api/plans/{pv}/students/S1/stage-progress").json()
    assert progress["rules_version"] == 2
    st1 = next(s for s in progress["stages"] if s["stage_id"] == "ST1")
    assert st1["frozen"] is False  # v2 尚未关账

    # v2 下需要重新顺序关账。
    resp = client.post(f"/api/plans/{pv}/stage-freezes/ST2")
    assert resp.status_code == 409
    assert client.post(f"/api/plans/{pv}/stage-freezes/ST1").status_code == 201


def test_concurrent_stage_freeze_only_one_wins(client):
    pv = _create_plan(client)
    _publish_rules(client, pv)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-30T08:00:00+08:00",
                "2024-03-30T10:00:00+08:00",
            )
        ],
    )

    from app import services
    from tests.conftest import TestSessionLocal

    created_flags: list[bool] = []
    lock = threading.Lock()

    def _freeze():
        session = TestSessionLocal()
        try:
            _, created = services.freeze_stage(
                session, plan_version=pv, stage_id="ST1"
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


def test_concurrent_out_of_order_freeze_never_pierces_order(client):
    pv = _create_plan(client)
    _publish_rules(client, pv)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-30T08:00:00+08:00",
                "2024-03-30T10:00:00+08:00",
            )
        ],
    )

    from app import services
    from app.services import StageOrderError
    from tests.conftest import TestSessionLocal

    errors: list[bool] = []
    lock = threading.Lock()

    def _freeze_st2():
        session = TestSessionLocal()
        try:
            services.freeze_stage(session, plan_version=pv, stage_id="ST2")
            with lock:
                errors.append(False)
        except StageOrderError:
            with lock:
                errors.append(True)
        finally:
            session.close()

    threads = [threading.Thread(target=_freeze_st2) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [True, True, True, True]
    # ST2 凭证绝不存在；随后仍可正常顺序关账。
    assert client.post(f"/api/plans/{pv}/stage-freezes/ST2").status_code == 409
    assert client.post(f"/api/plans/{pv}/stage-freezes/ST1").status_code == 201
    assert client.post(f"/api/plans/{pv}/stage-freezes/ST2").status_code == 201
