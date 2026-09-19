"""批量封签占用检查的单元测试与 API 集成测试。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.batch import validate_batch
from app.main import app
from app.models import BatchValidateRequest, ChainEvents, Event, EventType

client = TestClient(app)

T = "2026-09-18T%02d:00:00Z"


def ev(i: int, type_: EventType, **kw) -> Event:
    return Event(type=type_, timestamp=T % i, **kw)


def chain(chain_id: str, events: list[Event]) -> ChainEvents:
    return ChainEvents(chain_id=chain_id, events=events)


def batch_request(*chains: ChainEvents) -> BatchValidateRequest:
    return BatchValidateRequest(chains=list(chains))


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------


def test_batch_all_valid_without_conflict():
    a = chain("chain-a", [
        ev(1, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, EventType.HANDOVER, custodian="Bob"),
    ])
    b = chain("chain-b", [
        ev(1, EventType.SEAL, seal_id="S002", custodian="Carol"),
    ])
    resp = validate_batch(batch_request(a, b))
    assert resp.valid is True
    assert resp.conflicts == []
    assert {s.chain_id: s.conflicts for s in resp.summary} == {"chain-a": 0, "chain-b": 0}
    # chains 保持输入顺序
    assert [c.chain_id for c in resp.chains] == ["chain-a", "chain-b"]
    # 单链结果中所有事件均未被标记冲突
    assert all(not e.cross_chain_conflict for c in resp.chains for e in c.events)


def test_batch_cross_chain_conflict_marks_both_events():
    a = chain("chain-a", [ev(1, EventType.SEAL, seal_id="S001", custodian="Alice")])
    b = chain("chain-b", [ev(1, EventType.SEAL, seal_id="S001", custodian="Carol")])
    resp = validate_batch(batch_request(a, b))

    assert resp.valid is False
    assert resp.chains[0].events[0].cross_chain_conflict is True
    assert resp.chains[1].events[0].cross_chain_conflict is True

    conflict = {c.chain_id: c for c in resp.conflicts}
    assert set(conflict) == {"chain-a", "chain-b"}
    for item in conflict.values():
        assert item.seal_id == "S001"
        assert item.event_index == 0
        assert item.first_chain_id == "chain-a"


def test_batch_conflict_does_not_change_chain_status():
    a = chain("chain-a", [ev(1, EventType.SEAL, seal_id="S001", custodian="Alice")])
    b = chain("chain-b", [ev(1, EventType.SEAL, seal_id="S001", custodian="Carol")])
    resp = validate_batch(batch_request(a, b))

    # 两条链自身校验均合法：冲突不改变各链状态
    for result in resp.chains:
        assert result.valid is True
        assert result.sealed is True
        assert result.violations == []
        assert all(e.status == "ok" for e in result.events)
    # 但批次 valid 必须为 false
    assert resp.valid is False
    assert {s.chain_id: s.valid for s in resp.summary} == {"chain-a": True, "chain-b": True}


def test_batch_first_chain_id_is_first_occupier_in_input_order():
    a = chain("first", [ev(1, EventType.SEAL, seal_id="S777", custodian="Alice")])
    b = chain("second", [ev(1, EventType.SEAL, seal_id="S777", custodian="Bob")])
    c = chain("third", [ev(1, EventType.SEAL, seal_id="S777", custodian="Carol")])
    resp = validate_batch(batch_request(a, b, c))

    assert len(resp.conflicts) == 3
    assert [item.chain_id for item in resp.conflicts] == ["first", "second", "third"]
    assert all(item.first_chain_id == "first" for item in resp.conflicts)
    # 三条链的对应事件全部被标记
    assert [c_result.events[0].cross_chain_conflict for c_result in resp.chains] == [
        True,
        True,
        True,
    ]


def test_batch_violation_event_does_not_occupy_seal():
    # chain-a：已封装状态下再次 seal S002 —— 违规，S002 不构成占用
    a = chain("chain-a", [
        ev(1, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, EventType.SEAL, seal_id="S002", custodian="Alice"),
    ])
    b = chain("chain-b", [ev(1, EventType.SEAL, seal_id="S002", custodian="Carol")])
    resp = validate_batch(batch_request(a, b))

    assert resp.conflicts == []
    assert not any(e.cross_chain_conflict for c_result in resp.chains for e in c_result.events)
    # a 自身有违规导致批次不通过，与跨链冲突无关
    assert resp.valid is False
    assert resp.chains[0].valid is False
    assert resp.chains[1].valid is True


def test_batch_invalid_single_chain_makes_batch_invalid_without_conflict():
    a = chain("chain-a", [ev(1, EventType.HANDOVER, custodian="Bob")])  # 未封装交接
    resp = validate_batch(batch_request(a))
    assert resp.valid is False
    assert resp.conflicts == []
    assert resp.summary[0].valid is False
    assert resp.summary[0].conflicts == 0


def test_batch_same_chain_reuse_is_intra_chain_violation_only():
    a = chain("chain-a", [
        ev(1, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, EventType.UNSEAL),
        ev(3, EventType.RESEAL, seal_id="S001", custodian="Alice"),  # 复用，违规
    ])
    resp = validate_batch(batch_request(a))
    assert resp.valid is False
    assert resp.conflicts == []
    assert [v.index for v in resp.chains[0].violations] == [2]
    assert not any(e.cross_chain_conflict for e in resp.chains[0].events)


def test_batch_conflicts_ordered_in_batch_input_order():
    # 输入顺序 A、B、C；S1 被 A/B/C 使用，S2 被 A/B 使用
    a = chain("chain-a", [
        ev(1, EventType.SEAL, seal_id="S1", custodian="Alice"),
        ev(2, EventType.UNSEAL),
        ev(3, EventType.RESEAL, seal_id="S2", custodian="Alice"),
    ])
    b = chain("chain-b", [
        ev(1, EventType.SEAL, seal_id="S1", custodian="Bob"),
        ev(2, EventType.UNSEAL),
        ev(3, EventType.RESEAL, seal_id="S2", custodian="Bob"),
    ])
    c = chain("chain-c", [ev(1, EventType.SEAL, seal_id="S1", custodian="Carol")])
    resp = validate_batch(batch_request(a, b, c))

    assert [(item.seal_id, item.chain_id, item.event_index, item.first_chain_id)
            for item in resp.conflicts] == [
        ("S1", "chain-a", 0, "chain-a"),
        ("S1", "chain-b", 0, "chain-a"),
        ("S2", "chain-a", 2, "chain-a"),
        ("S2", "chain-b", 2, "chain-a"),
        ("S1", "chain-c", 0, "chain-a"),
    ]


def test_batch_summary_sorted_by_chain_id_stable():
    c2 = chain("c-2", [ev(1, EventType.SEAL, seal_id="S2", custodian="Alice")])
    c10 = chain("c-10", [ev(1, EventType.SEAL, seal_id="S10", custodian="Bob")])
    c1 = chain("c-1", [
        ev(1, EventType.SEAL, seal_id="S2", custodian="Carol"),  # 与 c-2 冲突
        ev(2, EventType.SEAL, seal_id="S10", custodian="Carol"),  # 已封装，违规
    ])
    resp = validate_batch(batch_request(c2, c10, c1))

    # chains 保持输入顺序
    assert [c_result.chain_id for c_result in resp.chains] == ["c-2", "c-10", "c-1"]
    # 汇总按 chain_id 字典序稳定排序
    assert [s.chain_id for s in resp.summary] == ["c-1", "c-10", "c-2"]
    by_id = {s.chain_id: s for s in resp.summary}
    assert by_id["c-1"].valid is False  # c-1 自身有一次违规
    assert by_id["c-1"].conflicts == 1  # 仅 S2 的有效事件构成冲突
    assert by_id["c-2"].conflicts == 1
    assert by_id["c-10"].conflicts == 0


def test_batch_empty_request_is_valid():
    resp = validate_batch(batch_request())
    assert resp.valid is True
    assert resp.chains == []
    assert resp.conflicts == []
    assert resp.summary == []


# ---------------------------------------------------------------------------
# API 集成
# ---------------------------------------------------------------------------


def test_api_batch_validate_conflict_response():
    payload = {
        "chains": [
            {"chain_id": "chain-a", "events": [
                {"type": "seal", "timestamp": "2026-09-18T10:00:00Z",
                 "seal_id": "S001", "custodian": "Alice"},
            ]},
            {"chain_id": "chain-b", "events": [
                {"type": "seal", "timestamp": "2026-09-18T10:00:00Z",
                 "seal_id": "S001", "custodian": "Carol"},
                {"type": "handover", "timestamp": "2026-09-18T11:00:00Z",
                 "custodian": "Dave"},
            ]},
        ]
    }
    r = client.post("/api/v1/seal-chain/batch-validate", json=payload)
    assert r.status_code == 200
    body = r.json()

    assert body["valid"] is False
    assert [c["chain_id"] for c in body["chains"]] == ["chain-a", "chain-b"]
    # 每链原校验结果保留：两链自身均合法
    assert all(c["valid"] is True for c in body["chains"])
    assert body["chains"][0]["events"][0]["cross_chain_conflict"] is True
    assert body["chains"][1]["events"][0]["cross_chain_conflict"] is True
    assert body["chains"][1]["events"][1]["cross_chain_conflict"] is False

    assert body["conflicts"] == [
        {"seal_id": "S001", "chain_id": "chain-a", "event_index": 0,
         "first_chain_id": "chain-a"},
        {"seal_id": "S001", "chain_id": "chain-b", "event_index": 0,
         "first_chain_id": "chain-a"},
    ]
    assert body["summary"] == [
        {"chain_id": "chain-a", "valid": True, "conflicts": 1},
        {"chain_id": "chain-b", "valid": True, "conflicts": 1},
    ]


def test_api_batch_validate_empty_body():
    r = client.post("/api/v1/seal-chain/batch-validate", json={"chains": []})
    assert r.status_code == 200
    assert r.json() == {"valid": True, "chains": [], "conflicts": [], "summary": []}


def test_api_batch_duplicate_chain_id_rejected_with_422():
    payload = {"chains": [
        {"chain_id": "dup", "events": []},
        {"chain_id": "dup", "events": []},
    ]}
    r = client.post("/api/v1/seal-chain/batch-validate", json=payload)
    assert r.status_code == 422


def test_single_validate_endpoint_flags_default_false():
    payload = {"events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z",
         "seal_id": "S001", "custodian": "Alice"},
    ]}
    r = client.post("/api/v1/seal-chain/validate", json=payload)
    assert r.status_code == 200
    assert r.json()["events"][0]["cross_chain_conflict"] is False
