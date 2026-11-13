# 第三方组件与许可说明

本目录里的第一方源码按仓库根目录的 [MIT License](../LICENSE) 开源。下列**第三方二进制不在 git 中**，只随 [Release 安装包](https://github.com/qin-zhipeng/baoyan-skills/releases/tag/v1.1.0) 一起分发，各自适用其原许可；MIT 不覆盖它们。

| 组件 | 用途 | 来源 / 许可 |
| --- | --- | --- |
| Python 3.13.1 embeddable + 标准库 `.pyd` / `.dll` | 自带运行环境 | Python Software Foundation，PSF License（`runtime/LICENSE.txt`） |
| PySide6 6.12 | 界面 | Qt Company，LGPLv3 / GPLv3 / 商业许可，按其条款再分发 |
| pycryptodome | SQLCipher 4 解密（AES-256-CBC、PBKDF2-HMAC-SHA512） | BSD-2-Clause / Public Domain，随包 `runtime/Lib/site-packages/Crypto` |
| zstandard | 微信 4.x 消息内容解压 | BSD-3-Clause |
| `libcrypto-3.dll` / `libssl-3.dll`、MSVC `vcruntime140*.dll` | 运行库 | OpenSSL License / Microsoft Visual C++ Redistributable 再分发条款 |
| `bin/wx_key.dll` | 本机微信登录时捕获数据库密钥 | 开源项目 WXexport-tool（WeChat-Export-Tool）v1.2.0，作者 Ray0612；原项目标注 CC 授权，**具体版本与再分发条件请以其上游仓库为准** |

## 使用提醒

1. **再分发前请自行核对上游许可**，尤其是 `bin/wx_key.dll` 与 PySide6；本仓库不对第三方组件的许可兼容性作担保。
2. 密钥捕获组件会挂载登录监听，部分安全软件会误报，属已知现象；不接入也可以使用常用语、常用文件、打标签等功能。
3. 本工具读取微信本地数据库的方式依赖微信 4.x 的实现细节，**可能不符合微信平台规则**，版本更新后也可能失效。使用者需自行评估并承担合规责任，遵守所在地法律法规与微信平台规则。
4. 导出内容含客户隐私，只在本地生成，请自行妥善保管、不要外传。

原项目文档中的组件致谢见 [INSTALL.md](INSTALL.md) 第 6 节。
