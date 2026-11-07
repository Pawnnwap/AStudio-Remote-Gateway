# AI生成
# -*- coding: utf-8 -*-
"""飞书长连接客户端：flark（vendor 版 lark_oapi）收事件 + REST 直连回复。

- 企业自建应用 + WS 长连接（无公网 IP），flark 只负责收事件
- 回复/发送走官方 im REST 端点（vendored ws.Client 无 REST 能力，实测）
- SDK 自带心跳与断线重连
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

try:
    import flark  # noqa: E402  # 推荐：本地 vendor（见 README「依赖」）
except ImportError:
    import lark_oapi as flark  # noqa: E402  # 官方 SDK（pip install lark-oapi）
from flark.api.im.v1 import P2ImMessageReceiveV1  # noqa: E402
from flark.event.dispatcher_handler import EventDispatcherHandler  # noqa: E402

from gateway.config import FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_DOMAIN  # noqa: E402

_MESSAGE_HANDLER = None  # callable(open_id, chat_id, message_id, text)


def set_message_handler(handler) -> None:
    """注册消息回调：handler(open_id, chat_id, message_id, text) -> None。"""
    global _MESSAGE_HANDLER
    _MESSAGE_HANDLER = handler


def _extract_text(event: P2ImMessageReceiveV1) -> tuple[str, str, str, str] | None:
    """解析事件 -> (open_id, chat_id, message_id, text)。非文本消息返回 None。"""
    try:
        msg = event.event.message
        if msg.chat_type not in ("p2p",):
            # 群聊消息需 @机器人，这里取 @ 后正文；text 类型才处理
            pass
        if msg.message_type != "text":
            return None
        import json

        content = json.loads(msg.content)
        text = content.get("text", "").strip()
        # 去掉群聊 @机器人 前缀占位符
        text = text.split("@_user_1", 1)[-1].strip()
        open_id = event.event.sender.sender_id.open_id
        return open_id, msg.chat_id, msg.message_id, text
    except Exception:
        return None


def _on_message(data: P2ImMessageReceiveV1) -> None:
    if _MESSAGE_HANDLER is None:
        return
    parsed = _extract_text(data)
    if not parsed:
        return
    open_id, chat_id, message_id, text = parsed
    if not text:
        return
    _MESSAGE_HANDLER(open_id, chat_id, message_id, text)


def build_client(app_id: str | None = None, app_secret: str | None = None) -> flark.ws.Client:
    """构建长连接客户端（未启动）。"""
    handler = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message)
        .register_p2_application_bot_menu_v6(_on_menu)
        .build()
    )
    return flark.ws.Client(
        app_id or FEISHU_APP_ID,
        app_secret or FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=flark.LogLevel.INFO,
        domain=FEISHU_DOMAIN,
    )


def _on_menu(data) -> None:
    """机器人菜单点击：event_key 即命令文本（如 /help），按该 open_id 的新消息处理。"""
    if _MESSAGE_HANDLER is None:
        return
    try:
        open_id = data.event.operator.operator_id.open_id
        key = (data.event.event_key or "").strip()
    except Exception:
        return
    if not key:
        return
    print(f"[菜单点击] open_id={open_id[:12]}… cmd={key}")
    _MESSAGE_HANDLER(open_id, "", "", key)  # 菜单点击无 message_id，回复走 open_id 直发


_TENANT_TOKEN: tuple[str, float] | None = None  # (token, expires_at_epoch)


def _tenant_token() -> str:
    """tenant_access_token（缓存，过期前 60s 刷新）。"""
    global _TENANT_TOKEN
    if _TENANT_TOKEN and _TENANT_TOKEN[1] > time.time() + 60:
        return _TENANT_TOKEN[0]
    body = json.dumps({
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET,
    }).encode()
    req = urllib.request.Request(
        FEISHU_DOMAIN + "/open-apis/auth/v3/tenant_access_token/internal",
        data=body, method="POST", headers={"Content-Type": "application/json"},
    )
    r = json.loads(urllib.request.urlopen(req, timeout=10).read())
    if r.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: code={r.get('code')} msg={r.get('msg')}")
    _TENANT_TOKEN = (r["tenant_access_token"], time.time() + int(r.get("expire", 7200)))
    return _TENANT_TOKEN[0]


def _im_post(path: str, body: dict) -> dict:
    """调用 im REST 端点，返回响应 JSON。"""
    req = urllib.request.Request(
        FEISHU_DOMAIN + path,
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + _tenant_token()},
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


_DIALOGUE_PREFIX = re.compile(r"^\[(用户|助手)\]\s*")


def _elements(text: str) -> list[dict]:
    """把回复拆成卡片元素：一问一答各一块（用户/助手徽标 + hr 分隔），其余为单 markdown。

    对话文本形如：
        [用户] 问题…
        [助手] 回答…
    也可带前置说明行（第一段对话行之前的内容）。
    """
    lines = text.split("\n")
    header_lines = []
    i = 0
    while i < len(lines) and not _DIALOGUE_PREFIX.match(lines[i]):
        header_lines.append(lines[i])
        i += 1
    blocks: list[tuple[str, list[str]]] = []
    while i < len(lines):
        m = _DIALOGUE_PREFIX.match(lines[i])
        role = "user" if m.group(1) == "用户" else "assistant"
        content = [lines[i][m.end():]]
        i += 1
        while i < len(lines) and not _DIALOGUE_PREFIX.match(lines[i]):
            content.append(lines[i])
            i += 1
        blocks.append((role, content))
    if not blocks:
        return [{"tag": "markdown", "content": text}]

    elements: list[dict] = []
    header = "\n".join(l for l in header_lines if l.strip())
    if header:
        elements.append({"tag": "markdown", "content": header})
    for role, content in blocks:
        who = "🙋 **用户**" if role == "user" else "🤖 **助手**"
        body = "\n".join(content).strip()
        elements.append({"tag": "markdown", "content": f"{who}\n{body}" if body else who})
        elements.append({"tag": "hr"})
    if elements and elements[-1].get("tag") == "hr":
        elements.pop()
    return elements


def _card(text: str) -> dict:
    """把文本包装成 markdown 卡片（一问一答分块渲染；纯文本降级保底）。"""
    return {
        "config": {"wide_screen_mode": True},
        "elements": _elements(text),
    }


def reply_text(client, chat_id: str, message_id: str, text: str) -> bool:
    """回复消息：markdown 卡片优先，纯文本保底。client 参数仅为兼容保留。"""
    try:
        r = _im_post(
            f"/open-apis/im/v1/messages/{message_id}/reply",
            {"msg_type": "interactive", "content": json.dumps(_card(text))},
        )
        if r.get("code") != 0:
            print(f"[飞书回复失败(卡片)] code={r.get('code')} msg={r.get('msg')}（降级纯文本）")
            return _send_text(client, chat_id, text)
        return True
    except Exception as e:
        print(f"[飞书回复异常] {e!r}（降级纯文本）")
        return _send_text(client, chat_id, text)


def _send_text(client, receive_id: str, text: str, receive_id_type: str = "chat_id") -> bool:
    try:
        r = _im_post(
            f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            {"receive_id": receive_id, "msg_type": "text",
             "content": json.dumps({"text": text})},
        )
        if r.get("code") != 0:
            print(f"[飞书发送失败] code={r.get('code')} msg={r.get('msg')}")
            return False
        return True
    except Exception as e:
        print(f"[飞书发送异常] {e!r}")
        return False


def start(app_id: str | None = None, app_secret: str | None = None) -> None:
    """启动长连接（阻塞）。消息回调需先 set_message_handler 注册。"""
    client = build_client(app_id, app_secret)

    def on_msg(open_id: str, chat_id: str, message_id: str, text: str) -> None:
        # 飞书长连接要求 3 秒内 ack；模型调用放线程池，避免触发超时重推
        import threading

        print(f"[收到消息] open_id={open_id[:12]}… text={text[:40]}")
        def work() -> None:
            from gateway.router import handle_message

            try:
                reply = handle_message(open_id, text)
            except Exception as e:
                reply = f"网关处理出错：{e}"
            if len(reply) > 4000:
                reply = reply[:3900] + "\n…（过长已截断）"
            if message_id:
                reply_text(client, chat_id, message_id, reply)
            else:
                _send_text(client, open_id, reply, receive_id_type="open_id")

        threading.Thread(target=work, daemon=True).start()

    set_message_handler(on_msg)
    print("飞书长连接启动中（app_id=%s）…" % (app_id or FEISHU_APP_ID or "未配置"))
    client.start()
