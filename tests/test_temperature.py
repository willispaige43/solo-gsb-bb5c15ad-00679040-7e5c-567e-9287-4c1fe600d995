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

DAY = "2026-09-19"


def dt(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hour:02d}:{minute:02d}:{second:02d}+00:00")


def ev(type_: EventType, hour: int, minute: int = 0, **kw) -> Event:
    return Event(type=type_, timestamp=dt(hour, minute), **kw)


def smp(hour: int, minute: int, temperature: float) -> TemperatureSample:
    return TemperatureSample(timestamp=dt(hour, minute), temperature=temperature)


def make_request(events=(), samples=(), min_temp=2.0, max_temp=8.0, max_gap=600.0):
    return TemperatureAuditRequest(
        events=list(events),
        samples=list(samples),
        min_temperature=min_temp,
        max_temperature=max_temp,
        max_gap_seconds=max_gap,
    )


def seg_tuples(segments):
    return [
        (
            s.start.strftime("%H:%M"),
            s.end.strftime("%H:%M"),
            s.seconds,
            s.kind,
            s.deviation,
            s.sample_index,
            s.custodian,
            s.custodian_event_index,
        )
        for s in segments
    ]


def alice_chain():
    """seal@08 Alice（合法链，单保管人）。"""
    return [ev(EventType.SEAL, 8, seal_id="S001", custodian="Alice")]


# ---------------------------------------------------------------------------
# 基本归属：高温 / 低温越界与汇总
# ---------------------------------------------------------------------------


def test_single_custodian_high_and_low():
    resp = audit_temperature(
        make_request(
            events=alice_chain(),
            samples=[smp(9, 0, 10.0), smp(9, 10, 5.0), smp(9, 20, 0.0), smp(9, 30, 5.0)],
        )
    )
    assert seg_tuples(resp.segments) == [
        ("09:00", "09:10", 600.0, "high", 2.0, 0, "Alice", 0),
        ("09:20", "09:30", 600.0, "low", 2.0, 2, "Alice", 0),
    ]
    assert len(resp.custodians) == 1
    summary = resp.custodians[0]
    assert summary.custodian == "Alice"
    assert summary.high_seconds == 600.0
    assert summary.low_seconds == 600.0
    assert summary.max_high_deviation == 2.0
    assert summary.max_low_deviation == 2.0
    assert resp.unattributed_seconds == 0.0
    # 高、低温偏差并列 2.0：全局最大偏差取时间最早者
    assert resp.max_deviation.kind == "high"
    assert resp.max_deviation.deviation == 2.0
    assert resp.max_deviation.sample_index == 0
    assert resp.max_deviation.timestamp == dt(9, 0)
    # 原校验结果保留
    assert resp.validation.valid is True
    assert resp.validation.final_custodian == "Alice"


def test_in_range_and_boundary_temperatures_produce_nothing():
    # 恰好等于阈值（8.0 / 2.0）视为在范围内
    resp = audit_temperature(
        make_request(
            events=alice_chain(),
            samples=[smp(9, 0, 8.0), smp(9, 10, 2.0), smp(9, 20, 5.0)],
        )
    )
    assert resp.segments == []
    assert resp.custodians == []
    assert resp.unattributed_seconds == 0.0
    assert resp.max_deviation is None


def test_empty_or_single_sample_produces_nothing():
    resp = audit_temperature(make_request(events=alice_chain(), samples=[]))
    assert resp.segments == []
    assert resp.custodians == []
    assert resp.max_deviation is None
    assert resp.validation.valid is True

    # 最后一个样本不形成区间（无后继样本）
    resp = audit_temperature(make_request(events=alice_chain(), samples=[smp(9, 0, 10.0)]))
    assert resp.segments == []
    assert resp.max_deviation is None


# ---------------------------------------------------------------------------
# 前值保持与数据空洞
# ---------------------------------------------------------------------------


def test_gap_equal_to_max_gap_is_counted_and_hole_is_not():
    resp = audit_temperature(
        make_request(
            events=alice_chain(),
            samples=[smp(9, 0, 10.0), smp(9, 10, 10.0), smp(9, 30, 10.0)],
            max_gap=600.0,
        )
    )
    # [09:00,09:10) 间隔恰好 600（不超过阈值）计入；
    # [09:10,09:30) 间隔 1200 超出阈值视为数据空洞，不计时
    assert seg_tuples(resp.segments) == [
        ("09:00", "09:10", 600.0, "high", 2.0, 0, "Alice", 0),
    ]
    assert resp.custodians[0].high_seconds == 600.0


def test_hole_does_not_bridge_breach_intervals():
    # 空洞两侧的越界区间保持独立，各自携带自己的起始样本下标
    resp = audit_temperature(
        make_request(
            events=alice_chain(),
            samples=[smp(9, 0, 10.0), smp(9, 5, 10.0), smp(9, 30, 10.0), smp(9, 40, 5.0)],
        )
    )
    assert seg_tuples(resp.segments) == [
        ("09:00", "09:05", 300.0, "high", 2.0, 0, "Alice", 0),
        ("09:30", "09:40", 600.0, "high", 2.0, 2, "Alice", 0),
    ]
    assert resp.custodians[0].high_seconds == 900.0


# ---------------------------------------------------------------------------
# 保管人区间求交与交接点拆分
# ---------------------------------------------------------------------------


def test_breach_split_at_handover_point():
    events = [
        ev(EventType.SEAL, 8, seal_id="S001", custodian="Alice"),
        ev(EventType.HANDOVER, 9, 5, custodian="Bob"),
    ]
    resp = audit_temperature(
        make_request(
            events=events,
            samples=[smp(9, 0, 10.5), smp(9, 10, 5.0), smp(9, 20, 1.0), smp(9, 30, 5.0)],
        )
    )
    # 越界区间 [09:00,09:10) 跨交接点 09:05，必须拆分
    assert seg_tuples(resp.segments) == [
        ("09:00", "09:05", 300.0, "high", 2.5, 0, "Alice", 0),
        ("09:05", "09:10", 300.0, "high", 2.5, 0, "Bob", 1),
        ("09:20", "09:30", 600.0, "low", 1.0, 2, "Bob", 1),
    ]
    assert [s.custodian for s in resp.custodians] == ["Alice", "Bob"]
    summaries = {s.custodian: s for s in resp.custodians}
    assert summaries["Alice"].high_seconds == 300.0
    assert summaries["Alice"].low_seconds == 0.0
    assert summaries["Alice"].max_high_deviation == 2.5
    assert summaries["Alice"].max_low_deviation is None
    assert summaries["Bob"].high_seconds == 300.0
    assert summaries["Bob"].low_seconds == 600.0
    assert summaries["Bob"].max_high_deviation == 2.5
    assert summaries["Bob"].max_low_deviation == 1.0
    assert resp.unattributed_seconds == 0.0
    assert resp.max_deviation.deviation == 2.5


def test_custodian_interval_is_half_open():
    events = [
        ev(EventType.SEAL, 8, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 9, 0, custodian="Bob"),
    ]
    resp = audit_temperature(
        make_request(
            events=events,
            samples=[smp(8, 50, 10.0), smp(9, 0, 10.0), smp(9, 10, 5.0)],
        )
    )
    # [08:50,09:00) 完全归 Alice；[09:00,09:10) 完全归 Bob（09:00 起属新保管人）
    assert seg_tuples(resp.segments) == [
        ("08:50", "09:00", 600.0, "high", 2.0, 0, "Alice", 0),
        ("09:00", "09:10", 600.0, "high", 2.0, 1, "Bob", 1),
    ]


def test_same_custodian_handover_still_splits_segment():
    events = [
        ev(EventType.SEAL, 8, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 9, 5, custodian="Alice"),
    ]
    resp = audit_temperature(
        make_request(events=events, samples=[smp(9, 0, 10.0), smp(9, 10, 5.0)])
    )
    # 每个有效 seal/reseal/handover 都开启新的保管人区间，跨交接点必须拆分
    assert seg_tuples(resp.segments) == [
        ("09:00", "09:05", 300.0, "high", 2.0, 0, "Alice", 0),
        ("09:05", "09:10", 300.0, "high", 2.0, 0, "Alice", 1),
    ]
    assert resp.custodians[0].high_seconds == 600.0


def test_unseal_does_not_change_custodian():
    events = [
        ev(EventType.SEAL, 8, seal_id="S1", custodian="Alice"),
        ev(EventType.UNSEAL, 9, 0),
        ev(EventType.RESEAL, 9, 30, seal_id="S2", custodian="Alice"),
    ]
    resp = audit_temperature(
        make_request(events=events, samples=[smp(9, 10, 10.0), smp(9, 20, 5.0)])
    )
    # unseal 不改变保管人：开封期间仍归 Alice（区间由事件 0 开启）
    assert seg_tuples(resp.segments) == [
        ("09:10", "09:20", 600.0, "high", 2.0, 0, "Alice", 0),
    ]


def test_last_custodian_interval_is_open_ended():
    events = [
        ev(EventType.SEAL, 8, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 9, 0, custodian="Bob"),
    ]
    resp = audit_temperature(
        make_request(events=events, samples=[smp(20, 0, 10.0), smp(20, 10, 5.0)])
    )
    # 样本远晚于最后一个事件：归最后一个保管人 Bob（区间向右开放）
    assert seg_tuples(resp.segments) == [
        ("20:00", "20:10", 600.0, "high", 2.0, 0, "Bob", 1),
    ]


# ---------------------------------------------------------------------------
# 仅 status=ok 的事件生成保管人区间
# ---------------------------------------------------------------------------


def test_violation_events_generate_no_custodian_interval():
    events = [
        ev(EventType.SEAL, 8, 0, seal_id="S1", custodian="Alice"),
        ev(EventType.UNSEAL, 8, 30),
        ev(EventType.HANDOVER, 8, 40, custodian="Bob"),  # 违规：未封装状态下不得交接
        ev(EventType.RESEAL, 8, 50, seal_id="S2", custodian="Carol"),
    ]
    resp = audit_temperature(
        make_request(
            events=events,
            samples=[smp(8, 35, 10.0), smp(8, 45, 5.0), smp(8, 55, 10.0), smp(9, 5, 5.0)],
        )
    )
    # 违规 handover 不产生区间：08:35–08:45 归 Alice（区间 [08:00,08:50)），
    # 08:55–09:05 归 Carol（区间 [08:50,∞)）
    assert seg_tuples(resp.segments) == [
        ("08:35", "08:45", 600.0, "high", 2.0, 0, "Alice", 0),
        ("08:55", "09:05", 600.0, "high", 2.0, 2, "Carol", 3),
    ]
    # 链存在状态机违规不影响审计返回，违规只体现在 validation 中
    assert resp.validation.valid is False
    assert [v.index for v in resp.validation.violations] == [2]


def test_custodian_without_breach_not_in_summary():
    events = [
        ev(EventType.SEAL, 8, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 9, 0, custodian="Bob"),
    ]
    resp = audit_temperature(
        make_request(events=events, samples=[smp(8, 0, 10.0), smp(8, 10, 5.0)])
    )
    # 仅包含有越界时长的保管人
    assert [s.custodian for s in resp.custodians] == ["Alice"]


# ---------------------------------------------------------------------------
# 未归属时长
# ---------------------------------------------------------------------------


def test_breach_before_first_custodian_event_is_unattributed():
    events = [ev(EventType.SEAL, 9, 30, seal_id="S001", custodian="Alice")]
    resp = audit_temperature(
        make_request(events=events, samples=[smp(9, 25, 10.0), smp(9, 35, 5.0)])
    )
    # 越界区间 [09:25,09:35) 跨保管人区间起点 09:30：前半未归属，后半归 Alice
    assert seg_tuples(resp.segments) == [
        ("09:25", "09:30", 300.0, "high", 2.0, 0, None, None),
        ("09:30", "09:35", 300.0, "high", 2.0, 0, "Alice", 0),
    ]
    assert resp.unattributed_seconds == 300.0
    assert resp.custodians[0].high_seconds == 300.0


def test_no_custodian_events_everything_unattributed():
    resp = audit_temperature(
        make_request(events=[], samples=[smp(9, 0, 10.0), smp(9, 10, 5.0)])
    )
    assert seg_tuples(resp.segments) == [
        ("09:00", "09:10", 600.0, "high", 2.0, 0, None, None),
    ]
    assert resp.custodians == []
    assert resp.unattributed_seconds == 600.0
    # 未归属段也参与全局最大偏差
    assert resp.max_deviation.sample_index == 0
    assert resp.max_deviation.deviation == 2.0


# ---------------------------------------------------------------------------
# 全局最大偏差
# ---------------------------------------------------------------------------


def test_max_deviation_largest_wins():
    resp = audit_temperature(
        make_request(
            events=alice_chain(),
            samples=[smp(9, 0, 10.0), smp(9, 10, 0.0), smp(9, 20, 12.0), smp(9, 30, 5.0)],
        )
    )
    # 高温 2.0（样本 0）、低温 2.0（样本 1）、高温 4.0（样本 2）
    assert resp.max_deviation.kind == "high"
    assert resp.max_deviation.deviation == 4.0
    assert resp.max_deviation.sample_index == 2
    assert resp.max_deviation.timestamp == dt(9, 20)


def test_max_deviation_tie_prefers_earliest():
    resp = audit_temperature(
        make_request(
            events=alice_chain(),
            samples=[smp(9, 0, 0.0), smp(9, 10, 10.0), smp(9, 20, 5.0)],
        )
    )
    # 低温 2.0（样本 0）与高温 2.0（样本 1）并列：取时间最早者
    assert resp.max_deviation.kind == "low"
    assert resp.max_deviation.deviation == 2.0
    assert resp.max_deviation.sample_index == 0


# ---------------------------------------------------------------------------
# 422 语义错误
# ---------------------------------------------------------------------------


def test_samples_not_strictly_increasing_raise_422():
    with pytest.raises(TemperatureAuditError):
        audit_temperature(make_request(samples=[smp(9, 0, 5.0), smp(9, 0, 6.0)]))
    with pytest.raises(TemperatureAuditError):
        audit_temperature(make_request(samples=[smp(9, 10, 5.0), smp(9, 0, 6.0)]))


def test_mixed_timezone_raises_422():
    events = [ev(EventType.SEAL, 8, seal_id="S1", custodian="Alice")]  # 带时区
    samples = [
        TemperatureSample(
            timestamp=datetime.fromisoformat(f"{DAY}T09:00:00"), temperature=10.0
        ),
        TemperatureSample(
            timestamp=datetime.fromisoformat(f"{DAY}T09:10:00"), temperature=5.0
        ),
    ]
    with pytest.raises(TemperatureAuditError):
        audit_temperature(make_request(events=events, samples=samples))


def test_invalid_thresholds_raise_422():
    with pytest.raises(ValidationError):
        make_request(min_temp=9.0, max_temp=8.0)  # min > max
    with pytest.raises(ValidationError):
        make_request(max_gap=0.0)  # 必须为正
    with pytest.raises(ValidationError):
        make_request(max_gap=-1.0)


def test_nan_and_inf_rejected():
    with pytest.raises(ValidationError):
        TemperatureSample(timestamp=dt(9), temperature=float("nan"))
    with pytest.raises(ValidationError):
        TemperatureSample(timestamp=dt(9), temperature=float("inf"))
    with pytest.raises(ValidationError):
        make_request(min_temp=float("nan"))
    with pytest.raises(ValidationError):
        make_request(max_gap=float("inf"))


# ---------------------------------------------------------------------------
# API 集成
# ---------------------------------------------------------------------------


def test_api_temperature_audit_full_response():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T08:00:00Z",
             "seal_id": "S001", "custodian": "Alice"},
            {"type": "handover", "timestamp": f"{DAY}T09:05:00Z",
             "custodian": "Bob"},
        ],
        "samples": [
            {"timestamp": f"{DAY}T09:00:00Z", "temperature": 10.5},
            {"timestamp": f"{DAY}T09:10:00Z", "temperature": 5.0},
            {"timestamp": f"{DAY}T09:20:00Z", "temperature": 1.0},
            {"timestamp": f"{DAY}T09:30:00Z", "temperature": 5.0},
        ],
        "min_temperature": 2.0,
        "max_temperature": 8.0,
        "max_gap_seconds": 600,
    }
    r = client.post("/api/v1/seal-chain/temperature-audit", json=payload)
    assert r.status_code == 200
    body = r.json()

    # validation 保留原单链校验结构
    assert body["validation"]["valid"] is True
    assert body["validation"]["final_custodian"] == "Bob"
    assert [e["status"] for e in body["validation"]["events"]] == ["ok", "ok"]

    assert body["segments"] == [
        {"start": f"{DAY}T09:00:00Z", "end": f"{DAY}T09:05:00Z", "seconds": 300.0,
         "kind": "high", "temperature": 10.5, "deviation": 2.5, "sample_index": 0,
         "custodian": "Alice", "custodian_event_index": 0},
        {"start": f"{DAY}T09:05:00Z", "end": f"{DAY}T09:10:00Z", "seconds": 300.0,
         "kind": "high", "temperature": 10.5, "deviation": 2.5, "sample_index": 0,
         "custodian": "Bob", "custodian_event_index": 1},
        {"start": f"{DAY}T09:20:00Z", "end": f"{DAY}T09:30:00Z", "seconds": 600.0,
         "kind": "low", "temperature": 1.0, "deviation": 1.0, "sample_index": 2,
         "custodian": "Bob", "custodian_event_index": 1},
    ]
    assert body["custodians"] == [
        {"custodian": "Alice", "high_seconds": 300.0, "low_seconds": 0.0,
         "max_high_deviation": 2.5, "max_low_deviation": None},
        {"custodian": "Bob", "high_seconds": 300.0, "low_seconds": 600.0,
         "max_high_deviation": 2.5, "max_low_deviation": 1.0},
    ]
    assert body["unattributed_seconds"] == 0.0
    assert body["max_deviation"] == {
        "kind": "high", "deviation": 2.5, "sample_index": 0,
        "timestamp": f"{DAY}T09:00:00Z",
    }


def test_api_non_increasing_samples_returns_422():
    payload = {
        "events": [],
        "samples": [
            {"timestamp": f"{DAY}T09:10:00Z", "temperature": 5.0},
            {"timestamp": f"{DAY}T09:00:00Z", "temperature": 6.0},
        ],
        "min_temperature": 2.0,
        "max_temperature": 8.0,
        "max_gap_seconds": 600,
    }
    r = client.post("/api/v1/seal-chain/temperature-audit", json=payload)
    assert r.status_code == 422
    assert "严格递增" in r.json()["detail"]


def test_api_invalid_thresholds_return_422():
    base = {"events": [], "samples": []}
    r = client.post(
        "/api/v1/seal-chain/temperature-audit",
        json={**base, "min_temperature": 9.0, "max_temperature": 8.0,
              "max_gap_seconds": 600},
    )
    assert r.status_code == 422
    r = client.post(
        "/api/v1/seal-chain/temperature-audit",
        json={**base, "min_temperature": 2.0, "max_temperature": 8.0,
              "max_gap_seconds": 0},
    )
    assert r.status_code == 422


def test_api_mixed_timezone_returns_422():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T08:00:00Z",
             "seal_id": "S1", "custodian": "Alice"}
        ],
        "samples": [
            {"timestamp": f"{DAY}T09:00:00", "temperature": 10.0},  # naive，与事件混用
            {"timestamp": f"{DAY}T09:10:00", "temperature": 5.0},
        ],
        "min_temperature": 2.0,
        "max_temperature": 8.0,
        "max_gap_seconds": 600,
    }
    r = client.post("/api/v1/seal-chain/temperature-audit", json=payload)
    assert r.status_code == 422
    assert "时区" in r.json()["detail"]


def test_api_missing_required_fields_returns_422():
    r = client.post("/api/v1/seal-chain/temperature-audit", json={"events": []})
    assert r.status_code == 422
