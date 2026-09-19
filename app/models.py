"""请求 / 响应数据模型。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class EventType(str, Enum):
    """封签链支持的四种事件类型。"""

    SEAL = "seal"          # 封装：首次（或开封后）以封签封装展品
    HANDOVER = "handover"  # 交接：保管人变更
    UNSEAL = "unseal"      # 开封：拆除当前封签
    RESEAL = "reseal"      # 重封：开封后使用新封签重新封装


class Event(BaseModel):
    """单个封签链事件。timestamp 使用 ISO 8601 格式。"""

    type: EventType
    timestamp: datetime
    seal_id: Optional[str] = Field(default=None, description="封签编号，seal/reseal 事件必填")
    custodian: Optional[str] = Field(default=None, description="保管人，seal/reseal/handover 事件必填")


class ValidateRequest(BaseModel):
    """校验请求：按时间排列的事件列表。"""

    events: List[Event] = Field(default_factory=list)


class EventResult(BaseModel):
    """逐事件判定结果。"""

    index: int
    type: EventType
    status: str = Field(description="ok 或 violation")
    reason: Optional[str] = Field(default=None, description="违规原因，status 为 violation 时给出")
    cross_chain_conflict: bool = Field(
        default=False,
        description="该有效 seal/reseal 事件是否与其他链占用同一 seal_id；单链校验接口恒为 false",
    )


class Violation(BaseModel):
    """违规事件的位置与原因。"""

    index: int
    reason: str


class ValidateResponse(BaseModel):
    """校验响应。"""

    valid: bool = Field(description="整条链是否无任何违规")
    sealed: bool = Field(description="处理完所有有效事件后是否处于封装状态")
    final_custodian: Optional[str] = Field(description="最终保管人")
    current_seal: Optional[str] = Field(description="当前有效封签编号，未封装时为 null")
    events: List[EventResult] = Field(description="逐事件状态")
    violations: List[Violation] = Field(description="所有违规事件的位置与原因")


# ---------------------------------------------------------------------------
# 批量封签占用检查（/api/v1/seal-chain/batch-validate）
# ---------------------------------------------------------------------------


class ChainEvents(BaseModel):
    """批次中的一条事件链。"""

    chain_id: str = Field(description="链标识，同一批次内不可重复")
    events: List[Event] = Field(default_factory=list)


class BatchValidateRequest(BaseModel):
    """批量校验请求：按输入顺序排列的多条带 chain_id 的事件链。"""

    chains: List[ChainEvents] = Field(default_factory=list)

    @model_validator(mode="after")
    def _chain_id_must_be_unique(self) -> "BatchValidateRequest":
        seen: set[str] = set()
        for chain in self.chains:
            if chain.chain_id in seen:
                raise ValueError(f"批次内 chain_id 不可重复：{chain.chain_id}")
            seen.add(chain.chain_id)
        return self


class CrossChainConflict(BaseModel):
    """跨链封签占用冲突项：对应一个被标为 cross_chain_conflict 的事件。"""

    seal_id: str = Field(description="被不同链重复占用的封签编号")
    chain_id: str = Field(description="冲突事件所属链")
    event_index: int = Field(description="冲突事件在该链中的下标（从 0 开始）")
    first_chain_id: str = Field(description="按批次输入顺序首次占用该 seal_id 的链")


class BatchChainResult(ValidateResponse):
    """单链结果：结构与单链校验接口完全一致，另附 chain_id。

    冲突仅会把对应有效事件的 cross_chain_conflict 置为 true，
    valid / sealed / violations 等各链自身字段不因跨链冲突改变。
    """

    chain_id: str = Field(description="链标识，回显请求中的 chain_id")


class BatchChainSummary(BaseModel):
    """批次汇总中的单链条目，按 chain_id 稳定排序。"""

    chain_id: str
    valid: bool = Field(description="该链自身校验是否通过（不受跨链冲突影响）")
    conflicts: int = Field(description="该链被标为 cross_chain_conflict 的事件数")


class BatchValidateResponse(BaseModel):
    """批量校验响应。"""

    valid: bool = Field(description="批次是否通过：各链均无违规且不存在任何跨链冲突")
    chains: List[BatchChainResult] = Field(description="各链原校验结果（按输入顺序）")
    conflicts: List[CrossChainConflict] = Field(
        default_factory=list,
        description="跨链封签占用冲突项，按首次占用链输入顺序排列",
    )
    summary: List[BatchChainSummary] = Field(
        default_factory=list,
        description="按 chain_id 稳定排序的批次汇总",
    )


# ---------------------------------------------------------------------------
# 离线片段合并重建（/api/v1/seal-chain/reconcile-validate）
# ---------------------------------------------------------------------------


class ReconcileEvent(Event):
    """离线片段中的事件：在单链事件基础上增加链式标识。"""

    event_id: str = Field(min_length=1, description="事件唯一标识，跨片段判重依据")
    prev_event_id: Optional[str] = Field(
        default=None, description="前驱事件标识；null 表示链头事件"
    )


class Fragment(BaseModel):
    """同一 chain_id 的一个离线片段，其编号为在请求中的下标（从 0 开始）。"""

    events: List[ReconcileEvent] = Field(default_factory=list)


class ReconcileRequest(BaseModel):
    """合并重建请求：同一 chain_id 的多个离线片段。"""

    chain_id: str = Field(description="链标识，所有片段必须属于同一条链")
    fragments: List[Fragment] = Field(default_factory=list)


class EventVariant(BaseModel):
    """同一 event_id 的一种内容变体及其来源片段。"""

    fragments: List[int] = Field(description="该变体出现的片段编号（从 0 开始，升序去重）")
    event: ReconcileEvent = Field(description="该变体的事件内容")


class EventConflict(BaseModel):
    """同一 event_id 出现内容分歧时的冲突明细。"""

    event_id: str
    differing_fields: List[str] = Field(description="各变体间存在分歧的字段名")
    variants: List[EventVariant] = Field(description="全部内容变体，按首次出现顺序排列")


class MissingPredecessor(BaseModel):
    """缺失前驱：事件的 prev_event_id 在无冲突事件集中不存在。"""

    event_id: str
    prev_event_id: str


class Fork(BaseModel):
    """分叉：多个事件以同一事件为前驱。"""

    prev_event_id: str
    event_ids: List[str] = Field(description="以该事件为前驱的全部事件，按首次出现顺序")


class StructureReport(BaseModel):
    """按 prev_event_id 重建唯一序列时的结构诊断（仅基于无冲突事件）。"""

    heads: List[str] = Field(description="prev_event_id 为 null 的链头事件；必须恰好一个")
    missing_predecessors: List[MissingPredecessor] = Field(
        default_factory=list, description="前驱不在无冲突事件集中的事件"
    )
    forks: List[Fork] = Field(default_factory=list, description="同一前驱被多个事件引用")
    cycles: List[List[str]] = Field(
        default_factory=list,
        description="环：环内每个事件的 prev_event_id 依次指向列表下一事件，末尾指回开头",
    )


class EventSource(BaseModel):
    """一个 event_id 的来源片段编号列表。"""

    event_id: str
    fragments: List[int] = Field(description="该事件出现的片段编号（从 0 开始，升序去重）")


class ReconcileResponse(BaseModel):
    """合并重建响应。"""

    chain_id: str = Field(description="链标识，回显请求中的 chain_id")
    reconciled: bool = Field(
        description="是否成功重建唯一序列：无内容冲突、恰好一个链头、无缺失前驱、无分叉、无环"
    )
    conflicts: List[EventConflict] = Field(
        default_factory=list, description="按 event_id 判重发现的内容冲突明细"
    )
    structure: StructureReport = Field(description="基于无冲突事件的结构诊断")
    sequence: Optional[List[str]] = Field(
        description="重建成功时的事件序列（event_id 顺序），否则为 null"
    )
    validation: Optional[ValidateResponse] = Field(
        description="仅重建成功时调用现有单链校验器的结果；index 对应 sequence 中的位置"
    )
    event_sources: List[EventSource] = Field(
        default_factory=list,
        description="每个 event_id 的来源片段编号列表，按首次出现顺序排列",
    )


# ---------------------------------------------------------------------------
# 开封暴露审计（/api/v1/seal-chain/exposure-audit）
# ---------------------------------------------------------------------------


class AllowedWindow(BaseModel):
    """授权开窗窗口：半开时间区间 [start, end)，start 必须严格早于 end。"""

    start: datetime = Field(description="窗口起点（含）")
    end: datetime = Field(description="窗口终点（不含）")

    @model_validator(mode="after")
    def _start_before_end(self) -> "AllowedWindow":
        try:
            start_before_end = self.start < self.end
        except TypeError:
            raise ValueError("窗口起止时间的时区信息不一致（不能混用带时区与不带时区的时间）")
        if not start_before_end:
            raise ValueError("allowed_windows 的 start 必须早于 end")
        return self


class ExposureAuditRequest(BaseModel):
    """开封暴露审计请求：事件序列、审计截止时间与授权开窗窗口。"""

    events: List[Event] = Field(default_factory=list)
    audit_until: datetime = Field(description="审计截止时间；末次开封未重封时暴露区间延伸至此")
    allowed_windows: List[AllowedWindow] = Field(
        default_factory=list,
        description="授权开窗窗口（半开 [start, end)），允许乱序、嵌套或首尾相接；"
        "相交或首尾相接的窗口先合并再参与区间差",
    )


class ExposureSegment(BaseModel):
    """一段（已覆盖或未覆盖的）半开时间区间 [start, end)。"""

    start: datetime
    end: datetime
    seconds: float = Field(description="区间时长（秒），等于 end - start")
    unseal_index: int = Field(description="产生该段的有效开封（unseal）事件下标（从 0 开始）")
    reseal_index: Optional[int] = Field(
        description="对应的有效重封（reseal）事件下标；末次开封未重封而延伸至 audit_until 时为 null"
    )


class ExposureAuditResponse(BaseModel):
    """开封暴露审计响应：保留原单链校验结果，另附区间差明细。"""

    audit_until: datetime = Field(description="回显请求中的审计截止时间")
    validation: ValidateResponse = Field(
        description="现有单链校验器的原始结果；仅以其中 status=ok 的 unseal/reseal 推导暴露区间"
    )
    covered: List[ExposureSegment] = Field(
        default_factory=list,
        description="落在授权窗口内的已覆盖分段（半开区间），按时间升序",
    )
    uncovered: List[ExposureSegment] = Field(
        default_factory=list,
        description="落在授权窗口外的未覆盖分段（半开区间），按时间升序",
    )
    unauthorized_seconds: float = Field(
        default=0.0, description="未授权暴露总秒数，即全部 uncovered 分段秒数之和"
    )


# ---------------------------------------------------------------------------
# 超长事件链分段校验（/api/v1/seal-chain/validate-chunk）
# ---------------------------------------------------------------------------


class ChunkValidateRequest(BaseModel):
    """分段校验请求：首段不带 continuation_token，后续段携带上一响应返回的令牌。"""

    events: List[Event] = Field(default_factory=list, description="本段事件，按时间排列")
    continuation_token: Optional[str] = Field(
        default=None,
        description="上一响应返回的续传令牌（不透明字符串）；首段不传或为 null",
    )


class ChunkSummary(BaseModel):
    """全链累计摘要（含本段）。"""

    event_count: int = Field(description="全链已累计处理的事件总数（含违规事件）")
    violation_count: int = Field(description="全链累计违规事件数")
    valid: bool = Field(description="全链截至目前是否无任何违规")
    sealed: bool = Field(description="处理完所有有效事件后当前是否处于封装状态")
    custodian: Optional[str] = Field(description="当前保管人")
    current_seal: Optional[str] = Field(description="当前有效封签编号，未封装时为 null")


class ChunkValidateResponse(BaseModel):
    """分段校验响应：本段结果 + 全链累计摘要 + 下一段续传令牌。"""

    events: List[EventResult] = Field(
        description="本段逐事件判定结果；index 为全链偏移（从 0 开始），"
        "与把所有分段拼接后调用单链接口完全一致"
    )
    violations: List[Violation] = Field(
        description="本段违规事件的位置（全链偏移）与原因"
    )
    summary: ChunkSummary = Field(description="全链累计摘要（含本段）")
    continuation_token: str = Field(
        description="下一段请求需携带的续传令牌（不透明字符串，带签名）"
    )


# ---------------------------------------------------------------------------
# 交接计划符合性审计（/api/v1/seal-chain/handover-plan-audit）
# ---------------------------------------------------------------------------


class HandoverPlanNode(BaseModel):
    """计划节点：某保管人应在半开交接窗口 [window_start, window_end) 内到任。

    窗口为闭起点 / 开终点：window_start <= t < window_end 计为准时；
    window_start 必须严格早于 window_end。
    """

    custodian: str = Field(min_length=1, description="计划到任保管人")
    window_start: datetime = Field(description="计划窗口起点（含）")
    window_end: datetime = Field(description="计划窗口终点（不含）")

    @model_validator(mode="after")
    def _start_before_end(self) -> "HandoverPlanNode":
        try:
            start_before_end = self.window_start < self.window_end
        except TypeError:
            raise ValueError("窗口起止时间的时区信息不一致（不能混用带时区与不带时区的时间）")
        if not start_before_end:
            raise ValueError("window_start 必须早于 window_end")
        return self


class HandoverPlanAuditRequest(BaseModel):
    """交接计划符合性审计请求：事件序列与按顺序排列的计划节点。"""

    events: List[Event] = Field(default_factory=list)
    plan: List[HandoverPlanNode] = Field(
        default_factory=list,
        description="按计划顺序排列的到任节点；匹配必须保持该顺序且不交叉",
    )


class HandoverPlanNodeResult(BaseModel):
    """单个计划节点的匹配结果。"""

    index: int = Field(description="计划节点下标（从 0 开始，对应请求 plan 中的位置）")
    custodian: str = Field(description="回显计划保管人")
    window_start: datetime = Field(description="回显窗口起点")
    window_end: datetime = Field(description="回显窗口终点")
    status: str = Field(description="matched（已匹配实际记录）或 missing（无实际到任记录）")
    event_index: Optional[int] = Field(
        default=None,
        description="匹配到的实际记录所在事件下标（从 0 开始，全链下标）；missing 时为 null",
    )
    actual_timestamp: Optional[datetime] = Field(
        default=None, description="匹配事件的实际时间；missing 时为 null"
    )
    timing: Optional[str] = Field(
        default=None,
        description="实际到任相对窗口的状态：early（早于 window_start）/ "
        "on_time（window_start <= t < window_end）/ late（不早于 window_end）；missing 时为 null",
    )
    deviation_seconds: Optional[float] = Field(
        default=None,
        description="实际时间偏离对应窗口的秒数：early 为 window_start - t，"
        "late 为 t - window_end，on_time 为 0；missing 时为 null",
    )


class UnexpectedArrival(BaseModel):
    """未被任何计划节点匹配的实际到任记录。"""

    event_index: int = Field(description="该记录在事件链中的下标（从 0 开始）")
    type: EventType = Field(description="来源事件类型：seal / reseal / handover")
    timestamp: datetime = Field(description="实际到任时间")
    custodian: str = Field(description="实际到任保管人")


class HandoverPlanSummary(BaseModel):
    """交接计划符合性审计汇总计数。"""

    matched: int = Field(description="已匹配的计划节点数")
    missing: int = Field(description="未匹配的计划节点数")
    unexpected: int = Field(description="未匹配的实际到任记录数")
    early: int = Field(description="已匹配记录中早到（early）的数量")
    on_time: int = Field(description="已匹配记录中准时（on_time）的数量")
    late: int = Field(description="已匹配记录中迟到（late）的数量")


class HandoverPlanAuditResponse(BaseModel):
    """交接计划符合性审计响应：保留原单链校验结果，另附全局最优匹配明细。"""

    validation: ValidateResponse = Field(
        description="现有单链校验器的原始结果；仅以其中 status=ok 的 seal/reseal/handover "
        "提取实际到任记录"
    )
    plan_results: List[HandoverPlanNodeResult] = Field(
        description="每个计划节点的匹配结果，按请求 plan 顺序"
    )
    unexpected: List[UnexpectedArrival] = Field(
        default_factory=list,
        description="未匹配任何计划节点的实际到任记录，按事件下标升序",
    )
    summary: HandoverPlanSummary = Field(description="各状态数量汇总")
