# AI生成
# -*- coding: utf-8 -*-
"""AStudio 存量 GUI 会话：发现 / 接管（项目核心：远程调度 GUI 任务）。

- list_sessions()      列出 GUI 会话（含消息数/可接管性）
- takeover_session()   接管：任务消息直达 AStudio 原对话
只读访问，不改动 AStudio 任何数据。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

APP_DB = Path(os.environ.get(
    "ASTUDIO_APP_DB", r"E:\AStudio Data\userdata\state.sqlite"
))


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)




def _gui_kernel_bindings(app_con) -> dict:
    """从 provider_session_runtime.resume_cursor_json 提取 GUI 线程 -> 内核线程 id。"""
    bindings = {}
    try:
        for tid, cursor in app_con.execute(
            "SELECT thread_id, resume_cursor_json FROM provider_session_runtime"
        ):
            try:
                kid = json.loads(cursor or "{}").get("threadId")
            except ValueError:
                continue
            if kid:
                bindings[tid] = kid
    except sqlite3.Error:
        pass
    return bindings


def list_sessions() -> list[dict]:
    """列出 GUI 会话（有消息的），按创建时间倒序（编号稳定，不随活跃度漂移）。"""
    items = []
    if not APP_DB.exists():
        return items

    con = _ro(APP_DB)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT thread_id, title, created_at, updated_at FROM projection_threads "
        "WHERE deleted_at IS NULL ORDER BY created_at DESC"
    ).fetchall()
    counts = dict(cur.execute(
        "SELECT thread_id, COUNT(*) FROM projection_thread_messages GROUP BY thread_id"
    ).fetchall())
    firsts = {}
    for tid, text, ts in cur.execute(
        "SELECT thread_id, text, created_at FROM projection_thread_messages "
        "WHERE role='user' ORDER BY created_at"
    ):
        firsts.setdefault(tid, (text or "")[:50])
    bindings = _gui_kernel_bindings(con)  # GUI 线程 -> 内核线程 id（可接管性）
    con.close()
    rows = [(tid, title, created, updated) for tid, title, created, updated in rows
            if counts.get(tid, 0) > 0]  # 过滤空线程（新建未对话，不可导入）
    rows.sort(key=lambda r: r[2] or "", reverse=True)  # 序号按创建时间固定
    for i, (tid, title, created, updated) in enumerate(rows, 1):
        kid = bindings.get(tid)
        items.append({
            "key": str(i),  # 稳定序号：按创建时间，不随活跃度漂移
            "id": tid,
            "title": (clean_title(title) or firsts.get(tid) or "（无标题）").strip()[:40],
            "messages": counts.get(tid, 0),
            "updated": (updated or created or "")[:16].replace("T", " "),
            "takeover": bool(kid),
            "kernel_id": kid,
        })
    items.sort(key=lambda x: x["updated"], reverse=True)  # 展示按最近活跃
    return items


def format_session_list(items: list[dict], current_thread_id: str | None = None) -> str:
    if not items:
        return "未发现 AStudio 存量 GUI 会话（AStudio 是否在运行？）。"
    lines = ["AStudio GUI 会话（按最近活跃排序；序号固定；/task 序号 进入）"]
    for it in items:
        extra = "" if it.get("takeover") else "·不可接管"
        if it.get("status") == "working":
            extra += "·●执行中"
        if current_thread_id and it["id"] == current_thread_id:
            extra += "·←当前"
        lines.append(
            f"  {it['key']}  {it['title']}（{it['messages']}条消息{extra}·活跃 {it['updated']}）"
        )
    return "\n".join(lines)


def clean_title(title: str) -> str:
    """App 线程标题去掉追加的时间后缀（形如 " - 2026-11-07T15:xx:xx"）。"""
    return re.sub(r"\s*-\s*20\d{2}-\d{2}-\d{2}.*$", "", (title or "").strip())


def _task_name(title: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\s]+", "-", clean_title(title))[:20].strip("-")
    return name or "default"



# ---------- 接管 ----------


def takeover_session(key: str, open_id: str, store) -> dict:
    """把 GUI 会话绑定为接管任务：该任务的消息直达 AStudio 原对话。

    返回 {'ok','name','kernel_id','cwd'} 或 {'ok': False, 'error'}。
    """
    items = list_sessions()
    target = next((it for it in items if it["key"] == key or it["id"] == key), None)
    if target is None:
        return {"ok": False, "error": f"未找到会话：{key}。发送 /sessions 查看列表。"}
    if not target.get("takeover") or not target.get("kernel_id"):
        return {
            "ok": False,
            "error": f"会话「{target['title']}」没有可接管的内核会话（可能从未成功执行过回合）。",
        }

    kernel_id = target["kernel_id"]

    # 幂等：该线程已有任务 -> 直接切回，不重复建
    user = store.get_user(open_id)
    for name, t_ in user.get("tasks", {}).items():
        if t_.get("gui_thread_id") == target["id"]:
            store.switch_task(open_id, name)
            return {"ok": True, "name": name, "kernel_id": kernel_id, "existing": True}

    name = _task_name(target["title"])
    while name in user.get("tasks", {}):
        name = f"{name}-2"

    store.create_task(open_id, name, "接管自 AStudio GUI 会话（远程调度）")
    store._update(open_id, lambda u: u["tasks"][name].update(
        session_id=kernel_id,
        origin="gui",
        gui_thread_id=target["id"],
        turns=0,
    ))
    return {"ok": True, "name": name, "kernel_id": kernel_id}
