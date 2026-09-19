"""离线片段合并重建接口的单元测试与 API 集成测试。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.models import EventType, Fragment, ReconcileEvent, ReconcileRequest
from app.reconcile import reconcile

client = TestClient(app)

T = "2026-09-18T%02d:00:00Z"


def ev(i: int, event_id: str, type_: EventType, prev: str | None = None, **kw) -> ReconcileEvent:
    return ReconcileEvent(
        event_id=event_id, prev_event_id=prev, type=type_, timestamp=T % i, **kw
    )


def frag(*events: ReconcileEvent) -> Fragment:
    return Fragment(events=list(events))


def req(*fragments: Fragment, chain_id: str = "chain-a") -> ReconcileRequest:
    return ReconcileRequest(chain_id=chain_id, fragments=list(fragments))


# ---------------------------------------------------------------------------
# 去重与冲突
# ---------------------------------------------------------------------------


def test_reconcile_happy_path_merges_overlapping_fragments():
    # 两个离线片段存在完全相同的重叠事件 e2
    f0 = frag(
        ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, "e2", EventType.HANDOVER, prev="e1", custodian="Bob"),
    )
    f1 = frag(
        ev(2, "e2", EventType.HANDOVER, prev="e1", custodian="Bob"),  # 与 f0 完全一致
        ev(3, "e3", EventType.UNSEAL, prev="e2"),
        ev(4, "e4", EventType.RESEAL, prev="e3", seal_id="S002", custodian="Bob"),
    )
    resp = reconcile(req(f0, f1))

    assert resp.chain_id == "chain-a"
    assert resp.reconciled is True
    assert resp.conflicts == []
    assert resp.structure.heads == ["e1"]
    assert resp.structure.missing_predecessors == []
    assert resp.structure.forks == []
    assert resp.structure.cycles == []
    assert resp.sequence == ["e1", "e2", "e3", "e4"]

    # 重建成功后调用了现有单链校验器
    assert resp.validation is not None
    assert resp.validation.valid is True
    assert resp.validation.sealed is True
    assert resp.validation.final_custodian == "Bob"
    assert resp.validation.current_seal == "S002"
    assert [r.status for r in resp.validation.events] == ["ok"] * 4

    # 每个事件的来源片段编号列表
    assert {s.event_id: s.fragments for s in resp.event_sources} == {
        "e1": [0],
        "e2": [0, 1],
        "e3": [1],
        "e4": [1],
    }


def test_reconcile_duplicate_within_single_fragment_counted_once():
    f0 = frag(
        ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"),  # 同片段内重复
    )
    resp = reconcile(req(f0))
    assert resp.reconciled is True
    assert resp.sequence == ["e1"]
    assert resp.event_sources[0].fragments == [0]  # 不出现 [0, 0]


def test_reconcile_conflict_on_business_fields_returns_details():
    f0 = frag(ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"))
    f1 = frag(ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Carol"))
    resp = reconcile(req(f0, f1))

    assert resp.reconciled is False
    assert resp.sequence is None
    assert resp.validation is None  # 存在冲突时不调用校验器

    assert len(resp.conflicts) == 1
    conflict = resp.conflicts[0]
    assert conflict.event_id == "e1"
    assert conflict.differing_fields == ["custodian"]
    assert [v.fragments for v in conflict.variants] == [[0], [1]]
    assert [v.event.custodian for v in conflict.variants] == ["Alice", "Carol"]
    # 冲突事件的来源片段仍然完整返回
    assert {s.event_id: s.fragments for s in resp.event_sources} == {"e1": [0, 1]}


def test_reconcile_conflict_on_prev_event_id_is_reported():
    f0 = frag(ev(2, "e2", EventType.HANDOVER, prev="e1", custodian="Bob"))
    f1 = frag(ev(2, "e2", EventType.HANDOVER, prev="e9", custodian="Bob"))
    resp = reconcile(req(f0, f1))

    assert resp.reconciled is False
    assert resp.conflicts[0].differing_fields == ["prev_event_id"]


def test_reconcile_identical_instant_different_notation_is_not_a_conflict():
    # 同一时刻的不同 ISO 写法解析后相等，应视为同一内容
    f0 = frag(ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"))
    f1 = frag(
        ReconcileEvent(
            event_id="e1",
            prev_event_id=None,
            type=EventType.SEAL,
            timestamp="2026-09-18T01:00:00+00:00",
            seal_id="S001",
            custodian="Alice",
        )
    )
    resp = reconcile(req(f0, f1))
    assert resp.conflicts == []
    assert resp.reconciled is True


# ---------------------------------------------------------------------------
# 结构诊断：多头 / 缺失前驱 / 分叉 / 环
# ---------------------------------------------------------------------------


def test_reconcile_multiple_heads_reported():
    f0 = frag(
        ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, "e2", EventType.SEAL, seal_id="S002", custodian="Bob"),  # 第二个链头
    )
    resp = reconcile(req(f0))

    assert resp.reconciled is False
    assert resp.structure.heads == ["e1", "e2"]
    assert resp.validation is None
    assert resp.sequence is None


def test_reconcile_missing_predecessor_reported():
    f0 = frag(
        ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, "e2", EventType.HANDOVER, prev="ghost", custodian="Bob"),
    )
    resp = reconcile(req(f0))

    assert resp.reconciled is False
    assert resp.structure.heads == ["e1"]
    assert [(m.event_id, m.prev_event_id) for m in resp.structure.missing_predecessors] == [
        ("e2", "ghost")
    ]
    assert resp.validation is None


def test_reconcile_predecessor_lost_to_conflict_counts_as_missing():
    # e1 内容冲突被排除后，e2 的前驱随之缺失：两类问题同时报告
    f0 = frag(
        ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, "e2", EventType.HANDOVER, prev="e1", custodian="Bob"),
    )
    f1 = frag(ev(1, "e1", EventType.SEAL, seal_id="S009", custodian="Alice"))
    resp = reconcile(req(f0, f1))

    assert resp.reconciled is False
    assert [c.event_id for c in resp.conflicts] == ["e1"]
    assert [(m.event_id, m.prev_event_id) for m in resp.structure.missing_predecessors] == [
        ("e2", "e1")
    ]


def test_reconcile_fork_reported():
    f0 = frag(ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"))
    f1 = frag(
        ev(2, "e2", EventType.HANDOVER, prev="e1", custodian="Bob"),
        ev(3, "e3", EventType.HANDOVER, prev="e1", custodian="Carol"),  # 与 e2 分叉
    )
    resp = reconcile(req(f0, f1))

    assert resp.reconciled is False
    assert resp.structure.heads == ["e1"]
    assert [(f.prev_event_id, f.event_ids) for f in resp.structure.forks] == [
        ("e1", ["e2", "e3"])
    ]
    assert resp.validation is None


def test_reconcile_cycle_reported():
    f0_frag = frag(
        ev(1, "e1", EventType.SEAL, prev="e3", seal_id="S001", custodian="Alice"),
        ev(2, "e2", EventType.HANDOVER, prev="e1", custodian="Bob"),
        ev(3, "e3", EventType.HANDOVER, prev="e2", custodian="Carol"),
    )
    resp = reconcile(req(f0_frag))

    assert resp.reconciled is False
    assert resp.structure.heads == []
    # 环内每个事件的 prev_event_id 依次指向下一事件，末尾指回开头
    assert resp.structure.cycles == [["e1", "e3", "e2"]]
    assert resp.validation is None


def test_reconcile_self_loop_is_a_cycle():
    f0 = frag(ev(1, "e1", EventType.SEAL, prev="e1", seal_id="S001", custodian="Alice"))
    resp = reconcile(req(f0))

    assert resp.reconciled is False
    assert resp.structure.cycles == [["e1"]]


def test_reconcile_multiple_problems_reported_together():
    # 同时存在多头、缺失前驱、分叉与环时，全部如实报告
    f0 = frag(
        ev(1, "h1", EventType.SEAL, seal_id="S1", custodian="A"),
        ev(2, "h2", EventType.SEAL, seal_id="S2", custodian="B"),      # 多头
        ev(3, "m1", EventType.HANDOVER, prev="ghost", custodian="C"),   # 缺失前驱
        ev(4, "f1", EventType.HANDOVER, prev="h1", custodian="D"),      # 分叉（与 f2）
        ev(5, "f2", EventType.HANDOVER, prev="h1", custodian="E"),
        ev(6, "c1", EventType.HANDOVER, prev="c2", custodian="F"),      # 环 c1 <-> c2
        ev(7, "c2", EventType.HANDOVER, prev="c1", custodian="G"),
    )
    resp = reconcile(req(f0))

    assert resp.reconciled is False
    assert resp.structure.heads == ["h1", "h2"]
    assert [(m.event_id, m.prev_event_id) for m in resp.structure.missing_predecessors] == [
        ("m1", "ghost")
    ]
    assert [(f.prev_event_id, f.event_ids) for f in resp.structure.forks] == [
        ("h1", ["f1", "f2"])
    ]
    assert resp.structure.cycles == [["c1", "c2"]]
    assert resp.validation is None


# ---------------------------------------------------------------------------
# 重建语义：不按时间戳猜序，仅成功时调用校验器
# ---------------------------------------------------------------------------


def test_reconcile_sequence_follows_prev_links_not_input_order():
    # 片段内事件顺序打乱，序列仍按 prev_event_id 重建
    f0 = frag(
        ev(3, "e3", EventType.UNSEAL, prev="e2"),
        ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, "e2", EventType.HANDOVER, prev="e1", custodian="Bob"),
    )
    resp = reconcile(req(f0))

    assert resp.reconciled is True
    assert resp.sequence == ["e1", "e2", "e3"]
    assert resp.validation is not None
    assert resp.validation.valid is True


def test_reconcile_does_not_reorder_by_timestamp():
    # 时间戳沿链倒退：不得按时间戳重排，序列仍按 prev 链接，
    # 时间递增性交给现有校验器判定为违规
    f0 = frag(
        ev(3, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, "e2", EventType.HANDOVER, prev="e1", custodian="Bob"),
    )
    resp = reconcile(req(f0))

    assert resp.reconciled is True
    assert resp.sequence == ["e1", "e2"]
    assert resp.validation is not None
    assert resp.validation.valid is False
    assert [v.index for v in resp.validation.violations] == [1]
    assert "严格递增" in resp.validation.violations[0].reason


def test_reconcile_validation_indices_align_with_sequence():
    f0 = frag(
        ev(1, "e1", EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, "e2", EventType.UNSEAL, prev="e1"),
        ev(3, "e3", EventType.HANDOVER, prev="e2", custodian="Bob"),  # 未封装交接
        ev(4, "e4", EventType.RESEAL, prev="e3", seal_id="S002", custodian="Bob"),
    )
    resp = reconcile(req(f0))

    assert resp.reconciled is True
    assert resp.validation is not None
    assert resp.validation.valid is False
    # 违规下标对应重建后序列中的位置（e3 为序列第 3 个，index 2）
    assert [v.index for v in resp.validation.violations] == [2]
    assert "未封装" in resp.validation.violations[0].reason
    assert resp.validation.sealed is True
    assert resp.validation.current_seal == "S002"


def test_reconcile_empty_fragments_reconcile_to_empty_valid_chain():
    resp = reconcile(req(frag(), frag()))
    assert resp.reconciled is True
    assert resp.sequence == []
    assert resp.validation is not None
    assert resp.validation.valid is True
    assert resp.event_sources == []


# ---------------------------------------------------------------------------
# API 集成
# ---------------------------------------------------------------------------


def test_api_reconcile_validate_happy_path():
    payload = {
        "chain_id": "chain-a",
        "fragments": [
            {"events": [
                {"event_id": "e1", "prev_event_id": None, "type": "seal",
                 "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001",
                 "custodian": "Alice"},
                {"event_id": "e2", "prev_event_id": "e1", "type": "handover",
                 "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"},
            ]},
            {"events": [
                {"event_id": "e2", "prev_event_id": "e1", "type": "handover",
                 "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"},
                {"event_id": "e3", "prev_event_id": "e2", "type": "unseal",
                 "timestamp": "2026-09-18T12:00:00Z"},
            ]},
        ],
    }
    r = client.post("/api/v1/seal-chain/reconcile-validate", json=payload)
    assert r.status_code == 200
    body = r.json()

    assert body["chain_id"] == "chain-a"
    assert body["reconciled"] is True
    assert body["conflicts"] == []
    assert body["structure"] == {
        "heads": ["e1"],
        "missing_predecessors": [],
        "forks": [],
        "cycles": [],
    }
    assert body["sequence"] == ["e1", "e2", "e3"]
    assert body["validation"]["valid"] is True
    assert body["validation"]["sealed"] is False
    assert body["validation"]["final_custodian"] == "Bob"
    assert body["event_sources"] == [
        {"event_id": "e1", "fragments": [0]},
        {"event_id": "e2", "fragments": [0, 1]},
        {"event_id": "e3", "fragments": [1]},
    ]


def test_api_reconcile_validate_conflict_response():
    payload = {
        "chain_id": "chain-a",
        "fragments": [
            {"events": [
                {"event_id": "e1", "prev_event_id": None, "type": "seal",
                 "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001",
                 "custodian": "Alice"},
            ]},
            {"events": [
                {"event_id": "e1", "prev_event_id": None, "type": "seal",
                 "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001",
                 "custodian": "Carol"},
            ]},
        ],
    }
    r = client.post("/api/v1/seal-chain/reconcile-validate", json=payload)
    assert r.status_code == 200
    body = r.json()

    assert body["reconciled"] is False
    assert body["validation"] is None
    assert body["sequence"] is None
    assert body["conflicts"][0]["event_id"] == "e1"
    assert body["conflicts"][0]["differing_fields"] == ["custodian"]
    assert [v["fragments"] for v in body["conflicts"][0]["variants"]] == [[0], [1]]
    assert body["event_sources"] == [{"event_id": "e1", "fragments": [0, 1]}]


def test_api_reconcile_missing_event_id_rejected_with_422():
    payload = {
        "chain_id": "chain-a",
        "fragments": [{"events": [
            {"type": "seal", "timestamp": "2026-09-18T10:00:00Z",
             "seal_id": "S001", "custodian": "Alice"},
        ]}],
    }
    assert client.post("/api/v1/seal-chain/reconcile-validate", json=payload).status_code == 422


def test_api_reconcile_unknown_event_type_rejected_with_422():
    payload = {
        "chain_id": "chain-a",
        "fragments": [{"events": [
            {"event_id": "e1", "type": "teleport",
             "timestamp": "2026-09-18T10:00:00Z"},
        ]}],
    }
    assert client.post("/api/v1/seal-chain/reconcile-validate", json=payload).status_code == 422


def test_api_existing_endpoints_still_work():
    # 现有接口保持兼容
    assert client.get("/health").json() == {"status": "ok"}
    r = client.post("/api/v1/seal-chain/validate", json={"events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z",
         "seal_id": "S001", "custodian": "Alice"},
    ]})
    assert r.status_code == 200
    assert r.json()["valid"] is True
    r = client.post("/api/v1/seal-chain/batch-validate", json={"chains": []})
    assert r.status_code == 200
    assert r.json()["valid"] is True
