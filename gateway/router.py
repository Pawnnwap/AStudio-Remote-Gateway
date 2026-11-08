# AI生成
# -*- coding: utf-8 -*-
"""消息路由：命令解析 + 任务切换 + AStudio 投递的编排核心（渠道无关）。

命令语法（飞书消息以 / 开头即命令）：
  /help                     帮助
  /model [模型]             查看/切换当前任务的模型
  /task                     任务列表（带序号）
  /task sw <序号|名>        切换任务
  /task del <序号|名>       删除任务
  /task takeover <编号>     （已并入 /task <序号>）
  /sessions                 列出 AStudio 已有对话
  /history [条数]           读取当前任务上下文
  /status                   当前状态总览

直接发文本：
  - 当前有接管任务 -> 消息投递到该 AStudio 对话
  - 没有任务       -> 调度一个全新的 AStudio 任务（App 新建对话并执行，跑完自动绑定）
全部走 AStudio 自带通道，零 API key。
"""
from __future__ import annotations


import gateway.context_store as store
import gateway.session_import as session_import
from gateway.config import EXEC_TIMEOUT_SECONDS

HELP_TEXT = """可用命令：
/task                      已进入的任务列表
/task <序号>               进入 AStudio 对话（序号同 /sessions，如 /task 3）
/task del <序号|名>        删除任务
/sessions                  列出 AStudio 已有对话
/model [模型]              查看/切换当前任务的模型（改绑持久生效）
/history [条数]            读取当前任务上下文
/status                    当前状态总览
/stop                      停止当前正在执行的回合
/steer <插话>              执行中插话引导方向
/help                      本帮助
直接发文字 = 指挥当前任务；没有任务时 = 调度一个全新的 AStudio 任务。
任务忙碌时新指令会被拒收（用 /stop 或 /steer）。"""


def handle_message(open_id: str, text: str) -> str:
    """网关入口：一条用户消息 -> 回复文本。命令就地处理，对话走 AStudio 投递。"""
    text = text.strip()
    # 机器人菜单「发送文字消息」可能发的是菜单名而非命令文本，这里统一归一化
    text = MENU_LABELS.get(text, text)
    if text.startswith("/"):
        return _handle_command(open_id, text)
    return _handle_chat(open_id, text)


# 菜单名/别名 -> 命令（后台菜单配置无论填命令还是名称都能命中）
MENU_LABELS = {
    "帮助": "/help",
    "会话列表": "/sessions",
    "任务列表": "/task",
    "当前任务": "/task",
    "最近上下文": "/history 5",
    "上下文": "/history 5",
    "当前状态": "/status",
    "状态": "/status",
    "帮助菜单": "/help",
}


# ---------- 命令 ----------


def _resolve_task_arg(open_id: str, arg: str) -> tuple[str | None, str]:
    """任务参数解析：纯数字=按 /task 列表序号（1 起），否则按名称。返回 (name, 错误)。"""
    tasks = store.list_tasks(open_id)
    if arg.isdigit():
        i = int(arg)
        if 1 <= i <= len(tasks):
            return tasks[i - 1]["name"], ""
        names = ", ".join(f"{n}. {t['name']}" for n, t in enumerate(tasks, 1)) or "（无）"
        return None, f"序号 {i} 超出范围。当前编号：{names}"
    if arg in {t["name"] for t in tasks}:
        return arg, ""
    return None, f"任务不存在：{arg}（发送 /task 查看列表，可用序号）"


def _handle_command(open_id: str, text: str) -> str:
    parts = text.split()
    cmd = parts[0].lower()

    if cmd == "/help":
        return HELP_TEXT

    if cmd == "/stop":
        task = store.current_task(open_id)
        if not task or task.get("origin") != "gui":
            return "当前没有进入的对话。"
        thread_id = task.get("gui_thread_id") or task.get("session_id")
        if not session_import.thread_busy(thread_id):
            return "该对话当前没有正在执行的回合。"
        try:
            import gateway.astudio_mcp as mcp
            mcp.interrupt_thread(thread_id)
            return "已请求停止当前回合。"
        except Exception as e:
            return (f"实时停止通道不可用（{e}）。"
                    "可等待回合自然完成，或在 AStudio 界面里点击停止后重试。")

    if cmd == "/steer":
        task = store.current_task(open_id)
        if not task or task.get("origin") != "gui":
            return "当前没有进入的对话。"
        steer_text = text[len("/steer"):].strip()
        if not steer_text:
            return "用法：/steer <插话>（在执行中引导方向，如 /steer 优先做移动端适配）"
        return _chat_takeover(open_id, task["name"], task, steer_text, steer=True)

    if cmd == "/model":
        task = store.current_task(open_id)
        if not task or task.get("origin") != "gui":
            return "先进入一个对话（/task <序号>）或发一条文字调度新任务，即可用 /model 切换其模型。"
        return _takeover_model_command(open_id, task, parts[1:])

    if cmd == "/task" and len(parts) >= 2:
        arg = parts[1]
        if arg.isdigit():
            # 统一入口：进入 AStudio 对话（序号同 /sessions；已进入过则直接切回）
            r = session_import.takeover_session(arg, open_id, store)
            if not r["ok"]:
                return r["error"]
            tag = "已切回" if r.get("existing") else "已进入"
            return (
                f"{tag}对话「{r['name']}」。此后直接发文字即可指挥这条对话"
                f"（{r['kernel_id'][:13]}…，AStudio 自带模型，AStudio 界面实时可见；"
                f"空闲时投递约 1 分钟内完成）。"
            )
        if arg == "del":
            if len(parts) < 3:
                return "用法：/task del <序号|任务名>"
            name, err = _resolve_task_arg(open_id, parts[2])
            if err:
                return err
            store.delete_task(open_id, name)
            return f"任务「{name}」已删除。"
        # 兼容：任务名直接切换
        name, err = _resolve_task_arg(open_id, arg)
        if err:
            return err
        store.switch_task(open_id, name)
        return f"已切换到任务「{name}」。"

    if cmd == "/task":
        cur = store.current_task(open_id)
        if cur is None:
            return ("当前没有任务。发一条文字即可调度全新的 AStudio 任务；"
                    "或发 /sessions 后用 /task <序号> 进入已有对话。")
        lines = [f"当前任务：「{cur['name']}」"]
        lines.append(f"  类型：AStudio 对话")
        if cur.get("gui_thread_id"):
            lines.append(f"  线程：{cur['gui_thread_id'][:13]}…")
        if cur.get("model"):
            lines.append(f"  模型：{cur['model']}")
        lines.append(f"  轮数：{cur.get('turns', 0)}")
        tid = cur.get("gui_thread_id") or cur.get("session_id")
        st = "●执行中" if session_import.thread_busy(tid) else "空闲"
        lines.append(f"  状态：{st}")
        lines.append("（发 /sessions 用 /task <序号> 切换其他对话）")
        return "\n".join(lines)

    if cmd == "/history":
        return _history_output(open_id, parts[1] if len(parts) > 1 else "10")

    if cmd == "/sessions":
        items = session_import.list_sessions()
        cur = store.current_task(open_id)
        cur_tid = (cur or {}).get("gui_thread_id")
        import gateway.astudio_mcp as mcp
        for it in items:
            if not it.get("takeover"):
                continue
            it["status"] = "working" if session_import.thread_busy(it["id"]) else None
        return session_import.format_session_list(items, current_thread_id=cur_tid)

    if cmd == "/status":
        user = store.get_user(open_id)
        cur = store.current_task(open_id)
        lines = [f"任务数：{len(store.list_tasks(open_id))}"]
        if cur:
            lines.append(f"当前任务：{cur['name']}")
            lines.append(f"类型：{'对话' if cur.get('origin') == 'gui' else '本地'}")
            lines.append(f"轮数：{cur.get('turns', 0)}")
            if cur.get("model"):
                lines.append(f"模型：{cur['model']}")
        else:
            lines.append("当前任务：无（发文字将调度新的 AStudio 任务）")
        return "\n".join(lines)

    return f"未知命令：{cmd}。发送 /help 查看用法。"


# ---------- 对话 ----------


def _handle_chat(open_id: str, text: str) -> str:
    task = store.current_task(open_id)
    if task is None:
        return _dispatch_new_gui_task(open_id, text)
    if task.get("origin") != "gui":
        # 自动切到最近创建的任务继续投递，避免打断用户
        gui_tasks = [t for t in store.list_tasks(open_id) if t.get("origin") == "gui"]
        if gui_tasks:
            name = gui_tasks[-1]["name"]
            store.switch_task(open_id, name)
            task = store.current_task(open_id)
            return _chat_takeover(open_id, name, task, text)
        return "还没有可投递的对话。发 /sessions 查看，然后 /task <序号> 进入。"
    task_name = task["name"]
    return _chat_takeover(open_id, task_name, task, text)


def _dispatch_new_gui_task(open_id: str, text: str) -> str:
    """没有任务时：调度 standalone 自动化 -> App 新建对话并执行 -> 自动绑定。"""
    import gateway.astudio_automation as automation
    import gateway.astudio_mcp as mcp

    # 项目归属：取最近会话所带 projectId；没有则用模板兜底
    items = session_import.list_sessions()
    project_id = None
    if items:
        try:
            project_id = mcp.read_thread(items[0]["id"], message_limit=1).get("projectId")
        except mcp.McpError:
            pass

    base = session_import._task_name(text)
    automation_id = automation.schedule_delivery(
        thread_id=None, message=text, fire_in_seconds=1,
        project_id=project_id, standalone=True, automation_name=base,
    )
    run_status = automation.wait_run(automation_id, timeout=180)
    if run_status != "succeeded":
        return f"任务调度未成功（run={run_status}），请重试。"

    thread_id = automation.get_run_thread_id(automation_id)
    if not thread_id:
        return "任务已创建但未取回线程 id，请在 AStudio 里查看新对话。"

    # 等模型回复（新线程里 prompt 是第一条消息，等助手消息出现即可）
    reply = mcp.wait_for_reply(thread_id, 0, timeout=EXEC_TIMEOUT_SECONDS)

    # 绑定为接管任务：任务名 = App 线程标题（剥掉 App 追加的时间后缀）
    try:
        title = (mcp.read_thread(thread_id, message_limit=1).get("title") or "").strip()
    except mcp.McpError:
        title = ""
    title = session_import.clean_title(title)
    base = session_import._task_name(title) or base
    name = base
    i = 2
    user = store.get_user(open_id)
    while name in user.get("tasks", {}):
        name = f"{base}-{i}"
        i += 1
    store.create_task(open_id, name, "由 IM 消息调度的 AStudio 新任务")
    store._update(open_id, lambda u: u["tasks"][name].update(
        origin="gui",
        gui_thread_id=thread_id,
        turns=1,
    ))
    return f"已创建新任务「{name}」（AStudio 对话列表可查看）并设为当前任务：\n{reply}"


def _takeover_model_command(open_id: str, task: dict, args: list[str]) -> str:
    """当前任务的模型查看/切换：/model 列目录，/model <slug> 改绑线程模型。"""
    import gateway.astudio_automation as automation
    import gateway.astudio_mcp as mcp

    thread_id = task.get("gui_thread_id") or task.get("session_id")
    try:
        current = (mcp.read_thread(thread_id, message_limit=1).get("model") or "").strip()
        slugs = mcp.list_model_slugs()
    except mcp.McpError as e:
        return f"AStudio 会话不可用：{e}"

    if not args:
        lines = [f"当前会话模型：{current or '（未知）'}", "可用模型："]
        for s in slugs:
            lines.append(f"  {s}{' ←当前' if s == current else ''}")
        lines.append("切换：/model <模型名>（下一次消息投递时生效并改绑会话）")
        return "\n".join(lines)

    slug = args[0].strip()
    message = " ".join(args[1:]).strip()
    if slug not in slugs:
        return f"未知模型：{slug}。发送 /model 查看可选列表。"
    if message:
        # 一步切换并投递：投递定义自带新模型，无需单独的切换通知回合
        if session_import.thread_busy(thread_id):
            return ("任务正在执行中，暂不接收新指令。\n"
                    "可选：等它完成；/stop 停止；/steer <插话> 引导。")
        return _chat_takeover(open_id, task["name"], task, message, model_slug=slug)
    if slug == current:
        return f"会话模型已是 {slug}，无需切换。"
    prev_model = current or task.get("model")
    try:
        before = mcp.read_thread(thread_id, message_limit=1)
        automation.wait_thread_free(thread_id)
        automation_id = automation.schedule_delivery(
            thread_id,
            f"（飞书网关）模型已切换为 {slug}。",
            fire_in_seconds=1,
            model_slug=slug,
            project_id=before.get("projectId"),
            thread_model=before.get("model"),
        )
        run_status = automation.wait_run(automation_id, timeout=180)
    except (mcp.McpError, RuntimeError) as e:
        return f"模型切换失败：{e}"
    if run_status != "succeeded":
        # 切换失败可能在线程上留下坏路由：自动回滚到上一个已知可用模型
        try:
            automation.wait_thread_free(thread_id)
            rollback = automation.schedule_delivery(
                thread_id,
                f"（飞书网关）模型切换失败，已回滚为 {prev_model or '原模型'}。",
                fire_in_seconds=1,
                model_slug=prev_model,
                project_id=before.get("projectId"),
            )
            automation.wait_run(rollback, timeout=180)
            return f"模型切换未成功（{run_status}），已自动回滚为原模型 {prev_model or '原模型'}。"
        except Exception:
            return f"模型切换未完成（run={run_status}），请重试。"
    store._update(open_id, lambda u: u["tasks"][task["name"]].update(model=slug))
    after = mcp.read_thread(thread_id, message_limit=1).get("model")
    return f"模型已切换为 {slug}（线程改绑确认：{after}）。之后的消息投递继续使用该模型。"


def _history_output(open_id: str, n_raw: str) -> str:
    """读取当前任务上下文：直读本地投影库（不依赖 App 在线/凭证状态）。"""
    import sqlite3

    from gateway.session_import import APP_DB, clean_title

    try:
        n = max(1, min(50, int(n_raw)))
    except ValueError:
        n = 10

    task = store.current_task(open_id)
    if task is None:
        return "还没有任务。发一条文字调度新任务，或发 /sessions 后用 /task <序号> 进入对话。"
    if task.get("origin") != "gui":
        return f"任务「{task['name']}」是本地任务（无 AStudio 对话）。"

    name = task["name"]
    thread_id = task.get("gui_thread_id") or task.get("session_id")
    con = sqlite3.connect(f"file:{APP_DB.as_posix()}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT role, text FROM projection_thread_messages "
        "WHERE thread_id=? AND role IN ('user','assistant') AND text IS NOT NULL AND text != '' "
        "ORDER BY created_at DESC, sequence DESC LIMIT ?",
        (thread_id, n),
    ).fetchall()
    title = con.execute(
        "SELECT title FROM projection_threads WHERE thread_id=?", (thread_id,)
    ).fetchone()
    con.close()
    if not rows:
        return f"任务「{name}」：暂无消息。"
    lines = [f"任务「{name}」最近 {len(rows)} 条："]
    for role, text in reversed(rows):  # 转回正序展示
        who = "用户" if role == "user" else "助手"
        lines.append(f"  [{who}] {(text or '').replace(chr(10), ' ')[:160]}")
    if title and title[0]:
        lines.append(f"（AStudio 标题：{clean_title(title[0])}）")
    return "\n".join(lines)


def _chat_takeover(open_id: str, task_name: str, task: dict, text: str,
                   steer: bool = False, model_slug: str | None = None) -> str:
    """GUI 任务投递：主链路全本地（自动化写库 + 投影库读回复），零实时依赖。

    - 会话空闲：直写一次性 heartbeat 自动化，App 调度器 ≤60s 自主唤醒投递
    - 会话忙碌（steer=False）：拒收新指令，引导 /stop 或 /steer
    - 会话忙碌（steer=True）：优先 MCP 即时插话；凭证不可用时降级为排队投递
    """
    import gateway.astudio_automation as automation
    import gateway.astudio_mcp as mcp

    thread_id = task.get("gui_thread_id") or task.get("session_id")
    baseline = session_import.last_message_rowid(thread_id)
    busy = session_import.thread_busy(thread_id)

    if busy and steer:
        try:
            mcp.send_message(thread_id, text, mode="steer")
            return "已插话引导，当前回合会参考你的指示。"
        except mcp.McpError:
            automation.schedule_delivery(thread_id, f"（插话）{text}")
            return ("实时插话通道不可用（会话凭证已轮换），已把插话排队："
                    "将在当前回合完成后作为下一条消息送达。")
    if busy:
        return ("任务正在执行中，暂不接收新指令。\n"
                "可选：等它完成；/stop 停止当前执行；/steer <插话> 引导方向。")

    use_model = model_slug or task.get("model")
    automation_id = automation.schedule_delivery(
        thread_id, text, model_slug=use_model,
    )
    run_status = automation.wait_run(automation_id, timeout=180)
    if run_status not in ("succeeded",):
        return f"自动化投递未成功（run={run_status}），消息可能未送达。"
    if model_slug and model_slug != task.get("model"):
        store._update(open_id, lambda u: u["tasks"][task_name].update(model=model_slug))

    # 本地等回复：投影库出现基线之后的助手消息即为完成
    deadline = __import__("time").time() + EXEC_TIMEOUT_SECONDS
    new_msgs: list[tuple[str, str]] = []
    while __import__("time").time() < deadline:
        new_msgs = session_import.recent_messages(thread_id, after_rowid=baseline)
        if new_msgs and not session_import.thread_busy(thread_id):
            break
        __import__("time").sleep(3)
    if not new_msgs:
        return "消息已投递（回复等待超时）。"
    lines = [f"  [{('用户' if r == 'user' else '助手')}] {x.replace(chr(10), ' ')[:160]}"
             for r, x in new_msgs]
    store.bump_turns(open_id, task_name)
    return "\n".join(lines)

