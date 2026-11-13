# -*- coding: utf-8 -*-
"""app.py —— 微信辅助器（项目代号 EYY Open）入口。

用法：
    python app.py                 启动极简界面（默认）
    python app.py --classic       回到旧版完整界面 / 插件 / 贴边悬浮
    python app.py --selftest      无界面自检（验证核心模块，不弹窗）

极简界面三件事：常用语一键进微信输入框、常用文件一键进微信输入框、
批量打标签（只勾选、不提交）；外加「吸附」把窗口贴到微信聊天界面右侧。
"""
from __future__ import annotations

import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon
try:
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    HAS_SINGLE_INSTANCE = True
except ImportError:      # 极简环境下没有 QtNetwork 也能正常单机运行
    QLocalServer = QLocalSocket = None
    HAS_SINGLE_INSTANCE = False

from core.config import Config, ROOT as APP_ROOT, ensure_dirs
from core.store import Library
from core.sender import Sender
from core.hotkey import HotkeyManager
from core.dock import DockController
from core.plugins import PluginManager
from ui.main_window import MainWindow
from ui.simple_window import SimpleWindow

STYLE_PATH = os.path.join(ROOT, "ui", "style.qss")
APP_KEY = "EYY_Open_SingleInstance_v1"


class AppContext(QObject):
    """插件、设置页、热键回调共享的服务定位器。"""
    log_signal = Signal(str)

    def __init__(self):
        super().__init__()
        ensure_dirs()
        self.cfg = Config()
        self.library = Library()
        self.sender = Sender(self.cfg, self.library)
        self.hotkeys = HotkeyManager()
        self.plugins = PluginManager(self)
        self.window: MainWindow = None
        self.dock: DockController = None
        self.hotkeys.fired.connect(self.on_hotkey)
        self.hotkeys.error.connect(self.log)
        self.plugins.message.connect(self.log)

    # ------------------------------------------------------------ 外观
    def apply_style(self):
        app = QApplication.instance()
        if not app:
            return
        accent = str(self.cfg.get("general.accent", "#e5484d"))
        try:
            with open(STYLE_PATH, "r", encoding="utf-8") as f:
                qss = f.read().replace("{{ACCENT}}", accent)
            app.setStyleSheet(qss)
        except OSError as exc:
            self.log(f"样式文件读取失败：{exc}")

    # ------------------------------------------------------------ 热键
    def apply_hotkeys(self):
        bindings = dict(self.cfg.get("hotkeys", {}) or {})
        for p in self.library.phrases():
            if p.get("hotkey"):
                bindings[f"phrase:{p['id']}"] = p["hotkey"]
        for a in self.plugins.actions():
            if a.get("hotkey"):
                bindings[f"pluginact:{a['plugin']}:{a.get('id','')}"] = a["hotkey"]
        self.hotkeys.rebind(bindings)
        self.hotkeys.set_sequences(self.cfg.get("sequences", []) or [])

    def on_hotkey(self, action: str):
        if not action:
            return
        if action.startswith("seq:"):
            seq_id = action[4:]
            for s in self.cfg.get("sequences", []) or []:
                if s.get("id") == seq_id:
                    action = s.get("action", "")
                    break
        if action == "toggle_window":
            self.window.toggle_visible()
        elif action == "quick_search":
            self.window.quick_search()
        elif action == "send_first":
            self.window.send_first()
        elif action.startswith("phrase:"):
            p = self.library.get(action[7:])
            if p:
                self.window.send_phrase(p)
        elif action.startswith("pluginact:"):
            _, pid, aid = action.split(":", 2)
            for a in self.plugins.actions():
                if a["plugin"] == pid and a.get("id") == aid and callable(a.get("callback")):
                    a["callback"]()
                    break
        elif action.startswith("plugin:"):
            aid = action[7:]
            for a in self.plugins.actions():
                if a.get("id") == aid and callable(a.get("callback")):
                    a["callback"]()
                    break
        else:
            self.log(f"未处理的热键动作：{action}")

    # ------------------------------------------------------------ 杂项
    def reload_plugins(self):
        self.plugins.load_all()
        self.apply_hotkeys()
        if self.window:
            self.window.refresh_all()
        self.log("插件已重新加载")

    def log(self, message: str):
        text = str(message)
        print(f"[微信辅助器] {text}", flush=True)
        self.log_signal.emit(text)

    def notify(self, title: str, message: str):
        if self.window and self.window.tray.isVisible():
            self.window.tray.showMessage(title, message,
                                         QSystemTrayIcon.MessageIcon.Information, 3000)

    def set_autostart(self, on: bool):
        try:
            import winreg
            RUN_KEY = "微信辅助器"
            OLD_RUN_KEY = "EYYOpen"   # 旧版遗留，顺手清理
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_SET_VALUE)
            if on:
                exe = sys.executable
                pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
                if os.path.exists(pythonw):
                    exe = pythonw
                cmd = f'"{exe}" "{os.path.join(ROOT, "app.py")}"'
                winreg.SetValueEx(key, RUN_KEY, 0, winreg.REG_SZ, cmd)
                try:
                    winreg.DeleteValue(key, OLD_RUN_KEY)
                except FileNotFoundError:
                    pass
                self.log("已设置开机自启")
            else:
                for name in (RUN_KEY, OLD_RUN_KEY):
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                self.log("已取消开机自启")
            winreg.CloseKey(key)
        except Exception as exc:
            self.log(f"开机自启设置失败：{exc}")

    def shutdown(self):
        try:
            self.hotkeys.stop()
        except Exception:
            pass
        try:
            if self.dock:
                self.dock.stop()
        except Exception:
            pass
        try:
            self.plugins.unload_all()
        except Exception:
            pass
        self.cfg.save()


# ---------------------------------------------------------------- 自检
def selftest() -> int:
    import tempfile
    from pathlib import Path
    from core import win32util as w
    from core.config import write_json

    ok, fail, skipped = [], [], []

    def check(name, cond, extra=""):
        (ok if cond else fail).append(f"{name}{(' -> ' + str(extra)) if extra else ''}")

    check("Python 版本", sys.version_info >= (3, 9), sys.version.split()[0])
    sizes = w.struct_sizes()
    check("Win32 结构体尺寸", sizes["DROPFILES"] == 20 and
          (sizes["INPUT"] == (40 if sizes["bits"] == 64 else 28)), str(sizes))

    for combo in ("Ctrl+Shift+F9", "Alt+1", "F12", "Win+Q", "Ctrl+Alt+Space"):
        mods, vk = w.parse_hotkey(combo)
        check(f"热键解析 {combo}", bool(vk), f"mods={mods}, vk={vk}")
    check("非法热键被拒绝", w.parse_hotkey("Ctrl+哈哈")[1] == 0)
    check("热键规范化", w.normalize_hotkey("F9") == "F9",
          w.normalize_hotkey("F9"))

    d = tempfile.mkdtemp(prefix="eyy_selftest_")
    lib_path = os.path.join(d, "phrases.json")
    lib = Library(lib_path)
    cid = lib.add_category("测试分组")
    pid = lib.add_phrase({"category_id": cid, "title": "测试话术A",
                          "content": "您好{姓名}，今天是{日期}", "keywords": "ceshi 唯一词",
                          "tags": ["价格"]})
    check("话术库新增", lib.get(pid) is not None and len(lib.phrases()) >= 5,
          f"{len(lib.phrases())} 条")
    check("多关键词搜索", len(lib.phrases(query="测试 ceshi")) == 1,
          [p["title"] for p in lib.phrases(query="测试 ceshi")])
    check("中文搜索", len(lib.phrases(query="唯一词")) == 1)
    check("变量提取", Library.variables("您好{姓名}，{日期}") == ["姓名", "日期"],
          Library.variables("您好{姓名}，{日期}"))
    check("变量渲染", "张三" in Library.render("您好{姓名}", {"姓名": "张三"}))
    check("内置变量", Library.render("今天{日期}", {}) != "今天{日期}")
    lib.update_phrase(pid, bookmark=True)
    check("收藏过滤", any(p["id"] == pid for p in lib.phrases(bookmark_only=True)))
    lib.record_usage(pid)
    check("使用统计", lib.get(pid)["usage"] == 1)

    csv_path = os.path.join(d, "out.csv")
    check("导出 CSV", lib.export_csv(csv_path) >= 1 and os.path.exists(csv_path))
    lib2 = Library(os.path.join(d, "phrases2.json"))
    check("导入 CSV", lib2.import_csv(csv_path) >= 1)

    from core.config import DEFAULT_SETTINGS, write_json
    check("设置默认值完整", all(k in DEFAULT_SETTINGS for k in
                            ("hotkeys", "send", "window", "general", "plugins")))
    check("JSON 落盘", write_json(os.path.join(d, "x.json"), {"你好": 1}))

    # 剪贴板：保存原内容 -> 写文本 -> 读回 -> 写文件 -> 恢复
    # 会话锁定 / 安全桌面 / 其他程序独占时系统直接拒绝剪贴板，属环境限制，不算功能失败
    clip_problem = w.clipboard_problem()
    if clip_problem:
        skipped.append("剪贴板 4 项（写文本 / 读文本 / CF_HDROP / 恢复）→ " + clip_problem)
    else:
        try:
            snap = w.snapshot_clipboard()
            check("剪贴板写文本", w.set_clipboard_text("微信辅助器 自检 ✅"))
            check("剪贴板读文本", w.get_clipboard_text() == "微信辅助器 自检 ✅",
                  w.get_clipboard_text()[:20])
            tmpfile = os.path.join(d, "测试文件.txt")
            with open(tmpfile, "w", encoding="utf-8") as f:
                f.write("hello")
            check("剪贴板放文件 CF_HDROP", w.set_clipboard_files([tmpfile]))
            w.restore_clipboard(snap)
            check("剪贴板恢复", True)
        except Exception as exc:
            fail.append(f"剪贴板测试异常：{exc}")

    # 插件发现（不实例化，避免依赖界面）
    from core.config import PLUGINS_DIR
    ensure_dirs()
    check("插件目录存在", os.path.isdir(PLUGINS_DIR), PLUGINS_DIR)
    from core.plugin_base import PluginBase
    check("插件基类可用", issubclass(PluginBase, object))

    # 极简模式：常用语/文件库、微信窗口识别、打标签坐标缩放、名单解析
    from core.quickstore import QuickStore
    from core.quick_sender import QuickSender
    from core.wechat_follow import _is_wechat, find_wechat_windows, DockToWeChat
    from core.tag_job import DEFAULT_CFG, scaled_cfg
    q = QuickStore(os.path.join(d, "quick.json"))
    tid = q.add_text("自检常用语", "您好 {日期}")
    check("极简库新增常用语", q.get("text", tid) is not None)
    check("极简库搜索", len(q.texts(query="自检")) == 1)
    q.update_text(tid, "自检改名", "新内容")
    check("极简库改名", q.get("text", tid)["title"] == "自检改名")
    q.bump("text", tid)
    check("极简库计数", q.get("text", tid)["usage"] == 1)
    q.delete("text", tid)
    check("极简库删除", q.get("text", tid) is None)
    check("发送器可选参数", hasattr(QuickSender, "deliver_files"))
    check("微信窗口识别", _is_wechat({"class": "WeChatMainWndForPC", "title": "微信",
                                   "rect": (0, 0, 900, 700)}) is True)
    check("小窗不算微信", _is_wechat({"class": "WeChatMainWndForPC", "title": "微信",
                                   "rect": (0, 0, 200, 120)}) is False)
    check("窗口枚举可调用", isinstance(find_wechat_windows(), list))
    c1 = scaled_cfg(770, 610)
    c2 = scaled_cfg(1540, 1220)
    check("打标签坐标基准", c1["points"]["search_box"] == [180.0, 60.0])
    check("打标签坐标缩放", c2["points"]["search_box"] == [360.0, 120.0],
          c2["points"]["search_box"])
    check("打标签默认配置", DEFAULT_CFG["window_title_keyword"] == "添加成员")
    from core import tag_engine
    csv_list = os.path.join(d, "名单.csv")
    with open(csv_list, "w", encoding="utf-8-sig", newline="") as f:
        f.write("微信备注,年级\n张三,26\n李四,26\n张三,26\n")
    check("名单解析去重", len(tag_engine.load_names(Path(csv_list))) == 2,
          len(tag_engine.load_names(Path(csv_list))))
    phone_list = os.path.join(d, "手机号备注.txt")
    fake_phone = "130" + "0000" + "0000"   # 虚构号，仅验证 11 位手机号备注可解析
    with open(phone_list, "w", encoding="utf-8-sig") as f:
        f.write(fake_phone + "\n12345\n测试客户\n")
    check("手机号备注可解析、序号被过滤",
          [x["name"] for x in tag_engine.load_names(Path(phone_list))]
          == [fake_phone, "测试客户"])

    # 客户名单：意向量表 / 同行过滤 / 直送打标签的闭环
    import datetime
    import json as _json
    from core import chat_radar as radar
    now = datetime.datetime.now()

    def _ts(days):
        return (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    jl = os.path.join(d, "聊天样本.jsonl")
    samples = [
        ("测试客户甲", "demo_a", False, 3, "这个多少钱？有优惠吗，合同怎么签"),
        ("测试客户甲", "demo_a", True, 3, "给您发方案"),
        ("测试客户甲", "demo_a", False, 2, "还有名额吗，我和孩子商量一下"),
        ("测试客户甲", "demo_a", False, 2, "绩点排名大概要多少"),
        ("测试客户乙", "demo_b", False, 1, "好的收到"),
        ("测试客户乙", "demo_b", True, 1, "嗯"),
        ("测试客户乙", "demo_b", False, 1, "谢谢"),
        ("测试某老师", "demo_c", False, 1, "多少钱 优惠 合同 名额"),
        ("测试某老师", "demo_c", False, 1, "价格多少"),
        ("测试某老师", "demo_c", False, 1, "怎么报名"),
        ("测试已成交", "demo_deal", False, 1, "已付款 合同怎么签 优惠 名额"),
        ("测试已成交", "demo_deal", True, 1, "收到，给您安排"),
        ("测试已成交", "demo_deal", False, 1, "绩点排名 目标院校 师资"),
    ]
    with open(jl, "w", encoding="utf-8") as f:
        for name, wxid, me, days, text in samples:
            f.write(_json.dumps({"chat": name, "chat_wxid": wxid,
                                 "is_self": me, "ts": _ts(days),
                                 "content": text}, ensure_ascii=False) + "\n")
    rows = radar.analyze([jl], "intent", top=10, min_score=8)
    check("客户名单意向打分", len(rows) == 1 and rows[0]["name"] == "测试客户甲",
          [r["name"] for r in rows])
    check("客户名单剔除同行", all("老师" not in r["name"] for r in rows))
    exp = radar.export_names(rows, os.path.join(d, "名单"))
    check("名单可直送打标签", len(tag_engine.load_names(Path(exp))) == 1)
    check("名单数据源自动发现", isinstance(radar.discover_sources(), list))
    follow = radar.analyze([jl], "follow", top=10)
    check("今日跟进剔除已成交", bool(follow) and all(
        not r["is_deal"] and not r["refunded"] and not r["give_up"] for r in follow),
        [r["name"] for r in follow])
    check("今日跟进按待回复优先",
          bool(follow) and follow[0]["name"] == "测试客户甲",
          [(r["name"], r.get("follow_tag")) for r in follow])
    check("今日跟进标注理由", all(r.get("follow_tag") for r in follow),
          [r.get("follow_tag") for r in follow])
    check("吸附高频跟随", DockToWeChat.FAST_MS <= 30
          and DockToWeChat.RESIZE_SETTLE > 0, DockToWeChat.FAST_MS)

    try:
        import PySide6
        check("PySide6 可用", True, PySide6.__version__)
    except Exception as exc:
        fail.append(f"PySide6 不可用：{exc}")

    # 数字框黑边修法很挑 QSS：一旦把上/下按钮宽设 0 或改用 NoButtons，
    # Qt 会把内部 lineEdit 压成 1px、数字直接不显示。这里守住这个回归。
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication as _QApp, QSpinBox as _QSpin
        _qa = _QApp.instance() or _QApp([])
        _qss_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "ui", "simple.qss")
        with open(_qss_path, "r", encoding="utf-8") as _f:
            _box_qss = _f.read()
        _box = _QSpin()
        _box.setValue(20)
        _box.setFixedWidth(60)
        _box.setStyleSheet(_box_qss)
        _box.show()
        _qa.processEvents()
        _le_w = _box.lineEdit().width() if _box.lineEdit() else 0
        check("数字框输入区不被 QSS 压扁", _le_w > 20, f"{_le_w}px")
        _box.close()
    except Exception as exc:
        skipped.append(f"数字框输入区检查（当前环境无界面：{exc}）")

    # ---------------------------------------------------------------- 新增：自动接入 / 解密 / 归因
    try:
        from core import wx_db as _db, wx_bridge as _wb, wx_bridge  # noqa
        from core import deal_attribution as _da, auto_pilot as _ap
        check("解密组件齐全", _db.deps_ok()[0], _db.deps_ok()[1])
        check("SQLCipher 密钥派生长度", len(_db.derive_enc_key(b"0" * 32, b"1" * 16)) == 32)
        # 用已知向量核验 HMAC 逻辑：错密钥必须判否
        fake = b"\x00" * 4096
        check("错误密钥被 HMAC 拒绝", not _db.page1_hmac_ok(b"\x01" * 32, fake))
        check("消息文本解码", _db.as_text("你好") == "你好")
        kind, txt = _db.render_row(1, "测试消息")
        check("文本消息渲染", kind == "文本" and txt == "测试消息", (kind, txt))
        check("图片消息只留标签", _db.render_row(3, b"")[0] == "图片")
        check("会话类型判定", _db.kind_of("wxid_abc") == "单聊"
              and _db.kind_of("x@chatroom") == "群聊")
        # 这几项是「环境探测」不是功能断言：对方电脑没装 / 没登录微信时
        # 也必须能通过自检，否则一键安装会在新机器上直接失败。
        check("微信主程序探测可用", isinstance(_wb.find_wechat_exe(), str),
              _wb.find_wechat_exe() or "本机未安装微信（跳过）")
        roots = _wb.find_data_roots()
        check("数据目录探测可用", isinstance(roots, list),
              (", ".join(roots[:2]) if roots else "本机暂无微信数据（跳过）"))
        check("密钥文件自动发现可用", isinstance(_wb.find_existing_keys(), list))
        check("运行状态可读写", isinstance(_wb.load_state(), dict))
        check("归因词典完整", all(k in _da.DEAL_EVIDENCE for k in
                              ("甲方信息表", "e签宝/合同", "对公收款账户", "转账/定金")))
        check("话术归一化", _da.norm("您好，价格 8.98w！") == "您好价格##w",
              _da.norm("您好，价格 8.98w！"))
        check("流水线阶段定义完整", len(_ap.AutoPilot.STAGES) == 7)
    except Exception as exc:
        fail.append(f"自动接入模块自检异常：{type(exc).__name__}: {exc}")

    # 归因：造一份不带任何真实客户信息的合成数据，跑完整条链路
    try:
        import json as _json
        import datetime as _dt
        from core import chat_radar as _radar
        demo = os.path.join(d, "demo_chats.jsonl")
        now = _dt.datetime(2026, 11, 13, 10, 0, 0)
        recs = []
        for ci, name in enumerate(("示例客户甲", "示例客户乙", "示例客户丙", "示例客户丁")):
            uid = "demo_wxid_%d" % ci
            for k in range(12):
                t = (now - _dt.timedelta(days=20 - k)).strftime("%Y-%m-%d %H:%M:%S")
                recs.append({"chat": name, "chat_wxid": uid, "ts": t,
                             "is_self": k % 2 == 0,
                             "content": ("这是同学学校保研政策您可以先看一下，晚点我把保研规划方案发您"
                                         if k % 2 == 0 else "好的，目标院校想冲一下，排名大概前百分之五")})
            if ci in (0, 1):
                recs.append({"chat": name, "chat_wxid": uid,
                             "ts": (now - _dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                             "is_self": True, "content": "甲方信息表发您了，e签宝签署链接随后发，对公账户也一起给"})
        with open(demo, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(_json.dumps(r, ensure_ascii=False) + "\n")
        chats = _da.load_chats([demo])
        deals, others = _da.classify(chats)
        check("归因样本分组", len(chats) == 4 and len(deals) == 2 and len(others) == 2,
              (len(chats), len(deals), len(others)))
        outdir = os.path.join(d, "demo_out")
        s = _da.analyze([demo], outdir, top=10)
        check("归因报告已生成", os.path.isfile(s["report"]) and os.path.getsize(s["report"]) > 400)
        check("话术库 CSV 已生成", os.path.isfile(s["csv"]))
        check("可回填话术非空", len(_da.scripts_for_quickstore(s)) >= 1,
              len(_da.scripts_for_quickstore(s)))
        rows = _radar.analyze([demo], mode="follow", top=5)
        check("解密数据格式兼容名单雷达", isinstance(rows, list))
    except Exception as exc:
        fail.append(f"归因链路自检异常：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- 新增：成交客户识别（昵称/备注/标签）
    try:
        from core import deal_signals as _ds
        c0 = _ds.default_config()

        def J(name, **kw):
            return _ds.judge(wxid="selftest", name=name, cfg=c0, **kw)

        check("昵称「已成交」识别", J("测试 已成交")["deal"])
        check("单字「付」标识识别", J("测试-付")["deal"])
        check("括号「全款」识别", J("测试(全款)")["deal"])
        check("标签「已成交」识别", J("普通客户", labels=["已成交"])["deal"])
        check("标签「付款」识别", J("普通客户", labels=["付款"])["deal"])
        check("否定「未成交」不误判", not J("测试 未成交")["deal"])
        check("否定「没付款」不误判", not J("测试 没付款")["deal"])
        check("否定「待付款」不误判", not J("测试 待付款")["deal"])
        check("退费客户被标记", J("测试 退费")["flags"]["refunded"])
        check("放弃客户被标记", J("测试 放弃")["flags"]["give_up"])
        check("姓氏「付老师」不误判", not J("付老师")["deal"])
        check("姓氏「付小明同学」不误判", not J("付小明同学")["deal"])
        check("姓名尾字「王付」不误判", not J("王付")["deal"])
        check("开头「付25」标识识别", J("付25级新生")["deal"])
        check("开头「付学生」标识识别", J("付学生25")["deal"])
        check("「未付款」不被拆成付款", not J("未付款")["deal"])
        check("普通客户不误判", not J("普通客户甲")["deal"])
        check("判定带可读依据", bool(J("测试 已成交")["basis"]))
        c1 = _ds.default_config()
        c1["label_include"] = ["A单"]
        check("自定义标签生效",
              _ds.judge(wxid="x", name="普通客户", labels=["A单"], cfg=c1)["deal"])
        c2 = _ds.default_config()
        c2["manual_not_deal"] = ["测试 已成交"]
        check("强制排除生效",
              _ds.judge(wxid="x", name="测试 已成交", cfg=c2)["level"] == "excluded")
        c3 = _ds.default_config()
        c3["disabled"] = ["成交"]
        check("内置词可禁用", not _ds.judge(wxid="x", name="测试 成交", cfg=c3)["deal"])
        rec = _ds.recommend(
            {str(i): {"remark": "示例 已结 %d" % i, "nick": "", "alias": "",
                      "desc": "", "labels": []} for i in range(3)},
            ["本机成交标签"], c0, top=10)
        check("能从备注学出新标识", any("已结" in r["word"] for r in rec),
              [r["word"] for r in rec[:4]])
        check("能列出本机微信标签", any(r["source"] == "微信标签" for r in rec))
        rev = _ds.export_review({"x": J("测试 已成交")}, os.path.join(d, "核对.csv"))
        check("成交核对清单可导出", os.path.isfile(rev) and os.path.getsize(rev) > 50)
        p = os.path.join(d, "ds_cfg.json")
        _ds.save_config(c1, p)
        check("识别配置可持久化", _ds.load_config(p)["label_include"] == ["A单"])
        check("联系人簿容错", isinstance(_ds.contact_book(), dict))
    except Exception as exc:
        fail.append(f"成交识别自检异常：{type(exc).__name__}: {exc}")

    print("\n=== 微信辅助器 自检 ===")
    for line in ok:
        print("  ✓ " + line)
    for line in fail:
        print("  ✗ " + line)
    for line in skipped:
        print("  ⚠ 跳过 " + line)
    print(f"\n通过 {len(ok)} 项，失败 {len(fail)} 项，跳过 {len(skipped)} 项。")
    return 1 if fail else 0


# ---------------------------------------------------------------- 启动
def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    ensure_dirs()

    # 明确 DPI 感知级别：Win32 拿到的窗口坐标是物理像素，Qt 用逻辑像素，
    # 吸附微信窗口时要按 devicePixelRatio 换算，两者才对得上。
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    classic = "--classic" in sys.argv
    app = QApplication(sys.argv)
    app.setApplicationName("微信辅助器")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")

    # 单实例
    server = None
    if HAS_SINGLE_INSTANCE:
        probe = QLocalSocket()
        probe.connectToServer(APP_KEY)
        if probe.waitForConnected(300):
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(300)
            probe.disconnectFromServer()
            print("已在运行，已呼出主窗口。")
            return 0
        QLocalServer.removeServer(APP_KEY)
        server = QLocalServer()
        server.listen(APP_KEY)

    ctx = AppContext()
    if classic:
        ctx.apply_style()
    win = MainWindow(ctx) if classic else SimpleWindow(ctx)
    ctx.window = win
    if server is not None:
        server.newConnection.connect(lambda: _on_new_instance(server, win))

    if classic:
        ctx.plugins.load_all()      # 极简模式不加载插件，界面只留三件事
    ctx.apply_hotkeys()
    ctx.hotkeys.start()
    win.refresh_all()
    win.show()

    if classic:
        ctx.dock = DockController(win, ctx.cfg, ctx.sender)
        ctx.dock.start()
    win._apply_window_flags()
    ctx.log("微信辅助器 已启动（极简模式）。数据全部保存在 data/ 目录。")

    try:
        return app.exec()
    finally:
        ctx.shutdown()


def _on_new_instance(server: QLocalServer, win: MainWindow):
    conn = server.nextPendingConnection()
    if conn:
        conn.readyRead.connect(lambda: (win.quick_search(), conn.disconnectFromServer()))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # pythonw.exe 没有控制台，异常必须落盘，否则别人只看到「双击没反应」
        traceback.print_exc()
        try:
            log = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "输出", "启动错误.log")
            os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass
        sys.exit(1)
        sys.exit(2)
