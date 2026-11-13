# plugins/ 插件目录

把插件放这里，两种形态都识别：

```
plugins/
├── hello_world/
│   └── plugin.py          ← 最小模板，复制它开始 DIY
├── wechat_tagger/
│   ├── plugin.py          ← 微信打标签助手（含完整面板 + 坐标宏）
│   └── _data/             ← 运行时自动生成，存标签池/清单/宏
└── 我的插件.py             ← 单文件也可以
```

- 每个 `plugin.py` 里放一个继承 `core.plugin_base.PluginBase` 的 `Plugin` 类。
- 完整 API 与教程见项目根目录 **PLUGIN_GUIDE.md**。
- 启用/禁用、重新扫描在主界面「插件」页。
- 目录名以 `_` 或 `.` 开头会被忽略（`__pycache__` 等）。
