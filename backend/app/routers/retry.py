"""失败重试策略接口：维护重试策略，覆盖提交校验、启用配置、停用配置等动作。

重试预演相关接口：
- POST /api/retry/preview  按采集日志的错误类型与已尝试次数生成后续时间点；已有留档时断线续接；
- POST /api/retry/batches  送入失败批次，同一批次重复送入仅接纳一份；
- GET  /api/retry/queue    处理队列，查看即将执行的失败重试待办；
- GET  /api/retry/previews 预演留档，含启动时口径快照。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.retry import RetryService

router = APIRouter(prefix="/api/retry", tags=["失败重试策略"])

service = RetryService()

LIST_FIELDS = ["策略编号", "适用任务", "最大重试", "重试间隔", "退避系数", "可重试异常", "排除异常", "最终动作", "判定条件", "阈值范围", "优先级标准"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤失败重试策略列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/queue", response_model=PageResult[dict])
def list_queue(
    status: str | None = Query(default=None, description="待执行、已消费、已取消、待人工"),
) -> PageResult[dict]:
    """处理队列：即将执行的失败重试待办，按计划时间升序。"""
    items, total = service.list_queue(status=status)
    return PageResult(items=items, total=total, page=1, size=total or 1)


@router.get("/previews", response_model=PageResult[dict])
def list_previews(
    log_no: str | None = Query(default=None, description="按日志编号检索预演留档"),
) -> PageResult[dict]:
    """预演留档：每条预演都带启动时口径快照，历史重试不随新配置改排。"""
    items, total = service.list_previews(log_no=log_no)
    return PageResult(items=items, total=total, page=1, size=total or 1)


@router.post("/preview", response_model=ActionResult)
def run_preview(payload: EntryPayload) -> ActionResult:
    """生成或续接重试预演；结论回写采集日志详情，并生成处理队列待办。"""
    log_no = str(payload.values.get("日志编号") or "").strip()
    if not log_no:
        return ActionResult(ok=False, message="缺少必填字段：日志编号")
    preview, message = service.run_preview(log_no)
    if preview is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=preview)


@router.post("/batches", response_model=ActionResult)
def intake_batch(payload: EntryPayload) -> ActionResult:
    """送入失败批次推进预演；同一批次重复送入仅接纳一份，排除异常命中即停止。"""
    batch, message, duplicated = service.intake_batch(payload.values)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry={"批次": batch, "重复送入": duplicated})


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出失败重试策略清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "retry", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条重试策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重试策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条重试策略；判定条件、阈值范围、上限与优先级标准逐项校验，问题逐条列明。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="重试策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条重试策略执行提交校验、启用配置、停用配置；跨状态或校验不通过会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
