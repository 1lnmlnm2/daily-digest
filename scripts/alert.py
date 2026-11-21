#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alert.py —— 来源连续失败维护告警（与普通内容推送完全分离）

设计目标：
1. push_mode 只控制普通文章推送；本脚本只读取 alerts.enabled，不受 push_mode 影响。
2. 只有存在达到阈值且尚未告警的来源时，才读取 SERVERCHAN_SENDKEY。
3. 多个来源同时异常时只发送一条合并消息。
4. 发送失败不把 alert_open 置为 True；保留连续失败计数，下一次运行可重试。
5. 任何异常都只记录脱敏日志并返回 0，绝不使 workflow 失败。

运行方式：
    python scripts/alert.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import core
import notify

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yml"
STATE_PATH = ROOT / "data" / "state.json"
DATA_PATH = ROOT / "data" / "latest.json"

DEFAULT_AFTER_FAILURES = 2
KEY_PREFIX = "SCT"


def log(message: str) -> None:
    print(f"[alert] {message}", flush=True)


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_state_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)


def load_config() -> dict:
    import yaml

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"找不到配置文件：{CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("config/sources.yml 的顶层必须是键值对结构")
    return data


def get_alerts_config(config: dict) -> tuple[bool, int]:
    """
    读取独立告警开关和阈值。
    推荐配置：alerts.enabled: true + settings.alert_after_failures: 2。
    同时兼容 alerts.after_failures 覆盖阈值。
    """
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    alerts = config.get("alerts") if isinstance(config.get("alerts"), dict) else {}
    raw_enabled = alerts.get("enabled", False)
    if isinstance(raw_enabled, str):
        enabled = raw_enabled.strip().lower() in ("1", "true", "yes", "on")
    else:
        enabled = bool(raw_enabled)

    raw_threshold = alerts.get("after_failures", settings.get("alert_after_failures", DEFAULT_AFTER_FAILURES))
    try:
        threshold = int(raw_threshold)
    except (TypeError, ValueError):
        threshold = DEFAULT_AFTER_FAILURES
    if threshold < 1:
        threshold = DEFAULT_AFTER_FAILURES
    return enabled, threshold


def get_sendkey() -> str:
    """只有确定要发告警时才调用，避免无告警运行读取或打印密钥。"""
    return (os.environ.get("SERVERCHAN_SENDKEY") or "").strip()


def build_run_url() -> str:
    """只使用 GitHub Actions 提供的环境变量拼接，不伪造链接。"""
    server = (os.environ.get("GITHUB_SERVER_URL") or "").strip().rstrip("/")
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip().strip("/")
    run_id = (os.environ.get("GITHUB_RUN_ID") or "").strip()
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""


def short_time(value) -> str:
    moment = core.to_datetime(value)
    if moment is None:
        return "暂无记录"
    return moment.strftime("%Y-%m-%d %H:%M")


def collect_candidates(config: dict, state: dict, latest: dict | None) -> list[dict]:
    """按阈值收集本轮需要告警的来源；多源在这里合并，后面只发一次请求。"""
    enabled, threshold = get_alerts_config(config)
    if not enabled:
        return []

    latest_sources = {
        str(item.get("id")): item
        for item in ((latest or {}).get("sources") or [])
        if isinstance(item, dict) and item.get("id")
    }
    health_map = state.get("sources") or {}
    candidates: list[dict] = []

    for source in config.get("sources") or []:
        if not isinstance(source, dict) or not source.get("enabled", True):
            continue
        source_id = str(source.get("id") or "").strip()
        if not source_id:
            continue
        health = health_map.get(source_id) or {}
        try:
            failures = int(health.get("consecutive_failures") or 0)
        except (TypeError, ValueError):
            failures = 0
        alert_open = bool(health.get("alert_open", health.get("alert_active", False)))
        if failures < threshold or alert_open:
            continue

        latest_source = latest_sources.get(source_id) or {}
        candidates.append(
            {
                "id": source_id,
                "name": source.get("name") or source_id,
                "failures": failures,
                "last_success_at": health.get("last_success_at"),
                "last_attempt_at": health.get("last_attempt_at"),
                "stale": bool(latest_source.get("stale")) if latest_source else None,
            }
        )
    return candidates


def build_message(candidates: list[dict], run_url: str, now: datetime) -> tuple[str, str]:
    count = len(candidates)
    title = f"每日科技速览 · {count} 个来源告警"
    if len(title) > 32:
        title = title[:32]

    lines = [
        f"**来源连续失败告警：{count} 个来源**",
        "",
        f"运行时间：{now.strftime('%Y-%m-%d %H:%M')}",
    ]
    if run_url:
        lines.append(f"运行链接：{run_url}")
    else:
        lines.append("运行链接：不可用（缺少 GitHub Actions 环境变量）")
    lines.append("")

    for item in candidates:
        lines.append(f"- **{item['name']}**")
        lines.append(f"  连续失败：{item['failures']} 次")
        lines.append(f"  最近成功：{short_time(item.get('last_success_at'))}")
        lines.append(f"  最近尝试：{short_time(item.get('last_attempt_at'))}")
        if item.get("stale") is True:
            lines.append("  缓存保底：是")
        elif item.get("stale") is False:
            lines.append("  缓存保底：否")
        else:
            lines.append("  缓存保底：未知")
        lines.append("")

    lines.append("页面仍保留最近一次成功数据。")
    return title, "\n".join(lines)


def send_to_serverchan(sendkey: str, title: str, desp: str) -> bool:
    """复用 notify 的发送实现；测试可单独替换此函数。"""
    return notify.send_to_serverchan(sendkey, title, desp)


def _mark_attempt(candidates: list[dict], state: dict, now_text: str, error: str | None) -> None:
    for item in candidates:
        health = (state.get("sources") or {}).get(item["id"])
        if not isinstance(health, dict):
            continue
        health["alert_last_attempt_at"] = now_text
        if error:
            health["alert_last_error"] = error
        else:
            health["alert_last_error"] = None


def main(now: datetime | None = None) -> int:
    """返回 0 是硬约束：告警跳过、失败都不能使 workflow 失败。"""
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        log(f"配置读取失败，跳过告警：{exc}")
        return 0

    enabled, threshold = get_alerts_config(config)
    if not enabled:
        log("来源告警未启用，跳过")
        return 0

    state = load_json(STATE_PATH)
    if not isinstance(state, dict) or not isinstance(state.get("sources"), dict):
        log("state.json 不存在或不可读，跳过告警")
        return 0

    latest = load_json(DATA_PATH)
    candidates = collect_candidates(config, state, latest if isinstance(latest, dict) else None)
    if not candidates:
        log("没有达到阈值且未告警的来源，跳过（不读取 SendKey）")
        return 0

    # 只有确认存在候选后才读取密钥
    sendkey = get_sendkey()
    if not sendkey:
        log("未配置 SERVERCHAN_SENDKEY，告警未发送，保持可重试")
        return 0
    if not sendkey.startswith(KEY_PREFIX):
        log("SERVERCHAN_SENDKEY 格式不符（应以 SCT 开头），告警未发送，保持可重试")
        return 0

    if now is None:
        tz_offset = 8
        if isinstance(latest, dict):
            tz_offset = latest.get("tz_offset") or 8
        now = datetime.now(core.get_timezone(tz_offset))
    now_text = now.isoformat(timespec="seconds")
    run_url = build_run_url()
    title, desp = build_message(candidates, run_url, now)

    log(f"准备发送维护告警：{title}（{len(candidates)} 个来源，合并 1 条）")
    try:
        sent = send_to_serverchan(sendkey, title, desp)
    except Exception as exc:  # noqa: BLE001
        log(f"维护告警发送异常：{notify.mask_secret(str(exc), sendkey)}")
        sent = False

    if not sent:
        _mark_attempt(candidates, state, now_text, "发送失败，保持可重试")
        state["updated_at"] = now_text
        try:
            write_state_atomic(STATE_PATH, state)
        except Exception as exc:  # noqa: BLE001
            log(f"告警发送失败，且状态写入失败：{notify.mask_secret(str(exc), sendkey)}")
        log("维护告警发送失败，alert_open 保持 false，下一次运行可重试")
        return 0

    for item in candidates:
        health = (state.get("sources") or {}).get(item["id"])
        if not isinstance(health, dict):
            continue
        health["alert_open"] = True
        health["alert_active"] = True
        health["last_alert_at"] = now_text
        health["alert_last_attempt_at"] = now_text
        health["alert_last_error"] = None
    state["updated_at"] = now_text
    state["last_alert"] = {
        "at": now_text,
        "count": len(candidates),
        "source_ids": [item["id"] for item in candidates],
        "reason": "ok",
        "run_url": run_url,
    }

    try:
        write_state_atomic(STATE_PATH, state)
    except Exception as exc:  # noqa: BLE001
        log(f"告警已发送，但 state 写入失败：{notify.mask_secret(str(exc), sendkey)}；下一次可能重复尝试")
        return 0

    log(f"维护告警发送成功，已标记 {len(candidates)} 个来源为已告警")
    return 0


if __name__ == "__main__":
    sys.exit(main())
