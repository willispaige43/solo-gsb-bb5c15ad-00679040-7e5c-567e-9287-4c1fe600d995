"""封签链校验 API 入口。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .batch import validate_batch
from .chunk import ChunkValidateError, validate_chunk
from .exposure import ExposureAuditError, audit_exposure
from .handover import HandoverPlanAuditError, audit_handover_plan
from .models import (
    BatchValidateRequest,
    BatchValidateResponse,
    ChunkValidateRequest,
    ChunkValidateResponse,
    ExposureAuditRequest,
    ExposureAuditResponse,
    HandoverPlanAuditRequest,
    HandoverPlanAuditResponse,
    ReconcileRequest,
    ReconcileResponse,
    TemperatureAuditRequest,
    TemperatureAuditResponse,
    ValidateRequest,
    ValidateResponse,
)
from .reconcile import reconcile
from .temperature import TemperatureAuditError, audit_temperature
from .validator import validate_events

app = FastAPI(
    title="封签链校验 API",
    description="用于展品运输交接场景的封签链状态机校验服务",
    version="1.6.0",
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    """健康检查。"""
    return {"status": "ok"}


@app.post(
    "/api/v1/seal-chain/validate",
    response_model=ValidateResponse,
    tags=["seal-chain"],
    summary="校验封签链事件序列",
)
def validate(request: ValidateRequest) -> ValidateResponse:
    """按状态机逐事件校验，返回最终保管人、当前封签、逐事件状态与全部违规。"""
    return validate_events(request.events)


@app.post(
    "/api/v1/seal-chain/batch-validate",
    response_model=BatchValidateResponse,
    tags=["seal-chain"],
    summary="批量封签占用检查（跨链冲突）",
)
def batch_validate(request: BatchValidateRequest) -> BatchValidateResponse:
    """先分别校验每条链，再按输入顺序建立全批次封签占用表，标记跨链占用冲突。"""
    return validate_batch(request)


@app.post(
    "/api/v1/seal-chain/reconcile-validate",
    response_model=ReconcileResponse,
    tags=["seal-chain"],
    summary="合并同一链的多个离线片段，重建唯一序列并校验",
)
def reconcile_validate(request: ReconcileRequest) -> ReconcileResponse:
    """按 event_id 去重（内容分歧返回冲突明细），按 prev_event_id 重建唯一序列
    （报告多头 / 缺失前驱 / 分叉 / 环，不按时间戳猜序），仅重建成功时调用
    现有单链校验器，并返回每个事件的来源片段编号列表。"""
    return reconcile(request)


@app.post(
    "/api/v1/seal-chain/exposure-audit",
    response_model=ExposureAuditResponse,
    tags=["seal-chain"],
    summary="开封暴露审计：开封区间相对授权开窗窗口的区间差",
)
def exposure_audit(request: ExposureAuditRequest) -> ExposureAuditResponse:
    """保留原单链校验结果；仅以其中 status=ok 的 unseal/reseal 推导半开开封区间
    [start,end)（末次开封未重封延伸至 audit_until），先合并相交或首尾相接的
    授权窗口，再逐段做区间差，返回 covered / uncovered 分段、未授权总秒数及
    对应事件下标。audit_until 早于最后一个有效事件时间时返回 422。"""
    try:
        return audit_exposure(request)
    except ExposureAuditError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/api/v1/seal-chain/validate-chunk",
    response_model=ChunkValidateResponse,
    tags=["seal-chain"],
    summary="分段校验超长事件链（续传令牌）",
)
def chunk_validate(request: ChunkValidateRequest) -> ChunkValidateResponse:
    """首段不带 continuation_token，后续段携带上一响应返回的令牌，从上一段结束
    时的状态机状态继续校验；逐事件判定与把所有分段拼接后调用单链接口完全一致，
    响应下标为全链偏移。令牌完整保存封装状态、保管人、当前及已用封签、最近有效
    时间与累计事件数，并使用环境变量 SEAL_CHAIN_TOKEN_SECRET 做确定性签名；
    任何篡改、版本不支持或状态字段非法均返回 422，且不处理本段事件。
    响应返回本段结果、全链累计摘要与下一令牌。"""
    try:
        return validate_chunk(request)
    except ChunkValidateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/api/v1/seal-chain/handover-plan-audit",
    response_model=HandoverPlanAuditResponse,
    tags=["seal-chain"],
    summary="交接计划符合性审计：实际到任记录与计划窗口的全局最优匹配",
)
def handover_plan_audit(request: HandoverPlanAuditRequest) -> HandoverPlanAuditResponse:
    """保留原单链校验结果；仅从 status=ok 的 seal/reseal/handover 提取实际到任
    记录（保留全链事件下标），按保管人相同且顺序不交叉做一对一全局最优匹配：
    先最大化匹配数，再最小化实际时间偏离对应窗口的总秒数，仍并列时取事件下标
    序列字典序最小的方案（动态规划，不做逐项贪心）。返回每个计划节点的
    matched/missing、匹配事件下标及 early/on_time/late，未匹配实际记录列为
    unexpected，并汇总各状态数量。窗口起止时间混用带时区与不带时区时间时返回 422。"""
    try:
        return audit_handover_plan(request)
    except HandoverPlanAuditError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/api/v1/seal-chain/temperature-audit",
    response_model=TemperatureAuditResponse,
    tags=["seal-chain"],
    summary="温控责任审计：越界温度时长按保管人归属",
)
def temperature_audit(request: TemperatureAuditRequest) -> TemperatureAuditResponse:
    """保留原单链校验结果；仅以其中 status=ok 的 seal/reseal/handover 生成保管人
    半开区间 [start,end)（末个区间向右开放）。温度样本时间戳必须严格递增，
    相邻样本间隔不超过 max_gap_seconds 时按前值保持形成 [t_i,t_{i+1})，
    超出视为数据空洞且不计时；起始样本温度越出 [min_temperature,
    max_temperature] 的区间为越界区间。将越界区间与保管人区间求交，跨交接点
    必须拆分，按保管人汇总高温/低温秒数与各自最大偏差，返回逐段明细（含起始
    样本下标）、全局最大偏差与未归属总秒数。混用时区、样本非严格递增、
    min_temperature 大于 max_temperature 或 max_gap_seconds 非正数时返回 422。"""
    try:
        return audit_temperature(request)
    except TemperatureAuditError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
