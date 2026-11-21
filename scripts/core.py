#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core.py —— M3 的纯函数层（不联网、不读写项目文件，方便单元测试）

职责：
1. 链接规范化与稳定文章 ID
2. RSS / Atom 时间解析与时区统一
3. 时间窗口过滤（含无日期条目的可配置策略）
4. 关键词过滤（全局与单源取并集，exclude 优先，英文忽略大小写）
5. 正文清洗（去脚本/样式/标签，防 XSS）
6. 单源限额（在日期与关键词过滤之后生效）
7. 全站限额
8. 状态条目的清理（保留天数 + 数量上限）

设计原则：所有函数只依赖传入参数，不读环境变量、不读配置文件、不发请求。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

# ---------- 默认常量 ----------

DEFAULT_TZ_OFFSET = 8
DEFAULT_MAX_ITEMS = 10
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_SUMMARY_CHARS = 160

# 出现在链接里但对内容无意义的跟踪参数，规范化时去掉，保证同一篇文章 ID 稳定
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {
    "spm", "scm", "from", "ref", "referer", "referrer",
    "share", "share_source", "fbclid", "gclid", "yclid",
    "mc_cid", "mc_eid",
}

# 未来时间容忍度：超过这个小时数视为异常日期，直接跳过，避免“未来文章”冒充最新
FUTURE_TOLERANCE_HOURS = 2


# ---------- 时区 ----------


def get_timezone(offset) -> timezone:
    """按小时偏移构造时区对象，默认北京时间。"""
    try:
        hours = int(offset)
    except (TypeError, ValueError):
        hours = DEFAULT_TZ_OFFSET
    return timezone(timedelta(hours=hours))


# ---------- 链接与 ID ----------


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith(_TRACKING_PREFIXES) or lowered in _TRACKING_KEYS


def normalize_url(link: str, base: str = "") -> str:
    """把相对链接补全，去掉片段和跟踪参数，拒绝非 http(s) 链接。"""
    link = (link or "").strip()
    if not link:
        return ""
    absolute = urljoin(base, link) if base else link
    parts = urlparse(absolute)
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    if not parts.netloc:
        return ""
    kept_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(key)
    ]
    return urlunparse(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path or "/",
            parts.params,
            urlencode(kept_query, doseq=True),
            "",
        )
    )


def make_item_id(source_id: str, guid: str = "", link: str = "") -> str:
    """
    稳定文章 ID：优先 Feed 的 guid/id，没有则用规范化链接。
    统一加源前缀再哈希，避免两个源收录同一篇文章时 ID 冲突。
    """
    seed = (guid or "").strip() or normalize_url(link)
    if not seed:
        return ""
    digest = hashlib.sha256(f"{source_id}|{seed}".encode("utf-8")).hexdigest()
    return digest[:16]


# ---------- 时间 ----------


def parse_entry_date(entry, tz: timezone) -> tuple[str | None, str]:
    """
    取条目发布时间，返回 (带时区的 ISO 字符串, 来源标记)。
    来源标记：published / updated / created / missing。
    """
    for key, label in (
        ("published_parsed", "published"),
        ("updated_parsed", "updated"),
        ("created_parsed", "created"),
    ):
        parsed = entry.get(key)
        if not parsed:
            continue
        try:
            moment = datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        return moment.astimezone(tz).isoformat(timespec="seconds"), label
    return None, "missing"


def to_datetime(value) -> datetime | None:
    """把 ISO 字符串或 datetime 转成 datetime，解析失败返回 None。"""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def in_window(published_iso: str | None, now: datetime, lookback_hours: float) -> tuple[bool, str]:
    """
    判断发布时间是否落在窗口内，返回 (是否保留, 原因)。
    原因：ok / no_date / too_old / future。
    """
    moment = to_datetime(published_iso)
    if moment is None:
        return False, "no_date"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now.tzinfo)
    if moment > now + timedelta(hours=FUTURE_TOLERANCE_HOURS):
        return False, "future"
    if moment < now - timedelta(hours=lookback_hours):
        return False, "too_old"
    return True, "ok"


# ---------- 关键词 ----------


def merge_keywords(*groups) -> list[str]:
    """把多组关键词合并去重（大小写不敏感去重），保持原顺序。"""
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for word in group or []:
            text = str(word).strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)
    return merged


def keyword_hit(text: str, keywords) -> bool:
    """任一关键词命中即返回 True；英文按 casefold 忽略大小写。"""
    if not keywords:
        return False
    haystack = (text or "").casefold()
    return any(str(word).strip().casefold() in haystack for word in keywords if str(word).strip())


def keyword_allowed(title: str, summary: str, include, exclude) -> tuple[bool, str]:
    """
    关键词判定：排除优先于包含；包含为空表示不限制。
    返回 (是否通过, 原因)：ok / excluded / not_included。
    """
    text = f"{title}\n{summary}"
    if keyword_hit(text, exclude):
        return False, "excluded"
    if not include:
        return True, "ok"
    if keyword_hit(text, include):
        return True, "ok"
    return False, "not_included"


# ---------- 文本清洗 ----------


_SCRIPT_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_SPACE_RE = re.compile(r"[\t\r\f\v]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_READ_MORE_RE = re.compile(
    r"[\[\]\.…\s]*(查看全文|阅读全文|阅读原文|阅读更多|详见原文|Read more|Continue reading)[\[\]\.…\s]*",
    re.IGNORECASE,
)


def clean_text(value) -> str:
    """
    把 Feed 里的富文本清成纯文本：整块删掉 script/style，再去标签、
    还原 HTML 实体、二次去标签（防止 &lt;script&gt; 还原后又被当成标签）。
    """
    if not value:
        return ""
    text = str(value)
    # 第一遍：处理原始 HTML（含 <script> 块）
    text = _SCRIPT_BLOCK_RE.sub(" ", text)
    text = _COMMENT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    # 还原实体后必须再来一遍：&lt;script&gt; 会变成真正的 <script>，只去标签会留下脚本文本
    text = unescape(text)
    text = _SCRIPT_BLOCK_RE.sub(" ", text)
    text = _COMMENT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _READ_MORE_RE.sub("", text)
    text = _INLINE_SPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    text = _CONTROL_RE.sub("", text)
    return text.strip()


def truncate(text: str, limit: int) -> str:
    """按字符数截断，超出补省略号。"""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


# ---------- 单源处理流水线 ----------


def _entry_raw_summary(entry) -> str:
    raw = entry.get("summary") or entry.get("description") or ""
    if not raw and entry.get("content"):
        try:
            raw = entry["content"][0].get("value", "")
        except (IndexError, AttributeError, TypeError):
            raw = ""
    return raw


def process_entries(entries, source: dict, settings: dict, now: datetime, tz: timezone) -> dict:
    """
    单源处理：清洗 → 稳定 ID → 日期解析 → 时间窗口 → 关键词 → 排序 → 单源限额。
    限额发生在所有过滤之后，保证高频源不会挤占别的源。
    """
    settings = settings or {}
    source = source or {}

    max_items = int(source.get("max_items") or settings.get("max_items_per_source") or DEFAULT_MAX_ITEMS)
    lookback_hours = float(
        source.get("lookback_hours")
        or settings.get("default_lookback_hours")
        or DEFAULT_LOOKBACK_HOURS
    )
    summary_limit = int(settings.get("summary_max_chars") or DEFAULT_SUMMARY_CHARS)
    undated_policy = str(settings.get("undated_policy") or "skip").strip().lower()
    category = str(source.get("category") or settings.get("default_category") or "未分类")
    filters = settings.get("filters") or {}
    include = merge_keywords(filters.get("include_keywords"), source.get("include_keywords"))
    exclude = merge_keywords(filters.get("exclude_keywords"), source.get("exclude_keywords"))

    stats = {
        "entries_total": len(entries),
        "invalid_entries": 0,
        "duplicate_entries": 0,
        "undated_skipped": 0,
        "future_skipped": 0,
        "out_of_window": 0,
        "keyword_excluded": 0,
        "keyword_not_included": 0,
        "kept": 0,
    }

    base_url = source.get("home") or source.get("url") or ""
    items: list[dict] = []
    seen_ids: set[str] = set()

    for entry in entries:
        title = clean_text(entry.get("title", ""))
        link = normalize_url(entry.get("link", ""), base_url)
        if not title or not link:
            stats["invalid_entries"] += 1
            continue

        guid = entry.get("id") or entry.get("guid") or ""
        item_id = make_item_id(source.get("id", ""), guid, link)
        if not item_id:
            stats["invalid_entries"] += 1
            continue
        if item_id in seen_ids:
            stats["duplicate_entries"] += 1
            continue
        seen_ids.add(item_id)

        published, date_source = parse_entry_date(entry, tz)
        if published is None:
            if undated_policy == "skip":
                stats["undated_skipped"] += 1
                continue

        keep, reason = in_window(published, now, lookback_hours)
        if published is None and undated_policy != "skip":
            keep, reason = True, "undated_kept"
        elif not keep:
            if reason == "future":
                stats["future_skipped"] += 1
            elif reason == "too_old":
                stats["out_of_window"] += 1
            else:
                stats["out_of_window"] += 1
            continue

        summary = truncate(clean_text(_entry_raw_summary(entry)), summary_limit)
        allowed, why = keyword_allowed(title, summary, include, exclude)
        if not allowed:
            if why == "excluded":
                stats["keyword_excluded"] += 1
            else:
                stats["keyword_not_included"] += 1
            continue

        items.append(
            {
                "id": item_id,
                "source_id": source.get("id", ""),
                "source_name": source.get("name") or source.get("id", ""),
                "category": category,
                "title": title,
                "link": link,
                "summary": summary,
                "author": clean_text(entry.get("author", "")),
                "published": published,
                "date_source": date_source,
            }
        )

    # 按发布时间倒序；无日期的排最后
    items.sort(key=lambda item: to_datetime(item["published"]).timestamp() if item["published"] else float("-inf"), reverse=True)
    items = items[:max_items]
    stats["kept"] = len(items)

    return {
        "items": items,
        "stats": stats,
        "config": {
            "max_items": max_items,
            "lookback_hours": lookback_hours,
            "category": category,
            "include_keywords": include,
            "exclude_keywords": exclude,
        },
    }


def merge_and_limit(items, total_max: int = DEFAULT_MAX_ITEMS) -> list[dict]:
    """全站合并：按时间倒序后截断，数量不足就少于 total_max，不用旧文凑数。"""
    ordered = sorted(
        items,
        key=lambda item: to_datetime(item.get("published")).timestamp() if item.get("published") else float("-inf"),
        reverse=True,
    )
    if total_max and total_max > 0:
        return ordered[:total_max]
    return ordered


# ---------- 状态清理 ----------


def prune_state_items(
    items: dict,
    now: datetime,
    retention_days: int = 120,
    max_items: int = 3000,
) -> tuple[dict, int]:
    """
    清理 state.items：
    1. last_seen 早于 retention_days 的条目删除（pending 除外）；
    2. 超过 max_items 时，从最旧的开始删，pending 最后才删。
    返回 (保留下来的字典, 删除数量)。
    保留天数大于归档保留天数，避免可入选旧文因状态被删而“复活”成新增。
    """
    cutoff = now - timedelta(days=retention_days)
    kept: dict = {}
    removed = 0

    for item_id, meta in (items or {}).items():
        status = (meta or {}).get("status", "")
        last_seen = to_datetime((meta or {}).get("last_seen"))
        if status == "pending":
            kept[item_id] = meta
            continue
        if last_seen is None or last_seen >= cutoff:
            kept[item_id] = meta
            continue
        removed += 1

    if max_items and len(kept) > max_items:
        excess = len(kept) - max_items
        ordered = sorted(
            kept.items(),
            key=lambda kv: (kv[1] or {}).get("last_seen") or "",
        )
        for item_id, meta in ordered:
            if excess <= 0:
                break
            if (meta or {}).get("status") == "pending":
                continue
            del kept[item_id]
            removed += 1
            excess -= 1

    return kept, removed


def empty_state(source_ids) -> dict:
    """生成一份初始状态骨架（阶段 2 首次运行时使用）。"""
    return {
        "schema": 2,
        "pipeline": "m3",
        "baseline": {"established": False, "established_at": None},
        "updated_at": None,
        "items": {},
        "sources": {
            str(sid): {
                "consecutive_failures": 0,
                "last_success_at": None,
                "last_attempt_at": None,
                "last_alert_at": None,
                "alert_open": False,
                "last_status": "unknown",
                "last_failure_at": None,
                "last_error": None,
                # 兼容旧版 state 字段；新逻辑以 alert_open 为准，两者保持同步
                "alert_active": False,
                "status": "unknown",
                "alert_last_attempt_at": None,
                "alert_last_error": None,
            }
            for sid in (source_ids or [])
        },
    }
