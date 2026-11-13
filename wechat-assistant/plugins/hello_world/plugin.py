# -*- coding: utf-8 -*-
"""示例插件：最小可用模板。复制这个文件夹改一改，就是你的新插件。"""
from core.plugin_base import PluginBase


class Plugin(PluginBase):
    id = "hello_world"
    name = "示例插件"
    version = "1.0.0"
    description = "演示插件能做什么：注入菜单动作、读写自己的配置、调用发送能力。点「试一下」看看。"
    author = "微信辅助器"
    has_panel = False

    def on_load(self, api):
        self.api = api
        self.n = int(api.get_config("clicks", 0))

    def actions(self):
        return [{
            "id": "say_hello",
            "label": "试一下（示例）",
            "tip": "把一句提示写到日志，并把文本复制到剪贴板",
            "hotkey": "Ctrl+Alt+H",
            "callback": self.say_hello,
        }]

    def say_hello(self):
        self.n += 1
        self.api.set_config("clicks", self.n)
        self.api.copy_text(f"你好，这是第 {self.n} 次调用插件 API。")
        self.api.log(f"示例插件被调用了 {self.n} 次")
        self.api.notify("示例插件", "已复制到剪贴板，可以直接 Ctrl+V")
