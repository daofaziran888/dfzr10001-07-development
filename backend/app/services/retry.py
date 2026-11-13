"""失败重试策略业务规则：状态流转、逐项配置校验、重试预演与失败批次接纳都收在这里。

重试预演约定：
- 按采集日志的错误类型与已尝试次数，套用策略口径生成后续重试时间点；
- 预演生成时把策略口径快照留档，历史重试只按启动时口径续排，不随新配置改排；
- 同一失败批次重复送入只接纳一份；排除异常与可重试异常冲突时以排除异常优先；
- 预演结论回写采集日志详情，并为每个待执行时间点生成处理队列待办。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from app.store import store

MODULE = "retry"
PREVIEW_MODULE = "retry_preview"
TODO_MODULE = "retry_todo"
BATCH_MODULE = "retry_batch"

# 重试规则的判定条件、阈值范围、上限与优先级标准必须逐项配置，缺一不可。
STRATEGY_FIELDS = [
    "策略编号", "适用任务", "最大重试", "重试间隔", "退避系数",
    "可重试异常", "排除异常", "最终动作", "判定条件", "阈值范围", "优先级标准",
]
REQUIRED_FIELDS = STRATEGY_FIELDS
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
ACTION_FROM = {"提交校验": "草稿", "启用配置": "待校验", "停用配置": "已启用"}
NEGATIVE_ACTIONS = ["停用配置"]

PRIORITY_STANDARD = "排除异常优先"
FINAL_ACTIONS = ["转人工处理", "告警并终止", "降级重采"]
MAX_RETRY_LIMIT = 10
TIME_FORMAT = "%Y-%m-%d %H:%M"
CODE_PATTERN = re.compile(r"^RETR-\d{4}$")
RANGE_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$")
SPLIT_PATTERN = re.compile(r"[、,，;；/\s]+")


def _split_exceptions(text: Any) -> list[str]:
    return [item for item in SPLIT_PATTERN.split(str(text or "").strip()) if item]


def _parse_time(text: Any) -> datetime | None:
    try:
        return datetime.strptime(str(text or "").strip(), TIME_FORMAT)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def validate_config(values: dict[str, Any]) -> list[str]:
    """逐项校验重试规则配置；返回全部问题，而不是只报第一项。"""
    errors: list[str] = []
    missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
    for field in missing:
        errors.append(f"缺少配置项：{field}")
    if missing:
        return errors

    if not CODE_PATTERN.match(str(values.get("策略编号")).strip()):
        errors.append("策略编号格式应为 RETR-0000")

    max_retry = _to_int(values.get("最大重试"))
    if max_retry is None or not 1 <= max_retry <= MAX_RETRY_LIMIT:
        errors.append(f"最大重试（上限）须为 1-{MAX_RETRY_LIMIT} 的整数")

    interval = _to_int(values.get("重试间隔"))
    if interval is None or interval <= 0:
        errors.append("重试间隔须为正整数分钟")

    coefficient = _to_float(values.get("退避系数"))
    if coefficient is None or coefficient < 1:
        errors.append("退避系数须为不小于 1 的数字")

    range_match = RANGE_PATTERN.match(str(values.get("阈值范围")).strip())
    if not range_match:
        errors.append("阈值范围格式应为 下限-上限（分钟），如 1-120")
    else:
        lower, upper = float(range_match.group(1)), float(range_match.group(2))
        if lower >= upper:
            errors.append("阈值范围下限须小于上限")
        elif interval is not None and not lower <= interval <= upper:
            errors.append(f"重试间隔 {interval} 分钟不在阈值范围 {range_match.group(0)} 内")

    if str(values.get("优先级标准")).strip() != PRIORITY_STANDARD:
        errors.append(f"优先级标准须配置为「{PRIORITY_STANDARD}」，冲突时排除异常优先于可重试异常")

    if str(values.get("最终动作")).strip() not in FINAL_ACTIONS:
        errors.append(f"最终动作须为：{'、'.join(FINAL_ACTIONS)}")

    if not _split_exceptions(values.get("可重试异常")):
        errors.append("可重试异常至少配置一类")
    if not _split_exceptions(values.get("排除异常")):
        errors.append("排除异常至少配置一类，无排除项时填「无」并保留配置位")
    return errors


def _snapshot(strategy: dict[str, Any]) -> dict[str, Any]:
    """把排期预演要展开的口径固化为快照，供历史重试留档。"""
    return {
        "策略编号": strategy.get("策略编号"),
        "最大重试": strategy.get("最大重试"),
        "重试间隔": strategy.get("重试间隔"),
        "退避系数": strategy.get("退避系数"),
        "可重试异常": strategy.get("可重试异常"),
        "排除异常": strategy.get("排除异常"),
        "最终动作": strategy.get("最终动作"),
        "判定条件": strategy.get("判定条件"),
        "阈值范围": strategy.get("阈值范围"),
        "优先级标准": strategy.get("优先级标准"),
        "策略状态": strategy.get("status"),
    }


def _classify(error_type: str, caliber: dict[str, Any]) -> tuple[str, bool]:
    """返回 (分类, 是否冲突)；冲突时排除异常优先于可重试异常。"""
    retryable = _split_exceptions(caliber.get("可重试异常"))
    excluded = _split_exceptions(caliber.get("排除异常"))
    in_retry = error_type in retryable
    in_exclude = error_type in excluded
    if in_exclude:
        return "排除异常", in_retry
    if in_retry:
        return "可重试异常", False
    return "未纳入", False


def _compute_points(last_failure: datetime, interval: int, coefficient: float,
                    attempted: int, max_retry: int) -> list[dict[str, Any]]:
    """从上次失败时间起累计退避，生成第 attempted+1 至 max_retry 次的时间点，不超过上限。"""
    points: list[dict[str, Any]] = []
    current = last_failure
    for order in range(attempted + 1, max_retry + 1):
        current = current + timedelta(minutes=round(interval * (coefficient ** (order - 1))))
        points.append({"次序": order, "计划时间": current.strftime(TIME_FORMAT), "状态": "待执行"})
    return points


def _next_code(rows: list[dict[str, Any]], field: str, prefix: str) -> str:
    serial = 0
    for row in rows:
        match = re.match(rf"^{prefix}-(\d+)$", str(row.get(field, "")))
        if match:
            serial = max(serial, int(match.group(1)))
    return f"{prefix}-{serial + 1:04d}"


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class RetryService:
    # ---------- 策略列表与状态流转 ----------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        errors = validate_config(values)
        if errors:
            return None, errors
        rows = store.rows(MODULE)
        if any(row.get("策略编号") == str(values.get("策略编号")).strip() for row in rows):
            return None, [f"策略编号 {values.get('策略编号')} 已存在，请更换编号"]
        entry = {"id": _next_id(rows)}
        entry.update({field: str(values.get(field)).strip() for field in STRATEGY_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"重试策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于失败重试策略可执行范围"
        expected = ACTION_FROM[action]
        if entry.get("status") != expected:
            return None, f"当前状态为「{entry.get('status')}」，仅「{expected}」状态可执行{action}"
        if action in ("提交校验", "启用配置"):
            errors = validate_config(entry)
            if errors:
                return None, f"逐项校验未通过：{'；'.join(errors)}"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"重试策略已{action}"

    # ---------- 重试预演 ----------

    def _find_log(self, log_no: str) -> dict[str, Any] | None:
        for row in store.rows("log"):
            if row.get("日志编号") == log_no:
                return row
        return None

    def _find_strategy(self, log: dict[str, Any]) -> dict[str, Any] | None:
        task = str(log.get("关联任务", ""))
        candidates = [
            row for row in store.rows(MODULE)
            if row.get("适用任务") == task and row.get("status") != "已停用"
        ]
        if not candidates:
            return None
        rank = {"已启用": 0, "待校验": 1, "草稿": 2}
        return sorted(candidates, key=lambda row: rank.get(str(row.get("status")), 3))[0]

    def _find_preview(self, log_no: str) -> dict[str, Any] | None:
        for row in store.rows(PREVIEW_MODULE):
            if row.get("日志编号") == log_no:
                return row
        return None

    def _next_point(self, preview: dict[str, Any]) -> dict[str, Any] | None:
        for point in preview.get("时间点", []):
            if point.get("状态") == "待执行":
                return point
        return None

    def _add_todo(self, preview: dict[str, Any], point: dict[str, Any] | None,
                  *, source: str, status: str, note: str) -> dict[str, Any]:
        rows = store.rows(TODO_MODULE)
        todo = {
            "id": _next_id(rows),
            "待办编号": _next_code(rows, "待办编号", "TODO"),
            "预演编号": preview.get("预演编号"),
            "日志编号": preview.get("日志编号"),
            "策略编号": preview.get("策略编号"),
            "批次号": preview.get("批次号"),
            "计划时间": (point or {}).get("计划时间") or preview.get("创建时间"),
            "重试次序": (point or {}).get("次序") or "—",
            "来源": source,
            "状态": status,
            "结论": note,
        }
        rows.append(todo)
        return todo

    def _write_back_log(self, log: dict[str, Any], conclusion: str,
                        next_time: str, retry_status: str) -> None:
        log["预演结论"] = conclusion
        log["下一重试时间"] = next_time
        log["重试状态"] = retry_status

    def _build_preview(self, log: dict[str, Any], strategy: dict[str, Any]) -> dict[str, Any]:
        """按当前策略口径生成预演并留档；结论回写日志、待办进入处理队列。"""
        caliber = _snapshot(strategy)
        error_type = str(log.get("错误类型") or "").strip()
        attempted = _to_int(log.get("已尝试次数")) or 0
        max_retry = _to_int(caliber.get("最大重试")) or 0
        interval = _to_int(caliber.get("重试间隔")) or 0
        coefficient = _to_float(caliber.get("退避系数")) or 1.0
        last_failure = _parse_time(log.get("上次失败时间"))

        category, conflict = _classify(error_type, caliber)
        points: list[dict[str, Any]] = []
        if category == "可重试异常" and attempted < max_retry and last_failure is not None:
            points = _compute_points(last_failure, interval, coefficient, attempted, max_retry)

        if points:
            conclusion = f"可重试异常命中（{error_type}），预演 {len(points)} 个时间点，最近 {points[0]['计划时间']}"
            retry_status, next_time = "等待重试", points[0]["计划时间"]
        elif category == "排除异常":
            conflict_note = "，该异常同时列入可重试异常，按优先级标准排除" if conflict else ""
            conclusion = f"排除异常命中（{error_type}{conflict_note}），停止重试，执行最终动作：{caliber.get('最终动作')}"
            retry_status, next_time = "已终止", "—"
        elif category == "可重试异常":
            conclusion = f"已达最大重试上限 {max_retry} 次，执行最终动作：{caliber.get('最终动作')}"
            retry_status, next_time = "已终止", "—"
        else:
            conclusion = f"错误类型（{error_type or '未记录'}）未纳入可重试范围，执行最终动作：{caliber.get('最终动作')}"
            retry_status, next_time = "已终止", "—"

        rows = store.rows(PREVIEW_MODULE)
        preview = {
            "id": _next_id(rows),
            "预演编号": _next_code(rows, "预演编号", "PREV"),
            "日志编号": log.get("日志编号"),
            "策略编号": strategy.get("策略编号"),
            "批次号": log.get("失败批次号") or "—",
            "策略状态": strategy.get("status"),
            "口径快照": caliber,
            "已尝试次数": attempted,
            "时间点": points,
            "结论": conclusion,
            "创建时间": datetime.now().strftime(TIME_FORMAT),
            "来源": "启动时口径留档",
        }
        rows.append(preview)

        for point in points:
            self._add_todo(preview, point, source="预演排期", status="待执行",
                           note=f"第 {point['次序']} 次重试，计划 {point['计划时间']}")
        if not points:
            self._add_todo(preview, None, source="最终动作", status="待人工", note=conclusion)

        self._write_back_log(log, conclusion, next_time, retry_status)
        return preview

    def run_preview(self, log_no: str) -> tuple[dict[str, Any] | None, str]:
        log = self._find_log(log_no)
        if log is None:
            return None, f"采集日志 {log_no} 不存在，无法预演"
        existing = self._find_preview(log_no)
        if existing is not None:
            # 历史重试按启动时口径留档：断线后续接，只报下一待执行时间点，不按新配置重排。
            point = self._next_point(existing)
            if point is not None:
                return existing, f"已按启动时口径留档，断线后从下一时间点 {point['计划时间']} 继续"
            return existing, "预演已终结，无待执行时间点"
        strategy = self._find_strategy(log)
        if strategy is None:
            return None, f"日志 {log_no} 的关联任务未配置可用的重试策略"
        preview = self._build_preview(log, strategy)
        point = self._next_point(preview)
        if point is not None:
            return preview, f"预演完成，下一时间点 {point['计划时间']}"
        return preview, "预演完成，无需重试"

    # ---------- 失败批次接纳 ----------

    def intake_batch(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        """接纳失败批次并推进预演；返回 (批次记录, 说明, 是否重复)。"""
        required = ["批次号", "日志编号", "错误类型", "失败时间"]
        missing = [field for field in required if not str(values.get(field) or "").strip()]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}", False

        batch_no = str(values.get("批次号")).strip()
        log_no = str(values.get("日志编号")).strip()
        error_type = str(values.get("错误类型")).strip()
        failed_at = str(values.get("失败时间")).strip()

        for row in store.rows(BATCH_MODULE):
            if row.get("批次号") == batch_no:
                return row, f"同一失败批次 {batch_no} 仅接纳一份，重复送入已忽略", True

        if _parse_time(failed_at) is None:
            return None, f"失败时间格式应为 {TIME_FORMAT}，如 2026-11-12 14:30", False
        log = self._find_log(log_no)
        if log is None:
            return None, f"采集日志 {log_no} 不存在，批次未接纳", False
        if log.get("重试状态") == "已终止":
            return None, f"日志 {log_no} 的重试已终止，批次 {batch_no} 不再接纳", False

        preview = self._find_preview(log_no)
        if preview is None:
            strategy = self._find_strategy(log)
            if strategy is None:
                return None, f"日志 {log_no} 的关联任务未配置可用的重试策略", False
            preview = self._build_preview(log, strategy)

        # 历史重试按启动时口径留档：分类与上限一律取预演快照，不取当前配置。
        caliber = preview["口径快照"]
        max_retry = _to_int(caliber.get("最大重试")) or 0
        attempted = (_to_int(log.get("已尝试次数")) or 0) + 1
        log["已尝试次数"] = attempted
        log["上次失败时间"] = failed_at
        log["错误类型"] = error_type
        log["失败批次号"] = batch_no

        for point in preview.get("时间点", []):
            if point.get("次序") == attempted and point.get("状态") == "待执行":
                point["状态"] = "已消费"
                for todo in store.rows(TODO_MODULE):
                    if todo.get("预演编号") == preview.get("预演编号") and todo.get("重试次序") == attempted:
                        todo["状态"] = "已消费"

        category, conflict = _classify(error_type, caliber)
        if category != "可重试异常" or attempted >= max_retry:
            for point in preview.get("时间点", []):
                if point.get("状态") == "待执行":
                    point["状态"] = "已取消"
            for todo in store.rows(TODO_MODULE):
                if todo.get("预演编号") == preview.get("预演编号") and todo.get("状态") == "待执行":
                    todo["状态"] = "已取消"
            if category == "排除异常":
                conflict_note = "，该异常同时列入可重试异常，按优先级标准排除" if conflict else ""
                conclusion = (
                    f"第 {attempted} 次重试失败（{failed_at}）命中排除异常（{error_type}{conflict_note}），"
                    f"停止重试，执行最终动作：{caliber.get('最终动作')}"
                )
            elif category == "可重试异常":
                conclusion = f"第 {attempted} 次重试失败（{failed_at}）后已达最大重试上限 {max_retry} 次，执行最终动作：{caliber.get('最终动作')}"
            else:
                conclusion = f"第 {attempted} 次重试失败（{failed_at}），错误类型（{error_type}）未纳入可重试范围，执行最终动作：{caliber.get('最终动作')}"
            preview["结论"] = conclusion
            preview["已尝试次数"] = attempted
            self._add_todo(preview, None, source="最终动作", status="待人工", note=conclusion)
            self._write_back_log(log, conclusion, "—", "已终止")
        else:
            point = self._next_point(preview)
            next_time = point["计划时间"] if point else "—"
            conclusion = f"第 {attempted} 次重试失败（{failed_at}）命中可重试异常（{error_type}），下一时间点 {next_time}"
            preview["结论"] = conclusion
            preview["已尝试次数"] = attempted
            self._write_back_log(log, conclusion, next_time, "等待重试")

        rows = store.rows(BATCH_MODULE)
        batch = {
            "id": _next_id(rows),
            "批次号": batch_no,
            "日志编号": log_no,
            "错误类型": error_type,
            "失败时间": failed_at,
            "预演编号": preview.get("预演编号"),
            "结论": conclusion,
            "接纳时间": datetime.now().strftime(TIME_FORMAT),
        }
        rows.append(batch)
        return batch, f"失败批次 {batch_no} 已接纳：{conclusion}", False

    # ---------- 处理队列与留档查询 ----------

    def list_queue(self, *, status: str | None = None) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(TODO_MODULE))
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        rows.sort(key=lambda row: str(row.get("计划时间", "")))
        return rows, len(rows)

    def list_previews(self, *, log_no: str | None = None) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(PREVIEW_MODULE))
        if log_no:
            rows = [row for row in rows if row.get("日志编号") == log_no]
        return rows, len(rows)
