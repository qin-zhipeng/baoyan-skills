# -*- coding: utf-8 -*-
"""plugin_base.py —— 插件契约。写插件只需要继承 PluginBase，实现几个可选方法。"""
from __future__ import annotations


class PluginBase:
    """所有插件的基类。元数据直接写类属性即可，不需要额外的清单文件。"""

    id = "unnamed"
    name = "未命名插件"
    version = "1.0.0"
    description = ""
    author = ""
    # 是否在主界面「工具箱」里显示一个独立面板
    has_panel = False

    def on_load(self, api):
        """插件被启用时调用。api 是 PluginAPI 实例，保存下来即可。"""

    def on_unload(self):
        """插件被禁用 / 程序退出时调用，用来清理线程、热键等。"""

    def actions(self):
        """返回要注入到菜单/工具箱的动作列表：
        [{"id": "xxx", "label": "显示在菜单上的文字", "tip": "说明",
          "callback": callable, "hotkey": "Ctrl+Alt+X"}]
        """
        return []

    def create_panel(self, parent=None):
        """可选：返回 QWidget，会作为「工具箱」中的一个标签页。"""
        return None

    def config_defaults(self):
        """可选：插件配置默认值 dict。"""
        return {}
