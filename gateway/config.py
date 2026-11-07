# AI生成
# -*- coding: utf-8 -*-
"""网关配置：路径解析、模型表、默认参数。全部支持环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("GATEWAY_DATA_DIR", GATEWAY_ROOT / "data"))
REGISTRY_PATH = DATA_DIR / "registry.json"

# astron-code 内核（GUI 会话所在 overlay home，只读发现 + 接管绑定用）
GUI_ACODE_HOME_CANDIDATES = [
    r"E:\AStudio Data\acode-home-overlay",
    r"C:\ProgramData\AStudio\Data\acode-home-overlay",
]

# 飞书应用凭证（环境变量优先）
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_DOMAIN = os.environ.get("FEISHU_DOMAIN", "https://open.feishu.cn")

EXEC_TIMEOUT_SECONDS = int(os.environ.get("GATEWAY_EXEC_TIMEOUT", "600"))


def resolve_gui_acode_home() -> str:
    env = os.environ.get("ASTUDIO_GUI_ACODE_HOME")
    if env and Path(env).exists():
        return env
    for c in GUI_ACODE_HOME_CANDIDATES:
        if (Path(c) / "state_5.sqlite").exists():
            return c
    raise RuntimeError("未找到 AStudio overlay acode-home（GUI 会话所在内核 home）")


DATA_DIR.mkdir(parents=True, exist_ok=True)
