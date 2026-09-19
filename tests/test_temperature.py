"""温控责任审计接口的单元测试与 API 集成测试。"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
from app.models import Event, EventType, TemperatureAuditRequest, TemperatureSample
from app.temperature import TemperatureAuditError, audit_temperature

client = TestClient(app)

DAY = "2026-09-18"


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hour:02d}:{minute:02d}:00+00:00")


def ev(type_: EventType, hour: int, minute: int = 0, **kw) -> Event:
    return Event(type=type_, timestamp=dt(hour, minute), **kw)


def sample(hour: int, minute: int, temperature: float) -> TemperatureSample:
    return TemperatureSample(timestamp=dt(hour, minute), temperature=temperature)


def make_request(events, samples, min_t=2.0, max_t=8.0, gap=900.0) -> TemperatureAuditRequest:
    return TemperatureAuditRequest(
        events=events,
        samples=samples,
        min_temperature=min_t,
        max_temperature=max_t,
        max_gap_seconds=gap,
    )


def seg_tuples(segments):
    return [
        (
            s.custodian,
            s.kind,
            s.start.strftime("%H:%M"),
            s.end.strftime("%H:%M"),
            s.seconds,
            s.sample_index,
            s.deviation,
        )
        for s in segments
    ]


def alice_bob_chain():
    """seal@10:00 Alice, handover@12:00 Bob（合法链）。"""
    return [
        ev(EventType.SEAL, 10, seal_id="S001", custodian="Alice"),
        ev(EventType.HANDOVER, 12, custodian="Bob"),
    ]


# ---------------------------------------------------------------------------
# 核心逻辑：越界判定与交接点拆分
# ---------------------------------------------------------------------------


def test_high_violation_single_custodian():
    request = make_request(
        alice_bob_chain(),
        [sample(10, 30, 10.0), sample(10, 40, 5.0)],
    )
    resp = audit_temperature(request)
    assert seg_tuples(resp.violations) == [
        ("Alice", "high", "10:30", "10:40", 600.0, 0, 2.0),
    ]
    assert resp.custodians[0].custodian == "Alice"
    assert resp.custodians[0].high_seconds == 600.0
    assert resp.custodians[0].low_seconds == 0.0
    assert resp.unattributed_seconds == 0.0
    assert resp.data_holes == []
    # 原校验结果保留
    assert resp.validation.valid is True
    assert [e.status for e in resp.validation.events] == ["ok", "ok"]


def test_low_violation():
    request = make_request(
        alice_bob_chain(),
        [sample(10, 30, 0.5), sample(10, 40, 5.0)],
    )
    resp = audit_temperature(request)
    assert seg_tuples(resp.violations) == [
        ("Alice", "low", "10:30", "10:40", 600.0, 0, 1.5),
    ]
    assert resp.custodians[0].low_seconds == 600.0


def test_violation_split_at_handover():
    # 前值保持区间 [11:55,12:05) 跨 12:00 交接点，必须拆分为两段
    request = make_request(
        alice_bob_chain(),
        [sample(11, 55, 11.0), sample(12, 5, 5.0)],
    )
    resp = audit_temperature(request)
    assert seg_tuples(resp.violations) == [
        ("Alice", "high", "11:55", "12:00", 300.0, 0, 3.0),
        ("Bob", "high", "12:00", "12:05", 300.0, 0, 3.0),
    ]
    assert resp.custodians[0].high_seconds == 300.0
    assert resp.custodians[1].custodian == "Bob"
    assert resp.custodians[1].high_seconds == 300.0


def test_held_interval_ending_exactly_at_handover_is_not_split():
    # 半开区间：[11:50,12:00) 完全属于 Alice，不产生 Bob 的零长度分段
    request = make_request(
        alice_bob_chain(),
        [sample(11, 50, 11.0), sample(12, 0, 5.0)],
    )
    resp = audit_temperature(request)
    assert seg_tuples(resp.violations) == [
        ("Alice", "high", "11:50", "12:00", 600.0, 0, 3.0),
    ]


def test_unseal_does_not_split_but_reseal_does():
    events = [
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.UNSEAL, 10, 30),
        ev(EventType.RESEAL, 10, 40, seal_id="S2", custodian="Bob"),
    ]
    request = make_request(
        events, [sample(10, 20, 10.0), sample(10, 50, 5.0)], gap=1800.0
    )
    resp = audit_temperature(request)
    # unseal@10:30 不是交接点；reseal@10:40 是交接点
    assert seg_tuples(resp.violations) == [
        ("Alice", "high", "10:20", "10:40", 1200.0, 0, 2.0),
        ("Bob", "high", "10:40", "10:50", 600.0, 0, 2.0),
    ]


def test_last_custodian_interval_extends_to_infinity():
    # 最后一个有效到任事件之后的越界全部归属该保管人
    request = make_request(
        alice_bob_chain(),
        [sample(20, 0, 10.0), sample(20, 10, 5.0)],
    )
    resp = audit_temperature(request)
    assert seg_tuples(resp.violations) == [
        ("Bob", "high", "20:00", "20:10", 600.0, 0, 2.0),
    ]


def test_unattributed_before_first_arrival():
    request = make_request(
        alice_bob_chain(),
        [sample(9, 0, 10.0), sample(9, 10, 5.0)],
    )
    resp = audit_temperature(request)
    assert seg_tuples(resp.violations) == [
        (None, "high", "09:00", "09:10", 600.0, 0, 2.0),
    ]
    assert resp.unattributed_high_seconds == 600.0
    assert resp.unattributed_low_seconds == 0.0
    assert resp.unattributed_seconds == 600.0
    # 未归属不计入任何保管人
    assert resp.custodians[0].high_seconds == 0.0


def test_no_custodian_events_all_unattributed():
    events = [ev(EventType.SEAL, 10, seal_id="S1")]  # 缺 custodian，违规
    request = make_request(events, [sample(10, 30, 1.0), sample(10, 40, 5.0)])
    resp = audit_temperature(request)
    assert resp.validation.valid is False
    assert resp.custodians == []
    assert seg_tuples(resp.violations) == [
        (None, "low", "10:30", "10:40", 600.0, 0, 1.0),
    ]
    assert resp.unattributed_low_seconds == 600.0


def test_violation_events_do_not_create_custodian_intervals():
    events = [
        ev(EventType.UNSEAL, 9),                                  # 违规：未封装开封
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 11, custodian="Bob"),
        ev(EventType.HANDOVER, 11, 30),                           # 违规：缺 custodian
        ev(EventType.HANDOVER, 12, custodian="Carol"),
    ]
    request = make_request(
        events, [sample(11, 15, 10.0), sample(11, 45, 5.0)], gap=1800.0
    )
    resp = audit_temperature(request)
    # 违规的 handover@11:30 不产生交接点，[11:15,11:45) 完整归属 Bob
    assert seg_tuples(resp.violations) == [
        ("Bob", "high", "11:15", "11:45", 1800.0, 0, 2.0),
    ]
    assert resp.validation.valid is False
    assert [v.index for v in resp.validation.violations] == [0, 3]
    assert [c.custodian for c in resp.custodians] == ["Alice", "Bob", "Carol"]


def test_same_custodian_reappearing_is_aggregated():
    events = [
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 11, custodian="Bob"),
        ev(EventType.HANDOVER, 12, custodian="Alice"),  # Alice 再次到任
    ]
    request = make_request(
        events,
        [sample(10, 0, 10.0), sample(10, 10, 5.0),
         sample(12, 0, 10.0), sample(12, 10, 5.0)],
    )
    resp = audit_temperature(request)
    # 同一保管人两段任期合并汇总，且按首次到任顺序只出现一次
    assert [c.custodian for c in resp.custodians] == ["Alice", "Bob"]
    assert resp.custodians[0].high_seconds == 1200.0


# ---------------------------------------------------------------------------
# 前值保持与数据空洞
# ---------------------------------------------------------------------------


def test_gap_equal_to_threshold_still_holds():
    request = make_request(
        alice_bob_chain(),
        [sample(10, 0, 10.0), sample(10, 15, 5.0)],
        gap=900.0,
    )
    resp = audit_temperature(request)
    assert seg_tuples(resp.violations) == [
        ("Alice", "high", "10:00", "10:15", 900.0, 0, 2.0),
    ]
    assert resp.data_holes == []


def test_gap_exceeding_threshold_is_data_hole():
    request = make_request(
        alice_bob_chain(),
        [sample(10, 0, 10.0), sample(10, 16, 5.0)],
        gap=900.0,
    )
    resp = audit_temperature(request)
    # 间隔 960 > 900：空洞不计时，越界样本不产生任何分段
    assert resp.violations == []
    assert len(resp.data_holes) == 1
    hole = resp.data_holes[0]
    assert (hole.start.strftime("%H:%M"), hole.end.strftime("%H:%M")) == ("10:00", "10:16")
    assert hole.seconds == 960.0
    assert (hole.prev_sample_index, hole.next_sample_index) == (0, 1)
    assert resp.custodians[0].high_seconds == 0.0


def test_last_sample_forms_no_interval():
    request = make_request(alice_bob_chain(), [sample(10, 30, 10.0)])
    resp = audit_temperature(request)
    assert resp.violations == []
    assert resp.data_holes == []
    assert resp.max_deviation is None


def test_empty_samples():
    resp = audit_temperature(make_request(alice_bob_chain(), []))
    assert resp.violations == []
    assert resp.data_holes == []
    assert resp.max_deviation is None
    assert resp.unattributed_seconds == 0.0
    # 保管人仍列出，越界秒数为 0
    assert [c.custodian for c in resp.custodians] == ["Alice", "Bob"]
    assert resp.custodians[0].high_seconds == 0.0
    assert resp.custodians[0].max_deviation is None


def test_in_range_and_boundary_temperatures_produce_no_violations():
    request = make_request(
        alice_bob_chain(),
        [sample(10, 0, 2.0), sample(10, 10, 8.0), sample(10, 20, 5.0)],
    )
    resp = audit_temperature(request)
    # 边界值等于上下限属允许范围（闭区间）
    assert resp.violations == []
    assert resp.max_deviation is None


# ---------------------------------------------------------------------------
# 最大偏差与样本下标可追溯
# ---------------------------------------------------------------------------


def test_max_deviation_overall_and_per_custodian():
    request = make_request(
        alice_bob_chain(),
        [
            sample(10, 0, 10.0),   # idx0 → held [10:00,10:10) high dev 2.0（Alice）
            sample(10, 10, 5.0),
            sample(12, 0, 1.0),    # idx2 → held [12:00,12:10) low dev 1.0（Bob）
            sample(12, 10, 13.0),  # idx3 → held [12:10,12:20) high dev 5.0（Bob）
            sample(12, 20, 5.0),
        ],
    )
    resp = audit_temperature(request)
    overall = resp.max_deviation
    assert overall is not None
    assert (overall.sample_index, overall.kind, overall.temperature,
            overall.deviation) == (3, "high", 13.0, 5.0)
    alice, bob = resp.custodians
    assert (alice.max_deviation.sample_index, alice.max_deviation.deviation) == (0, 2.0)
    assert (bob.max_deviation.sample_index, bob.max_deviation.deviation) == (3, 5.0)


def test_max_deviation_tie_keeps_earliest_sample():
    request = make_request(
        alice_bob_chain(),
        [
            sample(10, 0, 10.0),   # idx0 → high dev 2.0
            sample(10, 10, 5.0),
            sample(10, 20, 10.0),  # idx2 → high dev 2.0（并列）
            sample(10, 30, 5.0),
        ],
    )
    resp = audit_temperature(request)
    assert resp.max_deviation.sample_index == 0


# ---------------------------------------------------------------------------
# 422 语义错误
# ---------------------------------------------------------------------------


def test_samples_not_strictly_increasing_raises_422():
    events = alice_bob_chain()
    with pytest.raises(TemperatureAuditError):
        audit_temperature(make_request(events, [sample(10, 0, 5.0), sample(10, 0, 6.0)]))
    with pytest.raises(TemperatureAuditError):
        audit_temperature(make_request(events, [sample(10, 10, 5.0), sample(10, 0, 6.0)]))


def test_mixed_timezone_samples_raise_422():
    request = TemperatureAuditRequest(
        events=[],
        samples=[
            TemperatureSample(
                timestamp=datetime.fromisoformat(f"{DAY}T10:00:00+00:00"), temperature=5.0
            ),
            TemperatureSample(
                timestamp=datetime.fromisoformat(f"{DAY}T10:10:00"), temperature=6.0
            ),
        ],
        min_temperature=2.0,
        max_temperature=8.0,
        max_gap_seconds=900.0,
    )
    with pytest.raises(TemperatureAuditError):
        audit_temperature(request)


def test_mixed_timezone_samples_vs_events_raise_422():
    request = TemperatureAuditRequest(
        events=[
            Event(type=EventType.SEAL,
                  timestamp=datetime.fromisoformat(f"{DAY}T10:00:00"),  # naive
                  seal_id="S1", custodian="Alice"),
        ],
        samples=[sample(10, 30, 5.0), sample(10, 40, 6.0)],  # aware
        min_temperature=2.0,
        max_temperature=8.0,
        max_gap_seconds=900.0,
    )
    with pytest.raises(TemperatureAuditError):
        audit_temperature(request)


def test_invalid_temperature_range_raises_422():
    events = alice_bob_chain()
    with pytest.raises(ValidationError):
        make_request(events, [], min_t=8.0, max_t=8.0)
    with pytest.raises(ValidationError):
        make_request(events, [], min_t=9.0, max_t=8.0)


def test_non_positive_max_gap_raises_422():
    events = alice_bob_chain()
    with pytest.raises(ValidationError):
        make_request(events, [], gap=0.0)
    with pytest.raises(ValidationError):
        make_request(events, [], gap=-1.0)


def test_nan_or_infinite_temperature_raises_422():
    with pytest.raises(ValidationError):
        make_request(alice_bob_chain(), [sample(10, 0, float("nan"))])
    with pytest.raises(ValidationError):
        make_request(alice_bob_chain(), [], min_t=float("-inf"))


# ---------------------------------------------------------------------------
# API 集成
# ---------------------------------------------------------------------------


def test_api_temperature_audit_full_response():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T10:00:00Z",
             "seal_id": "S001", "custodian": "Alice"},
            {"type": "handover", "timestamp": f"{DAY}T12:00:00Z",
             "custodian": "Bob"},
        ],
        "samples": [
            {"timestamp": f"{DAY}T09:00:00Z", "temperature": 10.0},   # 0 → 未归属高温
            {"timestamp": f"{DAY}T09:10:00Z", "temperature": 5.0},    # 1 → 空洞起点
            {"timestamp": f"{DAY}T10:30:00Z", "temperature": 12.0},   # 2 → Alice 高温 4.0
            {"timestamp": f"{DAY}T10:40:00Z", "temperature": 0.0},    # 3 → 空洞起点
            {"timestamp": f"{DAY}T11:30:00Z", "temperature": 1.0},    # 4 → Alice 低温 1.0
            {"timestamp": f"{DAY}T11:40:00Z", "temperature": 9.0},    # 5 → Alice 高温 1.0
            {"timestamp": f"{DAY}T11:55:00Z", "temperature": 9.0},    # 6 → 跨交接点拆分
            {"timestamp": f"{DAY}T12:05:00Z", "temperature": 4.0},    # 7 → 范围内
            {"timestamp": f"{DAY}T12:20:00Z", "temperature": 4.0},    # 8 → 末样本不成区间
        ],
        "min_temperature": 2.0,
        "max_temperature": 8.0,
        "max_gap_seconds": 900.0,
    }
    r = client.post("/api/v1/seal-chain/temperature-audit", json=payload)
    assert r.status_code == 200
    body = r.json()

    # 原单链校验结果保留
    assert body["validation"]["valid"] is True
    assert body["validation"]["sealed"] is True
    assert body["validation"]["final_custodian"] == "Bob"
    assert [e["status"] for e in body["validation"]["events"]] == ["ok", "ok"]

    # 回显阈值
    assert body["min_temperature"] == 2.0
    assert body["max_temperature"] == 8.0
    assert body["max_gap_seconds"] == 900.0

    # 越界分段：跨 12:00 交接点拆分，按时间升序，样本下标可追溯
    assert body["violations"] == [
        {"custodian": None, "kind": "high",
         "start": f"{DAY}T09:00:00Z", "end": f"{DAY}T09:10:00Z",
         "seconds": 600.0, "sample_index": 0, "temperature": 10.0, "deviation": 2.0},
        {"custodian": "Alice", "kind": "high",
         "start": f"{DAY}T10:30:00Z", "end": f"{DAY}T10:40:00Z",
         "seconds": 600.0, "sample_index": 2, "temperature": 12.0, "deviation": 4.0},
        {"custodian": "Alice", "kind": "low",
         "start": f"{DAY}T11:30:00Z", "end": f"{DAY}T11:40:00Z",
         "seconds": 600.0, "sample_index": 4, "temperature": 1.0, "deviation": 1.0},
        {"custodian": "Alice", "kind": "high",
         "start": f"{DAY}T11:40:00Z", "end": f"{DAY}T11:55:00Z",
         "seconds": 900.0, "sample_index": 5, "temperature": 9.0, "deviation": 1.0},
        {"custodian": "Alice", "kind": "high",
         "start": f"{DAY}T11:55:00Z", "end": f"{DAY}T12:00:00Z",
         "seconds": 300.0, "sample_index": 6, "temperature": 9.0, "deviation": 1.0},
        {"custodian": "Bob", "kind": "high",
         "start": f"{DAY}T12:00:00Z", "end": f"{DAY}T12:05:00Z",
         "seconds": 300.0, "sample_index": 6, "temperature": 9.0, "deviation": 1.0},
    ]

    # 按保管人汇总（按首次到任顺序）
    assert body["custodians"] == [
        {"custodian": "Alice", "high_seconds": 1800.0, "low_seconds": 600.0,
         "max_deviation": {"sample_index": 2, "kind": "high",
                           "temperature": 12.0, "deviation": 4.0}},
        {"custodian": "Bob", "high_seconds": 300.0, "low_seconds": 0.0,
         "max_deviation": {"sample_index": 6, "kind": "high",
                           "temperature": 9.0, "deviation": 1.0}},
    ]

    # 未归属时长与全链最大偏差
    assert body["unattributed_high_seconds"] == 600.0
    assert body["unattributed_low_seconds"] == 0.0
    assert body["unattributed_seconds"] == 600.0
    assert body["max_deviation"] == {"sample_index": 2, "kind": "high",
                                     "temperature": 12.0, "deviation": 4.0}

    # 数据空洞：间隔超过 900 秒的两处（09:10→10:30、10:40→11:30）
    assert body["data_holes"] == [
        {"start": f"{DAY}T09:10:00Z", "end": f"{DAY}T10:30:00Z",
         "seconds": 4800.0, "prev_sample_index": 1, "next_sample_index": 2},
        {"start": f"{DAY}T10:40:00Z", "end": f"{DAY}T11:30:00Z",
         "seconds": 3000.0, "prev_sample_index": 3, "next_sample_index": 4},
    ]


def test_api_empty_request_is_valid():
    payload = {
        "events": [],
        "samples": [],
        "min_temperature": 2.0,
        "max_temperature": 8.0,
        "max_gap_seconds": 300.0,
    }
    r = client.post("/api/v1/seal-chain/temperature-audit", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["validation"]["valid"] is True
    assert body["custodians"] == []
    assert body["violations"] == []
    assert body["max_deviation"] is None
    assert body["unattributed_seconds"] == 0.0
    assert body["data_holes"] == []


def test_api_samples_not_strictly_increasing_returns_422():
    payload = {
        "events": [],
        "samples": [
            {"timestamp": f"{DAY}T10:00:00Z", "temperature": 5.0},
            {"timestamp": f"{DAY}T10:00:00Z", "temperature": 6.0},
        ],
        "min_temperature": 2.0,
        "max_temperature": 8.0,
        "max_gap_seconds": 300.0,
    }
    r = client.post("/api/v1/seal-chain/temperature-audit", json=payload)
    assert r.status_code == 422
    assert "严格递增" in r.json()["detail"]


def test_api_mixed_timezone_returns_422():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T10:00:00Z",
             "seal_id": "S1", "custodian": "Alice"},
        ],
        "samples": [
            {"timestamp": f"{DAY}T10:30:00", "temperature": 5.0},  # naive，与事件混用
            {"timestamp": f"{DAY}T10:40:00", "temperature": 6.0},
        ],
        "min_temperature": 2.0,
        "max_temperature": 8.0,
        "max_gap_seconds": 300.0,
    }
    r = client.post("/api/v1/seal-chain/temperature-audit", json=payload)
    assert r.status_code == 422
    assert "时区" in r.json()["detail"]


def test_api_invalid_thresholds_return_422():
    base = {
        "events": [],
        "samples": [],
        "min_temperature": 2.0,
        "max_temperature": 8.0,
        "max_gap_seconds": 300.0,
    }
    # min_temperature 未严格小于 max_temperature
    r = client.post("/api/v1/seal-chain/temperature-audit",
                    json={**base, "min_temperature": 8.0})
    assert r.status_code == 422
    # max_gap_seconds 非正数
    r = client.post("/api/v1/seal-chain/temperature-audit",
                    json={**base, "max_gap_seconds": 0})
    assert r.status_code == 422
    r = client.post("/api/v1/seal-chain/temperature-audit",
                    json={**base, "max_gap_seconds": -5})
    assert r.status_code == 422
    # 缺少必填阈值
    r = client.post("/api/v1/seal-chain/temperature-audit",
                    json={"events": [], "samples": []})
    assert r.status_code == 422


def test_api_chain_violations_still_return_200():
    payload = {
        "events": [
            {"type": "handover", "timestamp": f"{DAY}T10:00:00Z",
             "custodian": "Alice"},  # 违规：未封装状态下不得交接
        ],
        "samples": [
            {"timestamp": f"{DAY}T10:30:00Z", "temperature": 10.0},
            {"timestamp": f"{DAY}T10:40:00Z", "temperature": 5.0},
        ],
        "min_temperature": 2.0,
        "max_temperature": 8.0,
        "max_gap_seconds": 900.0,
    }
    r = client.post("/api/v1/seal-chain/temperature-audit", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["validation"]["valid"] is False
    # 违规事件不产生保管人区间，越界时间全部未归属
    assert body["custodians"] == []
    assert body["unattributed_high_seconds"] == 600.0
