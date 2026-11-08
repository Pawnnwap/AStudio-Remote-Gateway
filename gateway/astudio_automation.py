# AI生成
# -*- coding: utf-8 -*-
"""AStudio 自动化调度通道：直写一次性 heartbeat 自动化，由 App 调度器投递远程消息。

机制（实测验证）：
- App 的自动化调度器每约 60s 轮询 automation_definitions 的到期行
  （enabled=1 AND next_run_at <= now），heartbeat 模式唤醒既有目标线程发 prompt
- max_iterations=1 + 每分钟间隔 = 一次性投递；触发后 next_run_at 置空自动停用
- 不经过 MCP（无活跃回合门槛）、不依赖 GUI 操作；模型走 AStudio 自带通道
- 模板值取自官方 astron_create_automation 工具的实际落库行（2026-11-07 实测）
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

APP_DB = Path(os.environ.get("ASTUDIO_APP_DB", r"E:\AStudio Data\userdata\state.sqlite"))

# 官方工具落库的模板值（本项目工作区；custom 绑定为线程原模型 spark-x2.6-4b）
TEMPLATE = {
    "project_id": "53f9d709-e043-4641-8bd5-f3d85816e949",
    "model_selection_json": json.dumps({
        "provider": "acode",
        "model": "spark-x2.6-4b",
        "options": {"reasoningEffort": "xhigh"},
        "customModel": {
            "configId": "REDACTED-CONFIG-ID",
            "executionRevision": 2,
        },
    }, ensure_ascii=False, separators=(",", ":")),
    "runtime_mode": "full-access",
    "interaction_mode": "default",
    "worktree_mode": "local",
    "mode": "heartbeat",
    "stop_on_error": 1,
    "completion_policy_json": '{"type":"none"}',
    "completion_policy_version": 1,
    "max_runtime_seconds": 3600,
    "retry_policy_json": '{"type":"none"}',
    "misfire_policy": "coalesce",
    "acknowledged_risks_json": '["full-access","local-checkout"]',
    "minimum_interval_seconds": 60,
    "max_iterations": 1,
}


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{int(ts * 1000) % 1000:03d}Z"
    )


def resolve_model_selection(thread_id: str, model_slug: str) -> dict:
    """构造 model_selection。

    custom 绑定模型（如 spark-x2.6-4b）必须带 customModel.configId，否则心跳
    唤醒的回合会以 invalid request 失败（实测）。还原链：
    1) 全部 provider_session_runtime 行里该模型的完整绑定（换模型会改写本线程
       的 payload 且丢失 customModel 细节，故不能只读本线程）；
    2) TEMPLATE（本项目 spark 自定义绑定的已知完好形态）；
    3) 简单形状（内置模型即可）。
    """
    simple = {"provider": "acode", "model": model_slug}
    try:
        con = sqlite3.connect(str(APP_DB), timeout=10)
        con.execute("PRAGMA busy_timeout=8000")
        rows = con.execute(
            "SELECT runtime_payload_json FROM provider_session_runtime"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        rows = []
    for (rp,) in rows:
        try:
            sel = json.loads(rp).get("modelSelection") or {}
        except ValueError:
            continue
        if sel.get("model") == model_slug and sel.get("customModel"):
            out = {"provider": "acode", "model": model_slug}
            if sel.get("options"):
                out["options"] = sel["options"]
            out["customModel"] = sel["customModel"]
            return out
    try:
        tpl = json.loads(TEMPLATE["model_selection_json"])
    except ValueError:
        tpl = {}
    if tpl.get("model") == model_slug and tpl.get("customModel"):
        return tpl
    return simple


def schedule_delivery(
    thread_id: str | None,
    message: str,
    fire_in_seconds: int = 3,
    model_slug: str | None = None,
    project_id: str | None = None,
    thread_model: str | None = None,
    standalone: bool = False,
    automation_name: str | None = None,
) -> str:
    """写入一次性自动化定义，返回 automation_id。

    - heartbeat（默认）：唤醒 thread_id 指定的既有线程发 message
    - standalone=True ：不指定线程，App 新建 GUI 线程并以 message 为首条指令执行
    - model_slug 给定时同时改绑/指定线程模型（心跳唤醒会应用 model_selection）
    - project_id / thread_model 由调用方从 astron_read_thread 结果带入，
      保证新环境零硬编码
    """
    now = time.time()
    run_at = _iso(now + fire_in_seconds)
    ts = _iso(now)
    automation_id = f"automation:{uuid.uuid4()}"
    if standalone:
        mode = "standalone"
        source_thread_id = None
        target_thread_id = None
        # 新线程默认模型：App 目录默认（astronclaw-auto，内置模型，简单形状即可）
        model_selection = json.dumps(
            resolve_model_selection(thread_id or "", model_slug)
            if model_slug else {"provider": "acode", "model": "astronclaw-auto"},
            ensure_ascii=False, separators=(",", ":"),
        )
    else:
        mode = TEMPLATE["mode"]
        source_thread_id = thread_id
        target_thread_id = thread_id
        target_model = model_slug or thread_model
        if target_model:
            # custom 绑定模型必须走还原链补全 customModel（简单形状会 invalid request）
            model_selection = json.dumps(
                resolve_model_selection(thread_id or "", target_model),
                ensure_ascii=False, separators=(",", ":"),
            )
        else:
            model_selection = TEMPLATE["model_selection_json"]
    pid = project_id or TEMPLATE["project_id"]

    con = sqlite3.connect(str(APP_DB), timeout=10)
    con.execute("PRAGMA busy_timeout=8000")
    con.execute(
        """
        INSERT INTO automation_definitions (
          automation_id, project_id, source_thread_id, name, prompt,
          schedule_json, enabled, next_run_at, model_selection_json,
          provider_options_json, runtime_mode, interaction_mode, worktree_mode,
          mode, target_thread_id, max_iterations, stop_on_error,
          completion_policy_json, completion_policy_version,
          completion_policy_updated_at, minimum_interval_seconds,
          max_runtime_seconds, retry_policy_json, misfire_policy,
          acknowledged_risks_json, iteration_count, created_at, updated_at, archived_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)
        """,
        (
            automation_id, pid, source_thread_id,
            (automation_name or f"feishu-gateway-{int(now)}")[:40], message,
            json.dumps({"type": "interval", "everySeconds": 60}, separators=(",", ":")),
            run_at, model_selection,
            TEMPLATE["runtime_mode"], TEMPLATE["interaction_mode"], TEMPLATE["worktree_mode"],
            mode, target_thread_id, TEMPLATE["max_iterations"], TEMPLATE["stop_on_error"],
            TEMPLATE["completion_policy_json"], TEMPLATE["completion_policy_version"],
            ts, TEMPLATE["minimum_interval_seconds"], TEMPLATE["max_runtime_seconds"],
            TEMPLATE["retry_policy_json"], TEMPLATE["misfire_policy"],
            TEMPLATE["acknowledged_risks_json"], ts, ts,
        ),
    )
    con.commit()
    con.close()
    return automation_id


def wait_thread_free(thread_id: str, timeout: int = 150, interval: float = 3.0) -> None:
    """等目标线程上没有进行中的自动化 run（调度器对同线程并发 run 会 skip）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        con = sqlite3.connect(str(APP_DB), timeout=10)
        con.execute("PRAGMA busy_timeout=8000")
        row = con.execute(
            "SELECT status FROM automation_runs WHERE thread_id=? AND finished_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        con.close()
        if not row:
            return
        time.sleep(interval)
    raise RuntimeError("目标对话仍有进行中的自动化任务，请稍候重试")


def wait_run(automation_id: str, timeout: int = 180, interval: float = 3.0) -> str:
    """等调度器触发并跑完，返回 run 终态（succeeded/failed/skipped…）。超时抛 RuntimeError。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        con = sqlite3.connect(str(APP_DB), timeout=10)
        con.execute("PRAGMA busy_timeout=8000")
        row = con.execute(
            "SELECT status FROM automation_runs WHERE automation_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (automation_id,),
        ).fetchone()
        con.close()
        if row:
            last = row[0] or ""
            if last in ("succeeded", "failed", "cancelled", "skipped", "interrupted"):
                archive(automation_id)
                return last
        time.sleep(interval)
    raise RuntimeError(f"自动化触发等待超时（{timeout}s，最后状态: {last or '无 run 记录'}）")


def get_run_thread_id(automation_id: str) -> str | None:
    """取该自动化 run 创建/绑定的线程 id（standalone 模式用于拿新建线程）。"""
    con = sqlite3.connect(str(APP_DB), timeout=10)
    con.execute("PRAGMA busy_timeout=8000")
    row = con.execute(
        "SELECT thread_id FROM automation_runs WHERE automation_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (automation_id,),
    ).fetchone()
    con.close()
    return row[0] if row and row[0] else None


def archive(automation_id: str) -> None:
    """归档定义（官方归档行为：archived_at + enabled=0），保持列表干净。"""
    con = sqlite3.connect(str(APP_DB), timeout=10)
    con.execute("PRAGMA busy_timeout=8000")
    con.execute(
        "UPDATE automation_definitions SET archived_at=?, updated_at=?, enabled=0 "
        "WHERE automation_id=?",
        (_iso(time.time()), _iso(time.time()), automation_id),
    )
    con.commit()
    con.close()
