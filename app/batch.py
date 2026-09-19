"""批量封签占用检查。

处理流程：
1. 先按现有单链状态机规则分别校验批次中的每条链（``validate_events``），
   得到各链的原校验结果；
2. 再按输入顺序建立全批次封签占用表：同一 ``seal_id`` 被不同链的
   *有效* seal/reseal 事件（即单链校验中 status 为 ``ok`` 的事件）使用时，
   两条链对应的事件均标记为 ``cross_chain_conflict``；
3. 冲突不改变各链自身状态（``valid`` / ``sealed`` / ``violations`` 等保持不变），
   但只要存在冲突，批次的 ``valid`` 必为 ``false``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .models import (
    BatchChainResult,
    BatchChainSummary,
    BatchValidateRequest,
    BatchValidateResponse,
    CrossChainConflict,
    EventResult,
    EventType,
)
from .validator import validate_events

_SEAL_EVENTS = (EventType.SEAL, EventType.RESEAL)


@dataclass
class _Occupation:
    """某个 seal_id 在批次中的首次占用信息。"""

    first_chain_id: str
    first_event: EventResult
    recorded: bool = False  # 首次占用事件的冲突项是否已加入冲突列表


def validate_batch(request: BatchValidateRequest) -> BatchValidateResponse:
    """先逐链校验，再建立全批次封签占用表并标记跨链冲突。"""
    # 阶段 1：分别按现有规则校验每条链，保留输入顺序
    chain_results: List[BatchChainResult] = []
    for chain in request.chains:
        single = validate_events(chain.events)
        chain_results.append(BatchChainResult(chain_id=chain.chain_id, **single.model_dump()))

    # 阶段 2：按输入顺序建立全批次封签占用表
    occupations: Dict[str, _Occupation] = {}
    conflicts: List[CrossChainConflict] = []

    for chain, result in zip(request.chains, chain_results):
        for event, event_result in zip(chain.events, result.events):
            # 只有有效（ok）的 seal/reseal 事件才构成封签占用
            if event_result.status != "ok" or event.type not in _SEAL_EVENTS:
                continue
            seal_id = event.seal_id
            if seal_id is None:  # pragma: no cover - 有效事件必带 seal_id
                continue

            occupation = occupations.get(seal_id)
            if occupation is None:
                # 首次占用：先记录，暂不产生冲突项；被其他链复用时再标记
                occupations[seal_id] = _Occupation(
                    first_chain_id=chain.chain_id,
                    first_event=event_result,
                )
                continue

            if occupation.first_chain_id == chain.chain_id:
                # 同链内 seal_id 复用在单链校验阶段已被判为违规，
                # 不会以 ok 事件到达这里，防御性跳过。
                continue

            # 跨链冲突：首次占用事件（仅标记一次）与当前事件都标为冲突
            if not occupation.recorded:
                occupation.first_event.cross_chain_conflict = True
                conflicts.append(
                    CrossChainConflict(
                        seal_id=seal_id,
                        chain_id=occupation.first_chain_id,
                        event_index=occupation.first_event.index,
                        first_chain_id=occupation.first_chain_id,
                    )
                )
                occupation.recorded = True

            event_result.cross_chain_conflict = True
            conflicts.append(
                CrossChainConflict(
                    seal_id=seal_id,
                    chain_id=chain.chain_id,
                    event_index=event_result.index,
                    first_chain_id=occupation.first_chain_id,
                )
            )

    # 阶段 3：按 chain_id 稳定排序的汇总
    summary = [
        BatchChainSummary(
            chain_id=result.chain_id,
            valid=result.valid,
            conflicts=sum(1 for item in result.events if item.cross_chain_conflict),
        )
        for result in sorted(chain_results, key=lambda item: item.chain_id)
    ]

    return BatchValidateResponse(
        valid=all(result.valid for result in chain_results) and not conflicts,
        chains=chain_results,
        conflicts=conflicts,
        summary=summary,
    )
