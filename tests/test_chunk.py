"""超长事件链分段校验（续传令牌）的单元测试与 API 集成测试。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.chunk import (
    SECRET_ENV_VAR,
    TOKEN_VERSION,
    ChunkValidateError,
    validate_chunk,
)
from app.main import app
from app.models import ChunkValidateRequest, Event, EventType
from app.validator import validate_events

client = TestClient(app)

T = "2026-09-18T%02d:00:00Z"
TEST_SECRET = "test-secret-for-chunk-tokens"


def ev(i: int, type_: EventType, **kw) -> Event:
    return Event(type=type_, timestamp=T % i, **kw)


def run_chunks(chunks, secret: str = TEST_SECRET, monkeypatch=None):
    """依次校验各分段，返回响应列表（自动接力续传令牌）。"""
    if monkeypatch is not None:
        monkeypatch.setenv(SECRET_ENV_VAR, secret)
    responses = []
    token = None
    for events in chunks:
        resp = validate_chunk(ChunkValidateRequest(events=events, continuation_token=token))
        responses.append(resp)
        token = resp.continuation_token
    return responses


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def forged_token(payload: dict, secret: str = TEST_SECRET) -> str:
    """用指定密钥对任意负载签发令牌（用于构造版本 / 字段非法的令牌）。"""
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{b64url(raw)}.{b64url(signature)}"


def valid_payload(**overrides) -> dict:
    """一份合法的版本 1 负载，可按需覆盖字段。"""
    payload = {
        "v": TOKEN_VERSION,
        "sealed": True,
        "ever_sealed": True,
        "custodian": "Alice",
        "current_seal": "S001",
        "used_seals": ["S001"],
        "last_timestamp": "2026-09-18T10:00:00+00:00",
        "event_count": 1,
        "violation_count": 0,
    }
    payload.update(overrides)
    return payload


def decode_payload(token: str) -> dict:
    """不解签直接读取负载（测试令牌内容用）。"""
    raw = base64.urlsafe_b64decode(token.split(".")[0] + "=" * 4)
    return json.loads(raw.decode("utf-8"))


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    """所有测试使用固定密钥，与外部环境隔离。"""
    monkeypatch.setenv(SECRET_ENV_VAR, TEST_SECRET)


def sample_chain():
    """含一次违规（复用封签）的完整事件链。"""
    return [
        ev(1, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, EventType.HANDOVER, custodian="Bob"),
        ev(3, EventType.UNSEAL),
        ev(4, EventType.RESEAL, seal_id="S001", custodian="Bob"),   # 违规：复用
        ev(5, EventType.RESEAL, seal_id="S002", custodian="Bob"),
        ev(6, EventType.HANDOVER, custodian="Carol"),
    ]


# ---------------------------------------------------------------------------
# 与单链接口完全一致（核心保证）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("splits", [
    [6],                # 不分段
    [1, 5],
    [2, 1, 3],
    [3, 3],
    [1, 1, 1, 1, 1, 1],
    [0, 6],             # 空首段
    [6, 0],             # 空尾段
    [2, 0, 4],          # 空中段
])
def test_chunked_results_identical_to_single_shot(splits):
    chain = sample_chain()
    reference = validate_events(chain)

    chunks, cursor = [], 0
    for size in splits:
        chunks.append(chain[cursor:cursor + size])
        cursor += size
    responses = run_chunks(chunks)

    got = [item for resp in responses for item in resp.events]
    expected = [
        (e.index, e.type, e.status, e.reason) for e in reference.events
    ]
    assert [(e.index, e.type, e.status, e.reason) for e in got] == expected

    got_violations = [v for resp in responses for v in resp.violations]
    assert [(v.index, v.reason) for v in got_violations] == [
        (v.index, v.reason) for v in reference.violations
    ]

    final = responses[-1].summary
    assert final.sealed == reference.sealed
    assert final.custodian == reference.final_custodian
    assert final.current_seal == reference.current_seal
    assert final.event_count == len(chain)
    assert final.violation_count == len(reference.violations)
    assert final.valid == reference.valid


def test_indices_are_global_offsets_across_chunks():
    chain = sample_chain()
    responses = run_chunks([chain[:2], chain[2:4], chain[4:]])
    assert [e.index for resp in responses for e in resp.events] == [0, 1, 2, 3, 4, 5]
    # 违规事件（index 3）携带全链偏移
    assert [(v.index) for v in responses[1].violations] == [3]
    assert "不得复用" in responses[1].violations[0].reason


def test_state_carried_across_chunks_used_seals_and_timestamps():
    # 跨段复用封签与跨段时间倒退都必须被判定违规，与整链校验一致
    chunks = [
        [ev(2, EventType.SEAL, seal_id="S001", custodian="Alice"),
         ev(3, EventType.UNSEAL)],
        [ev(2, EventType.RESEAL, seal_id="S003", custodian="Alice"),  # 时间倒退，违规
         ev(4, EventType.RESEAL, seal_id="S001", custodian="Alice"),  # 复用，违规
         ev(5, EventType.RESEAL, seal_id="S002", custodian="Alice")],  # 合法
    ]
    responses = run_chunks(chunks)
    statuses = [e.status for resp in responses for e in resp.events]
    assert statuses == ["ok", "ok", "violation", "violation", "ok"]
    assert "严格递增" in responses[1].events[0].reason
    assert "不得复用" in responses[1].events[1].reason
    final = responses[-1].summary
    assert final.sealed is True
    assert final.current_seal == "S002"
    assert final.custodian == "Alice"
    assert final.event_count == 5
    assert final.violation_count == 2
    assert final.valid is False


def test_cumulative_summary_accumulates_over_chunks():
    chain = sample_chain()
    responses = run_chunks([chain[:2], chain[2:4], chain[4:]])

    s1, s2, s3 = (resp.summary for resp in responses)
    assert (s1.event_count, s1.violation_count, s1.valid) == (2, 0, True)
    assert (s1.sealed, s1.custodian, s1.current_seal) == (True, "Bob", "S001")
    assert (s2.event_count, s2.violation_count, s2.valid) == (4, 1, False)
    assert (s2.sealed, s2.custodian, s2.current_seal) == (False, "Bob", None)
    assert (s3.event_count, s3.violation_count, s3.valid) == (6, 1, False)
    assert (s3.sealed, s3.custodian, s3.current_seal) == (True, "Carol", "S002")


def test_first_chunk_without_token_and_empty_events():
    resp = validate_chunk(ChunkValidateRequest(events=[]))
    assert resp.events == []
    assert resp.violations == []
    assert resp.summary.event_count == 0
    assert resp.summary.valid is True
    assert resp.summary.sealed is False
    assert resp.summary.custodian is None
    assert resp.summary.current_seal is None
    assert resp.continuation_token  # 仍签发下一令牌


# ---------------------------------------------------------------------------
# 令牌：内容完整、确定性签名
# ---------------------------------------------------------------------------


def test_token_preserves_full_state():
    chain = [
        ev(1, EventType.SEAL, seal_id="S001", custodian="Alice"),
        ev(2, EventType.UNSEAL),
        ev(3, EventType.RESEAL, seal_id="S002", custodian="Bob"),
        ev(4, EventType.HANDOVER, custodian="Carol"),
    ]
    resp = run_chunks([chain])[0]
    payload = decode_payload(resp.continuation_token)

    assert payload["v"] == TOKEN_VERSION
    assert payload["sealed"] is True
    assert payload["ever_sealed"] is True
    assert payload["custodian"] == "Carol"
    assert payload["current_seal"] == "S002"
    assert payload["used_seals"] == ["S001", "S002"]
    assert payload["last_timestamp"] == "2026-09-18T04:00:00+00:00"
    assert payload["event_count"] == 4
    assert payload["violation_count"] == 0


def test_token_signature_is_deterministic():
    chain = sample_chain()[:3]
    t1 = run_chunks([chain])[0].continuation_token
    t2 = run_chunks([chain])[0].continuation_token
    assert t1 == t2


def test_token_signature_depends_on_secret(monkeypatch):
    chain = sample_chain()[:2]
    token = run_chunks([chain], secret="secret-a", monkeypatch=monkeypatch)[
        0
    ].continuation_token
    # 密钥不同则验签失败
    monkeypatch.setenv(SECRET_ENV_VAR, "secret-b")
    with pytest.raises(ChunkValidateError):
        validate_chunk(ChunkValidateRequest(events=[], continuation_token=token))


# ---------------------------------------------------------------------------
# 令牌非法：篡改 / 版本 / 状态字段 → 422，且不处理本段事件
# ---------------------------------------------------------------------------


def test_tampered_payload_rejected():
    token = run_chunks([sample_chain()[:2]])[0].continuation_token
    payload_part, signature = token.split(".")
    # 篡改负载（换一个字符）但保留原签名
    pos = 5
    replacement = "A" if payload_part[pos] != "A" else "B"
    tampered = payload_part[:pos] + replacement + payload_part[pos + 1:] + "." + signature
    with pytest.raises(ChunkValidateError, match="签名"):
        validate_chunk(ChunkValidateRequest(events=[], continuation_token=tampered))


def test_tampered_signature_rejected():
    token = run_chunks([sample_chain()[:2]])[0].continuation_token
    payload_part, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    tampered = payload_part + "." + replacement + signature[1:]
    with pytest.raises(ChunkValidateError, match="签名"):
        validate_chunk(ChunkValidateRequest(events=[], continuation_token=tampered))


def test_malformed_token_structure_rejected():
    for bad in ["", "not-a-token", "onlyonepart.", ".onlysig", "a.b.c", "!!!.???"]:
        with pytest.raises(ChunkValidateError):
            validate_chunk(ChunkValidateRequest(events=[], continuation_token=bad))


def test_unsupported_version_rejected():
    token = forged_token(valid_payload(v=TOKEN_VERSION + 1))
    with pytest.raises(ChunkValidateError, match="版本"):
        validate_chunk(ChunkValidateRequest(events=[], continuation_token=token))


@pytest.mark.parametrize("overrides", [
    {"sealed": "yes"},                                   # 类型非法
    {"event_count": -1},                                 # 负计数
    {"event_count": True},                               # 布尔不是合法计数
    {"violation_count": 2},                              # 违规数超过事件数
    {"used_seals": ["S001", "S001"]},                    # 重复封签
    {"used_seals": "S001"},                              # 不是数组
    {"last_timestamp": "not-a-time"},                    # 非法时间
    {"last_timestamp": 123},                             # 时间类型非法
    {"sealed": True, "current_seal": None},              # 状态自相矛盾
    {"sealed": False},                                   # 未封装却带当前封签
    {"current_seal": "S999"},                            # 当前封签不在已用集合
    {"ever_sealed": False},                              # 未曾封装却带完整状态
    {"custodian": None},                                 # 已封装过却无保管人
    {"custodian": ""},                                   # 空保管人
])
def test_illegal_state_fields_rejected(overrides):
    token = forged_token(valid_payload(**overrides))
    with pytest.raises(ChunkValidateError, match="状态字段非法"):
        validate_chunk(ChunkValidateRequest(events=[], continuation_token=token))


def test_missing_and_extra_fields_rejected():
    payload = valid_payload()
    del payload["custodian"]
    with pytest.raises(ChunkValidateError, match="状态字段非法"):
        validate_chunk(ChunkValidateRequest(
            events=[], continuation_token=forged_token(payload)))

    payload = valid_payload(extra_field="x")
    with pytest.raises(ChunkValidateError, match="状态字段非法"):
        validate_chunk(ChunkValidateRequest(
            events=[], continuation_token=forged_token(payload)))


def test_rejected_chunk_does_not_consume_events_or_advance_state():
    chain = sample_chain()
    first = run_chunks([chain[:2]])[0]
    token = first.continuation_token

    # 篡改令牌 → 422，本段事件不得处理
    bad = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    with pytest.raises(ChunkValidateError):
        validate_chunk(ChunkValidateRequest(events=chain[2:], continuation_token=bad))

    # 用原令牌重发本段：结果与未被干扰时完全一致
    resp = validate_chunk(ChunkValidateRequest(events=chain[2:], continuation_token=token))
    reference = validate_events(chain)
    assert [(e.index, e.status) for e in resp.events] == [
        (e.index, e.status) for e in reference.events[2:]
    ]
    assert resp.summary.event_count == len(chain)


# ---------------------------------------------------------------------------
# API 集成
# ---------------------------------------------------------------------------


def api_post(payload):
    return client.post("/api/v1/seal-chain/validate-chunk", json=payload)


def test_api_two_chunk_flow_matches_single_shot():
    events = [
        {"type": "seal", "timestamp": T % 1, "seal_id": "S001", "custodian": "Alice"},
        {"type": "handover", "timestamp": T % 2, "custodian": "Bob"},
        {"type": "unseal", "timestamp": T % 3},
        {"type": "reseal", "timestamp": T % 4, "seal_id": "S002", "custodian": "Bob"},
    ]
    r1 = api_post({"events": events[:2]})
    assert r1.status_code == 200
    body1 = r1.json()
    assert [e["index"] for e in body1["events"]] == [0, 1]
    assert body1["summary"]["event_count"] == 2
    token = body1["continuation_token"]
    assert isinstance(token, str) and token

    r2 = api_post({"events": events[2:], "continuation_token": token})
    assert r2.status_code == 200
    body2 = r2.json()
    assert [e["index"] for e in body2["events"]] == [2, 3]
    assert body2["summary"]["event_count"] == 4
    assert body2["summary"]["valid"] is True
    assert body2["summary"]["sealed"] is True
    assert body2["summary"]["custodian"] == "Bob"
    assert body2["summary"]["current_seal"] == "S002"

    # 与单链接口逐事件一致
    single = client.post("/api/v1/seal-chain/validate", json={"events": events}).json()
    chunked_events = body1["events"] + body2["events"]
    assert [(e["index"], e["status"]) for e in chunked_events] == [
        (e["index"], e["status"]) for e in single["events"]
    ]


def test_api_tampered_token_returns_422_and_events_not_processed():
    r1 = api_post({"events": [
        {"type": "seal", "timestamp": T % 1, "seal_id": "S001", "custodian": "Alice"},
    ]})
    token = r1.json()["continuation_token"]
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")

    r2 = api_post({"events": [
        {"type": "unseal", "timestamp": T % 2},
    ], "continuation_token": tampered})
    assert r2.status_code == 422
    assert "签名" in r2.json()["detail"]

    # 本段事件未被处理：用原令牌重放仍从全链偏移 1 开始
    r3 = api_post({"events": [
        {"type": "unseal", "timestamp": T % 2},
    ], "continuation_token": token})
    assert r3.status_code == 200
    body = r3.json()
    assert [e["index"] for e in body["events"]] == [1]
    assert body["events"][0]["status"] == "ok"
    assert body["summary"]["event_count"] == 2


def test_api_forged_token_with_unsupported_version_returns_422():
    token = forged_token(valid_payload(v=999))
    r = api_post({"events": [], "continuation_token": token})
    assert r.status_code == 422
    assert "版本" in r.json()["detail"]


def test_api_forged_token_with_illegal_state_returns_422():
    token = forged_token(valid_payload(sealed=True, current_seal=None))
    r = api_post({"events": [], "continuation_token": token})
    assert r.status_code == 422
    assert "状态字段非法" in r.json()["detail"]


def test_api_empty_string_token_returns_422():
    r = api_post({"events": [], "continuation_token": ""})
    assert r.status_code == 422


def test_api_invalid_event_structure_returns_422():
    r = api_post({"events": [{"type": "teleport", "timestamp": T % 1}]})
    assert r.status_code == 422
