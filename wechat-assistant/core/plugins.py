# -*- coding: utf-8 -*-
"""plugins.py —— 插件加载器与插件 API。

插件放在 plugins/ 下，两种形态都支持：
  plugins/xxx/plugin.py       （推荐，目录里还能放自己的数据）
  plugins/xxx.py              （单文件小插件）

热插拔：启动时扫描加载；在「插件」页可启用/禁用，禁用会调用 on_unload。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import traceback

from PySide6.QtCore import QObject, Signal

from .config import PLUGINS_DIR, ensure_dirs
from .plugin_base import PluginBase
from . import win32util as w


class PluginAPI:
    """传给插件的唯一入口。插件不需要 import 任何 core 内部模块。"""

    def __init__(self, ctx, plugin_id: str, plugin_dir: str):
        self._ctx = ctx
        self.plugin_id = plugin_id
        self.plugin_dir = plugin_dir

    # ---- 配置（每个插件独立命名空间，落盘到 data/settings.json） ----
    def get_config(self, key: str, default=None):
        return self._ctx.cfg.get(f"plugins.data.{self.plugin_id}.{key}", default)

    def set_config(self, key: str, value):
        self._ctx.cfg.set(f"plugins.data.{self.plugin_id}.{key}", value)

    def all_config(self) -> dict:
        return dict(self._ctx.cfg.get(f"plugins.data.{self.plugin_id}", {}) or {})

    # ---- 发送 / 剪贴板 ----
    def send_text(self, text, enter=True, target=None, block=False):
        self._ctx.sender.send_text(text, enter=enter, target=target, block=block)

    def send_files(self, paths, target=None, block=False):
        self._ctx.sender.send_files(paths, target=target, block=block)

    def copy_text(self, text) -> bool:
        return self._ctx.sender.copy_text(text)

    def snapshot_clipboard(self):
        """保存当前剪贴板，批量操作结束后可原样恢复。"""
        return w.snapshot_clipboard()

    def restore_clipboard(self, snapshot) -> bool:
        return w.restore_clipboard(snapshot)

    def paste_clipboard(self):
        """把当前剪贴板内容粘贴到已激活的窗口（不自动回车）。"""
        self._ctx.sender.combo([w.VK["CTRL"]], ord("V"))

    def paste_only(self, text, target=None):
        """只粘贴不回车（适合给输入框填内容）。"""
        self._ctx.sender.send_text(text, enter=False, target=target, block=True)

    # ---- 窗口 / 键鼠 ----
    def target_window(self) -> dict:
        return self._ctx.sender.target_info()

    def find_window(self, title_contains="", class_name=""):
        return w.find_window(title_contains, class_name)

    def list_windows(self):
        return w.list_windows()

    def window_info(self, hwnd):
        return w.get_window_info(hwnd)

    def activate(self, hwnd=None, title_contains="") -> bool:
        h = hwnd or (w.find_window(title_contains) if title_contains else 0)
        return self._ctx.sender.activate(int(h or 0))

    def click(self, x=None, y=None, double=False, right=False):
        self._ctx.sender.click(x, y, double=double, right=right)

    def tap(self, vk: int):
        self._ctx.sender.tap_vk(vk)

    def combo(self, modifiers, vk: int):
        self._ctx.sender.combo(modifiers, vk)

    def type_text(self, text: str):
        self._ctx.sender.type_text(text)

    def delay(self, seconds: float):
        self._ctx.sender.delay(seconds)

    def cursor_pos(self):
        return self._ctx.sender.cursor_pos()

    @property
    def VK(self):
        return w.VK

    # ---- 话术库 ----
    def phrases(self, query="", category_id=None):
        return self._ctx.library.phrases(query=query, category_id=category_id)

    def stats(self):
        return self._ctx.library.stats()

    # ---- 热键 / 界面 ----
    def register_hotkey(self, combo: str, action_id: str, callback) -> bool:
        return self._ctx.hotkeys.register_callback(action_id, combo, callback)

    def log(self, message: str):
        self._ctx.log(str(message))

    def notify(self, title: str, message: str):
        self._ctx.notify(title, message)

    def open_panel(self):
        self._ctx.window.open_toolbox(self.plugin_id)

    def data_path(self, filename: str) -> str:
        """插件自己的数据目录（自动创建）。"""
        d = os.path.join(self.plugin_dir, "_data")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, filename)


class LoadedPlugin:
    def __init__(self, instance: PluginBase, api: PluginAPI, module_path: str,
                 plugin_dir: str):
        self.instance = instance
        self.api = api
        self.module_path = module_path
        self.plugin_dir = plugin_dir
        self.error = ""
        self.panel = None

    @property
    def id(self):
        return self.instance.id

    @property
    def name(self):
        return self.instance.name


class PluginManager(QObject):
    changed = Signal()
    message = Signal(str)

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.plugins: dict[str, LoadedPlugin] = {}
        self.failures: list[dict] = []

    # ------------------------------------------------------------ 发现
    def discover(self):
        ensure_dirs()
        found = []
        for entry in sorted(os.listdir(PLUGINS_DIR)):
            full = os.path.join(PLUGINS_DIR, entry)
            if os.path.isdir(full) and not entry.startswith((".", "_", "__")):
                p = os.path.join(full, "plugin.py")
                if os.path.exists(p):
                    found.append((entry, p, full))
            elif entry.endswith(".py") and not entry.startswith(("_", ".")):
                found.append((entry[:-3], full, PLUGINS_DIR))
        return found

    # ------------------------------------------------------------ 加载
    def load_all(self):
        self.unload_all()
        for folder, path, pdir in self.discover():
            try:
                loaded = self._load_one(path, pdir)
                if not loaded:
                    continue
                if not self.ctx.cfg.plugin_enabled(loaded.id):
                    continue
                self.plugins[loaded.id] = loaded
                self.message.emit(f"插件已加载：{loaded.name} v{loaded.instance.version}")
            except Exception as exc:
                self.failures.append({"path": path, "error": f"{exc}",
                                      "trace": traceback.format_exc()})
                self.message.emit(f"插件加载失败 {folder}：{exc}")
        self.changed.emit()

    def _load_one(self, path: str, pdir: str):
        mod_name = "eyy_plugin_" + os.path.basename(pdir).replace("-", "_").replace(".", "_")
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if not spec or not spec.loader:
            raise RuntimeError("无法创建模块 spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)

        cls = None
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, PluginBase) and obj is not PluginBase:
                cls = obj
                break
        if cls is None:
            raise RuntimeError("plugin.py 里没有继承 PluginBase 的类")

        inst = cls()
        meta = getattr(module, "PLUGIN", None)
        if isinstance(meta, dict):
            for k in ("id", "name", "version", "description", "author", "has_panel"):
                if k in meta:
                    setattr(inst, k, meta[k])
        api = PluginAPI(self.ctx, inst.id, pdir)
        sys.modules[mod_name].__dict__["api"] = api
        inst.on_load(api)
        return LoadedPlugin(inst, api, path, pdir)

    def unload_all(self):
        for p in list(self.plugins.values()):
            try:
                p.instance.on_unload()
            except Exception:
                pass
        self.plugins.clear()

    def reload(self):
        self.load_all()

    def set_enabled(self, plugin_id: str, on: bool):
        self.ctx.cfg.set_plugin_enabled(plugin_id, on)
        self.reload()

    # ------------------------------------------------------------ 查询
    def infos(self):
        rows = []
        for folder, path, pdir in self.discover():
            pid = folder
            rows.append({
                "folder": folder, "path": path,
                "loaded": pid in self.plugins or any(p.id == pid for p in self.plugins.values()),
                "enabled": self.ctx.cfg.plugin_enabled(pid),
                "name": next((p.name for p in self.plugins.values()
                              if p.id == pid or p.plugin_dir == pdir), folder),
            })
        return rows

    def enabled_plugins(self):
        return list(self.plugins.values())

    def actions(self):
        out = []
        for p in self.plugins.values():
            try:
                for a in p.instance.actions() or []:
                    a = dict(a)
                    a["plugin"] = p.id
                    a["plugin_name"] = p.name
                    out.append(a)
            except Exception as exc:
                self.message.emit(f"插件 {p.name} 的 actions() 出错：{exc}")
        return out

    def panel(self, plugin_id: str, parent=None):
        p = self.plugins.get(plugin_id)
        if not p:
            return None
        if p.panel is None:
            try:
                p.panel = p.instance.create_panel(parent)
            except Exception as exc:
                self.message.emit(f"插件 {p.name} 面板创建失败：{exc}")
                return None
        return p.panel
