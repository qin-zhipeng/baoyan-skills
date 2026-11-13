# -*- coding: utf-8 -*-
"""store.py —— 话术库：分类 / 条目 / 搜索 / 变量 / 统计 / CSV 导入导出。

纯标准库（拼音搜索为可选增强），不依赖 Qt，方便在 --selftest 里直接验证。
数据就是一个可读的 JSON 文件，后期 DIY 可以直接用文本编辑器改。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import threading
import uuid

from .config import read_json, write_json, PHRASES_FILE

try:
    from pypinyin import lazy_pinyin, Style
    _HAS_PINYIN = True
except Exception:
    _HAS_PINYIN = False

VAR_RE = re.compile(r"\{([^{}]{1,32})\}")

BUILTIN_VARS = {
    "日期": lambda: dt.date.today().strftime("%Y-%m-%d"),
    "时间": lambda: dt.datetime.now().strftime("%H:%M"),
    "星期": lambda: "一二三四五六日"[dt.date.today().weekday()],
    "年份": lambda: str(dt.date.today().year),
}

DEFAULT_LIBRARY = {
    "version": 1,
    "categories": [
        {"id": "cat_greet", "name": "开场问候", "color": "#e5484d", "bookmark": False, "order": 0},
        {"id": "cat_faq", "name": "常见问题", "color": "#f5a524", "bookmark": False, "order": 1},
        {"id": "cat_file", "name": "资料发送", "color": "#3b82f6", "bookmark": False, "order": 2},
    ],
    "phrases": [
        {"id": "p_hello", "category_id": "cat_greet", "title": "首次问候",
         "content": "您好～我是{姓名}的顾问，很高兴为您服务，有什么想了解的随时问我 😊",
         "tags": ["问候"], "keywords": "nihao 你好 开场", "hotkey": "", "files": [],
         "bookmark": False, "order": 0, "usage": 0, "last_used": "", "enabled": True},
        {"id": "p_wait", "category_id": "cat_greet", "title": "稍等回复",
         "content": "稍等我一下，我帮您核实一下信息，马上回来～",
         "tags": ["过渡"], "keywords": "deng 稍等", "hotkey": "", "files": [],
         "bookmark": False, "order": 1, "usage": 0, "last_used": "", "enabled": True},
        {"id": "p_price", "category_id": "cat_faq", "title": "报价说明",
         "content": "关于费用，我整理了一份明细发您，先看下整体包含哪些服务。|||有任何一项不清楚，直接圈出来问我，我逐条给您说明。",
         "tags": ["报价", "重点"], "keywords": "baojia 价格 多少钱", "hotkey": "", "files": [],
         "bookmark": True, "order": 0, "usage": 0, "last_used": "", "enabled": True},
        {"id": "p_contract", "category_id": "cat_file", "title": "发送资料包",
         "content": "这是给您准备的资料，建议先看目录，重点内容我都标出来了。",
         "tags": ["文件"], "keywords": "ziliao 资料 pdf", "hotkey": "", "files": [],
         "bookmark": False, "order": 0, "usage": 0, "last_used": "", "enabled": True},
    ],
}


def new_id(prefix="id"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _pinyin_keys(text: str):
    if not _HAS_PINYIN or not text:
        return ""
    try:
        full = "".join(lazy_pinyin(text))
        initials = "".join(lazy_pinyin(text, style=Style.FIRST_LETTER))
        return (full + " " + initials).lower()
    except Exception:
        return ""


class Library:
    # 暴露给 UI：这些变量不需要弹窗让用户填
    BUILTIN_VARS = BUILTIN_VARS

    def __init__(self, path: str = PHRASES_FILE):
        self.path = path
        self._lock = threading.RLock()
        raw = read_json(path, None)
        self.data = raw if isinstance(raw, dict) and "phrases" in raw else \
            json.loads(json.dumps(DEFAULT_LIBRARY, ensure_ascii=False))
        self._normalize()

    # ------------------------------------------------------------ 基础
    def _normalize(self):
        self.data.setdefault("version", 1)
        self.data.setdefault("categories", [])
        self.data.setdefault("phrases", [])
        for p in self.data["phrases"]:
            p.setdefault("id", new_id("p"))
            p.setdefault("category_id", "")
            p.setdefault("title", "未命名")
            p.setdefault("content", "")
            p.setdefault("tags", [])
            p.setdefault("keywords", "")
            p.setdefault("hotkey", "")
            p.setdefault("files", [])
            p.setdefault("bookmark", False)
            p.setdefault("order", 0)
            p.setdefault("usage", 0)
            p.setdefault("last_used", "")
            p.setdefault("enabled", True)

    def save(self) -> bool:
        with self._lock:
            return write_json(self.path, self.data)

    def all(self) -> dict:
        return json.loads(json.dumps(self.data, ensure_ascii=False))

    # ------------------------------------------------------------ 分类
    def categories(self):
        with self._lock:
            return sorted(self.data["categories"], key=lambda c: (c.get("order", 0), c.get("name", "")))

    def add_category(self, name: str, color: str = "#e5484d") -> str:
        cid = new_id("cat")
        with self._lock:
            self.data["categories"].append(
                {"id": cid, "name": name, "color": color,
                 "bookmark": False, "order": len(self.data["categories"])})
            self.save()
        return cid

    def update_category(self, cid: str, **fields):
        with self._lock:
            for c in self.data["categories"]:
                if c["id"] == cid:
                    c.update(fields)
                    break
            self.save()

    def delete_category(self, cid: str):
        with self._lock:
            self.data["categories"] = [c for c in self.data["categories"] if c["id"] != cid]
            for p in self.data["phrases"]:
                if p["category_id"] == cid:
                    p["category_id"] = ""
            self.save()

    # ------------------------------------------------------------ 条目
    def phrases(self, category_id=None, query: str = "", bookmark_only: bool = False,
                include_disabled: bool = True):
        with self._lock:
            items = list(self.data["phrases"])
        if category_id:
            items = [p for p in items if p["category_id"] == category_id]
        if bookmark_only:
            items = [p for p in items if p.get("bookmark")]
        if not include_disabled:
            items = [p for p in items if p.get("enabled", True)]
        if query and query.strip():
            words = [w.lower() for w in query.split() if w]
            def hit(p):
                hay = " ".join([
                    p.get("title", ""), p.get("content", ""),
                    " ".join(p.get("tags", []) or []), p.get("keywords", ""),
                    _pinyin_keys(p.get("title", "") + " " + p.get("keywords", "")),
                ]).lower()
                return all(w in hay for w in words)
            items = [p for p in items if hit(p)]
        return sorted(items, key=lambda p: (-int(p.get("bookmark", False)),
                                           p.get("order", 0), p.get("title", "")))

    def get(self, pid: str):
        with self._lock:
            for p in self.data["phrases"]:
                if p["id"] == pid:
                    return dict(p)
        return None

    def add_phrase(self, fields: dict) -> str:
        pid = new_id("p")
        item = {
            "id": pid, "category_id": fields.get("category_id", ""),
            "title": fields.get("title", "未命名"), "content": fields.get("content", ""),
            "tags": fields.get("tags", []), "keywords": fields.get("keywords", ""),
            "hotkey": fields.get("hotkey", ""), "files": fields.get("files", []),
            "bookmark": bool(fields.get("bookmark", False)),
            "order": int(fields.get("order", 999)), "usage": 0,
            "last_used": "", "enabled": True,
        }
        with self._lock:
            self.data["phrases"].append(item)
            self.save()
        return pid

    def update_phrase(self, pid: str, **fields):
        with self._lock:
            for p in self.data["phrases"]:
                if p["id"] == pid:
                    p.update(fields)
                    break
            self.save()

    def delete_phrase(self, pid: str):
        with self._lock:
            self.data["phrases"] = [p for p in self.data["phrases"] if p["id"] != pid]
            self.save()

    def record_usage(self, pid: str):
        with self._lock:
            for p in self.data["phrases"]:
                if p["id"] == pid:
                    p["usage"] = int(p.get("usage", 0)) + 1
                    p["last_used"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    break
            self.save()

    def stats(self):
        with self._lock:
            ps = list(self.data["phrases"])
        top = sorted(ps, key=lambda p: p.get("usage", 0), reverse=True)[:10]
        return {"total": len(ps), "categories": len(self.data["categories"]),
                "sends": sum(int(p.get("usage", 0)) for p in ps),
                "top": [{"title": p["title"], "usage": p.get("usage", 0)} for p in top]}

    # ------------------------------------------------------------ 变量
    @staticmethod
    def variables(text: str):
        seen, out = set(), []
        for name in VAR_RE.findall(text or ""):
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    @staticmethod
    def render(text: str, values: dict) -> str:
        def repl(m):
            name = m.group(1)
            if name in values and str(values[name]) != "":
                return str(values[name])
            if name in BUILTIN_VARS:
                return BUILTIN_VARS[name]()
            return m.group(0)
        return VAR_RE.sub(repl, text or "")

    # ------------------------------------------------------------ 导入导出
    CSV_FIELDS = ["分类", "标题", "内容", "标签", "关键词", "热键", "附件", "收藏", "排序"]

    def export_csv(self, path: str) -> int:
        cat_name = {c["id"]: c["name"] for c in self.categories()}
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(self.CSV_FIELDS)
            for p in self.phrases():
                w.writerow([
                    cat_name.get(p["category_id"], ""), p["title"], p["content"],
                    "|".join(p.get("tags", []) or []), p.get("keywords", ""),
                    p.get("hotkey", ""), "|".join(p.get("files", []) or []),
                    "1" if p.get("bookmark") else "", p.get("order", 0),
                ])
        return len(self.phrases())

    def import_csv(self, path: str, merge: bool = True) -> int:
        count = 0
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                title = (row.get("标题") or "").strip()
                content = (row.get("内容") or "").strip()
                if not title and not content:
                    continue
                cname = (row.get("分类") or "").strip() or "导入"
                cid = next((c["id"] for c in self.categories() if c["name"] == cname), None)
                if cid is None:
                    cid = self.add_category(cname)
                self.add_phrase({
                    "category_id": cid,
                    "title": title or content[:16],
                    "content": content,
                    "tags": [t for t in (row.get("标签") or "").split("|") if t],
                    "keywords": row.get("关键词") or "",
                    "hotkey": row.get("热键") or "",
                    "files": [x for x in (row.get("附件") or "").split("|") if x],
                    "bookmark": (row.get("收藏") or "").strip() in ("1", "true", "是"),
                    "order": int(row.get("排序") or 999),
                })
                count += 1
        return count

    def backup(self, path: str) -> bool:
        return write_json(path, self.all())

    def restore(self, path: str) -> bool:
        raw = read_json(path, None)
        if not isinstance(raw, dict) or "phrases" not in raw:
            return False
        self.data = raw
        self._normalize()
        return self.save()
