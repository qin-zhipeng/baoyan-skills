# -*- coding: utf-8 -*-
"""config.py —— 设置与话术库的路径、读写、默认值合并。纯标准库，可单独测试。"""
from __future__ import annotations

import json
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
PHRASES_FILE = os.path.join(DATA_DIR, "phrases.json")
PLUGINS_DIR = os.path.join(ROOT, "plugins")


DEFAULT_SETTINGS = {
    "version": 1,
    "hotkeys": {
        "toggle_window": "Ctrl+Shift+Space",
        "quick_search": "Ctrl+Shift+F",
        "send_first": "Ctrl+Shift+Enter",
    },
    "sequences": [],
    "send": {
        "mode": "paste_enter",          # copy / paste / paste_enter
        "delay_ms": 120,
        "enter_after_paste": True,
        "restore_clipboard": True,
        "restore_delay_ms": 900,
        "split_segments": True,
        "segment_separator": "|||",
        "segment_delay_ms": 320,
        "paste_delay_ms": 90,
    },
    "window": {
        "always_on_top": True,
        "dock": True,
        "dock_side": "right",
        "dock_gap": 8,
        "no_activate": False,      # 默认关闭：保证搜索框能正常获得键盘焦点
        "x": 120, "y": 120, "w": 900, "h": 620,
        "opacity": 100,
    },
    "general": {
        "accent": "#e5484d",
        "minimize_to_tray": True,
        "autostart": False,
        "confirm_variables": True,
    },
    "plugins": {"enabled": {}, "data": {}},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(PLUGINS_DIR, exist_ok=True)


def read_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: str, obj) -> bool:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


class Config:
    """线程安全的轻量设置对象。改动会自动落盘。"""

    def __init__(self, path: str = SETTINGS_FILE):
        ensure_dirs()
        self.path = path
        self._lock = threading.RLock()
        self._data = _deep_merge(DEFAULT_SETTINGS, read_json(path, {}) or {})

    def get(self, dotted: str, default=None):
        with self._lock:
            cur = self._data
            for part in dotted.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return default
                cur = cur[part]
            return cur

    def set(self, dotted: str, value, save: bool = True):
        with self._lock:
            parts = dotted.split(".")
            cur = self._data
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = value
            if save:
                self.save()

    def section(self, name: str) -> dict:
        with self._lock:
            return dict(self._data.get(name, {}))

    def all(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    def save(self) -> bool:
        with self._lock:
            return write_json(self.path, self._data)

    def plugin_enabled(self, plugin_id: str) -> bool:
        enabled = self.get("plugins.enabled", {}) or {}
        return bool(enabled.get(plugin_id, True))

    def set_plugin_enabled(self, plugin_id: str, on: bool):
        enabled = dict(self.get("plugins.enabled", {}) or {})
        enabled[plugin_id] = bool(on)
        self.set("plugins.enabled", enabled)
