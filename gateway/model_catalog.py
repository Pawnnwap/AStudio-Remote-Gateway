# AI生成
# -*- coding: utf-8 -*-
"""模型目录扫描：以模型网关 /v1/models 目录为准，甄别哪些自带模型可用。

- 目录是模型路由的权威来源（astronclaw-auto 这类界面默认路由不在其中，投递必 409）
- 令牌来源：内核子进程环境变量 ASTUDIO_MODEL_CATALOG_TOKEN（零 API key，登录态派生）
- 结果缓存到 data/model_catalog.json，服务每次启动重新扫描
"""
from __future__ import annotations

import base64
import json
import time
import urllib.request
from pathlib import Path

from gateway.config import DATA_DIR

CACHE = DATA_DIR / "model_catalog.json"
KNOWN_BAD = {"astronclaw-auto": "App 界面默认路由，不经模型网关（投递 409，实测）"}


def _acode_pids() -> list[int]:
    import psutil

    return [p.info["pid"] for p in psutil.process_iter(["pid", "name"])
            if p.info["name"] and "acode" in p.info["name"].lower()]


def _catalog_token() -> str | None:
    """内核子进程环境变量里的目录令牌（App 注入，登录态派生）。"""
    import psutil

    for pid in _acode_pids():
        try:
            env = psutil.Process(pid).environ()
            tok = env.get("ASTUDIO_MODEL_CATALOG_TOKEN")
            if tok:
                return tok
        except Exception:
            continue
    return None


def _gateway_base_url(token: str) -> str | None:
    from gateway.astudio_mcp import _astudio_ports

    # 模型网关与 MCP 同属 App 进程端口；/v1/models 返回含 data.models 的 JSON 才算命中
    for port in _astudio_ports():
        url = f"http://127.0.0.1:{port}/v1/models"
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        try:
            r = urllib.request.urlopen(req, timeout=4)
            d = json.loads(r.read().decode("utf-8", errors="replace"))
            if isinstance(d.get("data", {}).get("models"), list):
                return f"http://127.0.0.1:{port}/v1"
        except (OSError, ValueError):
            continue
    return None


def _slug_name(slug: str) -> str | None:
    """从目录 slug 解出模型名：builtin/<b64> 与 custom/<cfg>/<rev>/<b64> 两种形态。"""
    parts = slug.split("/")
    try:
        if parts[0] == "builtin" and len(parts) == 2:
            return base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode("utf-8")
        if parts[0] == "custom" and len(parts) == 4:
            return base64.b64decode(parts[3] + "=" * (-len(parts[3]) % 4)).decode("utf-8")
    except Exception:
        return None
    return None


def _decode_builtin(slug: str) -> str:
    part = slug.split("/", 1)[1] if "/" in slug else slug
    try:
        return base64.b64decode(part + "=" * (-len(part) % 4)).decode(errors="replace")
    except Exception:
        return part


def scan(force: bool = False) -> dict:
    """扫描模型网关目录，返回 {"models": [...], "scanned_at": iso}。失败抛 RuntimeError。"""
    cache_p = Path(CACHE)
    if not force and cache_p.exists():
        try:
            d = json.loads(cache_p.read_text(encoding="utf-8"))
            if time.time() - d.get("scanned_at_epoch", 0) < 300:  # 5 分钟内缓存有效
                return d
        except (ValueError, OSError):
            pass

    tok = _catalog_token()
    base = _gateway_base_url(tok) if tok else None
    if not base and tok is None:
        # 无子进程环境令牌时，用 sagw 会话凭证兜底（/v1/models 同样接受）
        try:
            from gateway.astudio_mcp import ensure_session

            _, tokens = ensure_session()
            for t in tokens:
                base = _gateway_base_url(t)
                if base:
                    break
        except Exception:
            pass
    if not base:
        raise RuntimeError("未发现 AStudio 模型网关（AStudio 是否在运行？）")

    req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + tok})
    d = json.loads(urllib.request.urlopen(req, timeout=8).read())
    models = []
    seen = set()
    for m in d.get("data", {}).get("models", []):
        slug = m.get("slug", "")
        name = _slug_name(slug)
        if not name or name in seen:
            continue  # 解析失败的条目（非用户可见模型）跳过
        seen.add(name)
        models.append({
            "slug": slug,
            "name": name,
            "good": bool(m.get("supported_in_api")) and m.get("visibility") == "list"
                    and name not in KNOWN_BAD,
            "bad_reason": KNOWN_BAD.get(name, ""),
        })
    out = {"models": models, "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "scanned_at_epoch": time.time(), "gateway": base}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def good_models() -> list[dict]:
    """可直接投递的模型（目录成员且非已知坏路由）。scan 失败时返回缓存。"""
    try:
        d = scan()
    except RuntimeError:
        if CACHE.exists():
            d = json.loads(CACHE.read_text(encoding="utf-8"))
        else:
            return []
    return [m for m in d.get("models", []) if m.get("good")]


def good_names() -> list[str]:
    return [m["name"] for m in good_models()]
