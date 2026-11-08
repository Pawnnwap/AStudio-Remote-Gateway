# AI生成
# -*- coding: utf-8 -*-
"""AStudio App 控制面客户端：通过 App 自带 MCP 服务驱动 GUI 会话。

机制（本机逆向验证）：
- AStudio App 内置 MCP 服务（serverInfo.name=astudio，127.0.0.1 动态端口 /mcp）
- 认证用 provider-session 凭证（sagw_session_*），仅存在于 App 与内核子进程内存，
  从活内核子进程内存提取（本机本用户进程互操作）
- astron_send_message 让 App 自己对 GUI 线程发起回合：模型走 AStudio 自带通道
  （App 本地模型网关 + 登录态），全程无 API key；GUI 实时可见
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import re
import subprocess
import urllib.request
import urllib.error

TIMEOUT = 30


class McpError(RuntimeError):
    pass


# ---------- 发现 ----------


def _astudio_ports() -> list[int]:
    pids = subprocess.run(["tasklist"], capture_output=True, text=True).stdout
    astudio_pids = set(re.findall(r"AStudio\.exe\s+(\d+)", pids))
    net = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    ports = []
    for ln in net.splitlines():
        if "LISTENING" not in ln or "127.0.0.1:" not in ln:
            continue
        if ln.split()[-1] in astudio_pids:
            ports.append(int(ln.split()[1].rsplit(":", 1)[1]))
    return sorted(set(ports))


def _initialize(url: str, token: str) -> bool:
    """探测端点：initialize 成功且 serverInfo.name=astudio 才算 MCP 服务。"""
    req = urllib.request.Request(
        url, data=json.dumps({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "feishu-gateway", "version": "0.1"}},
        }).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": "Bearer " + token},
    )
    try:
        r = urllib.request.urlopen(req, timeout=4)
        body = r.read().decode("utf-8", errors="replace")
        d = _parse_body(body)
        return (d.get("result", {}).get("serverInfo", {}).get("name") == "astudio")
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return False


def extract_session_tokens() -> list[str]:
    """提取全部 provider-session 凭证（sagw_session_*）。

    来源依次扫描：内核子进程（快）→ AStudio App 全部进程（慢但最全）。
    App 侧协调器持有当前活凭证；凭证轮换后子进程内存里的旧凭证会 401，
    必须扫 App 才能拿到新值。返回全部候选，由端口×凭证矩阵验证。
    """
    import psutil
    from concurrent.futures import ThreadPoolExecutor

    pat = re.compile(rb"sagw_session_[0-9a-f-]{8,40}")
    procs = []
    seen_pids = set()
    for p in psutil.process_iter(["pid", "name"]):
        name = (p.info["name"] or "").lower()
        if "acode" in name:
            procs.append(p)
            seen_pids.add(p.info["pid"])
    for p in psutil.process_iter(["pid", "name"]):
        if p.info["name"] == "AStudio.exe" and p.info["pid"] not in seen_pids:
            procs.append(p)
    if not procs:
        raise McpError("未发现 AStudio 进程（AStudio 是否在运行？）")

    def scan(proc):
        try:
            return _scan_memory(proc.info["pid"], pat)
        except Exception:
            return []

    candidates: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, len(procs))) as ex:
        for toks in ex.map(scan, procs):
            for tok in toks:
                if tok not in candidates:
                    candidates.append(tok)
    if not candidates:
        raise McpError(
            "未能从 AStudio 进程内存提取会话凭证。"
            "请在 AStudio 界面里打开任意一个对话（会生成新凭证）后重试。"
        )
    return candidates


def _scan_memory(pid: int, pat: re.Pattern) -> list[str]:
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x0410, False, pid)  # VM_READ | QUERY_INFORMATION
    if not h:
        return []

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                    ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]

    out: list[str] = []
    addr = 0
    mbi = MBI()
    while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        size = mbi.RegionSize or 0x1000
        if mbi.State == 0x1000 and mbi.Protect in {0x02, 0x04, 0x20, 0x40} and size < 64 * 1024 * 1024:
            buf = ctypes.create_string_buffer(size)
            n = ctypes.c_size_t()
            if k32.ReadProcessMemory(h, ctypes.c_void_p(mbi.BaseAddress), buf, size, ctypes.byref(n)):
                out += [m.group(0).decode("ascii", errors="ignore")
                        for m in pat.finditer(buf.raw[: n.value])]
        addr = (mbi.BaseAddress or 0) + size
        if addr > 0x7FFFFFFF0000:
            break
    k32.CloseHandle(h)
    return out


def discover_endpoint() -> tuple[str, list[str]]:
    """发现 App MCP 服务：端口 × 凭证全矩阵验证，返回 (mcp_url, 候选凭证)。"""
    tokens = extract_session_tokens()
    ports = _astudio_ports()
    for port in ports:
        url = f"http://127.0.0.1:{port}/mcp"
        for tok in tokens:
            if _initialize(url, tok):
                # 把可用凭证排到队首，后续调用优先使用
                tokens.remove(tok)
                tokens.insert(0, tok)
                return url, tokens
    raise McpError(
        f"未发现可用的 AStudio App MCP 服务（已探测 {len(ports)} 个端口 × {len(tokens)} 个凭证）。"
        "请在 AStudio 界面里打开任意对话一次（刷新会话凭证）后重试。"
    )


# ---------- 会话 ----------

_cached: tuple[str, list[str]] | None = None  # (mcp_url, 候选凭证列表)


def ensure_session() -> tuple[str, list[str]]:
    global _cached
    if _cached:
        url, tokens = _cached
        if any(_initialize(url, tok) for tok in tokens):
            return url, tokens
        _cached = None
    _cached = discover_endpoint()
    return _cached


def _parse_body(body: str) -> dict:
    if body.startswith("event:") or "\ndata:" in body or body.startswith("data:"):
        for ln in body.splitlines():
            if ln.startswith("data:"):
                return json.loads(ln[5:].strip())
        raise ValueError("SSE 响应缺少 data 行")
    return json.loads(body)


def _rpc(url: str, token: str, method: str, params: dict, id_: int = 1) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps({"jsonrpc": "2.0", "id": id_, "method": method,
                              "params": params}).encode(),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": "Bearer " + token},
    )
    try:
        r = urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", errors="replace")
        raise McpError(f"MCP HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise McpError(f"MCP 连接失败: {e}") from e
    d = _parse_body(r.read().decode("utf-8", errors="replace"))
    if "error" in d:
        raise McpError(f"MCP 错误: {d['error']}")
    return d.get("result", {})


def call_tool(name: str, arguments: dict) -> dict:
    """调用 App MCP 工具；写操作受会话凭证约束，逐个候选凭证尝试。"""
    url, tokens = ensure_session()
    last: Exception | None = None
    for tok in tokens:
        try:
            result = _rpc(url, tok, "tools/call", {"name": name, "arguments": arguments})
        except McpError as e:
            s = str(e)
            if "caller_turn_inactive" in s or "caller_session_inactive" in s or "401" in s or "403" in s:
                last = e
                continue  # 该凭证无权/无活跃回合，换下一个候选
            raise
        content = result.get("content", [])
        text = "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
        if result.get("isError"):
            s = text[:300]
            if "caller_turn_inactive" in s or "caller_session_inactive" in s:
                last = McpError(f"工具 {name} 执行失败: {s}")
                continue
            raise McpError(f"工具 {name} 执行失败: {s}")
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}
    raise last or McpError(f"工具 {name} 所有候选凭证均不可用")


# ---------- 高层操作 ----------


def send_message(thread_id: str, message: str, mode: str = "queue") -> dict:
    """向 GUI 线程发送后续消息（App 发起回合，模型走自带通道）。"""
    return call_tool("astron_send_message",
                     {"threadId": thread_id, "message": message, "mode": mode})


def interrupt_thread(thread_id: str) -> dict:
    """打断线程当前正在执行的回合。"""
    return call_tool("astron_interrupt_thread", {"threadId": thread_id})


def thread_status(thread_id: str) -> str:
    """线程状态：working / idle / error / waiting-for-approval…（只读，无回合门槛）"""
    data = read_thread(thread_id, message_limit=1)
    return (data.get("status") or "").lower()


def read_thread(thread_id: str, message_limit: int = 30) -> dict:
    return call_tool("astron_read_thread",
                     {"threadId": thread_id, "messageLimit": message_limit})


_models_cache: list[str] | None = None


def list_model_slugs() -> list[str]:
    """AStudio 可用模型 slug 列表（astron_capabilities，只读，带缓存）。"""
    global _models_cache
    if _models_cache is None:
        r = call_tool("astron_capabilities", {})
        slugs: list[str] = []
        for p in r.get("providers", []):
            for m in p.get("models", []):
                slug = m.get("slug")
                # astronclaw-auto 是 App 界面默认路由，不经模型网关（投递会 409），不提供
                if slug and slug != "astronclaw-auto":
                    slugs.append(slug)
        _models_cache = slugs
    return _models_cache


def wait_for_reply(thread_id: str, baseline_index: int, timeout: int = 300, interval: int = 3) -> str:
    """轮询线程，等 baseline_index 之后的新助手消息；返回拼接文本。"""
    import time

    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            data = read_thread(thread_id, message_limit=30)
            msgs = data.get("messages", [])
            new = [m for m in msgs if m.get("index", 0) > baseline_index]
            status = (data.get("status") or "").lower()
            done_state = (data.get("latestTurnState") or "").lower()
            if new and status != "working" and done_state in ("completed", "failed", "idle", ""):
                parts = []
                for m in new:
                    who = "助手" if m.get("role") == "assistant" else "用户"
                    parts.append(f"[{who}] {m.get('text', '')}")
                return "\n".join(parts)
            last_err = None
        except (McpError, ValueError) as e:
            last_err = e
        time.sleep(interval)
    if last_err:
        raise McpError(f"等待回复超时（最后一次错误: {last_err}）")
    raise McpError(f"等待回复超时（{timeout}s）")
