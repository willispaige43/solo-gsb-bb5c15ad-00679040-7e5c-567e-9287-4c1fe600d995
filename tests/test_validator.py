"""状态机校验器单元测试与 API 集成测试。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.models import Event, EventType
from app.validator import validate_events

client = TestClient(app)

T = "2026-09-18T%02d:00:00Z"


def ev(i: int, type_: EventType, **kw) -> Event:
    return Event(type=type_, timestamp=T % i, **kw)


def test_happy_path_full_chain():
    events = [
        ev(1, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, EventType.HANDOVER, custodian="Bob"),
        ev(3, EventType.UNSEAL),
        ev(4, EventType.RESEAL, seal_id="S002", custodian="Bob"),
        ev(5, EventType.HANDOVER, custodian="Carol"),
    ]
    resp = validate_events(events)
    assert resp.valid is True
    assert resp.sealed is True
    assert resp.final_custodian == "Carol"
    assert resp.current_seal == "S002"
    assert [r.status for r in resp.events] == ["ok"] * 5
    assert resp.violations == []


def test_handover_and_unseal_before_seal_are_violations():
    events = [
        ev(1, EventType.HANDOVER, custodian="Bob"),
        ev(2, EventType.UNSEAL),
        ev(3, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(4, EventType.HANDOVER, custodian="Bob"),
    ]
    resp = validate_events(events)
    assert resp.valid is False
    assert [v.index for v in resp.violations] == [0, 1]
    assert "未封装" in resp.violations[0].reason
    assert "未封装" in resp.violations[1].reason
    # 违规事件不阻止后续判定：第 3、4 个事件正常通过
    assert [r.status for r in resp.events] == ["violation", "violation", "ok", "ok"]
    assert resp.final_custodian == "Bob"
    assert resp.current_seal == "S001"


def test_seal_reuse_rejected_and_state_unchanged():
    events = [
        ev(1, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, EventType.UNSEAL),
        ev(3, EventType.RESEAL, seal_id="S001", custodian="Alice"),  # 复用旧封签
        ev(4, EventType.RESEAL, seal_id="S002", custodian="Alice"),  # 新封签可通过
    ]
    resp = validate_events(events)
    assert resp.valid is False
    assert [v.index for v in resp.violations] == [2]
    assert "不得复用" in resp.violations[0].reason
    # 违规事件不改变状态：第 2 步后处于未封装，第 4 步重封成功
    assert resp.sealed is True
    assert resp.current_seal == "S002"


def test_double_seal_rejected():
    events = [
        ev(1, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, EventType.SEAL, seal_id="S002", custodian="Alice"),
    ]
    resp = validate_events(events)
    assert [v.index for v in resp.violations] == [1]
    assert "已处于封装状态" in resp.violations[0].reason
    assert resp.current_seal == "S001"  # 状态未被违规事件改变


def test_reseal_without_prior_seal_rejected():
    events = [ev(1, EventType.RESEAL, seal_id="S001", custodian="Alice")]
    resp = validate_events(events)
    assert resp.valid is False
    assert "未曾封装" in resp.violations[0].reason


def test_timestamps_must_be_strictly_increasing():
    events = [
        ev(2, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, EventType.HANDOVER, custodian="Bob"),   # 相等，违规
        ev(1, EventType.HANDOVER, custodian="Carol"),  # 倒退，违规
        ev(3, EventType.HANDOVER, custodian="Dave"),   # 相对最后有效时间递增，通过
    ]
    resp = validate_events(events)
    assert [v.index for v in resp.violations] == [1, 2]
    assert all("严格递增" in v.reason for v in resp.violations)
    # 违规事件不推进时间基准：index 3 与 index 0 的有效时间比较
    assert resp.events[3].status == "ok"
    assert resp.final_custodian == "Dave"


def test_missing_required_fields_reported_as_violations():
    events = [
        ev(1, EventType.SEAL, custodian="Alice"),               # 缺 seal_id
        ev(2, EventType.SEAL, seal_id="S001"),                  # 缺 custodian
        ev(3, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(4, EventType.HANDOVER),                              # 缺 custodian
    ]
    resp = validate_events(events)
    assert [v.index for v in resp.violations] == [0, 1, 3]
    assert resp.events[2].status == "ok"


def test_api_endpoint_returns_full_response():
    payload = {
        "events": [
            {"type": "seal", "timestamp": "2026-09-18T10:00:00Z",
             "seal_id": "S001", "custodian": "Alice"},
            {"type": "unseal", "timestamp": "2026-09-18T11:00:00Z"},
            {"type": "handover", "timestamp": "2026-09-18T12:00:00Z",
             "custodian": "Bob"},
            {"type": "reseal", "timestamp": "2026-09-18T13:00:00Z",
             "seal_id": "S002", "custodian": "Alice"},
        ]
    }
    r = client.post("/api/v1/seal-chain/validate", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["final_custodian"] == "Alice"
    assert body["current_seal"] == "S002"
    assert body["sealed"] is True
    assert body["violations"] == [
        {"index": 2, "reason": "未封装状态下不得交接"}
    ]
    assert [e["status"] for e in body["events"]] == ["ok", "ok", "violation", "ok"]


def test_api_empty_chain_and_health():
    assert client.get("/health").json() == {"status": "ok"}
    r = client.post("/api/v1/seal-chain/validate", json={"events": []})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["final_custodian"] is None
    assert body["current_seal"] is None


def test_api_unknown_event_type_rejected_with_422():
    payload = {"events": [{"type": "teleport", "timestamp": "2026-09-18T10:00:00Z"}]}
    assert client.post("/api/v1/seal-chain/validate", json=payload).status_code == 422
