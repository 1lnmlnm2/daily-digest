#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify.py —— 把本次更新摘要推送到 Server 酱（微信）

三条安全约定：
1. 没配置 SendKey → 跳过推送，退出码 0，不影响任何其他流程。
2. 网络超时、HTTP 错误、Server 酱返回失败 → 只记录脱敏后的错误，退出码 0。
3. 日志里绝不出现完整 SendKey：所有错误信息都会先经过 mask_secret()。

推送内容只有三样：更新时间、前 3 条标题、页面链接。不推正文。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "latest.json"

API_BASE = "https://sctapi.ftqq.com"
REQUEST_TIMEOUT = 15      # 单次请求超时（秒）
MAX_ATTEMPTS = 2          # 最多尝试两次，容忍一次网络抖动
RETRY_WAIT = 3            # 重试前等待（秒）
PREVIEW_COUNT = 3         # 消息里最多列几条标题
KEY_PREFIX = "SCT"        # Server 酱 Turbo 的 SendKey 以此开头


def log(message: str) -> None:
    print(f"[notify] {message}", flush=True)


def mask_secret(text: str, secret: str) -> str:
    """
    把文本里可能出现的 SendKey 全部替换掉。
    requests 的异常字符串常带着完整 URL（URL 里就含 SendKey），
    所以每条错误信息都必须先过这里再打印。
    """
    if not secret:
        return text
    for variant in {secret, quote(secret, safe="")}:
        if variant:
            text = text.replace(variant, "SCT***已隐藏***")
    return text


def build_site_url() -> str:
    """页面地址：优先读 SITE_URL，否则用 GitHub 仓库信息推断。"""
    explicit = (os.environ.get("SITE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/") + "/"

    owner = (os.environ.get("GITHUB_REPOSITORY_OWNER") or "").strip()
    repo_full = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    repo = repo_full.split("/")[-1] if "/" in repo_full else repo_full
    if owner and repo:
        return f"https://{owner}.github.io/{repo}/"
    return ""


def build_message(payload: dict, site_url: str) -> tuple[str, str]:
    """拼出推送标题和正文（Markdown）。"""
    generated = payload.get("generated_at") or ""
    try:
        stamp = datetime.fromisoformat(generated).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        stamp = generated or "未知时间"

    stats = payload.get("stats") or {}
    lines = [
        f"**更新时间：{stamp}**",
        "",
        f"本次成功 {stats.get('source_ok', 0)}/{stats.get('source_total', 0)} 个源，"
        f"共 {stats.get('item_total', 0)} 条。",
    ]

    items = []
    for source in payload.get("sources") or []:
        for item in (source.get("items") or []):
            if item.get("title"):
                items.append(item)
    items = items[:PREVIEW_COUNT]

    if items:
        lines.append("")
        for index, item in enumerate(items, start=1):
            lines.append(f"{index}. {item['title']}")

    if site_url:
        lines.extend(["", f"[打开页面]({site_url})"])

    title = f"每日科技速览已更新 · {stamp}"
    if len(title) > 32:
        title = title[:32]
    return title, "\n".join(lines)


def send_to_serverchan(sendkey: str, title: str, desp: str) -> bool:
    """调用 Server 酱接口。成功 True，失败 False，任何情况下都不抛异常。"""
    url = f"{API_BASE}/{sendkey}.send"
    body = {"title": title, "desp": desp}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(url, data=body, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            result = response.json()
            code = result.get("code")
            if code == 0:
                log("推送成功，微信应已收到消息")
                return True
            reason = result.get("message") or result.get("info") or "未知原因"
            log(f"Server 酱返回失败（code={code}）：{reason}")
            return False
        except Exception as exc:
            error = mask_secret(str(exc), sendkey)
            if attempt < MAX_ATTEMPTS:
                log(f"第 {attempt} 次推送失败：{error}；{RETRY_WAIT} 秒后重试")
                time.sleep(RETRY_WAIT)
            else:
                log(f"推送失败：{error}")
    return False


def main() -> int:
    sendkey = (os.environ.get("SERVERCHAN_SENDKEY") or "").strip()

    if not sendkey:
        log("未配置 SERVERCHAN_SENDKEY，跳过推送（不影响抓取与部署）")
        return 0

    if not sendkey.startswith(KEY_PREFIX):
        log("SERVERCHAN_SENDKEY 格式不符（应以 SCT 开头），跳过推送")
        return 0

    if not DATA_PATH.exists():
        log(f"找不到 {DATA_PATH.name}，跳过推送")
        return 0

    try:
        payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"数据文件读取失败，跳过推送：{mask_secret(str(exc), sendkey)}")
        return 0

    site_url = build_site_url()
    title, desp = build_message(payload, site_url)

    log(f"准备推送：{title}")
    if not site_url:
        log("提示：未能推断页面地址，消息里不会附带链接")

    try:
        send_to_serverchan(sendkey, title, desp)
    except Exception as exc:
        log(f"推送过程出现未预期错误：{mask_secret(str(exc), sendkey)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
