# 插件开发指南（DIY 手册）

目标：**加功能不用改主程序**。把文件夹丢进 `plugins/`，重启或点「插件 → 重新扫描」即可。

---

## 一、三步写出第一个插件

**第 1 步**：新建 `plugins/my_tool/plugin.py`

```python
from core.plugin_base import PluginBase

class Plugin(PluginBase):
    id = "my_tool"                 # 唯一，别和别人重名
    name = "我的工具"
    version = "1.0.0"
    description = "一句话说明它干什么"
    author = "我"
    has_panel = False              # True = 在「工具箱」里多一个标签页

    def on_load(self, api):
        self.api = api             # 保存 API，后面全靠它

    def actions(self):
        return [{
            "id": "do_it",
            "label": "跑一下",
            "tip": "鼠标悬停时的说明",
            "hotkey": "Ctrl+Alt+M",         # 可选
            "callback": self.do_it,
        }]

    def do_it(self):
        self.api.copy_text("你好")
        self.api.log("跑过了")
        self.api.notify("我的工具", "完成")
```

**第 2 步**：主界面「插件」页 → 重新扫描（默认自动启用）。

**第 3 步**：托盘菜单「插件动作」里就能点了；`Ctrl+Alt+M` 直接触发。

单文件也行：`plugins/my_tool.py` 会被同样加载。目录形式更适合带自己的数据文件。

---

## 二、PluginBase 可实现的接口

| 方法 | 何时调用 | 返回值 |
|---|---|---|
| `on_load(api)` | 启用时（每次重载都会重新调用） | — |
| `on_unload()` | 禁用 / 退出时，用来停线程、清理资源 | — |
| `actions()` | 构建菜单时 | `list[dict]`，见上 |
| `create_panel(parent)` | 打开「工具箱」时（`has_panel=True`） | `QWidget` |
| `config_defaults()` | 可选，声明配置默认值 | `dict` |

类属性也可以写在模块级的 `PLUGIN = {...}` 字典里，加载器会覆盖同名类属性。

`actions()` 每条支持的字段：`id` / `label` / `tip` / `callback` / `hotkey`。
动作会被注入到 **托盘菜单「插件动作」** 和 **插件管理页按钮**。

---

## 三、PluginAPI 全表

`api` 是插件唯一需要认识的对象，**不要用 `import core.sender` 之类的方式绕过去**。

### 配置与数据
| 方法 | 说明 |
|---|---|
| `api.get_config(key, default)` | 读本插件的配置（自动存在 `settings.json` 的 `plugins.data.<id>` 下） |
| `api.set_config(key, value)` | 写配置，自动落盘 |
| `api.all_config()` | 本插件全部配置 |
| `api.data_path("x.json")` | 插件私有目录 `plugins/<id>/_data/x.json`，自动建目录，适合存大数据 |

### 发送 / 剪贴板
| 方法 | 说明 |
|---|---|
| `api.send_text(text, enter=True, target=None, block=False)` | 发送文字到目标聊天窗口 |
| `api.send_files(paths, target=None)` | 发送图片/文件（`CF_HDROP`） |
| `api.copy_text(text)` | 写 Unicode 文本到剪贴板 |
| `api.paste_clipboard()` | 把当前剪贴板粘到已激活窗口（不回车） |
| `api.paste_only(text, target=None)` | 复制 + 粘贴，适合填输入框 |
| `api.snapshot_clipboard()` / `api.restore_clipboard(snap)` | 批量操作前后保存/恢复剪贴板 |

### 窗口 / 键鼠（UI 自动化）
| 方法 | 说明 |
|---|---|
| `api.list_windows()` | 所有顶层窗口 `[{hwnd,title,class,rect,pid,visible}]` |
| `api.find_window(title_contains="", class_name="")` | 按标题片段 / 类名找窗口，返回 hwnd |
| `api.window_info(hwnd)` | 单个窗口信息 |
| `api.target_window()` | 当前话术发送目标 |
| `api.activate(hwnd=None, title_contains="")` | 激活窗口（带前台锁绕过） |
| `api.click(x, y, double=False, right=False)` | 鼠标点击 |
| `api.tap(vk)` / `api.combo([vk,...], vk)` | 单键 / 组合键 |
| `api.type_text(text)` | SendInput Unicode 逐字输入（**中文请用 copy+paste**） |
| `api.delay(seconds)` / `api.cursor_pos()` | 延时 / 读鼠标坐标 |
| `api.VK` | 虚拟键码字典：`VK["ENTER"]`、`VK["CTRL"]`、`VK["F"]`… |

### 热键 / 界面 / 话术库
| 方法 | 说明 |
|---|---|
| `api.register_hotkey(combo, action_id, callback)` | 注册全局热键（如 `F8`） |
| `api.open_panel()` | 打开本插件的工具箱面板 |
| `api.log(msg)` / `api.notify(title, msg)` | 写日志 / 托盘气泡 |
| `api.phrases(query="", category_id=None)` | 读话术库 |
| `api.stats()` | 使用统计 |

> **线程安全**：热键回调运行在热键线程里。要操作 Qt 控件，请像示例插件那样用 `QObject` 的 `Signal` 发回主线程，或用 `QMetaObject.invokeMethod(..., Qt.QueuedConnection)`。

---

## 四、插件配置与设置项

插件自己的设置可以直接用 `get_config/set_config`，也可以在 `create_panel()` 里画一个设置区。

全局设置（`data/settings.json`）里与插件相关的：

```json
"plugins": {
  "enabled": { "wechat_tagger": true, "hello_world": true },
  "data":    { "wechat_tagger": { "...": "..." } }
},
"sequences": [
  { "id": "打标签", "keys": ["Ctrl+A", "Ctrl+B"], "action": "plugin:tag_current" }
]
```

`action` 可写：
- `toggle_window` / `quick_search` / `send_first`
- `phrase:<话术ID>` — 直接发某条话术
- `pluginact:<插件ID>:<动作ID>` — 调用插件动作
- `plugin:<动作ID>` — 按动作 ID 找

序列热键在「设置 → 热键」页的表格里维护，1.2 秒内依次按下即触发。

---

## 五、微信打标签助手（完整示例）

位置：`plugins/wechat_tagger/plugin.py`。这是「把 DIY 能力用起来」的参考实现。

### 它能做什么
1. **标签池**：维护 `意向高 / 已报价 / 待跟进 / 已成交 …`，勾选后一键赋给清单里勾中的联系人。
2. **联系人清单**：表格增删改、CSV 导入导出、完成状态持久化（重跑不重复打）。
3. **坐标宏录制**：把鼠标移到微信里要点的地方，按 `F8` 采样，生成可回放的步骤序列。
4. **批量执行**：逐个 `Ctrl+F` 搜索联系人 → 回放宏 → 自动填标签 → 标记完成。
5. **不依赖宏也能用**：生成/复制/发送标签备注文本；联系人与标签管理本身完全可用。
6. **实验性控件定位**：装了 `uiautomation` 时尝试按控件名查找，失败自动提示改用坐标宏。

### 为什么用坐标宏
微信 PC 客户端的 UI 自动化控件树在不同版本差异很大，且没有公开接口。坐标宏是**最稳、最 DIY**的方案：微信改版了，重录一次 5~6 个点就行，不用等谁适配。

### 典型宏（6 步）
| # | 类型 | 参数 | 说明 |
|---|---|---|---|
| 1 | 单击 | F8 采样 | 点聊天窗口右上角「…」/ 头像 |
| 2 | 单击 | F8 采样 | 点「设置备注和标签」 |
| 3 | 单击 | F8 采样 | 点标签输入框 |
| 4 | 粘贴文本 | `{标签}` | 自动替换成该联系人的标签（顿号分隔） |
| 5 | 按键 | `ENTER` | 确认标签 |
| 6 | 单击 | F8 采样 | 点「确定 / 保存」 |

其中 `{标签}`、`{姓名}`、`{备注}` 是运行时变量；步骤可上移下移、删除、随时重录。

### CSV 格式
```csv
名称,标签,备注,已完成
张三,意向高、已报价,9月咨询,
李四,待跟进,学生家长,
```
列名也兼容 `name` / `tags` / `note`。导出用 UTF-8 BOM，Excel 双击不乱码。

### 执行流程（源码 `_worker`）
```
保存剪贴板 → 找微信窗口 → 逐个联系人：
    激活窗口 → Ctrl+F → 粘贴昵称 → Enter → 回放宏 → 标记完成 → 延时
恢复剪贴板
```
`停止` 按钮是软停止，当前联系人跑完就停。所有耗时操作在后台线程，界面不卡；UI 更新通过 `Signal` 排队回主线程。

### 边界
只做 Windows UI 层的**模拟人工点击 + 剪贴板粘贴**。不注入微信进程、不 hook 协议、不读写内存、不碰聊天记录。批量操作请控制频率，遵守平台使用规则。

---

## 六、还能怎么 DIY（几个现成思路）

| 想法 | 用哪些 API |
|---|---|
| AI 改写话术后再发 | `phrases()` 取内容 → 调你自己的 LLM → `send_text()` |
| 客户信息自动填变量 | `find_window()` + `copy_text()` + `get_config()` 记住上次值 |
| 话术库同步到网盘/数据库 | 直接读写 `data/phrases.json`，或 `api.data_path()` |
| 定时/批量发送报表 | `QTimer` + `send_files()`，加限速与失败重试 |
| 快捷打开常用资料 | `actions()` 里 `os.startfile(path)` |
| 自定义统计看板 | `stats()` + `create_panel()` 里画个表 |

---

## 七、调试

- 用 `调试启动.bat` 启动，所有 `api.log()` 会打印到控制台。
- 插件加载失败会在「插件」页列出 `✗ 文件名：错误原因`，完整堆栈在 `PluginManager.failures`。
- 改完插件不用重启整个程序：「插件 → 重新扫描」会重新 `import`（会清掉插件内存状态，重要数据记得 `save()`）。
- `python app.py --selftest` 可随时验证核心模块没被改坏。
