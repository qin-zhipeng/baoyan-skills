# -*- coding: utf-8 -*-
"""quickstore.py —— 极简版「常用语 / 常用文件」库。

只有一个 JSON：data/quick.json，结构一眼能看懂，也能直接用记事本改。
首次启动会把旧版 data/phrases.json 里的话术自动搬过来，不丢数据。
"""
from __future__ import annotations

import json
import os
import threading
import uuid

from .config import DATA_DIR, read_json, write_json, PHRASES_FILE

QUICK_FILE = os.path.join(DATA_DIR, "quick.json")

SAMPLE_TEXTS = [
    {"title": "开场问候", "content": "您好～有什么想了解的随时问我，我这边都在 😊"},
    {"title": "稍等一下", "content": "稍等我一下，我帮您核实一下信息，马上回来～"},
    {"title": "收到确认", "content": "收到啦，我这边已经记下了，后续有进展第一时间同步给您。"},
]


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class QuickStore:
    def __init__(self, path: str = QUICK_FILE):
        self.path = path
        self._lock = threading.RLock()
        raw = read_json(path, None)
        if not isinstance(raw, dict) or "texts" not in raw:
            raw = self._migrate()
        self.data = raw
        self.data.setdefault("version", 2)
        self.data.setdefault("texts", [])
        self.data.setdefault("files", [])
        self.data.setdefault("settings", {})
        self.save()

    # ------------------------------------------------------------ 旧数据迁移
    def _migrate(self) -> dict:
        texts, files = [], []
        old = read_json(PHRASES_FILE, None)
        builtin_ids = {"p_hello", "p_wait", "p_price", "p_contract"}
        if isinstance(old, dict):
            phrases = old.get("phrases", []) or []
            # 只有用户自己加过话术才搬旧数据；纯内置示例（里面带 {姓名} 占位符）
            # 直接用下面三条干净示例，避免一打开就看到没替换的变量。
            has_custom = any(p.get("id") not in builtin_ids for p in phrases)
            if has_custom:
                for p in phrases:
                    title = str(p.get("title") or "").strip() or "未命名"
                    content = str(p.get("content") or "").strip()
                    if content:
                        texts.append(self._new_text(title, content))
                    for f in p.get("files", []) or []:
                        if f and os.path.exists(str(f)):
                            files.append(self._new_file(str(f)))
        if not texts:
            texts = [self._new_text(t["title"], t["content"]) for t in SAMPLE_TEXTS]
        return {"version": 2, "texts": texts, "files": files, "settings": {}}

    @staticmethod
    def _new_text(title: str, content: str) -> dict:
        return {"id": _uid("t"), "title": title, "content": content, "usage": 0}

    @staticmethod
    def _new_file(path: str) -> dict:
        path = os.path.abspath(path)
        return {"id": _uid("f"), "name": os.path.basename(path),
                "path": path, "usage": 0}

    # ------------------------------------------------------------ 读写
    def save(self) -> bool:
        with self._lock:
            return write_json(self.path, self.data)

    def all(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.data, ensure_ascii=False))

    def texts(self, query: str = "") -> list:
        with self._lock:
            items = [dict(x) for x in self.data["texts"]]
        if query.strip():
            q = query.strip().lower()
            items = [x for x in items
                     if q in (x.get("title", "") + " " + x.get("content", "")).lower()]
        # 用得多的自动浮到最前面，不用手动排序
        items.sort(key=lambda x: -int(x.get("usage", 0)))
        return items

    def files(self, query: str = "") -> list:
        with self._lock:
            items = [dict(x) for x in self.data["files"]]
        if query.strip():
            q = query.strip().lower()
            items = [x for x in items if q in x.get("name", "").lower()]
        items.sort(key=lambda x: -int(x.get("usage", 0)))
        return items

    def get(self, kind: str, item_id: str):
        key = "texts" if kind == "text" else "files"
        with self._lock:
            for x in self.data[key]:
                if x.get("id") == item_id:
                    return dict(x)
        return None

    def exists_path(self, path: str) -> bool:
        with self._lock:
            return any(os.path.normcase(x.get("path", "")) == os.path.normcase(path)
                       for x in self.data["files"])

    # ------------------------------------------------------------ 常用语
    def add_text(self, title: str, content: str) -> str:
        item = self._new_text(title.strip() or "未命名", content)
        with self._lock:
            self.data["texts"].append(item)
            self.save()
        return item["id"]

    def update_text(self, item_id: str, title: str, content: str):
        with self._lock:
            for x in self.data["texts"]:
                if x.get("id") == item_id:
                    x["title"] = title.strip() or "未命名"
                    x["content"] = content
                    break
            self.save()

    # ------------------------------------------------------------ 常用文件
    def add_files(self, paths) -> int:
        added = 0
        with self._lock:
            for p in paths:
                if not p or not os.path.isfile(p) or self.exists_path(p):
                    continue
                self.data["files"].append(self._new_file(p))
                added += 1
            self.save()
        return added

    def rename_file(self, item_id: str, name: str):
        with self._lock:
            for x in self.data["files"]:
                if x.get("id") == item_id:
                    x["name"] = name.strip() or x["name"]
                    break
            self.save()

    def delete(self, kind: str, item_id: str):
        key = "texts" if kind == "text" else "files"
        with self._lock:
            self.data[key] = [x for x in self.data[key] if x.get("id") != item_id]
            self.save()

    def move(self, kind: str, item_id: str, delta: int):
        """上移 / 下移，方便把最常用的排到前面。"""
        key = "texts" if kind == "text" else "files"
        with self._lock:
            arr = self.data[key]
            i = next((n for n, x in enumerate(arr) if x.get("id") == item_id), -1)
            j = i + delta
            if i < 0 or j < 0 or j >= len(arr):
                return
            arr[i], arr[j] = arr[j], arr[i]
            self.save()

    def bump(self, kind: str, item_id: str):
        key = "texts" if kind == "text" else "files"
        with self._lock:
            for x in self.data[key]:
                if x.get("id") == item_id:
                    x["usage"] = int(x.get("usage", 0)) + 1
                    break
            self.save()

    # ------------------------------------------------------------ 小设置
    def setting(self, key: str, default=None):
        with self._lock:
            return self.data["settings"].get(key, default)

    def set_setting(self, key: str, value):
        with self._lock:
            self.data["settings"][key] = value
            self.save()
