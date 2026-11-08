# AI生成
# -*- coding: utf-8 -*-
"""上下文持久化层：用户-任务-会话注册表。

设计：
- 以飞书 open_id 为用户主键
- 每用户多任务（task），每任务绑定一个 astron-code session_id（原文模式的上下文）
- 总结模式下：任务存 summary 文本，每次新对话把 summary 注入 prompt 前缀
- 状态全部落盘 registry.json，跨进程可恢复；共享该文件即多端同步
"""
from __future__ import annotations

import json
import threading
from datetime import datetime

from gateway.config import REGISTRY_PATH

LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    if REGISTRY_PATH.exists():
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {"users": {}}


def _save(data: dict) -> None:
    REGISTRY_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_user(open_id: str) -> dict:
    """读取（或初始化）用户档案。"""
    with LOCK:
        data = _load()
        users = data.setdefault("users", {})
        if open_id not in users:
            users[open_id] = {
                "current_task": None,
                "model": None,  # None = 网关默认
                "context_mode": "full",  # full=原文 / summary=总结
                "tasks": {},
            }
            _save(data)
        return users[open_id]


def _update(open_id: str, fn) -> dict:
    with LOCK:
        data = _load()
        user = data.setdefault("users", {}).setdefault(
            open_id,
            {
                "current_task": None,
                "model": None,
                "context_mode": "full",
                "tasks": {},
            },
        )
        fn(user)
        _save(data)
        return user


# ---------- 任务管理 ----------


def create_task(open_id: str, task_name: str, description: str = "") -> dict:
    """新建任务并切换为当前任务。"""

    def fn(user: dict) -> None:
        tasks = user.setdefault("tasks", {})
        if task_name in tasks:
            raise ValueError(f"任务已存在：{task_name}")
        tasks[task_name] = {
            "name": task_name,
            "description": description,
            "session_id": None,  # AStudio 内核会话 id（接管任务）
                                    "turns": 0,
            "created_at": _now(),
            "updated_at": _now(),
        }
        user["current_task"] = task_name

    return _update(open_id, fn)


def switch_task(open_id: str, task_name: str) -> dict:
    def fn(user: dict) -> None:
        if task_name not in user.get("tasks", {}):
            raise ValueError(f"任务不存在：{task_name}（可先 /task new 创建）")
        user["current_task"] = task_name

    return _update(open_id, fn)


def current_task(open_id: str) -> dict | None:
    user = get_user(open_id)
    name = user.get("current_task")
    if not name:
        return None
    return user["tasks"].get(name)


def list_tasks(open_id: str) -> list[dict]:
    user = get_user(open_id)
    return list(user.get("tasks", {}).values())


def delete_task(open_id: str, task_name: str) -> dict:
    def fn(user: dict) -> None:
        tasks = user.get("tasks", {})
        if task_name not in tasks:
            raise ValueError(f"任务不存在：{task_name}")
        del tasks[task_name]
        if user.get("current_task") == task_name:
            user["current_task"] = next(iter(tasks), None)

    return _update(open_id, fn)


# ---------- 会话与上下文 ----------




def bump_turns(open_id: str, task_name: str) -> None:
    def fn(user: dict) -> None:
        t = user["tasks"][task_name]
        t["turns"] = t.get("turns", 0) + 1
        t["updated_at"] = _now()

    _update(open_id, fn)










# ---------- 模型与上下文模式 ----------




def set_context_mode(open_id: str, mode: str) -> dict:
    def fn(user: dict) -> None:
        if mode not in ("full", "summary"):
            raise ValueError("模式只支持 full（原文）或 summary（总结）")
        user["context_mode"] = mode

    return _update(open_id, fn)


