"""超长事件链分段校验（续传令牌）。

``POST /api/v1/seal-chain/validate-chunk`` 把一条超长事件链分成若干段依次校验：
首段请求不带 ``continuation_token``，响应返回不透明续传令牌；后续段携带该令牌，
即可从上一段结束时的状态机状态继续校验。令牌完整保存封装状态（含是否曾封装）、
保管人、当前及已用封签、最近有效事件时间与累计事件数，因此逐事件判定与把所有
分段拼接后调用 ``/api/v1/seal-chain/validate`` 完全一致，响应下标为全链偏移。

令牌格式：``base64url(payload_json) + "." + base64url(hmac_sha256)``。

- payload 为键序固定的紧凑 JSON（``used_seals`` 排序、无随机成分），同一状态
  序列化结果唯一；
- 签名使用环境变量 ``SEAL_CHAIN_TOKEN_SECRET`` 作为密钥的 HMAC-SHA256，
  是确定性签名：同一状态 + 同一密钥必然得到同一令牌。

令牌在处理本段事件**之前**校验：任何篡改（签名不匹配）、版本不支持或状态字段
非法都会以 :class:`ChunkValidateError` 拒绝（路由层转换为 HTTP 422），本段事件
不会被处理，也不会签发新令牌。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from .models import ChunkSummary, ChunkValidateRequest, ChunkValidateResponse
from .validator import ChainState, validate_events_from

#: 当前支持的令牌结构版本
TOKEN_VERSION = 1

#: 签名密钥环境变量名
SECRET_ENV_VAR = "SEAL_CHAIN_TOKEN_SECRET"

#: 未配置环境变量时的开发用默认密钥（与 docker-compose.yml 中的默认值一致；
#: 生产环境务必通过 SEAL_CHAIN_TOKEN_SECRET 覆盖）
_DEV_DEFAULT_SECRET = "seal-chain-dev-secret"

#: 版本 1 令牌负载的完整字段集合
_PAYLOAD_FIELDS = {
    "v",
    "sealed",
    "ever_sealed",
    "custodian",
    "current_seal",
    "used_seals",
    "last_timestamp",
    "event_count",
    "violation_count",
}


class ChunkValidateError(ValueError):
    """续传令牌非法（在路由层转换为 HTTP 422，本段事件不被处理）。"""


@dataclass(frozen=True)
class _Continuation:
    """从续传令牌还原出的接续点：状态机状态与全链累计计数。"""

    state: ChainState
    event_count: int
    violation_count: int


# ---------------------------------------------------------------------------
# 令牌签发
# ---------------------------------------------------------------------------


def _secret_key() -> bytes:
    """读取签名密钥：优先环境变量，缺省回落到开发用默认值。"""
    secret = os.environ.get(SECRET_ENV_VAR) or _DEV_DEFAULT_SECRET
    return secret.encode("utf-8")


def _b64encode(raw: bytes) -> str:
    """无填充的 base64url 编码。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(segment: str) -> bytes:
    """严格解码无填充的 base64url；任何非法字符或长度都报错。"""
    if not segment:
        raise ChunkValidateError("续传令牌格式非法：存在空的编码段")
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise ChunkValidateError("续传令牌格式非法：不是有效的 base64url 编码") from exc


def _encode_token(state: ChainState, event_count: int, violation_count: int) -> str:
    """把状态机状态与累计计数序列化为签名令牌（确定性输出）。"""
    payload = {
        "v": TOKEN_VERSION,
        "sealed": state.sealed,
        "ever_sealed": state.ever_sealed,
        "custodian": state.custodian,
        "current_seal": state.current_seal,
        "used_seals": sorted(state.used_seals),
        "last_timestamp": (
            state.last_timestamp.isoformat() if state.last_timestamp is not None else None
        ),
        "event_count": event_count,
        "violation_count": violation_count,
    }
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    signature = hmac.new(_secret_key(), raw, hashlib.sha256).digest()
    return f"{_b64encode(raw)}.{_b64encode(signature)}"


# ---------------------------------------------------------------------------
# 令牌校验与状态还原
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    """严格整数判定（布尔值不算）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _check_payload(payload: Dict[str, Any]) -> None:
    """校验令牌负载的版本、字段类型与状态一致性，非法即抛 ChunkValidateError。"""
    version = payload.get("v")
    if not _is_int(version):
        raise ChunkValidateError("续传令牌状态字段非法：版本字段 v 必须是整数")
    if version != TOKEN_VERSION:
        raise ChunkValidateError(f"不支持的续传令牌版本：{version}")
    if set(payload) != _PAYLOAD_FIELDS:
        raise ChunkValidateError("续传令牌状态字段非法：字段集合与版本 1 不符")

    sealed = payload["sealed"]
    ever_sealed = payload["ever_sealed"]
    if not isinstance(sealed, bool) or not isinstance(ever_sealed, bool):
        raise ChunkValidateError("续传令牌状态字段非法：sealed / ever_sealed 必须是布尔值")

    custodian = payload["custodian"]
    if custodian is not None and not _is_non_empty_str(custodian):
        raise ChunkValidateError("续传令牌状态字段非法：custodian 必须是 null 或非空字符串")

    current_seal = payload["current_seal"]
    if current_seal is not None and not _is_non_empty_str(current_seal):
        raise ChunkValidateError(
            "续传令牌状态字段非法：current_seal 必须是 null 或非空字符串"
        )

    used_seals = payload["used_seals"]
    if not isinstance(used_seals, list) or any(
        not _is_non_empty_str(seal) for seal in used_seals
    ):
        raise ChunkValidateError("续传令牌状态字段非法：used_seals 必须是非空字符串数组")
    if len(set(used_seals)) != len(used_seals):
        raise ChunkValidateError("续传令牌状态字段非法：used_seals 存在重复封签")

    last_timestamp = payload["last_timestamp"]
    if last_timestamp is not None:
        if not isinstance(last_timestamp, str):
            raise ChunkValidateError(
                "续传令牌状态字段非法：last_timestamp 必须是 null 或 ISO 8601 字符串"
            )
        try:
            datetime.fromisoformat(last_timestamp)
        except ValueError as exc:
            raise ChunkValidateError(
                "续传令牌状态字段非法：last_timestamp 不是合法的 ISO 8601 时间"
            ) from exc

    event_count = payload["event_count"]
    violation_count = payload["violation_count"]
    if not _is_int(event_count) or event_count < 0:
        raise ChunkValidateError("续传令牌状态字段非法：event_count 必须是非负整数")
    if not _is_int(violation_count) or violation_count < 0:
        raise ChunkValidateError("续传令牌状态字段非法：violation_count 必须是非负整数")
    if violation_count > event_count:
        raise ChunkValidateError(
            "续传令牌状态字段非法：violation_count 不能大于 event_count"
        )

    # 状态一致性（与状态机不变量对齐，杜绝自相矛盾的伪造状态）
    if sealed != (current_seal is not None):
        raise ChunkValidateError(
            "续传令牌状态字段非法：sealed 与 current_seal 自相矛盾"
        )
    if current_seal is not None and current_seal not in used_seals:
        raise ChunkValidateError(
            "续传令牌状态字段非法：current_seal 必须包含在 used_seals 中"
        )
    if not ever_sealed:
        if sealed or used_seals or custodian is not None or last_timestamp is not None:
            raise ChunkValidateError(
                "续传令牌状态字段非法：未曾封装的状态不得携带封签、保管人或有效时间"
            )
    elif custodian is None or last_timestamp is None:
        raise ChunkValidateError(
            "续传令牌状态字段非法：已封装过的状态必须携带保管人与最近有效时间"
        )


def _decode_token(token: str) -> _Continuation:
    """验签并还原接续点；任何篡改、版本不支持或字段非法都抛 ChunkValidateError。"""
    parts = token.split(".")
    if len(parts) != 2:
        raise ChunkValidateError("续传令牌格式非法：应为 <负载>.<签名> 两段结构")

    raw = _b64decode(parts[0])
    signature = _b64decode(parts[1])
    expected = hmac.new(_secret_key(), raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise ChunkValidateError("续传令牌签名校验失败：令牌可能被篡改或密钥不匹配")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChunkValidateError("续传令牌格式非法：负载不是有效的 JSON") from exc
    if not isinstance(payload, dict):
        raise ChunkValidateError("续传令牌格式非法：负载必须是 JSON 对象")

    _check_payload(payload)

    last_timestamp_raw: Optional[str] = payload["last_timestamp"]
    state = ChainState(
        sealed=payload["sealed"],
        ever_sealed=payload["ever_sealed"],
        current_seal=payload["current_seal"],
        custodian=payload["custodian"],
        used_seals=set(payload["used_seals"]),
        last_timestamp=(
            datetime.fromisoformat(last_timestamp_raw)
            if last_timestamp_raw is not None
            else None
        ),
    )
    return _Continuation(
        state=state,
        event_count=payload["event_count"],
        violation_count=payload["violation_count"],
    )


# ---------------------------------------------------------------------------
# 分段校验
# ---------------------------------------------------------------------------


def validate_chunk(request: ChunkValidateRequest) -> ChunkValidateResponse:
    """校验一个事件分段，返回本段结果、全链累计摘要与下一段续传令牌。

    首段请求 ``continuation_token`` 为 null；后续段携带上一响应返回的令牌。
    令牌非法时抛出 :class:`ChunkValidateError`，本段事件不会被处理。
    """
    if request.continuation_token is None:
        state = ChainState()
        event_offset = 0
        violation_count = 0
    else:
        # 先完整验签与校验状态，通过后才允许处理本段事件
        continuation = _decode_token(request.continuation_token)
        state = continuation.state
        event_offset = continuation.event_count
        violation_count = continuation.violation_count

    # 与单链接口共用同一状态机，逐事件判定与拼接后整链校验完全一致
    result = validate_events_from(state, request.events, index_offset=event_offset)

    event_count = event_offset + len(request.events)
    violation_count += len(result.violations)
    summary = ChunkSummary(
        event_count=event_count,
        violation_count=violation_count,
        valid=violation_count == 0,
        sealed=state.sealed,
        custodian=state.custodian,
        current_seal=state.current_seal,
    )
    return ChunkValidateResponse(
        events=result.events,
        violations=result.violations,
        summary=summary,
        continuation_token=_encode_token(state, event_count, violation_count),
    )
