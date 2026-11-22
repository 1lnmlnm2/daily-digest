#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify.py —— 只推送本轮真正新增的文章摘要到 Server 酱（微信）

运行方式：
    python scripts/notify.py

环境变量：
    SERVERCHAN_SENDKEY  必填。Server 酱 SendKey（以 SCT 开头）。
                        未配置时不会报错，只打印「跳过推送」。
    SITE_URL            可选。页面地址；不填则按 GitHub 仓库信息自动推断。

推送规则（阶段 3 + 阶段 6）：
    1. baseline.established 不是 true → 直接跳过，不调用 Server 酱，不改状态。
    2. push_mode=off 或 new_count=0 → 直接跳过，不调用 Server 酱，不改状态。
    3. 只从 data/state.json 中 status=pending 的条目生成消息，不再拿页面上的旧文章凑数。
    4. 消息包含真实新增数量、最多 3 条新增标题和页面链接。
    5. 每次运行最多只发起一次普通内容推送；失败就保留 pending，下一次运行再试。
    6. 只有 Server 酱明确返回成功，才把本轮全部 pending 标成 notified 并写入 notified_at。
    7. 日志绝不出现完整 SendKey；所有错误文本都会先经过 mask_secret()。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

import core

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "latest.json"
STATE_PATH = ROOT / "data" / "state.json"

API_BASE = "https://sctapi.ftqq.com"
REQUEST_TIMEOUT = 15      # 单次请求超时（秒）
PREVIEW_COUNT = 3         # 消息里最多列几条新增标题
KEY_PREFIX = "SCT"        # Server 酱 Turbo 的 SendKey 以此开头


def log(message: str) -> None:
    """打印日志，前缀统一，方便在 Actions 日志里搜索。"""
    print(f"[notify] {message}", flush=True)


def mask_secret(text: str, secret: str) -> str:
    """
    把文本里可能出现的 SendKey 全部替换掉。
    requests 抛出的异常字符串里常常带着完整 URL（URL 里就含 SendKey），
    所以每条错误信息都必须先过这个函数，再打印。
    """
    if not secret:
        return text
    for variant in {secret, quote(secret, safe="")}:
        if variant:
            text = text.replace(variant, "SCT***已隐藏***")
    return text


def load_json(path: Path):
    """读取 JSON；文件不存在或损坏都返回 None，由调用方决定是否跳过。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_state_atomic(path: Path, payload: dict) -> None:
    """先写临时文件再替换，避免状态文件写坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)


def pending_items(state: dict) -> list[dict]:
    """
    从 state.items 中取出所有 status=pending 的条目，按发布时间倒序。
    只认状态，不认页面上的展示列表；这样历史文章不会被当成新增。
    """
    result: list[dict] = []
    for item_id, meta in (state.get("items") or {}).items():
        if not isinstance(meta, dict) or meta.get("status") != "pending":
            continue
        result.append(
            {
                "id": item_id,
                "source_id": meta.get("source_id", ""),
                "source_name": meta.get("source_name", ""),
                "category": meta.get("category", ""),
                "title": meta.get("title", ""),
                "link": meta.get("link", ""),
                "published": meta.get("published"),
            }
        )

    def sort_key(item):
        moment = core.to_datetime(item.get("published"))
        return moment.timestamp() if moment else float("-inf")

    result.sort(key=sort_key, reverse=True)
    return result


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


def build_message(payload: dict, pending: list[dict], site_url: str) -> tuple[str, str]:
    """
    生成一条普通内容推送：
      - 真实新增数量用 pending 的实际条数，不用页面展示条数；
      - 最多列 3 条新增标题；
      - 附页面链接。
    """
    generated = payload.get("generated_at") or ""
    try:
        stamp = datetime.fromisoformat(generated).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        stamp = generated or "未知时间"

    count = len(pending)
    lines = [
        f"**本次新增：{count} 条**",
        "",
        f"更新时间：{stamp}",
        "",
    ]

    for index, item in enumerate(pending[:PREVIEW_COUNT], start=1):
        lines.append(f"{index}. {item.get('title', '')}")

    if count > PREVIEW_COUNT:
        lines.append(f"……另有 {count - PREVIEW_COUNT} 条新增未展开")

    if site_url:
        lines.extend(["", f"[打开页面]({site_url})"])

    # Server 酱的标题建议控制在 32 字以内
    title = f"每日科技速览 · 新增 {count} 条"
    if len(title) > 32:
        title = title[:32]
    return title, "\n".join(lines)


def mark_notified(state: dict, pending: list[dict], now: datetime) -> int:
    """
    推送成功后，把本轮全部 pending 标记为 notified。
    一条消息代表本轮全部新增；只列 3 条标题，但未展开的条目也已包含在“新增 N 条”里，
    所以同样要标记，避免下一轮重复补推。
    """
    now_text = now.isoformat(timespec="seconds")
    changed = 0
    for item in pending:
        item_id = item.get("id")
        meta = (state.get("items") or {}).get(item_id)
        if not isinstance(meta, dict) or meta.get("status") != "pending":
            continue
        meta["status"] = "notified"
        meta["notified_at"] = now_text
        meta["suppressed_reason"] = None
        changed += 1

    state["updated_at"] = now_text
    state["last_notify"] = {
        "at": now_text,
        "count": changed,
        "reason": "ok",
    }
    return changed


def send_to_serverchan(sendkey: str, title: str, desp: str) -> bool:
    """
    调用一次 Server 酱接口。成功返回 True，失败返回 False，任何情况下都不抛异常。
    不在本函数内重试：失败后保留 pending，由下一次运行重试，避免一次运行发出重复消息。
    """
    url = f"{API_BASE}/{sendkey}.send"
    body = {"title": title, "desp": desp}

    try:
        response = requests.post(url, data=body, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        result = response.json()
        code = result.get("code")
        try:
            code_value = int(code)
        except (TypeError, ValueError):
            code_value = None
        if code_value == 0:
            log("推送成功，微信应已收到消息")
            return True
        reason = result.get("message") or result.get("info") or "未知原因"
        log(f"Server 酱返回失败（code={code}）：{mask_secret(str(reason), sendkey)}")
        return False
    except Exception as exc:  # noqa: BLE001 —— 这里就是要兜住所有异常
        log(f"推送失败：{mask_secret(str(exc), sendkey)}")
        return False


def get_sendkey() -> str:
    """只有确定要推送时才调用，避免无新增运行读取密钥。"""
    return (os.environ.get("SERVERCHAN_SENDKEY") or "").strip()


def main() -> int:
    payload = load_json(DATA_PATH)
    if not isinstance(payload, dict):
        log(f"找不到或无法读取 {DATA_PATH.name}，跳过推送")
        return 0

    state = load_json(STATE_PATH)
    if not isinstance(state, dict):
        log(f"找不到或无法读取 {STATE_PATH.name}，跳过推送（pending 不会被标记）")
        return 0

    # 阶段 6：baseline 未建立时，普通内容推送必须严格为 0。
    # 这是独立于 fetch 的最终闸门：即使 push_mode=on、latest 里还有候选，也绝不调用 SendKey。
    baseline = state.get("baseline") or {}
    if baseline.get("established") is not True:
        log("baseline 尚未建立，跳过普通推送（不调用 Server 酱、不标记 notified）")
        return 0

    # 过渡期 / 无新增守卫：绝不调用推送，也绝不修改状态。
    push = payload.get("push") or {}
    if push.get("enabled") is not True:
        log("push_mode=off 过渡期，跳过普通推送（不标记已通知）")
        return 0

    declared_raw = push.get("new_count") or 0
    try:
        declared_count = int(declared_raw)
    except (TypeError, ValueError):
        declared_count = 0
    if declared_count <= 0:
        log("没有新增内容，跳过普通推送")
        return 0

    pending = pending_items(state)
    if not pending:
        log("state 中没有 pending 条目，跳过普通推送")
        return 0
    if declared_count != len(pending):
        log(
            f"提示：latest 记录的新增数为 {declared_count}，state 中 pending 为 {len(pending)}；"
            "以 state 为准"
        )

    site_url = build_site_url()
    if not site_url:
        log("无法生成页面链接，跳过推送（pending 保留，下一次再试）")
        return 0

    sendkey = get_sendkey()

    # 情况一：没有配置 Secret —— 明确提示跳过，不算错误
    if not sendkey:
        log("未配置 SERVERCHAN_SENDKEY，跳过推送（pending 保留，不影响抓取与部署）")
        return 0

    # 情况二：格式明显不对（比如误填了 Server 酱³ 的 sctp 密钥）
    if not sendkey.startswith(KEY_PREFIX):
        log("SERVERCHAN_SENDKEY 格式不符（应以 SCT 开头），跳过推送（pending 保留）")
        return 0

    title, desp = build_message(payload, pending, site_url)

    log(f"准备推送：{title}")
    if not send_to_serverchan(sendkey, title, desp):
        log("推送失败，pending 保留，下一次运行会重试（不标记 notified）")
        return 0

    now = datetime.now(core.get_timezone(payload.get("tz_offset") or 8))
    marked = mark_notified(state, pending, now)
    try:
        write_state_atomic(STATE_PATH, state)
    except Exception as exc:  # noqa: BLE001
        log(f"推送已成功，但 state 写入失败：{exc}；下一次可能重复 1 条，请按失败矩阵处理")
        return 1

    log(f"推送成功并已标记 {marked} 条为 notified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
