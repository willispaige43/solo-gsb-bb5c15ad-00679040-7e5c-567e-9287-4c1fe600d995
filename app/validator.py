"""封签链状态机校验器。

状态机规则：
1. 未封装状态下不得交接或开封；
2. 已封装状态下不得重复封装 / 重封，须先开封；
3. 开封后必须使用新的（未使用过的）封签重封；
4. 已使用过的封签不得复用；
5. 事件时间戳必须严格递增。

违规事件不改变状态，也不阻止后续事件的判定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Set

from .models import Event, EventResult, EventType, ValidateResponse, Violation


@dataclass
class ChainState:
    """校验过程中维护的封签链状态。"""

    sealed: bool = False
    ever_sealed: bool = False
    current_seal: Optional[str] = None
    custodian: Optional[str] = None
    used_seals: Set[str] = field(default_factory=set)
    last_timestamp: Optional[datetime] = None  # 最近一个“有效”事件的时间戳


def _check_event(state: ChainState, event: Event) -> Optional[str]:
    """检查单个事件是否合法。返回 None 表示合法，否则返回违规原因。"""
    # 规则 5：时间严格递增（与最近一个有效事件比较；违规事件不推进时间）
    if state.last_timestamp is not None:
        try:
            not_increasing = event.timestamp <= state.last_timestamp
        except TypeError:
            return "时间戳时区信息不一致（不能混用带时区与不带时区的时间）"
        if not_increasing:
            return (
                "时间戳必须严格递增："
                f"{event.timestamp.isoformat()} 未晚于前一有效事件 "
                f"{state.last_timestamp.isoformat()}"
            )

    if event.type in (EventType.SEAL, EventType.RESEAL):
        # 规则 2：已封装时不得重复封装
        if state.sealed:
            return "当前已处于封装状态，须先开封后才能再次封装"
        # 重封语义：必须曾经封装过
        if event.type is EventType.RESEAL and not state.ever_sealed:
            return "未曾封装，不能执行重封（reseal），请使用封装（seal）"
        if not event.seal_id:
            return f"{event.type.value} 事件必须提供 seal_id"
        # 规则 3/4：必须使用未使用过的新封签
        if event.seal_id in state.used_seals:
            return f"封签 {event.seal_id} 已被使用，不得复用"
        if not event.custodian:
            return f"{event.type.value} 事件必须提供 custodian"
        return None

    if event.type is EventType.HANDOVER:
        # 规则 1：未封装不得交接
        if not state.sealed:
            return "未封装状态下不得交接"
        if not event.custodian:
            return "handover 事件必须提供接收保管人 custodian"
        return None

    if event.type is EventType.UNSEAL:
        # 规则 1：未封装不得开封
        if not state.sealed:
            return "未封装状态下不得开封"
        return None

    return f"未知事件类型：{event.type}"  # 理论上被枚举校验拦截，防御性保留


def _apply_event(state: ChainState, event: Event) -> None:
    """应用一个合法事件，推进状态机。"""
    state.last_timestamp = event.timestamp

    if event.type in (EventType.SEAL, EventType.RESEAL):
        state.sealed = True
        state.ever_sealed = True
        state.current_seal = event.seal_id
        state.used_seals.add(event.seal_id)  # type: ignore[arg-type]
        state.custodian = event.custodian
    elif event.type is EventType.HANDOVER:
        state.custodian = event.custodian
    elif event.type is EventType.UNSEAL:
        state.sealed = False
        state.current_seal = None
        # 开封不改变保管人


def validate_events(events: List[Event]) -> ValidateResponse:
    """按顺序校验整条事件链，返回逐事件结果与全部违规。"""
    return validate_events_from(ChainState(), events)


def validate_events_from(
    state: ChainState, events: List[Event], index_offset: int = 0
) -> ValidateResponse:
    """从给定状态继续校验事件序列，逐事件下标从 ``index_offset`` 起算。

    与 ``validate_events`` 共用同一套状态机规则；``state`` 会被就地推进，
    调用结束后即为处理完全部事件的状态（分段校验据此生成续传令牌）。
    """
    results: List[EventResult] = []
    violations: List[Violation] = []

    for offset, event in enumerate(events):
        index = index_offset + offset
        reason = _check_event(state, event)
        if reason is None:
            _apply_event(state, event)
            results.append(EventResult(index=index, type=event.type, status="ok"))
        else:
            # 违规事件不改变状态，后续事件继续判定
            results.append(
                EventResult(index=index, type=event.type, status="violation", reason=reason)
            )
            violations.append(Violation(index=index, reason=reason))

    return ValidateResponse(
        valid=not violations,
        sealed=state.sealed,
        final_custodian=state.custodian,
        current_seal=state.current_seal,
        events=results,
        violations=violations,
    )
