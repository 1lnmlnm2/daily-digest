#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch.py —— 抓取订阅源，汇总写入 data/latest.json

设计要点（照着看就能懂）：
1. 只读取 config/sources.yml 里配置的公开 RSS / Atom 地址，不抓正文、不绕验证。
2. 每个源单独处理：一个源超时、报错或返回空数据，不会影响其他源，也不会让任务失败。
3. 某个源抓取失败时，自动沿用上一次成功的数据（data/latest.json），并打上 stale 标记，
   页面会显示「缓存」提示。这样站点不会因为一个源挂掉就变空白。
4. 请求前检查 robots.txt，请求之间留出间隔，控制抓取频率。

运行方式：
    python scripts/fetch.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import requests
import yaml

# ---------- 路径与全局常量 ----------

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yml"
DATA_PATH = ROOT / "data" / "latest.json"

# 抓取标识：写清楚是谁在抓，方便对方站长识别和屏蔽
USER_AGENT = "StaticFeedSite/1.0 (low-frequency RSS digest bot)"

REQUEST_TIMEOUT = 20        # 单次请求超时（秒）
MAX_RETRIES = 3             # 单个源最多尝试几次
RETRY_BACKOFF = (3, 6, 12)  # 每次重试前等待的秒数
REQUEST_INTERVAL = 2.0      # 两个源之间的间隔，避免连续请求给对方压力

# 页面和日志统一使用北京时间（UTC+8）。settings.timezone_offset 可改。
DEFAULT_UTC_OFFSET = 8

# 全局时区对象：main() 会根据配置覆盖它，先给一个默认值保证函数可直接调用
GLOBAL_TZ = timezone(timedelta(hours=DEFAULT_UTC_OFFSET))


def log(message: str) -> None:
    """打印带时间的日志。GitHub Actions 的运行日志里能直接看到。"""
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


class NonRetryableError(Exception):
    """不需要重试的错误（例如 404 地址写错、403 被拒绝），直接判定失败。"""


def get_tz(settings: dict) -> timezone:
    """按配置里的时区偏移构造时区对象，默认北京时间。"""
    try:
        offset = int(settings.get("timezone_offset", DEFAULT_UTC_OFFSET))
    except (TypeError, ValueError):
        offset = DEFAULT_UTC_OFFSET
    return timezone(timedelta(hours=offset))


def load_config() -> dict:
    """读取 YAML 配置。文件不存在或格式写错时直接报错退出，方便排查。"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"找不到配置文件：{CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("config/sources.yml 的顶层必须是键值对结构")
    return data


def load_previous_data() -> dict:
    """读取上一次抓取的结果，用于失败时保底。文件不存在就返回空字典。"""
    if not DATA_PATH.exists():
        return {}
    try:
        with DATA_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log(f"旧数据读取失败（{exc}），本次将不使用缓存")
        return {}


_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_SPACE_RE = re.compile(r"[\t\r\f\v]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
# 很多 RSS 会在摘要里塞「查看全文」「阅读原文」之类的跳转提示（有的在末尾，有的在中间），
# 这些词对读者没有意义，统一清理掉，连同它周围的括号和省略号。
_READ_MORE_RE = re.compile(
    r"[\[\]\.…\s]*(查看全文|阅读全文|阅读原文|阅读更多|详见原文|Read more|Continue reading)[\[\]\.…\s]*",
    re.IGNORECASE,
)


def clean_text(value) -> str:
    """把摘要里的 HTML 标签、多余空白清理成纯文本。"""
    if not value:
        return ""
    text = str(value)
    # 先去标签，再还原 HTML 实体（&amp; → &），再去一次标签，
    # 防止 "&lt;script&gt;" 这类内容还原后又被当成标签带到页面上。
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    text = _TAG_RE.sub(" ", text)
    text = _INLINE_SPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    text = _READ_MORE_RE.sub("", text)
    return text.strip()


def truncate(text: str, limit: int) -> str:
    """按字符数截断摘要，超出部分用省略号代替。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def normalize_link(link: str, base: str) -> str:
    """把相对链接补成完整链接，并挡掉 javascript: 之类的非 http 链接。"""
    link = (link or "").strip()
    if not link:
        return ""
    absolute = urljoin(base, link)
    scheme = urlparse(absolute).scheme.lower()
    if scheme not in ("http", "https"):
        return ""
    return absolute


def entry_published(entry) -> str | None:
    """
    取条目的发布时间，统一转成带时区的 ISO 字符串。
    feedparser 会把时间解析成 UTC 的 struct_time，这里再换成本地偏移。
    """
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                moment = datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            return moment.astimezone(GLOBAL_TZ).isoformat(timespec="seconds")
    return None


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
        response = requests.get(
            robots_url, headers={"User-Agent": USER_AGENT}, timeout=15
        )
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
    """
    带重试的下载：5xx / 超时 / 连接错误会重试，4xx 直接失败（重试也没用）。
    """
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
            # 429 是「请求太频繁」，等一会儿再试有意义
            if response.status_code == 429:
                raise requests.HTTPError("HTTP 429 请求过于频繁")
            # 其它 4xx 是确定性错误（地址写错、被拒绝），重试也没用，直接失败
            if 400 <= response.status_code < 500:
                raise NonRetryableError(
                    f"HTTP {response.status_code}，地址可能写错或对方拒绝访问"
                )
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


def fetch_one_source(source: dict, settings: dict) -> dict:
    """抓取单个源，返回统一的字典结构。任何异常都在内部消化。"""
    result = {
        "id": source["id"],
        "name": source.get("name") or source["id"],
        "home": source.get("home", ""),
        "url": source["url"],
        "type": source.get("type", "rss"),
        "ok": False,
        "stale": False,        # true = 本次没抓到，用的是上次的缓存
        "error": None,
        "updated_at": None,    # 最近一次成功抓取的时间
        "stale_since": None,   # 缓存的原始时间
        "item_count": 0,
        "items": [],
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

    # bozo=1 说明 XML 有格式问题；但如果还能解析出条目，就继续用
    if feed.bozo and not feed.entries:
        result["error"] = f"订阅内容解析失败：{feed.bozo_exception}"
        log(f"  ✗ {result['error']}")
        return result

    max_items = int(source.get("max_items") or settings.get("max_items_per_source", 10))
    summary_limit = int(settings.get("summary_max_chars", 160))

    items = []
    seen_links = set()

    for entry in feed.entries:
        link = normalize_link(entry.get("link", ""), result["home"] or result["url"])
        title = clean_text(entry.get("title", ""))
        # 没有标题或没有可用链接的条目直接跳过
        if not title or not link or link in seen_links:
            continue
        seen_links.add(link)

        raw_summary = entry.get("summary") or entry.get("description") or ""
        if not raw_summary and entry.get("content"):
            raw_summary = entry["content"][0].get("value", "")

        items.append(
            {
                "title": title,
                "link": link,
                "summary": truncate(clean_text(raw_summary), summary_limit),
                "author": clean_text(entry.get("author", "")),
                "published": entry_published(entry),
            }
        )

    # 按发布时间倒序；没有时间的排在最后
    items.sort(key=lambda item: item["published"] or "", reverse=True)
    items = items[:max_items]

    if not items:
        result["error"] = "抓取成功但没有任何可用条目（源可能返回了空数据）"
        log(f"  ✗ {result['error']}")
        return result

    result["ok"] = True
    result["items"] = items
    result["item_count"] = len(items)
    result["updated_at"] = datetime.now(GLOBAL_TZ).isoformat(timespec="seconds")
    log(f"  ✓ 成功，{len(items)} 条")
    return result


def apply_cache_fallback(result: dict, previous: dict | None) -> None:
    """
    失败保底：本次没抓到（或抓到 0 条）时，用上一次的条目填充，
    并标记 stale，让页面能明确提示「这是缓存」。

    注意：updated_at 的含义是「最近一次成功抓取的时间」。
    连续多次失败时，要沿着上一次的数据把这个时间继承下来，
    否则页面上会出现「缓存时间未知」。
    """
    if not previous:
        return

    # 先把「最近一次成功时间」继承过来，失败时也不会丢
    if not result["updated_at"]:
        result["updated_at"] = previous.get("updated_at") or previous.get("stale_since")

    if result["ok"] and result["items"]:
        return

    prev_items = previous.get("items") or []
    if not prev_items:
        return

    result["items"] = prev_items
    result["item_count"] = len(prev_items)
    result["stale"] = True
    result["stale_since"] = result["updated_at"]
    log(
        f"  ↳ 本次失败，沿用上次缓存（{result['stale_since']}，{len(prev_items)} 条）"
    )


def build_run_url() -> str:
    """拼出本次运行的 GitHub Actions 链接（本地运行时为空字符串）。"""
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""


def main() -> int:
    global GLOBAL_TZ  # noqa: PLW0603 —— 时区需要先读配置再确定

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        log(f"配置文件有问题，任务终止：{exc}")
        return 1

    settings = config.get("settings") or {}
    GLOBAL_TZ = get_tz(settings)

    sources = [item for item in (config.get("sources") or []) if item.get("enabled", True)]
    if not sources:
        log("配置文件里没有任何启用的源（enabled: true），任务终止")
        return 1

    previous_data = load_previous_data()
    previous_map = {
        item.get("id"): item
        for item in (previous_data.get("sources") or [])
        if item.get("id")
    }

    results = []
    for index, source in enumerate(sources):
        if index > 0:
            time.sleep(REQUEST_INTERVAL)  # 控制频率，源之间留间隔
        try:
            result = fetch_one_source(source, settings)
        except Exception as exc:  # noqa: BLE001 —— 单个源的意外错误也不能中断整体
            log(f"  ✗ 抓取 {source.get('id')} 时出现未预期错误：{exc}")
            result = {
                "id": source.get("id"),
                "name": source.get("name") or source.get("id"),
                "home": source.get("home", ""),
                "url": source.get("url", ""),
                "type": source.get("type", "rss"),
                "ok": False,
                "stale": False,
                "error": f"未预期错误：{exc}",
                "updated_at": None,
                "stale_since": None,
                "item_count": 0,
                "items": [],
            }
        apply_cache_fallback(result, previous_map.get(result["id"]))
        results.append(result)

    ok_count = sum(1 for item in results if item["ok"])
    stale_count = sum(1 for item in results if item["stale"])
    total_items = sum(item["item_count"] for item in results)

    payload = {
        "generated_at": datetime.now(GLOBAL_TZ).isoformat(timespec="seconds"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_url": build_run_url(),
        "stats": {
            "source_total": len(results),
            "source_ok": ok_count,
            "source_failed": len(results) - ok_count,
            "source_cached": stale_count,
            "item_total": total_items,
        },
        "sources": results,
    }

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DATA_PATH.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    log(
        "完成：{} 个源，成功 {} 个，失败 {} 个（其中 {} 个沿用缓存），共 {} 条".format(
            len(results), ok_count, len(results) - ok_count, stale_count, total_items
        )
    )
    log(f"数据已写入：{DATA_PATH}")

    # 关键：即使所有源都失败，也返回 0。
    # 这样才能继续生成页面、部署一个带告警的站点，而不是让 workflow 直接失败。
    return 0


if __name__ == "__main__":
    sys.exit(main())
