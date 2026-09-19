"""交接计划符合性审计的单元测试与 API 集成测试。"""
from __future__ import annotations

import itertools
import random
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.handover import HandoverPlanAuditError, _optimal_matching, _timing, audit_handover_plan
from app.main import app
from app.models import (
    Event,
    EventType,
    HandoverPlanAuditRequest,
    HandoverPlanNode,
)

client = TestClient(app)

DAY = "2026-09-19"


def dt(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hour:02d}:{minute:02d}:{second:02d}+00:00")


def ev(type_: EventType, hour: int, minute: int = 0, **kw) -> Event:
    return Event(type=type_, timestamp=dt(hour, minute), **kw)


def node(custodian: str, start: tuple[int, int], end: tuple[int, int]) -> HandoverPlanNode:
    return HandoverPlanNode(
        custodian=custodian,
        window_start=dt(*start),
        window_end=dt(*end),
    )


def make_request(events, plan=()) -> HandoverPlanAuditRequest:
    return HandoverPlanAuditRequest(events=events, plan=list(plan))


def node_index(resp):
    return {r.index: r for r in resp.plan_results}


# ---------------------------------------------------------------------------
# 基本匹配：early / on_time / late 与窗口半开边界
# ---------------------------------------------------------------------------


def test_all_matched_with_three_timings():
    events = [
        ev(EventType.SEAL, 9, 50, seal_id="S1", custodian="Alice"),   # early
        ev(EventType.HANDOVER, 11, 0, custodian="Bob"),               # on_time（恰为起点）
        ev(EventType.HANDOVER, 12, 35, custodian="Carol"),            # late（过终点 300s）
    ]
    plan = [
        node("Alice", (10, 0), (11, 0)),
        node("Bob", (11, 0), (12, 0)),
        node("Carol", (12, 0), (12, 30)),
    ]
    resp = audit_handover_plan(make_request(events, plan))

    results = node_index(resp)
    assert results[0].status == "matched"
    assert results[0].event_index == 0
    assert results[0].timing == "early"
    assert results[0].deviation_seconds == 600.0

    assert results[1].event_index == 1
    assert results[1].timing == "on_time"  # t == window_start 算准时
    assert results[1].deviation_seconds == 0.0

    assert results[2].event_index == 2
    assert results[2].timing == "late"  # t >= window_end 算迟到（窗口半开）
    assert results[2].deviation_seconds == 300.0

    assert resp.unexpected == []
    assert resp.summary.model_dump() == {
        "matched": 3, "missing": 0, "unexpected": 0,
        "early": 1, "on_time": 1, "late": 1,
    }


def test_timing_helper_half_open_window():
    n = node("Alice", (10, 0), (11, 0))
    assert _timing(dt(9, 59), n) == ("early", 60.0)
    assert _timing(dt(10, 0), n) == ("on_time", 0.0)  # 起点含
    assert _timing(dt(10, 59), n) == ("on_time", 0.0)
    assert _timing(dt(11, 0), n) == ("late", 0.0)    # 终点不含：迟到但偏离为 0
    assert _timing(dt(11, 1), n) == ("late", 60.0)


def test_missing_node_has_null_fields():
    events = [ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice")]
    plan = [node("Alice", (9, 0), (11, 0)), node("Bob", (11, 0), (13, 0))]
    resp = audit_handover_plan(make_request(events, plan))

    results = node_index(resp)
    assert results[0].status == "matched"
    assert results[1].status == "missing"
    assert results[1].event_index is None
    assert results[1].actual_timestamp is None
    assert results[1].timing is None
    assert results[1].deviation_seconds is None
    assert resp.summary.matched == 1
    assert resp.summary.missing == 1


def test_unmatched_arrivals_are_unexpected_in_index_order():
    events = [
        ev(EventType.SEAL, 9, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 10, custodian="Bob"),
        ev(EventType.HANDOVER, 11, custodian="Carol"),
    ]
    resp = audit_handover_plan(make_request(events, [node("Bob", (9, 30), (10, 30))]))

    results = node_index(resp)
    assert results[0].status == "matched"
    assert results[0].event_index == 1

    assert [(u.event_index, u.custodian, u.type) for u in resp.unexpected] == [
        (0, "Alice", EventType.SEAL),
        (2, "Carol", EventType.HANDOVER),
    ]
    assert resp.summary.unexpected == 2


# ---------------------------------------------------------------------------
# 仅 status=ok 的 seal/reseal/handover 参与匹配；下标为全链下标
# ---------------------------------------------------------------------------


def test_illegal_events_never_participate_and_indices_preserved():
    events = [
        ev(EventType.UNSEAL, 8),                                       # 违规：未封装开封
        ev(EventType.SEAL, 9, seal_id="S1", custodian="Alice"),        # ok，全链下标 1
        ev(EventType.HANDOVER, 10, custodian="Bob"),                   # ok
        ev(EventType.UNSEAL, 10, 30),                                  # ok，但 unseal 不是到任记录
        ev(EventType.RESEAL, 11, seal_id="S1", custodian="Bob"),       # 违规：复用封签
        ev(EventType.RESEAL, 11, 30, seal_id="S2", custodian="Carol"),  # ok，下标 5
    ]
    plan = [
        node("Alice", (9, 0), (10, 0)),
        node("Bob", (9, 30), (10, 30)),
        node("Carol", (11, 0), (12, 0)),
        node("Dave", (12, 0), (13, 0)),  # 仅违规事件里没有 Dave → missing
    ]
    resp = audit_handover_plan(make_request(events, plan))
    results = node_index(resp)

    assert resp.validation.valid is False
    assert [v.index for v in resp.validation.violations] == [0, 4]
    assert results[0].event_index == 1
    assert results[1].event_index == 2
    assert results[2].event_index == 5
    assert results[3].status == "missing"
    # 合法 unseal（下标 3）不出现于任何匹配或 unexpected
    used = {r.event_index for r in resp.plan_results if r.event_index is not None}
    used |= {u.event_index for u in resp.unexpected}
    assert used == {1, 2, 5}


# ---------------------------------------------------------------------------
# 全局最优：匹配数优先
# ---------------------------------------------------------------------------


def test_more_matches_beats_lower_deviation():
    # 两条 Alice 记录可匹配两个 Alice 节点；即便“只匹配更准时的一个”偏离更小，
    # 也必须优先最大化匹配数
    events = [
        ev(EventType.SEAL, 9, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 12, custodian="Alice"),
    ]
    plan = [node("Alice", (10, 0), (11, 0)), node("Alice", (11, 0), (12, 0))]
    resp = audit_handover_plan(make_request(events, plan))

    results = node_index(resp)
    assert results[0].status == "matched" and results[0].event_index == 0
    assert results[1].status == "matched" and results[1].event_index == 1
    assert resp.summary.matched == 2


# ---------------------------------------------------------------------------
# 全局最优：总偏离最小（逐项贪心会失败的场景）
# ---------------------------------------------------------------------------


def test_min_total_deviation_is_global_not_greedy():
    # 记录：r1 Alice@09:00、r2 Alice@10:05、r3 Alice@11:05
    # 计划：node0 [10:00,11:00)、node1 [11:00,12:00)
    # “逐节点取最早可行记录”的贪心：node0←r1（早到 3600s）、node1←r2（早到 3300s），
    # 总偏离 6900s；全局最优：node0←r2（准时）、node1←r3（准时），总偏离 0
    events = [
        ev(EventType.SEAL, 9, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 10, 5, custodian="Alice"),
        ev(EventType.HANDOVER, 11, 5, custodian="Alice"),
    ]
    plan = [node("Alice", (10, 0), (11, 0)), node("Alice", (11, 0), (12, 0))]
    resp = audit_handover_plan(make_request(events, plan))

    results = node_index(resp)
    assert results[0].event_index == 1
    assert results[0].timing == "on_time"
    assert results[1].event_index == 2
    assert results[1].timing == "on_time"
    assert resp.unexpected[0].event_index == 0
    assert sum(r.deviation_seconds for r in resp.plan_results if r.deviation_seconds) == 0.0


# ---------------------------------------------------------------------------
# 全局最优：偏离并列时事件下标序列字典序最小
# ---------------------------------------------------------------------------


def test_lexicographic_smallest_index_sequence_on_tie():
    # r1 Alice@09:50（对 node0 早到 600s）、r2 Alice@11:10（对 node0 迟到 600s）、
    # r3 Alice@11:30（对 node1 准时）。node0←r1+node1←r3 与 node0←r2+node1←r3
    # 匹配数、总偏离均为 2 / 600s；下标序列 [0,2] 字典序小于 [1,2]
    events = [
        ev(EventType.SEAL, 9, 50, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 11, 10, custodian="Alice"),
        ev(EventType.HANDOVER, 11, 30, custodian="Alice"),
    ]
    plan = [node("Alice", (10, 0), (11, 0)), node("Alice", (11, 30), (12, 30))]
    resp = audit_handover_plan(make_request(events, plan))

    results = node_index(resp)
    assert results[0].event_index == 0
    assert results[1].event_index == 2
    assert resp.unexpected[0].event_index == 1


def test_single_node_equal_deviation_prefers_smaller_index():
    # 单节点两条记录偏离相同（600s）：取事件下标更小者
    events = [
        ev(EventType.SEAL, 9, 50, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 11, 10, custodian="Alice"),
    ]
    resp = audit_handover_plan(make_request(events, [node("Alice", (10, 0), (11, 0))]))
    assert resp.plan_results[0].event_index == 0
    assert [u.event_index for u in resp.unexpected] == [1]


# ---------------------------------------------------------------------------
# 顺序不交叉
# ---------------------------------------------------------------------------


def test_matching_must_be_order_preserving():
    # 记录顺序：Bob@11:00、Alice@11:30、Bob@12:30；计划顺序：Alice 后 Bob。
    # 第一条 Bob 不能越过 Alice 节点匹配 node1，只能成为 unexpected
    events = [
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Bob"),
        ev(EventType.HANDOVER, 11, 0, custodian="Bob"),
        ev(EventType.HANDOVER, 11, 30, custodian="Alice"),
        ev(EventType.HANDOVER, 12, 30, custodian="Bob"),
    ]
    plan = [node("Alice", (11, 0), (12, 0)), node("Bob", (12, 0), (13, 0))]
    resp = audit_handover_plan(make_request(events, plan))

    results = node_index(resp)
    assert results[0].event_index == 2
    assert results[1].event_index == 3
    assert [u.event_index for u in resp.unexpected] == [0, 1]
    assert resp.summary.matched == 2


def test_custodian_must_match():
    events = [ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice")]
    resp = audit_handover_plan(make_request(events, [node("Bob", (9, 0), (11, 0))]))
    assert resp.plan_results[0].status == "missing"
    assert resp.unexpected[0].custodian == "Alice"


# ---------------------------------------------------------------------------
# 空输入与 validation 保留
# ---------------------------------------------------------------------------


def test_empty_events_and_plan():
    resp = audit_handover_plan(make_request([]))
    assert resp.validation.valid is True
    assert resp.plan_results == []
    assert resp.unexpected == []
    assert resp.summary.model_dump() == {
        "matched": 0, "missing": 0, "unexpected": 0,
        "early": 0, "on_time": 0, "late": 0,
    }


def test_validation_result_is_preserved():
    events = [
        ev(EventType.HANDOVER, 10, custodian="Bob"),  # 违规：未封装交接
        ev(EventType.SEAL, 11, seal_id="S1", custodian="Alice"),
    ]
    resp = audit_handover_plan(make_request(events, [node("Alice", (10, 0), (12, 0))]))
    assert resp.validation.valid is False
    assert [e.status for e in resp.validation.events] == ["violation", "ok"]
    assert resp.plan_results[0].event_index == 1


# ---------------------------------------------------------------------------
# 动态规划与暴力枚举对拍
# ---------------------------------------------------------------------------


def _arrivals_from(events):
    from app.handover import _extract_arrivals
    from app.validator import validate_events
    return _extract_arrivals(events, validate_events(events).events)


def _brute_force_best(nodes, arrivals):
    """枚举全部保序一对一配对，返回与 DP 相同口径的最优状态键。"""
    best = None
    for k in range(min(len(nodes), len(arrivals)) + 1):
        for ni in itertools.combinations(range(len(nodes)), k):
            for ri in itertools.combinations(range(len(arrivals)), k):
                if any(
                    nodes[a].custodian != arrivals[b].custodian
                    for a, b in zip(ni, ri)
                ):
                    continue
                total = sum(
                    _timing(arrivals[b].timestamp, nodes[a])[1]
                    for a, b in zip(ni, ri)
                )
                key = (-k, total, ri)
                if best is None or key < best:
                    best = key
    return best


@pytest.mark.parametrize("seed", [0, 1, 7, 42, 99])
def test_dp_matches_brute_force(seed):
    rng = random.Random(seed)
    custodians = ["Alice", "Bob", "Carol"]
    events = [
        Event(type=EventType.SEAL, timestamp=dt(8, 0), seal_id="S0",
              custodian=rng.choice(custodians))
    ]
    minute = 480
    for i in range(1, rng.randint(3, 8)):
        minute += rng.randint(20, 90)
        kind = rng.choice(
            [EventType.HANDOVER, EventType.HANDOVER, EventType.UNSEAL,
             EventType.RESEAL, EventType.SEAL]
        )
        kw = {"custodian": rng.choice(custodians)}
        if kind in (EventType.SEAL, EventType.RESEAL):
            kw["seal_id"] = f"S{i}"
        timestamp = datetime.fromisoformat(
            f"{DAY}T{minute // 60:02d}:{minute % 60:02d}:00+00:00"
        )
        events.append(Event(type=kind, timestamp=timestamp, **kw))

    arrivals = _arrivals_from(events)
    nodes = []
    for i in range(rng.randint(1, 5)):
        start_min = rng.randint(460, minute + 60)
        end_min = start_min + rng.randint(15, 120)
        nodes.append(
            HandoverPlanNode(
                custodian=rng.choice(custodians),
                window_start=datetime.fromisoformat(
                    f"{DAY}T{start_min // 60:02d}:{start_min % 60:02d}:00+00:00"),
                window_end=datetime.fromisoformat(
                    f"{DAY}T{end_min // 60:02d}:{end_min % 60:02d}:00+00:00"),
            )
        )

    pairs = _optimal_matching(nodes, arrivals)
    dp_key = (
        -len(pairs),
        sum(
            _timing(arrivals[r].timestamp, nodes[i])[1] for i, r in pairs
        ),
        tuple(r for _, r in pairs),
    )
    assert dp_key == _brute_force_best(nodes, arrivals)


# ---------------------------------------------------------------------------
# 422 语义错误
# ---------------------------------------------------------------------------


def test_mixed_timezone_awareness_raises_422():
    events = [
        Event(type=EventType.SEAL,
              timestamp=datetime.fromisoformat(f"{DAY}T10:00:00+00:00"),
              seal_id="S1", custodian="Alice"),
    ]
    plan = [
        HandoverPlanNode.model_construct(
            custodian="Alice",
            window_start=datetime.fromisoformat(f"{DAY}T09:00:00"),  # naive
            window_end=datetime.fromisoformat(f"{DAY}T11:00:00"),
        )
    ]
    request = HandoverPlanAuditRequest.model_construct(events=events, plan=plan)
    with pytest.raises(HandoverPlanAuditError):
        audit_handover_plan(request)


def test_window_start_not_before_end_raises_validation_error():
    with pytest.raises(ValidationError):
        node("Alice", (12, 0), (12, 0))
    with pytest.raises(ValidationError):
        node("Alice", (13, 0), (12, 0))


def test_empty_custodian_rejected():
    with pytest.raises(ValidationError):
        HandoverPlanNode(
            custodian="",
            window_start=dt(10),
            window_end=dt(11),
        )


# ---------------------------------------------------------------------------
# API 集成
# ---------------------------------------------------------------------------


def test_api_handover_plan_audit_full_response():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T09:50:00Z",
             "seal_id": "S001", "custodian": "Alice"},
            {"type": "handover", "timestamp": f"{DAY}T11:00:00Z", "custodian": "Bob"},
            {"type": "unseal", "timestamp": f"{DAY}T11:30:00Z"},
            {"type": "reseal", "timestamp": f"{DAY}T11:45:00Z",
             "seal_id": "S002", "custodian": "Bob"},
        ],
        "plan": [
            {"custodian": "Alice",
             "window_start": f"{DAY}T10:00:00Z", "window_end": f"{DAY}T11:00:00Z"},
            {"custodian": "Bob",
             "window_start": f"{DAY}T11:00:00Z", "window_end": f"{DAY}T12:00:00Z"},
        ],
    }
    r = client.post("/api/v1/seal-chain/handover-plan-audit", json=payload)
    assert r.status_code == 200
    body = r.json()

    assert body["validation"]["valid"] is True
    assert body["plan_results"] == [
        {
            "index": 0, "custodian": "Alice",
            "window_start": f"{DAY}T10:00:00Z", "window_end": f"{DAY}T11:00:00Z",
            "status": "matched", "event_index": 0,
            "actual_timestamp": f"{DAY}T09:50:00Z",
            "timing": "early", "deviation_seconds": 600.0,
        },
        {
            "index": 1, "custodian": "Bob",
            "window_start": f"{DAY}T11:00:00Z", "window_end": f"{DAY}T12:00:00Z",
            "status": "matched", "event_index": 1,
            "actual_timestamp": f"{DAY}T11:00:00Z",
            "timing": "on_time", "deviation_seconds": 0.0,
        },
    ]
    # reseal@11:45 保管人仍为 Bob，无后续 Bob 计划节点 → unexpected；unseal 不计
    assert body["unexpected"] == [
        {"event_index": 3, "type": "reseal",
         "timestamp": f"{DAY}T11:45:00Z", "custodian": "Bob"}
    ]
    assert body["summary"] == {
        "matched": 2, "missing": 0, "unexpected": 1,
        "early": 1, "on_time": 1, "late": 0,
    }


def test_api_violation_event_index_is_full_chain_index():
    payload = {
        "events": [
            {"type": "unseal", "timestamp": f"{DAY}T08:00:00Z"},
            {"type": "seal", "timestamp": f"{DAY}T10:00:00Z",
             "seal_id": "S1", "custodian": "Alice"},
        ],
        "plan": [
            {"custodian": "Alice",
             "window_start": f"{DAY}T09:00:00Z", "window_end": f"{DAY}T11:00:00Z"},
        ],
    }
    r = client.post("/api/v1/seal-chain/handover-plan-audit", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["plan_results"][0]["event_index"] == 1
    assert body["validation"]["violations"][0]["index"] == 0


def test_api_mixed_timezone_returns_422():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T10:00:00Z",
             "seal_id": "S1", "custodian": "Alice"},
        ],
        "plan": [
            {"custodian": "Alice",
             "window_start": f"{DAY}T09:00:00", "window_end": f"{DAY}T11:00:00"},
        ],
    }
    r = client.post("/api/v1/seal-chain/handover-plan-audit", json=payload)
    assert r.status_code == 422
    assert "时区" in r.json()["detail"]


def test_api_window_start_after_end_returns_422():
    payload = {
        "events": [],
        "plan": [
            {"custodian": "Alice",
             "window_start": f"{DAY}T12:00:00Z", "window_end": f"{DAY}T11:00:00Z"},
        ],
    }
    assert client.post(
        "/api/v1/seal-chain/handover-plan-audit", json=payload
    ).status_code == 422


def test_api_empty_request_returns_200():
    r = client.post("/api/v1/seal-chain/handover-plan-audit", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["plan_results"] == []
    assert body["unexpected"] == []
    assert body["summary"]["matched"] == 0
