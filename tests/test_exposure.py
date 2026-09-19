"""开封暴露审计接口的单元测试与 API 集成测试。"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.exposure import ExposureAuditError, audit_exposure
from app.main import app
from app.models import AllowedWindow, Event, EventType, ExposureAuditRequest

client = TestClient(app)

DAY = "2026-09-18"


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hour:02d}:{minute:02d}:00+00:00")


def ev(type_: EventType, hour: int, minute: int = 0, **kw) -> Event:
    return Event(type=type_, timestamp=dt(hour, minute), **kw)


def win(start_h: int, start_m: int, end_h: int, end_m: int) -> AllowedWindow:
    return AllowedWindow(start=dt(start_h, start_m), end=dt(end_h, end_m))


def make_request(events, audit=(20, 0), windows=()) -> ExposureAuditRequest:
    return ExposureAuditRequest(
        events=events,
        audit_until=dt(*audit),
        allowed_windows=list(windows),
    )


def seg_tuples(segments):
    return [
        (
            s.start.strftime("%H:%M"),
            s.end.strftime("%H:%M"),
            s.seconds,
            s.unseal_index,
            s.reseal_index,
        )
        for s in segments
    ]


def sealed_chain():
    """seal@10, unseal@11, reseal@13（合法链）。"""
    return [
        ev(EventType.SEAL, 10, seal_id="S001", custodian="Alice"),
        ev(EventType.UNSEAL, 11),
        ev(EventType.RESEAL, 13, seal_id="S002", custodian="Alice"),
    ]


# ---------------------------------------------------------------------------
# 核心逻辑：区间差
# ---------------------------------------------------------------------------


def test_interval_fully_covered_by_single_window():
    resp = audit_exposure(make_request(sealed_chain(), windows=[win(11, 0, 13, 0)]))
    assert seg_tuples(resp.covered) == [("11:00", "13:00", 7200.0, 1, 2)]
    assert resp.uncovered == []
    assert resp.unauthorized_seconds == 0.0
    # 原校验结果保留
    assert resp.validation.valid is True
    assert resp.validation.sealed is True
    assert [e.status for e in resp.validation.events] == ["ok", "ok", "ok"]


def test_interval_partially_covered_uncovered_on_both_sides():
    resp = audit_exposure(make_request(sealed_chain(), windows=[win(12, 0, 12, 30)]))
    assert seg_tuples(resp.covered) == [("12:00", "12:30", 1800.0, 1, 2)]
    assert seg_tuples(resp.uncovered) == [
        ("11:00", "12:00", 3600.0, 1, 2),
        ("12:30", "13:00", 1800.0, 1, 2),
    ]
    assert resp.unauthorized_seconds == 5400.0


def test_one_open_interval_spanning_multiple_windows():
    events = [
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.UNSEAL, 10, 30),
        ev(EventType.RESEAL, 20, seal_id="S2", custodian="Alice"),
    ]
    resp = audit_exposure(
        make_request(events, windows=[win(11, 0, 12, 0), win(14, 0, 16, 0)])
    )
    # 区间 [10:30,20:00) 被两个窗口切成 5 段
    assert seg_tuples(resp.covered) == [
        ("11:00", "12:00", 3600.0, 1, 2),
        ("14:00", "16:00", 7200.0, 1, 2),
    ]
    assert seg_tuples(resp.uncovered) == [
        ("10:30", "11:00", 1800.0, 1, 2),
        ("12:00", "14:00", 7200.0, 1, 2),
        ("16:00", "20:00", 14400.0, 1, 2),
    ]
    assert resp.unauthorized_seconds == 23400.0


def test_window_clipped_to_interval_bounds():
    # 授权窗口完全包住开封区间：覆盖段以开封区间端点裁剪
    resp = audit_exposure(make_request(sealed_chain(), windows=[win(0, 0, 23, 0)]))
    assert seg_tuples(resp.covered) == [("11:00", "13:00", 7200.0, 1, 2)]
    assert resp.uncovered == []


def test_windows_outside_interval_leave_it_uncovered():
    resp = audit_exposure(make_request(sealed_chain(), windows=[win(13, 0, 14, 0)]))
    # 半开区间：窗口起点 13:00 == 区间终点，不覆盖任何时长
    assert resp.covered == []
    assert seg_tuples(resp.uncovered) == [("11:00", "13:00", 7200.0, 1, 2)]
    assert resp.unauthorized_seconds == 7200.0


# ---------------------------------------------------------------------------
# 窗口合并：相交 / 首尾相接 / 乱序 / 嵌套
# ---------------------------------------------------------------------------


def test_intersecting_and_abutting_windows_are_merged():
    events = [
        ev(EventType.SEAL, 9, seal_id="S1", custodian="Alice"),
        ev(EventType.UNSEAL, 10),
        ev(EventType.RESEAL, 20, seal_id="S2", custodian="Alice"),
    ]
    # [11,13) 与 [13,15) 首尾相接，[15,16) 相交 —— 合并为 [11,16)
    resp = audit_exposure(
        make_request(events, windows=[win(15, 0, 16, 0), win(11, 0, 13, 0),
                                      win(13, 0, 15, 0)])
    )
    assert seg_tuples(resp.covered) == [("11:00", "16:00", 18000.0, 1, 2)]
    assert seg_tuples(resp.uncovered) == [
        ("10:00", "11:00", 3600.0, 1, 2),
        ("16:00", "20:00", 14400.0, 1, 2),
    ]
    assert resp.unauthorized_seconds == 18000.0


def test_shuffled_and_nested_windows_give_stable_result():
    events = [
        ev(EventType.SEAL, 9, seal_id="S1", custodian="Alice"),
        ev(EventType.UNSEAL, 10),
        ev(EventType.RESEAL, 20, seal_id="S2", custodian="Alice"),
    ]
    # 嵌套窗口 [11,12) 被 [10:30,12:30) 包含；另给乱序的 [14,16)
    ordered = [win(10, 30, 12, 30), win(14, 0, 16, 0), win(11, 0, 12, 0)]
    shuffled = [win(14, 0, 16, 0), win(11, 0, 12, 0), win(10, 30, 12, 30)]

    a = audit_exposure(make_request(events, windows=ordered))
    b = audit_exposure(make_request(events, windows=shuffled))

    expected_covered = [
        ("10:30", "12:30", 7200.0, 1, 2),
        ("14:00", "16:00", 7200.0, 1, 2),
    ]
    expected_uncovered = [
        ("10:00", "10:30", 1800.0, 1, 2),
        ("12:30", "14:00", 5400.0, 1, 2),
        ("16:00", "20:00", 14400.0, 1, 2),
    ]
    assert seg_tuples(a.covered) == seg_tuples(b.covered) == expected_covered
    assert seg_tuples(a.uncovered) == seg_tuples(b.uncovered) == expected_uncovered
    assert a.unauthorized_seconds == b.unauthorized_seconds == 21600.0


# ---------------------------------------------------------------------------
# 末次开封未重封：延伸至 audit_until
# ---------------------------------------------------------------------------


def test_trailing_unseal_extends_to_audit_until():
    events = [
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.UNSEAL, 15),
    ]
    resp = audit_exposure(
        make_request(events, audit=(20, 0), windows=[win(16, 0, 18, 0)])
    )
    assert seg_tuples(resp.covered) == [("16:00", "18:00", 7200.0, 1, None)]
    assert seg_tuples(resp.uncovered) == [
        ("15:00", "16:00", 3600.0, 1, None),
        ("18:00", "20:00", 7200.0, 1, None),
    ]
    assert resp.unauthorized_seconds == 10800.0


def test_audit_until_equal_to_trailing_unseal_yields_zero_length_interval():
    events = [
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.UNSEAL, 15),
    ]
    resp = audit_exposure(make_request(events, audit=(15, 0), windows=[win(15, 0, 16, 0)]))
    # [15,15) 是退化的零长度区间：不产生任何分段、不计时长
    assert resp.covered == []
    assert resp.uncovered == []
    assert resp.unauthorized_seconds == 0.0


def test_multiple_open_intervals_with_indices():
    events = [
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.UNSEAL, 11),
        ev(EventType.RESEAL, 12, seal_id="S2", custodian="Alice"),
        ev(EventType.UNSEAL, 13),
        ev(EventType.RESEAL, 14, seal_id="S3", custodian="Alice"),
    ]
    resp = audit_exposure(make_request(events))  # 无任何授权窗口
    assert seg_tuples(resp.uncovered) == [
        ("11:00", "12:00", 3600.0, 1, 2),
        ("13:00", "14:00", 3600.0, 3, 4),
    ]
    assert resp.covered == []
    assert resp.unauthorized_seconds == 7200.0


# ---------------------------------------------------------------------------
# 仅采用 status=ok 的 unseal/reseal
# ---------------------------------------------------------------------------


def test_violation_events_are_ignored_in_interval_derivation():
    events = [
        ev(EventType.UNSEAL, 9),                                    # 违规：未封装开封
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.UNSEAL, 11),                                  # 开启区间
        ev(EventType.RESEAL, 12, seal_id="S1", custodian="Alice"),  # 违规：复用封签
        ev(EventType.RESEAL, 13, seal_id="S2", custodian="Alice"),  # 合法：闭合区间
    ]
    resp = audit_exposure(make_request(events, windows=[win(11, 0, 13, 0)]))
    # 唯一区间 [11,13)，重封下标是合法 reseal 的下标 4，不是违规的下标 3
    assert seg_tuples(resp.covered) == [("11:00", "13:00", 7200.0, 2, 4)]
    assert resp.uncovered == []
    assert resp.validation.valid is False
    assert [v.index for v in resp.validation.violations] == [0, 3]


def test_seal_and_handover_never_open_intervals():
    events = [
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 11, custodian="Bob"),
        ev(EventType.HANDOVER, 12, custodian="Carol"),
    ]
    resp = audit_exposure(make_request(events))
    assert resp.covered == []
    assert resp.uncovered == []
    assert resp.unauthorized_seconds == 0.0


def test_empty_events_returns_empty_segments():
    resp = audit_exposure(make_request([], audit=(20, 0)))
    assert resp.covered == []
    assert resp.uncovered == []
    assert resp.unauthorized_seconds == 0.0
    assert resp.validation.valid is True
    assert resp.audit_until == dt(20)


# ---------------------------------------------------------------------------
# 422 语义错误
# ---------------------------------------------------------------------------


def test_audit_until_before_last_valid_event_raises_422():
    events = [
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 15, custodian="Bob"),  # 最后一个有效事件 @15
    ]
    with pytest.raises(ExposureAuditError):
        audit_exposure(make_request(events, audit=(14, 0)))


def test_audit_until_equal_to_last_valid_event_is_allowed():
    events = [
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 15, custodian="Bob"),
    ]
    resp = audit_exposure(make_request(events, audit=(15, 0)))
    assert resp.validation.valid is True


def test_trailing_violation_does_not_push_last_valid_time():
    # 违规事件不推进有效时间基准：audit_until 只需不早于最后一个有效事件
    events = [
        ev(EventType.SEAL, 10, seal_id="S1", custodian="Alice"),
        ev(EventType.HANDOVER, 11, custodian="Bob"),
        ev(EventType.SEAL, 12, seal_id="S2", custodian="Bob"),  # 已封装，违规
    ]
    resp = audit_exposure(make_request(events, audit=(11, 30)))
    assert resp.validation.valid is False


def test_window_start_not_before_end_raises_422():
    with pytest.raises(ValidationError):
        make_request(sealed_chain(), windows=[win(12, 0, 12, 0)])  # start == end
    with pytest.raises(ValidationError):
        make_request(sealed_chain(), windows=[win(13, 0, 12, 0)])  # start > end


def test_mixed_timezone_awareness_raises_422():
    events = [
        Event(type=EventType.SEAL,
              timestamp=datetime.fromisoformat(f"{DAY}T10:00:00+00:00"),
              seal_id="S1", custodian="Alice"),
        Event(type=EventType.UNSEAL,
              timestamp=datetime.fromisoformat(f"{DAY}T11:00:00+00:00")),
    ]
    request = ExposureAuditRequest(
        events=events,
        audit_until=datetime.fromisoformat(f"{DAY}T20:00:00"),  # naive，与事件混用
        allowed_windows=[],
    )
    with pytest.raises(ExposureAuditError):
        audit_exposure(request)


def test_mixed_timezone_window_raises_422():
    # 模拟 API 层 JSON 直接构模：窗口自身起止同为 naive 可通过模型校验，
    # 但与带时区的 audit_until 混用时在审计阶段报 422
    request = ExposureAuditRequest.model_construct(
        events=[],
        audit_until=datetime.fromisoformat(f"{DAY}T20:00:00+00:00"),
        allowed_windows=[
            AllowedWindow.model_construct(
                start=datetime.fromisoformat(f"{DAY}T11:00:00"),
                end=datetime.fromisoformat(f"{DAY}T12:00:00"),
            )
        ],
    )
    with pytest.raises(ExposureAuditError):
        audit_exposure(request)


# ---------------------------------------------------------------------------
# API 集成
# ---------------------------------------------------------------------------


def test_api_exposure_audit_full_response():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T10:00:00Z",
             "seal_id": "S001", "custodian": "Alice"},
            {"type": "unseal", "timestamp": f"{DAY}T11:00:00Z"},
            {"type": "reseal", "timestamp": f"{DAY}T13:00:00Z",
             "seal_id": "S002", "custodian": "Alice"},
        ],
        "audit_until": f"{DAY}T20:00:00Z",
        "allowed_windows": [
            {"start": f"{DAY}T12:00:00Z", "end": f"{DAY}T12:30:00Z"},
        ],
    }
    r = client.post("/api/v1/seal-chain/exposure-audit", json=payload)
    assert r.status_code == 200
    body = r.json()

    assert body["audit_until"] == f"{DAY}T20:00:00Z"
    # validation 保留原单链校验结构
    assert body["validation"]["valid"] is True
    assert body["validation"]["sealed"] is True
    assert body["validation"]["current_seal"] == "S002"
    assert [e["status"] for e in body["validation"]["events"]] == ["ok", "ok", "ok"]

    assert body["covered"] == [
        {"start": f"{DAY}T12:00:00Z", "end": f"{DAY}T12:30:00Z",
         "seconds": 1800.0, "unseal_index": 1, "reseal_index": 2}
    ]
    assert body["uncovered"] == [
        {"start": f"{DAY}T11:00:00Z", "end": f"{DAY}T12:00:00Z",
         "seconds": 3600.0, "unseal_index": 1, "reseal_index": 2},
        {"start": f"{DAY}T12:30:00Z", "end": f"{DAY}T13:00:00Z",
         "seconds": 1800.0, "unseal_index": 1, "reseal_index": 2},
    ]
    assert body["unauthorized_seconds"] == 5400.0


def test_api_trailing_unseal_extends_with_null_reseal_index():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T10:00:00Z",
             "seal_id": "S001", "custodian": "Alice"},
            {"type": "unseal", "timestamp": f"{DAY}T15:00:00Z"},
        ],
        "audit_until": f"{DAY}T20:00:00Z",
        "allowed_windows": [],
    }
    r = client.post("/api/v1/seal-chain/exposure-audit", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["uncovered"] == [
        {"start": f"{DAY}T15:00:00Z", "end": f"{DAY}T20:00:00Z",
         "seconds": 18000.0, "unseal_index": 1, "reseal_index": None}
    ]
    assert body["unauthorized_seconds"] == 18000.0


def test_api_audit_until_before_last_valid_event_returns_422():
    payload = {
        "events": [
            {"type": "seal", "timestamp": f"{DAY}T10:00:00Z",
             "seal_id": "S001", "custodian": "Alice"},
            {"type": "handover", "timestamp": f"{DAY}T15:00:00Z",
             "custodian": "Bob"},
        ],
        "audit_until": f"{DAY}T14:00:00Z",
        "allowed_windows": [],
    }
    r = client.post("/api/v1/seal-chain/exposure-audit", json=payload)
    assert r.status_code == 422
    assert "audit_until" in r.json()["detail"]


def test_api_window_start_after_end_returns_422():
    payload = {
        "events": [],
        "audit_until": f"{DAY}T20:00:00Z",
        "allowed_windows": [
            {"start": f"{DAY}T13:00:00Z", "end": f"{DAY}T12:00:00Z"},
        ],
    }
    assert client.post("/api/v1/seal-chain/exposure-audit", json=payload).status_code == 422


def test_api_missing_audit_until_returns_422():
    r = client.post("/api/v1/seal-chain/exposure-audit", json={"events": []})
    assert r.status_code == 422
