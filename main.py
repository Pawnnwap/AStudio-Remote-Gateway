# AI生成
# -*- coding: utf-8 -*-
"""网关主入口。

用法：
  python main.py                 # 以 FEISHU_APP_ID/SECRET 环境变量启动飞书长连接
  python main.py --mock          # 本地 mock 模式（stdin 输入消息，不经飞书）

环境变量：
  FEISHU_APP_ID / FEISHU_APP_SECRET   飞书自建应用凭证
  ASTUDIO_BIN / ACODE_HOME            AStudio 内核路径覆盖
  GATEWAY_DATA_DIR                    注册表数据目录
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import argparse


def run_mock() -> None:
    """本地 mock：stdin 逐行作为消息，open_id 固定 mock_user。"""
    from gateway.router import handle_message

    open_id = "mock_user"
    print("mock 模式：输入消息（/help 查看命令，Ctrl+C 退出），open_id =", open_id)
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        if text == "/quit":
            break
        try:
            reply = handle_message(open_id, text)
        except Exception as e:
            reply = f"[错误] {e}"
        print("=" * 40)
        print(reply)
        print("=" * 40)


def run_feishu() -> None:
    from gateway import feishu_client
    from gateway.config import FEISHU_APP_ID, FEISHU_APP_SECRET

    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        print("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量。")
        print("先在飞书开放平台创建企业自建应用：开机器人能力、事件订阅选长连接、")
        print("订阅 im.message.receive_v1、开通 im:message 系列权限。")
        sys.exit(1)
    feishu_client.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书-AStudio 网关")
    parser.add_argument("--mock", action="store_true", help="本地 mock 模式")
    args = parser.parse_args()
    if args.mock:
        run_mock()
    else:
        run_feishu()


if __name__ == "__main__":
    main()
