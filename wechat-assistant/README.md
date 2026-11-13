# 微信辅助器 · 一键版（源码）

[完整介绍文案](../docs/微信辅助器-一键版.md) · [下载安装包与 20 秒宣传片](https://github.com/qin-zhipeng/baoyan-skills/releases/tag/v1.1.0)

这里是**源码目录**，不是可直接双击运行的安装包。

| 你想要的 | 去哪里 |
| --- | --- |
| 解压即用的完整安装包（自带运行环境） | [WeChat-Assistant-OneKey-20261113.zip](https://github.com/qin-zhipeng/baoyan-skills/releases/download/v1.1.0/WeChat-Assistant-OneKey-20261113.zip) |
| 20 秒宣传片 | [WeChat-Assistant-Promo-20261113.mp4](https://github.com/qin-zhipeng/baoyan-skills/releases/download/v1.1.0/WeChat-Assistant-Promo-20261113.mp4) |
| 完整介绍文案 | [docs/微信辅助器-一键版.md](../docs/微信辅助器-一键版.md) |
| 功能说明 / 安装说明 / 插件开发 | [功能说明.md](功能说明.md) · [INSTALL.md](INSTALL.md) · [PLUGIN_GUIDE.md](PLUGIN_GUIDE.md) |

## 为什么仓库里没有 `runtime/` 和 `bin/`

安装包里自带 Python 3.13 embeddable、PySide6、pycryptodome、zstandard 与 MSVC 运行库，以及密钥捕获组件 `bin/wx_key.dll`。这些都是**第三方二进制**，体积约 30 MB，且各有自己的再分发条款；因此它们**只随 Release 压缩包分发，不进 git**。

本仓库的 [MIT 许可](../LICENSE) 只覆盖这里的第一方源码（`app.py`、`core/`、`ui/`、`plugins/`、配置与文档）。第三方组件清单与来源见 [THIRD-PARTY.md](THIRD-PARTY.md)。

想从源码跑起来：把 Release 压缩包里的 `runtime/` 与 `bin/` 原样拷到本目录，再执行 `runtime\python.exe app.py`；自检是 `runtime\python.exe app.py --selftest`（96 项）。

## 目录

```
wechat-assistant/
├── app.py                 入口、单实例、托盘、--selftest
├── core/                  微信定位/密钥/解密、名单引擎、成交识别、归因、打标签、吸附
├── ui/                    PySide6 界面（极简 5 页签 + 经典界面）
├── plugins/               插件机制与两个示例插件
├── data/                  默认配置（不含任何客户数据）
├── assets/                图标
├── smoke_test.py          自检用例
├── 一键安装.bat / 启动.bat / 调试启动.bat / 创建快捷方式.vbs
├── 功能说明.md            原版 README（完整功能与 FAQ）
├── INSTALL.md             安装与首次接入
└── PLUGIN_GUIDE.md        插件开发指南
```

> `*.bat` 是 GBK 编码（中文 Windows 控制台需要），在 GitHub 网页上会显示乱码，属正常现象；已用 `.gitattributes` 标记为不做换行/编码归一化，直接下载使用不受影响。

## 运行前提

- Windows 微信 4.x（3.x 数据结构不同，不支持）
- 首次接入需要显式同意重启一次微信，并在手机上点微信自己的「确认登录」
- 只读取本机数据，不联网、不上传、无遥测

导出内容含客户隐私，使用前请确认已取得相关方同意，并遵守所在地法律法规与微信平台规则。
