"""验收序列 G：重试预演端到端验收。

脚本会自行在 8010 端口拉起一个临时后端，按验收序列 G 逐步断言，结束后关闭。

用法：
    backend/.venv/bin/python backend/scripts/acceptance_g.py

验收点：
- G1 RETR-0002 第 2 次失败落在 14:30，排期预演展开六要素且时间点不超过上限；
- G2 排除异常命中后停止（鉴权失败同时列入可重试异常，冲突时排除优先）；
- G3 RETR-0001 的下一时间点仍按 14:10 原间隔，不随新配置改成 14:40；
- G4 同一失败批次重复送入仅接纳一份；
- G5 预演结论回写采集日志详情，处理队列可见即将执行的失败重试待办。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8010"
BACKEND_DIR = Path(__file__).resolve().parents[1]
VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python"

failures: list[str] = []


def check(name: str, condition: bool, detail: str) -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name} — {detail}")
    if not condition:
        failures.append(name)


def req(method: str, path: str, values: dict | None = None) -> dict:
    payload = {"values": values} if values is not None else None
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode())


def wait_ready(proc: subprocess.Popen) -> None:
    for _ in range(60):
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=2):
                return
        except (urllib.error.URLError, ConnectionError):
            if proc.poll() is not None:
                raise SystemExit("临时后端启动失败")
            time.sleep(0.5)
    raise SystemExit("临时后端健康检查超时")


def main() -> int:
    python = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
    proc = subprocess.Popen(
        [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8010"],
        cwd=BACKEND_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_ready(proc)

        # G1：RETR-0002 第 2 次失败落在 14:30，预演展开六要素且不超过上限。
        preview = req("POST", "/api/retry/preview", {"日志编号": "LOG-0002"})
        entry = preview.get("entry") or {}
        points = entry.get("时间点") or []
        caliber = entry.get("口径快照") or {}
        check("G1-预演生成", preview.get("ok") is True, str(preview.get("message")))
        check(
            "G1-第2次失败落在14:30",
            bool(points) and points[0].get("计划时间") == "2026-11-12 14:30" and points[0].get("次序") == 2,
            f"首个时间点={points[0] if points else '无'}",
        )
        check(
            "G1-排期预演展开六要素",
            all(key in caliber for key in ("最大重试", "重试间隔", "退避系数", "可重试异常", "排除异常", "最终动作")),
            f"口径快照字段={sorted(caliber)}",
        )
        check(
            "G1-时间点不超过上限",
            1 + len(points) <= int(caliber.get("最大重试", 0)),
            f"已尝试1次，上限{caliber.get('最大重试')}，生成{len(points)}个时间点",
        )

        # G2：排除异常命中后停止（鉴权失败同时列入可重试异常，排除优先）。
        batch = req("POST", "/api/retry/batches", {
            "批次号": "BATCH-0002-B",
            "日志编号": "LOG-0002",
            "错误类型": "鉴权失败",
            "失败时间": "2026-11-12 14:30",
        })
        message = str(batch.get("message"))
        check("G2-批次接纳", batch.get("ok") is True, message)
        check(
            "G2-排除异常命中后停止",
            "排除异常" in message and "停止重试" in message and "优先级标准" in message,
            message,
        )
        log2 = req("GET", "/api/log/2")
        check(
            "G2-结论回写日志详情",
            "排除异常" in str(log2.get("预演结论")) and log2.get("重试状态") == "已终止" and str(log2.get("已尝试次数")) == "2",
            f"预演结论={log2.get('预演结论')}，重试状态={log2.get('重试状态')}，已尝试次数={log2.get('已尝试次数')}",
        )
        queue = req("GET", "/api/retry/queue")
        todos = queue.get("items") or []
        final_todos = [t for t in todos if t.get("日志编号") == "LOG-0002" and t.get("来源") == "最终动作" and t.get("状态") == "待人工"]
        cancelled = [t for t in todos if t.get("日志编号") == "LOG-0002" and t.get("状态") == "已取消"]
        check("G2-生成最终动作待办", len(final_todos) == 1, f"最终动作待办={len(final_todos)}")
        check("G2-剩余时间点已取消", len(cancelled) == 1, f"已取消待办={len(cancelled)}")

        # G3：RETR-0001 的下一时间点仍按 14:10 原间隔，不随新配置改成 14:40。
        resumed = req("POST", "/api/retry/preview", {"日志编号": "LOG-0001"})
        resumed_entry = resumed.get("entry") or {}
        resumed_points = resumed_entry.get("时间点") or []
        pending = [p for p in resumed_points if p.get("状态") == "待执行"]
        check(
            "G3-断线后续接下一时间点14:10",
            bool(pending) and pending[0].get("计划时间") == "2026-11-12 14:10",
            f"下一待执行={pending[0] if pending else '无'}，message={resumed.get('message')}",
        )
        check(
            "G3-不随新配置改成14:40",
            all(p.get("计划时间") != "2026-11-12 14:40" for p in resumed_points),
            f"留档口径间隔={ (resumed_entry.get('口径快照') or {}).get('重试间隔') } 分钟，当前草稿配置为 40 分钟",
        )

        # G4：同一失败批次重复送入仅接纳一份。
        again = req("POST", "/api/retry/batches", {
            "批次号": "BATCH-0002-B",
            "日志编号": "LOG-0002",
            "错误类型": "鉴权失败",
            "失败时间": "2026-11-12 14:30",
        })
        log2_after = req("GET", "/api/log/2")
        check(
            "G4-重复批次仅接纳一份",
            again.get("ok") is True and "仅接纳一份" in str(again.get("message")),
            str(again.get("message")),
        )
        check(
            "G4-已尝试次数不重复累加",
            str(log2_after.get("已尝试次数")) == "2",
            f"已尝试次数={log2_after.get('已尝试次数')}",
        )

        # G5：RETR-0003 退避系数展开，时间点不超上限；处理队列可见待执行重试。
        preview3 = req("POST", "/api/retry/preview", {"日志编号": "LOG-0003"})
        points3 = (preview3.get("entry") or {}).get("时间点") or []
        times3 = [p.get("计划时间") for p in points3]
        check(
            "G5-退避系数展开且不超过上限",
            times3 == ["2026-11-12 13:20", "2026-11-12 14:00"],
            f"间隔20分钟、系数2、上限2，时间点={times3}",
        )
        queue_after = req("GET", "/api/retry/queue", None)
        waiting = [t for t in (queue_after.get("items") or []) if t.get("状态") == "待执行"]
        check(
            "G5-处理队列可见即将执行的重试",
            any(t.get("日志编号") == "LOG-0003" for t in waiting) and any(t.get("日志编号") == "LOG-0001" for t in waiting),
            f"待执行待办={[(t.get('日志编号'), t.get('计划时间')) for t in waiting]}",
        )
        log3 = req("GET", "/api/log/3")
        check(
            "G5-预演结论回写采集日志",
            "预演 2 个时间点" in str(log3.get("预演结论")) and log3.get("下一重试时间") == "2026-11-12 13:20",
            f"预演结论={log3.get('预演结论')}",
        )
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    print()
    if failures:
        print(f"验收序列 G 未通过：{len(failures)} 项失败 -> {failures}")
        return 1
    print("验收序列 G 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
