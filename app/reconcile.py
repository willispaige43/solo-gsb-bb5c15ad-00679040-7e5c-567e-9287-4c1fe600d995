"""离线片段合并重建与校验。

处理流程：
1. 汇总所有片段的事件，按 ``event_id`` 分组并记录来源片段编号（片段在请求中的
   下标，从 0 开始）；
2. 内容去重：同一 ``event_id`` 的多个拷贝业务内容（type / timestamp / seal_id /
   custodian / prev_event_id）完全一致时合并为一份；存在分歧时记录冲突明细
   （分歧字段 + 各变体及其来源片段），冲突事件不参与后续重建；
3. 对无冲突事件按 ``prev_event_id`` 重建唯一序列，显式检测并报告
   多头 / 缺失前驱 / 分叉 / 环，**绝不按时间戳猜序**；
4. 仅当无冲突且结构无问题时，按重建序列调用现有单链校验器
   （``validate_events``），时间戳递增性等规则由该校验器判定。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .models import (
    EventConflict,
    EventSource,
    EventVariant,
    Fork,
    MissingPredecessor,
    ReconcileEvent,
    ReconcileRequest,
    ReconcileResponse,
    StructureReport,
)
from .validator import validate_events

#: 参与内容判重的业务字段（不含 event_id 本身）
_CONTENT_FIELDS = ("type", "timestamp", "seal_id", "custodian", "prev_event_id")


def _content_key(event: ReconcileEvent) -> Tuple:
    """用于判重的事件业务内容（不含 event_id）。"""
    return tuple(getattr(event, field) for field in _CONTENT_FIELDS)


def _dedup_keep_order(indices: List[int]) -> List[int]:
    """升序去重片段编号（输入已按片段顺序收集）。"""
    result: List[int] = []
    for index in indices:
        if not result or result[-1] != index:
            result.append(index)
    return result


def _detect_cycles(prev_of: Dict[str, str], event_ids: List[str]) -> List[List[str]]:
    """在前驱指针构成的函数图上检测环，按 event_ids 的顺序出发遍历。

    每个节点至多一条出边（指向其前驱）；prev 为 null 或缺失的节点是终点。
    返回的每个环满足：环内事件的 prev_event_id 依次指向列表下一事件，
    末尾事件指回开头。
    """
    unvisited, in_path, done = 0, 1, 2
    color = {event_id: unvisited for event_id in event_ids}
    cycles: List[List[str]] = []

    for start in event_ids:
        if color[start] != unvisited:
            continue
        path: List[str] = []
        node: Optional[str] = start
        while node is not None and color[node] == unvisited:
            color[node] = in_path
            path.append(node)
            node = prev_of.get(node)  # None 表示链头或前驱缺失，遍历终止
        if node is not None and color[node] == in_path:
            # 回到当前路径上的节点：从首次出现处起即为一个环
            cycles.append(path[path.index(node):])
        for event_id in path:
            color[event_id] = done

    return cycles


def reconcile(request: ReconcileRequest) -> ReconcileResponse:
    """合并同一 chain_id 的多个离线片段，重建唯一序列并按需校验。"""
    # 阶段 1：按 event_id 汇总全部出现位置（片段编号按请求顺序收集）
    occurrences: Dict[str, List[Tuple[int, ReconcileEvent]]] = {}
    for fragment_index, fragment in enumerate(request.fragments):
        for event in fragment.events:
            occurrences.setdefault(event.event_id, []).append((fragment_index, event))

    # 阶段 2：内容去重与冲突检测
    unique: Dict[str, ReconcileEvent] = {}  # 无冲突事件，保持首次出现顺序
    conflicts: List[EventConflict] = []
    event_sources: List[EventSource] = []

    for event_id, event_occurrences in occurrences.items():
        event_sources.append(
            EventSource(
                event_id=event_id,
                fragments=_dedup_keep_order([index for index, _ in event_occurrences]),
            )
        )

        variants: Dict[Tuple, EventVariant] = {}
        for fragment_index, event in event_occurrences:
            key = _content_key(event)
            variant = variants.get(key)
            if variant is None:
                variants[key] = EventVariant(fragments=[fragment_index], event=event)
            elif variant.fragments[-1] != fragment_index:
                variant.fragments.append(fragment_index)

        if len(variants) == 1:
            unique[event_id] = event_occurrences[0][1]
            continue

        variant_list = list(variants.values())
        first = variant_list[0].event
        differing = [
            field
            for field in _CONTENT_FIELDS
            if any(getattr(variant.event, field) != getattr(first, field)
                   for variant in variant_list[1:])
        ]
        conflicts.append(
            EventConflict(
                event_id=event_id,
                differing_fields=differing,
                variants=variant_list,
            )
        )

    # 阶段 3：对无冲突事件按 prev_event_id 做结构诊断（不按时间戳猜序）
    heads: List[str] = []
    missing: List[MissingPredecessor] = []
    children: Dict[str, List[str]] = {}
    prev_of: Dict[str, str] = {}  # 仅含前驱存在的事件

    for event_id, event in unique.items():
        prev = event.prev_event_id
        if prev is None:
            heads.append(event_id)
        elif prev not in unique:
            # 前驱未出现，或前驱因内容冲突被排除，均视为缺失前驱
            missing.append(MissingPredecessor(event_id=event_id, prev_event_id=prev))
        else:
            children.setdefault(prev, []).append(event_id)
            prev_of[event_id] = prev

    forks = [
        Fork(prev_event_id=prev, event_ids=event_ids)
        for prev, event_ids in children.items()
        if len(event_ids) > 1
    ]
    cycles = _detect_cycles(prev_of, list(unique))

    # 阶段 4：仅当无冲突且结构唯一时重建序列并调用现有单链校验器
    reconciled = (
        not conflicts
        and not missing
        and not forks
        and not cycles
        and (len(heads) == 1 or not unique)
    )

    sequence: Optional[List[str]] = None
    validation = None
    if reconciled:
        sequence = []
        if unique:
            # 唯一链头出发，沿唯一子节点走到底（无分叉保证每步至多一个后继）
            node = heads[0]
            while True:
                sequence.append(node)
                next_events = children.get(node)
                if not next_events:
                    break
                node = next_events[0]
        validation = validate_events([unique[event_id] for event_id in sequence])

    return ReconcileResponse(
        chain_id=request.chain_id,
        reconciled=reconciled,
        conflicts=conflicts,
        structure=StructureReport(
            heads=heads,
            missing_predecessors=missing,
            forks=forks,
            cycles=cycles,
        ),
        sequence=sequence,
        validation=validation,
        event_sources=event_sources,
    )
