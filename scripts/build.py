#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py —— 把 data/latest.json 渲染成静态页面，输出到 site/

流程：
1. 读 config/sources.yml（取站点标题等）
2. 读 data/latest.json（取抓取结果）
3. 用 Jinja2 渲染 templates/index.html → site/index.html
4. 复制 assets/ 到 site/assets/
5. 顺便把最新数据复制一份到 site/data/latest.json，方便以后做接口或二次开发

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
    except ValueError:
        return str(value)
    return moment.strftime(fmt)


def format_date(value) -> str:
    """只显示日期，用于条目时间。"""
    return format_datetime(value, "%Y-%m-%d")


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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

    # 页面上标注的时间是哪个时区，方便读者理解
    try:
        offset = int(settings.get("timezone_offset", 8))
    except (TypeError, ValueError):
        offset = 8
    if offset == 8:
        tz_label = "北京时间"
    elif offset == 0:
        tz_label = "UTC"
    else:
        tz_label = f"UTC{offset:+d}"

    # ---------- 准备模板环境 ----------
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
    )

    # ---------- 写出站点 ----------
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

    # 给 GitHub Pages 一个 404 之外的静态标记（可选但有用）
    (OUTPUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    generated = format_datetime(payload.get("generated_at"))
    print(f"[build] 页面已生成：{OUTPUT_DIR / 'index.html'}")
    print(f"[build] 数据时间：{generated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
