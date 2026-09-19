"""开封暴露审计。

处理流程：
1. 调用现有单链校验器（``validate_events``）得到原校验结果并原样保留；
2. 仅以其中 ``status=ok`` 的 unseal / reseal 事件推导半开开封区间
   ``[start, end)``：有效 unseal 开启一段，下一个有效 reseal 闭合该段；
   末次开封后没有有效重封时，区间末点延伸至 ``audit_until``
   （``end == start`` 的退化区间不产生任何分段、不计时长）；
3. 对授权窗口（``allowed_windows``）按起点排序后合并相交或首尾相接
   （``end == next.start``）的窗口，嵌套窗口随合并自然消解，乱序输入不影响结果；
4. 对每段开封区间与合并后的授权窗口做区间差：落在窗口内的部分计入
   ``covered``，其余计入 ``uncovered``，并给出未授权总秒数以及每段对应的
   有效开封 / 重封事件下标。

所有输出分段按时间升序排列；一段开封跨多个授权窗口时，被窗口切出的各
covered / uncovered 分段同样保持时间升序，结果与窗口输入顺序无关。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from .models import (
    AllowedWindow,
    Event,
    EventResult,
    EventType,
    ExposureAuditRequest,
    ExposureAuditResponse,
    ExposureSegment,
)
from .validator import validate_events


class ExposureAuditError(ValueError):
    """审计入参的语义错误（在路由层转换为 HTTP 422）。"""


@dataclass(frozen=True)
class _OpenInterval:
    """一段半开开封区间 [start, end) 及其来源事件下标。"""

    start: datetime
    end: datetime
    unseal_index: int
    reseal_index: Optional[int]  # 延伸至 audit_until 时为 None


def _derive_open_intervals(
    events: List[Event], event_results: List[EventResult], audit_until: datetime
) -> List[_OpenInterval]:
    """从 status=ok 的 unseal/reseal 事件推导半开开封区间。

    有效 unseal 开启一段；其后第一个有效 reseal 将其闭合；遍历结束仍开启的
    区间延伸至 audit_until。其余事件（seal/handover/违规事件）均不影响配对。
    """
    intervals: List[_OpenInterval] = []
    open_start: Optional[datetime] = None
    open_index: Optional[int] = None

    for event, result in zip(events, event_results):
        if result.status != "ok":
            continue
        if event.type is EventType.UNSEAL:
            if open_start is None:  # 状态机保证 unseal 前必然已封装
                open_start = event.timestamp
                open_index = result.index
        elif event.type is EventType.RESEAL and open_start is not None:
            intervals.append(
                _OpenInterval(
                    start=open_start,
                    end=event.timestamp,
                    unseal_index=open_index,  # type: ignore[arg-type]
                    reseal_index=result.index,
                )
            )
            open_start = None
            open_index = None

    if open_start is not None:
        intervals.append(
            _OpenInterval(
                start=open_start,
                end=audit_until,
                unseal_index=open_index,  # type: ignore[arg-type]
                reseal_index=None,
            )
        )

    return intervals


def _merge_windows(windows: List[AllowedWindow]) -> List[Tuple[datetime, datetime]]:
    """按起点排序后合并相交或首尾相接的窗口（乱序 / 嵌套结果稳定）。"""
    ordered = sorted(windows, key=lambda w: (w.start, w.end))
    merged: List[Tuple[datetime, datetime]] = []
    for window in ordered:
        if not merged or window.start > merged[-1][1]:
            # 与最后一段不相交且非首尾相接（半开区间下 start == end 可合并）
            merged.append((window.start, window.end))
        elif window.end > merged[-1][1]:
            merged[-1] = (merged[-1][0], window.end)
    return merged


def _segment(start: datetime, end: datetime, interval: _OpenInterval) -> ExposureSegment:
    """构造一个带来源开封 / 重封事件下标的分段。"""
    return ExposureSegment(
        start=start,
        end=end,
        seconds=(end - start).total_seconds(),
        unseal_index=interval.unseal_index,
        reseal_index=interval.reseal_index,
    )


def _subtract(
    interval: _OpenInterval, merged_windows: List[Tuple[datetime, datetime]]
) -> Tuple[List[ExposureSegment], List[ExposureSegment]]:
    """对一段开封区间做区间差，返回 (covered, uncovered)，各自按时间升序。"""
    covered: List[ExposureSegment] = []
    uncovered: List[ExposureSegment] = []
    cursor = interval.start

    for win_start, win_end in merged_windows:
        if win_end <= cursor or win_start >= interval.end:
            continue  # 窗口在游标之前或完全落在区间之后
        overlap_start = max(cursor, win_start)
        overlap_end = min(interval.end, win_end)
        if overlap_start >= overlap_end:
            continue

        if overlap_start > cursor:
            uncovered.append(_segment(cursor, overlap_start, interval))
        covered.append(_segment(overlap_start, overlap_end, interval))
        cursor = overlap_end
        if cursor >= interval.end:
            break

    if cursor < interval.end:
        uncovered.append(_segment(cursor, interval.end, interval))

    return covered, uncovered


def _ensure_comparable(values: List[datetime]) -> None:
    """混用带时区与不带时区的时间无法比较，统一报 422。"""
    if not values:
        return
    aware = values[0].tzinfo is not None and values[0].utcoffset() is not None
    for value in values[1:]:
        value_aware = value.tzinfo is not None and value.utcoffset() is not None
        if value_aware != aware:
            raise ExposureAuditError(
                "时间戳时区信息不一致（不能混用带时区与不带时区的时间）"
            )


def audit_exposure(request: ExposureAuditRequest) -> ExposureAuditResponse:
    """校验事件链并计算开封区间相对授权窗口的暴露情况。"""
    validation = validate_events(request.events)

    # audit_until 不得早于最后一个有效事件时间（违规事件不推进时间基准）
    last_valid = None
    for event, result in zip(request.events, validation.events):
        if result.status == "ok":
            last_valid = event.timestamp

    timestamps = [request.audit_until]
    if last_valid is not None:
        timestamps.append(last_valid)
    timestamps.extend(w.start for w in request.allowed_windows)
    timestamps.extend(w.end for w in request.allowed_windows)
    _ensure_comparable(timestamps)

    if last_valid is not None and request.audit_until < last_valid:
        raise ExposureAuditError(
            "audit_until 早于最后一个有效事件时间："
            f"{request.audit_until.isoformat()} 早于 {last_valid.isoformat()}"
        )

    intervals = _derive_open_intervals(
        request.events, validation.events, request.audit_until
    )
    merged_windows = _merge_windows(request.allowed_windows)

    covered: List[ExposureSegment] = []
    uncovered: List[ExposureSegment] = []
    for interval in intervals:  # 区间按事件顺序产生，本身即时间升序
        seg_covered, seg_uncovered = _subtract(interval, merged_windows)
        covered.extend(seg_covered)
        uncovered.extend(seg_uncovered)

    return ExposureAuditResponse(
        audit_until=request.audit_until,
        validation=validation,
        covered=covered,
        uncovered=uncovered,
        unauthorized_seconds=sum(segment.seconds for segment in uncovered),
    )
