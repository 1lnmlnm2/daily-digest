#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py —— 把 data/latest.json 和 data/archive/ 渲染成静态页面，输出到 site/

阶段 4 新增：
1. 遍历 data/archive/YYYY-MM-DD.json，生成单日归档页和归档索引页；
2. 生成浏览器端搜索页、搜索脚本和静态 JSON 索引；
3. 归档页/搜索索引只读归档数据，不修改 data/archive；
4. 按 archive_retention_days（北京时间）清理过期归档文件；
5. 所有页面使用相对路径，适配 GitHub Pages 的 /daily-digest/ 子路径。

运行方式：
    python scripts/build.py
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

import core

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yml"
DATA_PATH = ROOT / "data" / "latest.json"
ARCHIVE_DIR = ROOT / "data" / "archive"
TEMPLATE_DIR = ROOT / "templates"
ASSETS_DIR = ROOT / "assets"
OUTPUT_DIR = ROOT / "site"

ARCHIVE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
DEFAULT_TZ_OFFSET = 8
DEFAULT_RETENTION_DAYS = 90
DEFAULT_SEARCH_MAX_ITEMS = 2000


def format_datetime(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """把 ISO 时间字符串格式化成页面显示的样子；解析失败就原样返回。"""
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    return moment.strftime(fmt)


def format_date(value) -> str:
    return format_datetime(value, "%Y-%m-%d")


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_categories(payload: dict) -> list[dict]:
    """
    优先用 fetch.py 生成的 categories；
    兼容旧版 latest.json（没有 categories 字段）时，按来源顺序临时分组。
    """
    categories = payload.get("categories")
    if isinstance(categories, list) and categories:
        return categories

    order: list[str] = []
    for source in payload.get("sources") or []:
        category = source.get("category") or "未分类"
        if category not in order:
            order.append(category)

    groups = []
    for category in order:
        items = []
        for source in payload.get("sources") or []:
            if (source.get("category") or "未分类") != category:
                continue
            for item in source.get("items") or []:
                copy = dict(item)
                copy.setdefault("source_name", source.get("name", ""))
                copy.setdefault("category", category)
                copy.setdefault("stale", source.get("stale", False))
                items.append(copy)
        groups.append({"name": category, "count": len(items), "items": items})
    return groups


def _warn(message: str) -> None:
    print(f"[build] 警告：{message}")


def _parse_published(value, tz) -> datetime | None:
    """
    归档里的发布时间必须带时区；无法解析或没有时区时返回 None。
    这样做是为了避免把 UTC 时间误当成北京时间，导致归档日期错位。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(tz)


def _safe_link(value) -> str:
    """只接受 http(s) 链接，拒绝 javascript:、data:、相对链接等。"""
    if not isinstance(value, str):
        return ""
    return core.normalize_url(value.strip())


def _clean_field(value) -> str:
    return core.clean_text(value)


def load_archive_day(path: Path, tz) -> dict | None:
    """
    读取并校验一个归档文件。

    - 缺失、空、损坏、顶层字段/条目字段不完整：整份文件跳过并记录警告；
    - 同一文件内重复稳定 ID：去重，保留排序后第一条，并记录警告；
    - 条目顺序按发布时间倒序、ID 升序，保证重复构建稳定。

    返回：
        {"date": ..., "updated_at": ..., "count": ..., "items": [...]},
        或 None（表示跳过该文件）。
    """
    stem = path.stem
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"跳过归档 {path.name}：JSON 读取失败（{exc}）")
        return None

    if not isinstance(payload, dict):
        _warn(f"跳过归档 {path.name}：顶层不是对象")
        return None

    file_date = payload.get("date")
    if not isinstance(file_date, str) or file_date != stem:
        _warn(f"跳过归档 {path.name}：date 字段缺失或与文件名不一致")
        return None

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        _warn(f"跳过归档 {path.name}：items 为空或不是数组")
        return None

    raw_count = payload.get("count")
    if isinstance(raw_count, int) and raw_count != len(raw_items):
        _warn(f"归档 {path.name} 的 count={raw_count} 与 items 数量 {len(raw_items)} 不一致，按实际条目生成")

    items = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            _warn(f"跳过归档 {path.name}：第 {index} 条不是对象")
            return None

        item_id = str(item.get("id") or "").strip()
        title = _clean_field(item.get("title"))
        source_name = _clean_field(item.get("source_name") or item.get("source") or "")
        category = _clean_field(item.get("category"))
        link = _safe_link(item.get("link"))
        published_raw = item.get("published")
        published_at = _parse_published(published_raw, tz)

        if not item_id or not title or not source_name or not category or not link:
            _warn(f"跳过归档 {path.name}：第 {index} 条缺少 id/title/source_name/category/link")
            return None
        if published_at is None or published_at.date().isoformat() != stem:
            _warn(f"跳过归档 {path.name}：第 {index} 条 published 缺失、带时区无效或日期不匹配")
            return None

        items.append(
            {
                "id": item_id,
                "source_id": _clean_field(item.get("source_id")),
                "source_name": source_name,
                "category": category,
                "title": title,
                "link": link,
                "summary": _clean_field(item.get("summary")),
                "author": _clean_field(item.get("author")),
                "published": str(published_raw).strip(),
                "date": published_at.date().isoformat(),
                "first_collected_at": str(item.get("first_collected_at") or ""),
                "_published_ts": published_at.timestamp(),
            }
        )

    items.sort(key=lambda item: (-item["_published_ts"], item["id"]))
    unique_items = []
    seen_ids = set()
    duplicates = 0
    for item in items:
        if item["id"] in seen_ids:
            duplicates += 1
            continue
        seen_ids.add(item["id"])
        unique_items.append(item)
    if duplicates:
        _warn(f"归档 {path.name} 内有 {duplicates} 条重复 ID，已去重")

    updated_at = payload.get("updated_at")
    return {
        "date": stem,
        "updated_at": updated_at if isinstance(updated_at, str) else "",
        "count": len(unique_items),
        "items": unique_items,
    }


def load_archive_days(archive_dir: Path, tz) -> tuple[list[dict], list[dict], list[str]]:
    """
    遍历归档目录，返回 (日期页数据, 全局去重后的条目, 跳过的文件名)。

    全局去重规则：同一个稳定 ID 只保留发布时间最新的一条；
    如果它出现在多个日期文件中，旧日期文件生成的页面会少掉该条，避免重复展示。
    """
    days: list[dict] = []
    skipped: list[str] = []
    if not archive_dir.exists():
        return days, [], skipped

    for path in sorted(archive_dir.glob("*.json"), key=lambda item: item.name, reverse=True):
        if not ARCHIVE_NAME_RE.match(path.name):
            _warn(f"跳过非日期归档文件 {path.name}")
            skipped.append(path.name)
            continue
        day = load_archive_day(path, tz)
        if day is None:
            skipped.append(path.name)
            continue
        days.append(day)

    canonical: dict[str, dict] = {}
    for day in days:
        for item in day["items"]:
            item_id = item["id"]
            current = canonical.get(item_id)
            if current is None:
                canonical[item_id] = item
                continue
            if item["_published_ts"] > current["_published_ts"]:
                canonical[item_id] = item
                _warn(f"归档重复 ID {item_id}：保留 {day['date']} 的较新条目")

    # 从各日期页面中移除重复 ID；只让该 ID 的 canonical 条目留在它所属日期。
    canonical_dates = {item_id: item["date"] for item_id, item in canonical.items()}
    for day in days:
        kept = []
        for item in day["items"]:
            if canonical_dates.get(item["id"]) == day["date"]:
                kept.append(canonical[item["id"]])
        day["items"] = kept
        day["count"] = len(kept)

    all_items = sorted(canonical.values(), key=lambda item: (-item["_published_ts"], item["id"]))
    days.sort(key=lambda day: day["date"], reverse=True)
    return days, all_items, skipped


def prune_archive_files(archive_dir: Path, retention_days: int, today: date) -> list[str]:
    """
    只删除完整日期命名的归档文件，且只删早于保留期截止日的文件。
    不删除当天或未来日期；不处理非 YYYY-MM-DD.json 命名的文件。
    返回被删除的文件名列表。
    """
    if not archive_dir.exists():
        return []
    try:
        retention = max(0, int(retention_days))
    except (TypeError, ValueError):
        retention = DEFAULT_RETENTION_DAYS
    cutoff = today - timedelta(days=retention)
    removed = []
    for path in sorted(archive_dir.glob("*.json")):
        if not ARCHIVE_NAME_RE.match(path.name):
            continue
        try:
            file_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        # file_date < today 明确保护当天和未来日期
        if file_date < cutoff and file_date < today:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:
                _warn(f"过期归档 {path.name} 删除失败：{exc}")
    return removed


def make_search_index(all_items: list[dict], max_items: int) -> tuple[dict, int]:
    """生成纯文本搜索索引；按发布时间倒序保留最多 max_items 条。"""
    try:
        limit = max(0, int(max_items))
    except (TypeError, ValueError):
        limit = DEFAULT_SEARCH_MAX_ITEMS

    ordered = sorted(all_items, key=lambda item: (-item["_published_ts"], item["id"]))
    selected = ordered[:limit] if limit else []
    payload = {
        "schema": 4,
        "count": len(selected),
        "total": len(ordered),
        "limit": limit,
        "items": [
            {
                "id": item["id"],
                "title": item["title"],
                "summary": item["summary"],
                "source_name": item["source_name"],
                "category": item["category"],
                "published": item["published"],
                "date": item["date"],
                "link": item["link"],
            }
            for item in selected
        ],
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return payload, len(text.encode("utf-8"))


def main(now: datetime | None = None) -> int:
    if not DATA_PATH.exists():
        print(f"[build] 找不到 {DATA_PATH}，请先运行 python scripts/fetch.py")
        return 1

    config = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}

    settings = config.get("settings") or {}
    payload = load_json(DATA_PATH)
    categories = ensure_categories(payload)

    try:
        offset = int(settings.get("timezone_offset", payload.get("tz_offset", DEFAULT_TZ_OFFSET)))
    except (TypeError, ValueError):
        offset = DEFAULT_TZ_OFFSET
    tz = core.get_timezone(offset)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    now = now.astimezone(tz)
    today = now.date()

    if offset == 8:
        tz_label = "北京时间"
    elif offset == 0:
        tz_label = "UTC"
    else:
        tz_label = f"UTC{offset:+d}"

    try:
        retention_days = max(0, int(settings.get("archive_retention_days") or DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        retention_days = DEFAULT_RETENTION_DAYS
    try:
        search_limit = max(0, int(settings.get("search_max_items") or DEFAULT_SEARCH_MAX_ITEMS))
    except (TypeError, ValueError):
        search_limit = DEFAULT_SEARCH_MAX_ITEMS

    removed_archives = prune_archive_files(ARCHIVE_DIR, retention_days, today)
    if removed_archives:
        print(f"[build] 归档清理：删除 {len(removed_archives)} 个过期文件（保留最近 {retention_days} 天）")

    archive_days, archive_items, skipped_archives = load_archive_days(ARCHIVE_DIR, tz)
    archive_total = len(archive_items)
    search_payload, search_size = make_search_index(archive_items, search_limit)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),  # 自动转义，防止 XSS
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["dt"] = format_datetime
    env.filters["date"] = format_date

    site_title = settings.get("site_title", "我的信息站")
    site_description = settings.get("site_description", "定时抓取 · 自动生成")

    index_html = env.get_template("index.html").render(
        site_title=site_title,
        site_description=site_description,
        tz_label=tz_label,
        data=payload,
        stats=payload.get("stats") or {},
        categories=categories,
        has_items=any(group.get("items") for group in categories),
        asset_root="",
        home_href="",
        archive_href="archive/index.html",
        search_href="search.html",
    )

    archive_index_html = env.get_template("archive_index.html").render(
        site_title=site_title,
        site_description=site_description,
        days=archive_days,
        archive_total=archive_total,
        retention_days=retention_days,
        asset_root="../",
        home_href="../index.html",
        archive_href="index.html",
        search_href="../search.html",
    )

    archive_day_templates = env.get_template("archive_day.html")
    archive_day_pages = {}
    for day in archive_days:
        archive_day_pages[day["date"]] = archive_day_templates.render(
            site_title=site_title,
            site_description=site_description,
            day=day,
            asset_root="../",
            home_href="../index.html",
            archive_href="index.html",
            search_href="../search.html",
        )

    search_html = env.get_template("search.html").render(
        site_title=site_title,
        site_description=site_description,
        search_count=search_payload["count"],
        archive_total=archive_total,
        search_limit=search_limit,
        asset_root="",
        home_href="index.html",
        archive_href="archive/index.html",
        search_href="search.html",
    )

    # 每次重建先清空，避免旧文件残留在产物里
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "index.html").write_text(index_html, encoding="utf-8")

    archive_out = OUTPUT_DIR / "archive"
    archive_out.mkdir(parents=True, exist_ok=True)
    (archive_out / "index.html").write_text(archive_index_html, encoding="utf-8")
    for day in archive_days:
        (archive_out / f"{day['date']}.html").write_text(
            archive_day_pages[day["date"]], encoding="utf-8"
        )

    (OUTPUT_DIR / "search.html").write_text(search_html, encoding="utf-8")
    search_index_text = json.dumps(
        search_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    data_out = OUTPUT_DIR / "data"
    data_out.mkdir(parents=True, exist_ok=True)
    (data_out / "search-index.json").write_text(search_index_text, encoding="utf-8")
    shutil.copyfile(DATA_PATH, data_out / "latest.json")

    if ASSETS_DIR.exists():
        shutil.copytree(ASSETS_DIR, OUTPUT_DIR / "assets", dirs_exist_ok=True)

    (OUTPUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    generated = format_datetime(payload.get("generated_at"))
    print(f"[build] 页面已生成：{OUTPUT_DIR / 'index.html'}")
    print(f"[build] 数据时间：{generated}")
    print(f"[build] 条目数：{(payload.get('stats') or {}).get('item_total', 0)}")
    print(
        f"[build] 归档页：{len(archive_days)} 个日期，{archive_total} 条"
        f"（跳过 {len(skipped_archives)} 个文件）"
    )
    print(
        f"[build] 搜索索引：{search_payload['count']} 条"
        f"（归档 {search_payload['total']} 条，上限 {search_limit}），{search_size} B"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
