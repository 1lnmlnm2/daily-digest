#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py —— 把 data/latest.json 渲染成静态页面，输出到 site/

M3 的变化：
1. 页面按 category（科技资讯 / 实用工具 / 每周精选）分组，组内按发布时间倒序。
2. 没有任何内容时显示「本期暂无新内容」，不用旧文章凑数。
3. 顶部保留来源健康状态条，缓存和连续失败次数都能看到。
4. 页面仍然只展示标题、摘要和原文链接，不做美化、不做 AI 摘要。

运行方式：
    python scripts/build.py
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yml"
DATA_PATH = ROOT / "data" / "latest.json"
TEMPLATE_DIR = ROOT / "templates"
ASSETS_DIR = ROOT / "assets"
OUTPUT_DIR = ROOT / "site"


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


def main() -> int:
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
        offset = int(settings.get("timezone_offset", payload.get("tz_offset", 8)))
    except (TypeError, ValueError):
        offset = 8
    if offset == 8:
        tz_label = "北京时间"
    elif offset == 0:
        tz_label = "UTC"
    else:
        tz_label = f"UTC{offset:+d}"

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),  # 自动转义，防止 XSS
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["dt"] = format_datetime
    env.filters["date"] = format_date

    template = env.get_template("index.html")
    html = template.render(
        site_title=settings.get("site_title", "我的信息站"),
        site_description=settings.get("site_description", "定时抓取 · 自动生成"),
        tz_label=tz_label,
        data=payload,
        stats=payload.get("stats") or {},
        categories=categories,
        has_items=any(group.get("items") for group in categories),
    )

    # 每次重建先清空，避免旧文件残留在产物里
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    (OUTPUT_DIR / "index.html").write_text(html, encoding="utf-8")

    if ASSETS_DIR.exists():
        shutil.copytree(ASSETS_DIR, OUTPUT_DIR / "assets", dirs_exist_ok=True)

    # 把数据文件也放进产物，方便别人（或以后的你）直接消费 JSON
    data_out = OUTPUT_DIR / "data"
    data_out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DATA_PATH, data_out / "latest.json")

    (OUTPUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    generated = format_datetime(payload.get("generated_at"))
    print(f"[build] 页面已生成：{OUTPUT_DIR / 'index.html'}")
    print(f"[build] 数据时间：{generated}")
    print(f"[build] 条目数：{(payload.get('stats') or {}).get('item_total', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
