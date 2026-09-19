"""温控责任审计。

处理流程：
1. 调用现有单链校验器（``validate_events``）得到原校验结果并原样保留；
2. 仅以其中 ``status=ok`` 的 seal / reseal / handover 事件生成保管人半开区间：
   第 k 个有效到任事件在 t_k 使该保管人到任，区间为 ``[t_k, t_{k+1})``，
   最后一个区间延伸至正无穷（``end=None``）；unseal 与违规事件不产生区间，
   首个有效到任事件之前的时间不属于任何保管人；
3. 温度样本时间戳必须严格递增（否则 422）。相邻样本 i、i+1 的间隔不超过
   ``max_gap_seconds`` 时按前值保持形成半开区间 ``[t_i, t_{i+1})``，温度取
   样本 i 的值；间隔超出阈值视为数据空洞，期间不计时；最后一个样本不形成区间；
4. 样本温度超出 ``[min_temperature, max_temperature]``（闭区间）时，该前值保持
   区间成为越界区间：高于上限为高温（``high``），低于下限为低温（``low``），
   偏差为超出阈值的度数（恒大于 0）；
5. 将越界区间与保管人区间求交，跨交接点（保管人区间边界）必须拆分；
   落在任何保管人区间之外的部分计入未归属（``custodian=None``）；
6. 按保管人汇总高温 / 低温秒数（按首次到任顺序排列），并给出全链及每个保管人
   的最大偏差（可追溯到样本下标）与未归属时长。

所有输出分段按时间升序排列；链本身存在状态机违规不影响审计返回 200，
违规情况只体现在 ``validation`` 中。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from .models import (
    CustodianTemperatureSummary,
    DataHole,
    Event,
    EventResult,
    EventType,
    MaxDeviation,
    TemperatureAuditRequest,
    TemperatureAuditResponse,
    TemperatureSample,
    TemperatureViolationSegment,
)
from .validator import validate_events


class TemperatureAuditError(ValueError):
    """审计入参的语义错误（在路由层转换为 HTTP 422）。"""


@dataclass(frozen=True)
class _CustodianInterval:
    """一段保管人半开区间 [start, end)；end 为 None 表示延伸至正无穷。"""

    start: datetime
    end: Optional[datetime]
    custodian: str


@dataclass(frozen=True)
class _HeldInterval:
    """一段前值保持区间 [start, end)，温度取左端样本的值。"""

    start: datetime
    end: datetime
    sample_index: int
    temperature: float


def _derive_custodian_intervals(
    events: List[Event], event_results: List[EventResult]
) -> List[_CustodianInterval]:
    """从 status=ok 的 seal/reseal/handover 事件生成保管人半开区间。

    每个有效到任事件开启一段区间，下一个有效到任事件将其闭合；
    最后一个区间延伸至正无穷。unseal 与违规事件不产生区间。
    """
    arrivals: List[Tuple[datetime, str]] = []
    for event, result in zip(events, event_results):
        if result.status != "ok":
            continue
        if event.type not in (EventType.SEAL, EventType.RESEAL, EventType.HANDOVER):
            continue
        arrivals.append((event.timestamp, event.custodian))  # type: ignore[arg-type]

    intervals: List[_CustodianInterval] = []
    for k, (start, custodian) in enumerate(arrivals):
        end = arrivals[k + 1][0] if k + 1 < len(arrivals) else None
        intervals.append(_CustodianInterval(start=start, end=end, custodian=custodian))
    return intervals


def _ensure_samples_strictly_increasing(samples: List[TemperatureSample]) -> None:
    """温度样本时间戳必须严格递增；混用时区导致无法比较时同样报 422。"""
    for index, (prev, cur) in enumerate(zip(samples, samples[1:]), start=1):
        try:
            not_increasing = cur.timestamp <= prev.timestamp
        except TypeError:
            raise TemperatureAuditError(
                "温度样本时间戳时区信息不一致（不能混用带时区与不带时区的时间）"
            )
        if not_increasing:
            raise TemperatureAuditError(
                "温度样本时间戳必须严格递增："
                f"样本 {index} 的 {cur.timestamp.isoformat()} "
                f"未晚于前一样本的 {prev.timestamp.isoformat()}"
            )


def _ensure_comparable(
    samples: List[TemperatureSample], intervals: List[_CustodianInterval]
) -> None:
    """样本与保管人区间端点混用带时区与不带时区的时间无法比较，统一报 422。"""
    values: List[datetime] = [sample.timestamp for sample in samples]
    # 区间 end 均为后续到任事件的时间戳，已包含在各 start 中（末段 end 为 None）
    values.extend(interval.start for interval in intervals)
    if not values:
        return
    aware = values[0].tzinfo is not None and values[0].utcoffset() is not None
    for value in values[1:]:
        value_aware = value.tzinfo is not None and value.utcoffset() is not None
        if value_aware != aware:
            raise TemperatureAuditError(
                "时间戳时区信息不一致（不能混用带时区与不带时区的时间）"
            )


def _build_held_intervals(
    samples: List[TemperatureSample], max_gap_seconds: float
) -> Tuple[List[_HeldInterval], List[DataHole]]:
    """把相邻样本展开为前值保持区间；间隔超过阈值的记为数据空洞（不计时）。"""
    held: List[_HeldInterval] = []
    holes: List[DataHole] = []
    for i in range(len(samples) - 1):
        start = samples[i].timestamp
        end = samples[i + 1].timestamp
        gap = (end - start).total_seconds()
        if gap <= max_gap_seconds:
            held.append(
                _HeldInterval(
                    start=start,
                    end=end,
                    sample_index=i,
                    temperature=samples[i].temperature,
                )
            )
        else:
            holes.append(
                DataHole(
                    start=start,
                    end=end,
                    seconds=gap,
                    prev_sample_index=i,
                    next_sample_index=i + 1,
                )
            )
    return held, holes


def _classify(
    temperature: float, min_temperature: float, max_temperature: float
) -> Optional[Tuple[str, float]]:
    """判定样本温度是否越界，返回 (kind, deviation)；在允许范围内返回 None。"""
    if temperature > max_temperature:
        return "high", temperature - max_temperature
    if temperature < min_temperature:
        return "low", min_temperature - temperature
    return None


def _split_by_custodian(
    start: datetime, end: datetime, intervals: List[_CustodianInterval]
) -> List[Tuple[datetime, datetime, Optional[str]]]:
    """把越界区间 [start, end) 与保管人区间求交，跨交接点拆分。

    返回 ``[(start, end, custodian), ...]``，按时间升序、无零长度分段；
    落在任何保管人区间之外的部分 custodian 为 None（未归属）。
    保管人区间自首个有效到任事件起连续延伸至正无穷，因此未归属部分
    只会出现在首个到任事件之前（或没有任何有效到任事件时）。
    """
    pieces: List[Tuple[datetime, datetime, Optional[str]]] = []
    cursor = start
    for interval in intervals:
        if cursor >= end:
            break
        if interval.end is not None and interval.end <= cursor:
            continue
        if interval.start >= end:
            break
        if cursor < interval.start:
            # 首个保管人到任前的未归属部分
            pieces.append((cursor, interval.start, None))
            cursor = interval.start
        overlap_end = end if interval.end is None else min(end, interval.end)
        if cursor < overlap_end:
            pieces.append((cursor, overlap_end, interval.custodian))
            cursor = overlap_end
    if cursor < end:
        # 没有任何保管人区间时，整段越界均未归属
        pieces.append((cursor, end, None))
    return pieces


def _better_max(
    current: Optional[MaxDeviation], candidate: MaxDeviation
) -> MaxDeviation:
    """取偏差更大者；并列时保留先出现（样本下标更小）的记录。"""
    if current is None or candidate.deviation > current.deviation:
        return candidate
    return current


def audit_temperature(request: TemperatureAuditRequest) -> TemperatureAuditResponse:
    """校验事件链并按保管人归集越界温度时长。"""
    validation = validate_events(request.events)
    _ensure_samples_strictly_increasing(request.samples)
    intervals = _derive_custodian_intervals(request.events, validation.events)
    _ensure_comparable(request.samples, intervals)

    held, holes = _build_held_intervals(request.samples, request.max_gap_seconds)

    # 保管人汇总表：按首次到任顺序排列（同一保管人多次到任合并汇总）
    custodian_order: List[str] = []
    custodian_stats: dict[str, dict] = {}
    for interval in intervals:
        if interval.custodian not in custodian_stats:
            custodian_stats[interval.custodian] = {
                "high": 0.0,
                "low": 0.0,
                "max_deviation": None,
            }
            custodian_order.append(interval.custodian)

    violations: List[TemperatureViolationSegment] = []
    unattributed_high = 0.0
    unattributed_low = 0.0
    overall_max: Optional[MaxDeviation] = None

    for interval in held:  # 前值保持区间按样本顺序产生，本身即时间升序
        classified = _classify(
            interval.temperature, request.min_temperature, request.max_temperature
        )
        if classified is None:
            continue
        kind, deviation = classified
        candidate = MaxDeviation(
            sample_index=interval.sample_index,
            kind=kind,
            temperature=interval.temperature,
            deviation=deviation,
        )
        overall_max = _better_max(overall_max, candidate)

        for piece_start, piece_end, custodian in _split_by_custodian(
            interval.start, interval.end, intervals
        ):
            seconds = (piece_end - piece_start).total_seconds()
            violations.append(
                TemperatureViolationSegment(
                    custodian=custodian,
                    kind=kind,
                    start=piece_start,
                    end=piece_end,
                    seconds=seconds,
                    sample_index=interval.sample_index,
                    temperature=interval.temperature,
                    deviation=deviation,
                )
            )
            if custodian is None:
                if kind == "high":
                    unattributed_high += seconds
                else:
                    unattributed_low += seconds
            else:
                stats = custodian_stats[custodian]
                stats["high" if kind == "high" else "low"] += seconds
                stats["max_deviation"] = _better_max(stats["max_deviation"], candidate)

    custodians = [
        CustodianTemperatureSummary(
            custodian=name,
            high_seconds=custodian_stats[name]["high"],
            low_seconds=custodian_stats[name]["low"],
            max_deviation=custodian_stats[name]["max_deviation"],
        )
        for name in custodian_order
    ]

    return TemperatureAuditResponse(
        validation=validation,
        min_temperature=request.min_temperature,
        max_temperature=request.max_temperature,
        max_gap_seconds=request.max_gap_seconds,
        custodians=custodians,
        violations=violations,
        unattributed_high_seconds=unattributed_high,
        unattributed_low_seconds=unattributed_low,
        unattributed_seconds=unattributed_high + unattributed_low,
        max_deviation=overall_max,
        data_holes=holes,
    )
