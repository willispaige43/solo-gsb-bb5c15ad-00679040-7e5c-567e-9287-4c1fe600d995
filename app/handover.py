"""交接计划符合性审计。

处理流程：
1. 调用现有单链校验器（``validate_events``）得到原校验结果并原样保留；
2. 仅从校验结果中 ``status=ok`` 的 seal / reseal / handover 事件提取实际到任
   记录（保管人即事件发生后的在任保管人），并保留其在**全链事件**中的下标；
   unseal 及违规事件一律不是到任记录；
3. 按“保管人相同且顺序不交叉”做计划节点与实际记录间的一对一匹配，采用
   序列对齐式动态规划求**全局最优**（绝非逐项贪心），目标依次为：
     a. 最大化匹配数；
     b. 在匹配数相同的方案中，最小化实际时间偏离对应窗口的总秒数
        （early: ``window_start - t``；late: ``t - window_end``；on_time: 0）；
     c. 仍并列时，选择被匹配事件下标序列（按计划节点顺序）字典序最小的方案；
4. 输出每个计划节点的 matched / missing、匹配事件下标与 early / on_time / late，
   未匹配的实际记录列为 unexpected，并汇总各状态数量。

窗口为闭起点 / 开终点：``window_start <= t < window_end`` 计为 on_time。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from .models import (
    Event,
    EventResult,
    EventType,
    HandoverPlanAuditRequest,
    HandoverPlanAuditResponse,
    HandoverPlanNode,
    HandoverPlanNodeResult,
    HandoverPlanSummary,
    UnexpectedArrival,
)
from .validator import validate_events


class HandoverPlanAuditError(ValueError):
    """审计入参的语义错误（在路由层转换为 HTTP 422）。"""


@dataclass(frozen=True)
class _Arrival:
    """一条实际到任记录及其全链事件下标。"""

    event_index: int
    timestamp: datetime
    custodian: str
    type: EventType


# 动态规划状态：(-匹配数, 总偏离秒数, 被匹配记录下标元组)。
# 三者均按“越小越优”比较：匹配数取负后最大化匹配数即最小化该键；
# 记录按事件顺序提取，记录下标严格递增，事件下标也严格递增，
# 故记录下标元组的字典序与事件下标元组的字典序一致。
_DpKey = Tuple[int, float, Tuple[int, ...]]
_DP_ZERO: _DpKey = (0, 0.0, ())


def _extract_arrivals(
    events: Sequence[Event], event_results: Sequence[EventResult]
) -> List[_Arrival]:
    """从 status=ok 的 seal/reseal/handover 事件提取实际到任记录。"""
    arrivals: List[_Arrival] = []
    for event, result in zip(events, event_results):
        if result.status != "ok":
            continue
        if event.type not in (EventType.SEAL, EventType.RESEAL, EventType.HANDOVER):
            continue
        arrivals.append(
            _Arrival(
                event_index=result.index,
                timestamp=event.timestamp,
                custodian=event.custodian,  # type: ignore[arg-type]
                type=event.type,
            )
        )
    return arrivals


def _timing(timestamp: datetime, node: HandoverPlanNode) -> Tuple[str, float]:
    """判定实际时间相对窗口的状态，返回 (timing, 偏离秒数)。"""
    if timestamp < node.window_start:
        return "early", (node.window_start - timestamp).total_seconds()
    if timestamp >= node.window_end:
        return "late", (timestamp - node.window_end).total_seconds()
    return "on_time", 0.0


def _ensure_comparable(
    events: Sequence[Event], nodes: Sequence[HandoverPlanNode]
) -> None:
    """混用带时区与不带时区的时间无法比较，统一报 422。"""
    timestamps: List[datetime] = [event.timestamp for event in events]
    for node in nodes:
        timestamps.append(node.window_start)
        timestamps.append(node.window_end)
    if not timestamps:
        return
    aware = timestamps[0].tzinfo is not None and timestamps[0].utcoffset() is not None
    for value in timestamps[1:]:
        value_aware = value.tzinfo is not None and value.utcoffset() is not None
        if value_aware != aware:
            raise HandoverPlanAuditError(
                "时间戳时区信息不一致（不能混用带时区与不带时区的时间）"
            )


def _optimal_matching(
    nodes: Sequence[HandoverPlanNode], arrivals: Sequence[_Arrival]
) -> List[Tuple[int, int]]:
    """求全局最优匹配，返回 ``[(计划节点下标, 实际记录下标), ...]``。

    序列对齐 DP：``dp[i][j]`` 表示前 i 个计划节点与前 j 条实际记录的最优状态键，
    转移来自跳过计划节点、跳过实际记录、或在保管人相同时配对二者；
    键按 (匹配数降序, 总偏离升序, 事件下标序列字典序) 逐层比较，
    一次遍历即得到全局最优，不做逐项贪心。
    """
    m, n = len(nodes), len(arrivals)
    # 用 None 表示不可达；首行首列为空匹配基线。
    dp: List[List[Optional[_DpKey]]] = [
        [None for _ in range(n + 1)] for _ in range(m + 1)
    ]
    dp[0][0] = _DP_ZERO

    for i in range(m + 1):
        for j in range(n + 1):
            if i == 0 and j == 0:
                continue
            best: Optional[_DpKey] = None
            # 跳过第 i 个计划节点（该节点 missing）
            if i > 0 and dp[i - 1][j] is not None:
                best = dp[i - 1][j]
            # 跳过第 j 条实际记录（该记录 unexpected）
            if j > 0 and dp[i][j - 1] is not None:
                candidate = dp[i][j - 1]
                if best is None or candidate < best:
                    best = candidate
            # 配对：第 i 个计划节点与第 j 条实际记录，要求保管人相同
            if i > 0 and j > 0:
                prev = dp[i - 1][j - 1]
                node = nodes[i - 1]
                arrival = arrivals[j - 1]
                if prev is not None and node.custodian == arrival.custodian:
                    _, deviation = _timing(arrival.timestamp, node)
                    neg_count, total, indices = prev
                    candidate = (
                        neg_count - 1,
                        total + deviation,
                        indices + (j - 1,),
                    )
                    if best is None or candidate < best:
                        best = candidate
            dp[i][j] = best

    final_key = dp[m][n]
    assert final_key is not None  # 全部走“跳过”转移必然可达

    # 回溯重建配对。并列键对应的匹配集合相同（不同仅在被跳过的位置），
    # 按与前向相同的优先级取键即可复现全局最优方案。
    pairs: List[Tuple[int, int]] = []
    i, j = m, n
    while i > 0 or j > 0:
        current = dp[i][j]
        moved = False
        # 跳过计划节点（该节点 missing）
        if i > 0 and dp[i - 1][j] == current:
            i -= 1
            moved = True
        # 配对
        if (
            not moved
            and i > 0
            and j > 0
            and nodes[i - 1].custodian == arrivals[j - 1].custodian
            and dp[i - 1][j - 1] is not None
        ):
            _, deviation = _timing(arrivals[j - 1].timestamp, nodes[i - 1])
            neg_count, total, indices = dp[i - 1][j - 1]  # type: ignore[misc]
            candidate = (neg_count - 1, total + deviation, indices + (j - 1,))
            if candidate == current:
                pairs.append((i - 1, j - 1))
                i -= 1
                j -= 1
                moved = True
        # 跳过实际记录（该记录 unexpected）
        if not moved:
            j -= 1

    pairs.reverse()
    return pairs


def audit_handover_plan(request: HandoverPlanAuditRequest) -> HandoverPlanAuditResponse:
    """校验事件链并求实际到任记录与计划节点的全局最优匹配。"""
    validation = validate_events(request.events)
    _ensure_comparable(request.events, request.plan)

    arrivals = _extract_arrivals(request.events, validation.events)
    pairs = _optimal_matching(request.plan, arrivals)

    by_node = {node_i: record_j for node_i, record_j in pairs}
    matched_records = {record_j for _, record_j in pairs}

    plan_results: List[HandoverPlanNodeResult] = []
    early = on_time = late = 0
    for i, node in enumerate(request.plan):
        if i not in by_node:
            plan_results.append(
                HandoverPlanNodeResult(
                    index=i,
                    custodian=node.custodian,
                    window_start=node.window_start,
                    window_end=node.window_end,
                    status="missing",
                )
            )
            continue

        arrival = arrivals[by_node[i]]
        timing, deviation = _timing(arrival.timestamp, node)
        if timing == "early":
            early += 1
        elif timing == "on_time":
            on_time += 1
        else:
            late += 1
        plan_results.append(
            HandoverPlanNodeResult(
                index=i,
                custodian=node.custodian,
                window_start=node.window_start,
                window_end=node.window_end,
                status="matched",
                event_index=arrival.event_index,
                actual_timestamp=arrival.timestamp,
                timing=timing,
                deviation_seconds=deviation,
            )
        )

    unexpected = [
        UnexpectedArrival(
            event_index=arrival.event_index,
            type=arrival.type,
            timestamp=arrival.timestamp,
            custodian=arrival.custodian,
        )
        for j, arrival in enumerate(arrivals)
        if j not in matched_records
    ]

    matched_count = len(pairs)
    summary = HandoverPlanSummary(
        matched=matched_count,
        missing=len(request.plan) - matched_count,
        unexpected=len(arrivals) - matched_count,
        early=early,
        on_time=on_time,
        late=late,
    )

    return HandoverPlanAuditResponse(
        validation=validation,
        plan_results=plan_results,
        unexpected=unexpected,
        summary=summary,
    )
