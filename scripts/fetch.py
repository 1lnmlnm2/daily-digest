#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch.py —— M3 抓取入口：多源抓取 → 过滤 → 状态 → 归档 → data/latest.json

和 M1 的区别（照着看就能懂）：
1. 所有过滤规则集中到 scripts/core.py（纯函数），这里只负责联网、落盘、状态维护。
2. 五个源各自独立：日期窗口 → 关键词 → 单源限额，单源失败不会影响其他源。
3. 新增 data/state.json：记录每个文章的稳定 ID 和处理状态，保证手动重复运行
   不会重复归档、重复推送；升级后的第一次运行只建立基线，不推历史文章。
4. 新增 data/archive/YYYY-MM-DD.json：按文章发布日期归档，同一天重复运行按
   ID 合并去重，不会重复增加条目。
5. 失败保底继续保留：单源失败沿用上一次成功的数据，页面标「缓存」。

阶段 2 的推送开关由 config/sources.yml 的 push_mode 控制：
    "off" → 只抓取、归档、写状态（状态为 observed），notify.py 会直接跳过
    "on"  → 第一次运行只建立基线（把已有条目标 suppressed），之后只推新增

运行方式：
    python scripts/fetch.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import requests
import yaml

import core

# ---------- 路径与全局常量 ----------

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yml"
DATA_PATH = ROOT / "data" / "latest.json"
STATE_PATH = ROOT / "data" / "state.json"
ARCHIVE_DIR = ROOT / "data" / "archive"

# 抓取标识：写清楚是谁在抓，方便对方站长识别和屏蔽
USER_AGENT = "StaticFeedSite/1.0 (low-frequency RSS digest bot)"

# 下面的环境变量只为本地测试/排查提供便利，线上不设置，走默认值
REQUEST_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "20"))
MAX_RETRIES = max(1, int(os.environ.get("FETCH_MAX_RETRIES", "3")))
RETRY_BACKOFF = tuple(
    float(value)
    for value in os.environ.get("FETCH_RETRY_BACKOFF", "3,6,12").split(",")
    if value.strip()
) or (3.0, 6.0, 12.0)
REQUEST_INTERVAL = max(0.0, float(os.environ.get("FETCH_REQUEST_INTERVAL", "2.0")))

DEFAULT_UTC_OFFSET = 8
DEFAULT_TOTAL_MAX_ITEMS = 10
DEFAULT_ALERT_AFTER_FAILURES = 2


def log(message: str) -> None:
    """打印带时间的日志。GitHub Actions 的运行日志里能直接看到。"""
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


class NonRetryableError(Exception):
    """不需要重试的错误（例如 404 地址写错、403 被拒绝），直接判定失败。"""


# ---------- 读写 ----------


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"找不到配置文件：{CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("config/sources.yml 的顶层必须是键值对结构")
    return data


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def write_json_atomic(path: Path, payload: dict) -> None:
    """
    先写临时文件，成功了再替换正式文件。
    这样任何一次写失败都不会把上一份正常数据写坏。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, path)


def load_previous_data() -> dict:
    """读取上一次的 latest.json，用于单源失败时保底。"""
    if not DATA_PATH.exists():
        return {}
    try:
        return _read_json(DATA_PATH)
    except (json.JSONDecodeError, OSError) as exc:
        log(f"旧数据读取失败（{exc}），本次将不使用缓存")
        return {}


def load_state(source_ids) -> dict:
    """
    读取 data/state.json。文件不存在时生成初始骨架；
    文件损坏时直接报错退出，不能把历史状态悄悄清空。
    """
    if not STATE_PATH.exists():
        log("state.json 不存在，生成初始状态骨架")
        return core.empty_state(source_ids)

    try:
        state = _read_json(STATE_PATH)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"state.json 读取失败，为避免丢失历史状态，本次终止：{exc}") from exc

    if not isinstance(state.get("items"), dict):
        raise ValueError("state.json 的 items 必须是对象，本次终止以免误清历史状态")
    if not isinstance(state.get("sources"), dict):
        state["sources"] = {}

    skeleton = core.empty_state(source_ids)
    state.setdefault("schema", skeleton["schema"])
    state.setdefault("pipeline", skeleton["pipeline"])
    state.setdefault("baseline", {"established": False, "established_at": None})
    state["baseline"].setdefault("established", False)
    state["baseline"].setdefault("established_at", None)
    state.setdefault("updated_at", None)

    # 配置里新增/删除源时，保证 state 里的健康字段与配置对齐
    for source_id in source_ids:
        meta = state["sources"].get(source_id)
        if not isinstance(meta, dict):
            meta = {}
        defaults = skeleton["sources"][source_id]
        for key, value in defaults.items():
            meta.setdefault(key, value)
        state["sources"][source_id] = meta

    return state


# ---------- 网络 ----------


def robots_allowed(url: str) -> bool:
    """
    检查目标地址是否被 robots.txt 允许抓取。
    读不到 robots.txt（404、网络错误）时按「允许」处理——这是爬虫的通行做法。
    """
    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        response = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=15)
    except requests.RequestException as exc:
        log(f"  robots.txt 读取失败（{exc}），按允许处理")
        return True

    if response.status_code >= 400:
        log(f"  robots.txt 返回 {response.status_code}，按无限制处理")
        return True

    parser.parse(response.text.splitlines())
    allowed = parser.can_fetch(USER_AGENT, url)
    log(f"  robots.txt 检查：{'允许' if allowed else '禁止'}（{robots_url}）")
    return allowed


def http_get(url: str) -> requests.Response:
    """带重试的下载：5xx / 超时 / 连接错误会重试，4xx 直接失败。"""
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
                },
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 429:
                raise requests.HTTPError("HTTP 429 请求过于频繁")
            if 400 <= response.status_code < 500:
                raise NonRetryableError(f"HTTP {response.status_code}，地址可能写错或对方拒绝访问")
            if response.status_code >= 500:
                raise requests.HTTPError(f"HTTP {response.status_code}")
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
                log(f"  第 {attempt} 次请求失败（{exc}），{wait} 秒后重试")
                time.sleep(wait)
            else:
                log(f"  已重试 {MAX_RETRIES} 次，放弃")
    raise RuntimeError(f"下载失败：{last_error}")


# ---------- 单源 ----------


def fetch_one_source(source: dict, settings: dict, now: datetime, tz: timezone) -> dict:
    """抓取单个源，返回统一结构。任何异常都在内部消化。"""
    result = {
        "id": source["id"],
        "name": source.get("name") or source["id"],
        "home": source.get("home", ""),
        "url": source["url"],
        "type": source.get("type", "rss"),
        "category": source.get("category") or settings.get("default_category") or "未分类",
        "ok": False,
        "stale": False,
        "error": None,
        "updated_at": None,
        "stale_since": None,
        "item_count": 0,
        "items": [],
        "stats": {},
        "config": {},
    }

    log(f"→ 抓取 {result['name']}：{result['url']}")

    if not robots_allowed(result["url"]):
        result["error"] = "robots.txt 禁止抓取此地址"
        log(f"  ✗ {result['error']}")
        return result

    try:
        response = http_get(result["url"])
    except Exception as exc:  # noqa: BLE001 —— 这里就是要拦住所有异常
        result["error"] = str(exc)
        log(f"  ✗ {result['error']}")
        return result

    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        result["error"] = f"订阅内容解析失败：{feed.bozo_exception}"
        log(f"  ✗ {result['error']}")
        return result

    if not feed.entries:
        result["error"] = "抓取成功但没有任何条目（源可能返回了空数据）"
        log(f"  ✗ {result['error']}")
        return result

    processed = core.process_entries(feed.entries, source, settings, now, tz)
    result["items"] = processed["items"]
    result["stats"] = processed["stats"]
    result["config"] = processed["config"]
    result["item_count"] = len(processed["items"])
    result["ok"] = True
    result["updated_at"] = now.isoformat(timespec="seconds")

    stats = processed["stats"]
    log(
        "  ✓ 成功：抓到 {} 条，窗口/关键词淘汰 {} 条，保留 {} 条".format(
            stats.get("entries_total", 0),
            stats.get("out_of_window", 0)
            + stats.get("future_skipped", 0)
            + stats.get("undated_skipped", 0)
            + stats.get("keyword_excluded", 0)
            + stats.get("keyword_not_included", 0),
            stats.get("kept", 0),
        )
    )
    return result


def apply_cache_fallback(result: dict, previous: dict | None, health: dict, now: datetime) -> None:
    """
    失败保底：本次没抓到（或源返回空）时，用上一次的条目填充并标 stale。
    「最近一次成功抓取时间」从 state 的健康记录继承，和文章发布时间严格分开。
    """
    if not result["updated_at"]:
        result["updated_at"] = (health or {}).get("last_success_at") or (
            (previous or {}).get("updated_at") or (previous or {}).get("stale_since")
        )
    result["stale_since"] = (health or {}).get("last_success_at") or (previous or {}).get("updated_at")

    if result["ok"] and result["items"]:
        return

    prev_items = (previous or {}).get("items") or []
    if not prev_items:
        return

    cached = []
    for item in prev_items:
        if not isinstance(item, dict):
            continue
        copy = dict(item)
        copy["stale"] = True
        copy.setdefault("source_id", result["id"])
        copy.setdefault("source_name", result["name"])
        copy.setdefault("category", result["category"])
        cached.append(copy)

    result["items"] = cached
    result["item_count"] = len(cached)
    result["stale"] = True
    log(f"  ↳ 本次失败，沿用上次缓存（{result['stale_since']}，{len(cached)} 条）")


# ---------- 状态 ----------


def _now_iso(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def mark_source_success(state: dict, source_id: str, now: datetime) -> None:
    health = state["sources"].setdefault(source_id, {})
    failures = int(health.get("consecutive_failures") or 0)
    if failures:
        log(f"  ↺ {source_id} 恢复正常（此前连续失败 {failures} 次）")
    health["consecutive_failures"] = 0
    health["last_success_at"] = _now_iso(now)
    health["status"] = "ok"
    health["alert_active"] = False
    health["last_error"] = None


def mark_source_failure(state: dict, source_id: str, error: str, now: datetime) -> None:
    health = state["sources"].setdefault(source_id, {})
    health["consecutive_failures"] = int(health.get("consecutive_failures") or 0) + 1
    health["last_failure_at"] = _now_iso(now)
    health["status"] = "failing"
    health["last_error"] = error


def register_items(
    state: dict,
    items,
    source: dict,
    now: datetime,
    push_enabled: bool,
    baseline_established: bool,
) -> list[str]:
    """
    把本源的候选条目写入 state：
      - 已存在的条目只更新 last_seen / 展示字段，绝不改变状态；
      - 新条目在 push_mode=on 且基线已建立时为 pending，否则为 observed。
      - 基线已建立后，第一次见到的“发布时间早于基线时刻”的文章按历史文章处理，
        标 suppressed(pre_baseline)，避免某个源晚恢复时把旧文当成新增补推。
    返回本次首次出现的 ID 列表（用于日志；推送候选以 state 里的 pending 为准）。
    """
    now_text = _now_iso(now)
    new_ids: list[str] = []
    baseline_at = (state.get("baseline") or {}).get("established_at")

    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue
        meta = state["items"].get(item_id)
        if meta:
            meta["last_seen"] = now_text
            meta["title"] = item.get("title", meta.get("title", ""))
            meta["link"] = item.get("link", meta.get("link", ""))
            meta["category"] = item.get("category", meta.get("category", ""))
            meta["published"] = item.get("published", meta.get("published"))
            continue

        status = "observed"
        suppressed_reason = None
        if push_enabled and baseline_established:
            published_at = core.to_datetime(item.get("published"))
            boundary = core.to_datetime(baseline_at)
            if published_at is None or boundary is None or published_at <= boundary:
                status = "suppressed"
                suppressed_reason = "pre_baseline"
            else:
                status = "pending"

        state["items"][item_id] = {
            "source_id": source.get("id", ""),
            "source_name": source.get("name") or source.get("id", ""),
            "category": item.get("category", ""),
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "published": item.get("published"),
            "first_seen": now_text,
            "last_seen": now_text,
            "status": status,
            "notified_at": None,
            "suppressed_reason": suppressed_reason,
        }
        new_ids.append(item_id)

    return new_ids


def apply_baseline(state: dict, now: datetime) -> int:
    """
    正式首次 push_mode=on 时建立基线：
      - 把此前所有 observed / pending 条目转成 suppressed(baseline)；
      - notified / suppressed 保持原状态，不重置；
      - 已建立过基线时直接返回 0，幂等。
    这样首次开启推送不会把历史文章集中推出去，off 期间的条目也不会在恢复后补推。
    """
    baseline = state.setdefault("baseline", {"established": False, "established_at": None})
    if baseline.get("established"):
        return 0

    changed = 0
    for meta in state["items"].values():
        status = (meta or {}).get("status")
        if status in ("observed", "pending"):
            meta["status"] = "suppressed"
            meta["suppressed_reason"] = "baseline"
            changed += 1

    baseline["established"] = True
    baseline["established_at"] = _now_iso(now)
    baseline["suppressed_count"] = changed
    return changed


def pending_items(state: dict) -> list[dict]:
    """取出等待推送的条目，按发布时间倒序。"""
    result = []
    for item_id, meta in state["items"].items():
        if (meta or {}).get("status") != "pending":
            continue
        result.append(
            {
                "id": item_id,
                "source_id": meta.get("source_id", ""),
                "source_name": meta.get("source_name", ""),
                "title": meta.get("title", ""),
                "link": meta.get("link", ""),
                "category": meta.get("category", ""),
                "published": meta.get("published"),
            }
        )
    result.sort(
        key=lambda item: core.to_datetime(item.get("published")).timestamp()
        if core.to_datetime(item.get("published"))
        else float("-inf"),
        reverse=True,
    )
    return result


# ---------- 归档 ----------


def merge_archive_day(date_text: str, items, now: datetime) -> int:
    """
    把条目合并进 data/archive/<日期>.json：
      - 以稳定 ID 去重，同一天重复运行不会重复增加条目；
      - 已有条目更新展示字段，保留首次归档时间。
    返回该文件当前的条目数。
    """
    path = ARCHIVE_DIR / f"{date_text}.json"
    now_text = _now_iso(now)
    payload = {"date": date_text, "updated_at": now_text, "count": 0, "items": []}
    if path.exists():
        try:
            payload = _read_json(path)
        except (json.JSONDecodeError, OSError) as exc:
            log(f"归档文件 {path.name} 读取失败（{exc}），本次将重建该文件")

    existing = {
        str(item.get("id")): item
        for item in (payload.get("items") or [])
        if isinstance(item, dict) and item.get("id")
    }
    ordered = list(existing.values())

    for item in items:
        item_id = item.get("id")
        if not item_id:
            continue
        entry = existing.get(item_id)
        if entry is None:
            entry = {
                "id": item_id,
                "source_id": item.get("source_id", ""),
                "source_name": item.get("source_name", ""),
                "category": item.get("category", ""),
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "summary": item.get("summary", ""),
                "author": item.get("author", ""),
                "published": item.get("published"),
                "first_collected_at": now_text,
            }
            existing[item_id] = entry
            ordered.append(entry)
        else:
            entry["title"] = item.get("title", entry.get("title", ""))
            entry["link"] = item.get("link", entry.get("link", ""))
            entry["summary"] = item.get("summary", entry.get("summary", ""))
            entry["category"] = item.get("category", entry.get("category", ""))

    ordered.sort(
        key=lambda item: core.to_datetime(item.get("published")).timestamp()
        if core.to_datetime(item.get("published"))
        else float("-inf"),
        reverse=True,
    )
    payload = {
        "date": date_text,
        "updated_at": now_text,
        "count": len(ordered),
        "items": ordered,
    }
    write_json_atomic(path, payload)
    return len(ordered)


def archive_fresh_items(items, now: datetime) -> tuple[int, int]:
    """按文章发布日（北京时间）归档，返回 (归档文件数, 合并后条目总数)。"""
    by_date: dict[str, list] = {}
    for item in items:
        published = item.get("published")
        if not published:
            continue
        date_text = str(published)[:10]
        try:
            datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            continue
        by_date.setdefault(date_text, []).append(item)

    total = 0
    for date_text in sorted(by_date):
        total += merge_archive_day(date_text, by_date[date_text], now)
    return len(by_date), total


def prune_archive(retention_days: int, today: datetime) -> int:
    """删除超过保留天数的归档文件，避免仓库无限膨胀。"""
    if not ARCHIVE_DIR.exists():
        return 0
    cutoff = (today - timedelta(days=retention_days)).date()
    removed = 0
    for path in ARCHIVE_DIR.glob("*.json"):
        try:
            file_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            path.unlink()
            removed += 1
    return removed


# ---------- 组装 ----------


def build_run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""


def group_categories(display_items, sources) -> list[dict]:
    """按配置里的 source 顺序生成分类分组，组内按发布时间倒序。"""
    order: list[str] = []
    for source in sources:
        category = source.get("category") or "未分类"
        if category not in order:
            order.append(category)

    groups = []
    for category in order:
        items = [item for item in display_items if item.get("category") == category]
        items.sort(
            key=lambda item: core.to_datetime(item.get("published")).timestamp()
            if core.to_datetime(item.get("published"))
            else float("-inf"),
            reverse=True,
        )
        groups.append({"name": category, "count": len(items), "items": items})
    return groups


# ---------- 主流程 ----------


def main(now: datetime | None = None) -> int:
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        log(f"配置文件有问题，任务终止：{exc}")
        return 1

    settings = config.get("settings") or {}
    tz = core.get_timezone(settings.get("timezone_offset", DEFAULT_UTC_OFFSET))

    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    sources = [item for item in (config.get("sources") or []) if item.get("enabled", True)]
    if not sources:
        log("配置文件里没有任何启用的源（enabled: true），任务终止")
        return 1

    source_ids = [str(item.get("id") or "").strip() for item in sources]
    if any(not source_id for source_id in source_ids) or len(set(source_ids)) != len(source_ids):
        log("源配置里的 id 不能为空或重复，任务终止")
        return 1

    try:
        state = load_state(source_ids)
    except Exception as exc:  # noqa: BLE001
        log(f"状态文件有问题，任务终止（上一份数据未被修改）：{exc}")
        return 1

    push_mode = str(settings.get("push_mode", "off")).strip().lower()
    push_enabled = push_mode == "on"
    baseline_established = bool(state["baseline"].get("established"))

    previous_data = load_previous_data()
    previous_map = {
        item.get("id"): item
        for item in (previous_data.get("sources") or [])
        if isinstance(item, dict) and item.get("id")
    }

    results = []
    fresh_items = []
    for index, source in enumerate(sources):
        if index > 0 and REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL)

        try:
            result = fetch_one_source(source, settings, now, tz)
        except Exception as exc:  # noqa: BLE001 —— 单个源的意外错误也不能中断整体
            log(f"  ✗ 抓取 {source.get('id')} 时出现未预期错误：{exc}")
            result = {
                "id": source.get("id"),
                "name": source.get("name") or source.get("id"),
                "home": source.get("home", ""),
                "url": source.get("url", ""),
                "type": source.get("type", "rss"),
                "category": source.get("category") or "未分类",
                "ok": False,
                "stale": False,
                "error": f"未预期错误：{exc}",
                "updated_at": None,
                "stale_since": None,
                "item_count": 0,
                "items": [],
                "stats": {},
                "config": {},
            }

        if result["ok"]:
            mark_source_success(state, result["id"], now)
            register_items(state, result["items"], source, now, push_enabled, baseline_established)
            fresh_items.extend(result["items"])
        else:
            mark_source_failure(state, result["id"], result["error"] or "未知错误", now)
            apply_cache_fallback(result, previous_map.get(result["id"]), state["sources"].get(result["id"]), now)
            # 缓存条目只记录为已见过，绝不标成新增/已通知
            for item in result["items"]:
                item_id = item.get("id")
                if item_id and item_id not in state["items"]:
                    state["items"][item_id] = {
                        "source_id": result["id"],
                        "category": result["category"],
                        "title": item.get("title", ""),
                        "link": item.get("link", ""),
                        "published": item.get("published"),
                        "first_seen": _now_iso(now),
                        "last_seen": _now_iso(now),
                        "status": "observed",
                        "notified_at": None,
                        "suppressed_reason": "cache_fallback",
                    }

        result["health"] = dict(state["sources"].get(result["id"]) or {})
        results.append(result)

    # ---------- 正式首次基线 ----------
    baseline_this_run = False
    if push_enabled and not state["baseline"].get("established"):
        changed = apply_baseline(state, now)
        baseline_this_run = True
        baseline_established = True
        log(f"首次 push_mode=on：已建立基线，抑制 {changed} 条历史条目，本次不推送")

    # ---------- 全站限额 ----------
    total_max = int(settings.get("total_max_items") or DEFAULT_TOTAL_MAX_ITEMS)
    limited = core.merge_and_limit(fresh_items, total_max)
    kept_ids = {item.get("id") for item in limited}

    # 被全站限额挤掉的条目：状态改为 observed，避免以后被当成新增补推
    for item in fresh_items:
        item_id = item.get("id")
        if item_id and item_id not in kept_ids:
            meta = state["items"].get(item_id)
            if meta and meta.get("status") == "pending":
                meta["status"] = "observed"
                meta["suppressed_reason"] = "global_cap"

    # 每个源只保留最终入选的条目；缓存条目原样保留
    for result in results:
        if result["stale"]:
            continue
        result["items"] = [item for item in result["items"] if item.get("id") in kept_ids]
        result["item_count"] = len(result["items"])

    # ---------- 待推送 ----------
    pending = pending_items(state)
    if not push_enabled:
        push_reason = "push_mode_off"
    elif baseline_this_run:
        push_reason = "baseline_established"
    elif not pending:
        push_reason = "no_new_items"
    else:
        push_reason = "ok"

    push_payload = {
        "enabled": push_enabled,
        "mode": push_mode,
        "baseline_established": bool(state["baseline"].get("established")),
        "baseline_established_this_run": baseline_this_run,
        "new_count": len(pending) if push_enabled else 0,
        "reason": push_reason,
        "items": [
            {
                "id": item["id"],
                "title": item["title"],
                "link": item["link"],
                "source_name": item["source_name"],
                "category": item["category"],
                "published": item["published"],
            }
            for item in pending[:10]
        ],
    }

    # ---------- 归档 ----------
    try:
        archive_files, archive_total = archive_fresh_items(limited, now)
        archive_removed = prune_archive(
            int(settings.get("archive_retention_days") or 90), now
        )
    except Exception as exc:  # noqa: BLE001
        log(f"归档写入失败，任务终止（latest.json 与 state.json 未被修改）：{exc}")
        return 1

    # ---------- 状态清理 ----------
    try:
        state["items"], state_removed = core.prune_state_items(
            state["items"],
            now,
            retention_days=int(settings.get("state_retention_days") or 120),
            max_items=int(settings.get("state_max_items") or 3000),
        )
    except Exception as exc:  # noqa: BLE001
        log(f"状态清理失败，任务终止：{exc}")
        return 1

    state["updated_at"] = _now_iso(now)
    state["last_run"] = {
        "at": _now_iso(now),
        "run_url": build_run_url(),
        "push_mode": push_mode,
        "baseline_established": bool(state["baseline"].get("established")),
    }

    # ---------- 展示数据 ----------
    stale_items = []
    for result in results:
        if not result["stale"]:
            continue
        for item in result["items"]:
            item = dict(item)
            item["stale"] = True
            item["source_id"] = result["id"]
            item["source_name"] = result["name"]
            item["category"] = result["category"]
            stale_items.append(item)

    display_items = list(limited) + stale_items
    if total_max > 0 and len(display_items) > total_max:
        display_items = display_items[:total_max]

    category_groups = group_categories(display_items, sources)

    ok_count = sum(1 for item in results if item["ok"])
    stale_count = sum(1 for item in results if item["stale"])
    failed_count = len(results) - ok_count
    push_new_count = push_payload["new_count"]

    payload = {
        "schema": 3,
        "pipeline": "m3",
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "run_url": build_run_url(),
        "tz_offset": int(settings.get("timezone_offset") or DEFAULT_UTC_OFFSET),
        "push": push_payload,
        "settings": {
            "total_max_items": total_max,
            "archive_retention_days": int(settings.get("archive_retention_days") or 90),
            "undated_policy": settings.get("undated_policy", "skip"),
        },
        "stats": {
            "source_total": len(results),
            "source_ok": ok_count,
            "source_failed": failed_count,
            "source_cached": stale_count,
            "item_total": len(display_items),
            "new_count": push_new_count,
            "archive_files": archive_files,
            "archive_items": archive_total,
            "state_items_removed": state_removed,
            "archive_files_removed": archive_removed,
        },
        "sources": results,
        "categories": category_groups,
    }

    try:
        write_json_atomic(STATE_PATH, state)
        write_json_atomic(DATA_PATH, payload)
    except Exception as exc:  # noqa: BLE001
        log(f"数据写入失败，任务终止：{exc}")
        return 1

    log(
        "完成：{} 个源，成功 {} 个，失败 {} 个（其中 {} 个沿用缓存），展示 {} 条，待推送 {} 条".format(
            len(results), ok_count, failed_count, stale_count, len(display_items), push_new_count
        )
    )
    if not push_enabled:
        log("push_mode=off 过渡期：只归档与写状态，不推送普通内容")
    elif baseline_this_run:
        log("本次为首次基线运行：历史文章只归档，不推送")
    elif not pending:
        log("没有新增内容，普通推送将跳过")
    log(f"数据已写入：{DATA_PATH}")
    log(f"状态已写入：{STATE_PATH}")
    log(f"归档目录：{ARCHIVE_DIR}（本次涉及 {archive_files} 个日期文件）")

    # 关键：即使所有源都失败，也返回 0。
    # 这样页面仍可部署（显示缓存与告警），不会让 workflow 直接失败。
    return 0


if __name__ == "__main__":
    sys.exit(main())
