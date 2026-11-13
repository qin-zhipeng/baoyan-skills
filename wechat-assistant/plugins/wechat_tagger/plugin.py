# -*- coding: utf-8 -*-
"""微信打标签助手 —— 微信辅助器 官方示例插件（也是你 DIY 的参考实现）。

能做什么：
  1. 标签池 + 联系人清单（增删改、勾选、CSV 导入导出、进度持久化）
  2. 「坐标宏录制」：把鼠标放到微信里要点的地方，按 F8 采样，存成步骤序列
  3. 批量执行：逐个搜索联系人 → 回放宏 → 自动填标签 → 记录完成状态
  4. 不依赖宏也能用：一键生成/复制/发送标签备注文本
  5. 实验性控件定位：装了 uiautomation 库时，尝试按控件名自动打标签

边界（重要）：
  只做 Windows UI 层的"模拟人工点击 + 剪贴板粘贴"，不注入微信进程、
  不 hook 协议、不读写内存、不碰聊天数据。微信版本改版后，重录一次坐标即可。
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.plugin_base import PluginBase

STEP_TYPES = ["单击", "双击", "右键", "粘贴文本", "按键", "等待"]
DEFAULT_MACRO_HINT = (
    "还没录制坐标宏。点「录制坐标宏」，把鼠标移到微信里要点的地方按 F8 采样，"
    "例如：① 点头像/更多 ② 点「设置备注和标签」 ③ 点标签输入框 "
    "④ 粘贴文本 {标签} ⑤ 按键 ENTER ⑥ 点确定。"
)


class _Signals(QObject):
    log = Signal(str)
    progress = Signal(int, int)
    running = Signal(bool)
    reload = Signal()
    capture = Signal()


class Plugin(PluginBase):
    id = "wechat_tagger"
    name = "微信打标签助手"
    version = "1.0.0"
    description = ("批量给微信联系人打标签：维护标签池与联系人清单，录制一次坐标宏，"
                   "之后一键批量执行。纯 UI 模拟人工操作，不注入微信。")
    author = "微信辅助器"
    has_panel = True

    # ------------------------------------------------------------ 生命周期
    def on_load(self, api):
        self.api = api
        self.file = api.data_path("tagger.json")
        self.data = self._load()
        self.signals = _Signals()
        self.panel = None
        self._running = False
        self._stop = False
        self._recorder = None
        # F8 是录制采样的全局热键；回调里只发信号，UI 操作回到主线程执行
        api.register_hotkey("F8", "wechat_tagger_capture", self._capture_hotkey)

    def on_unload(self):
        self._stop = True

    def actions(self):
        return [
            {"id": "open_panel", "label": "打开打标签助手",
             "tip": "打开工具箱里的标签管理面板", "callback": self.open_panel},
            {"id": "tag_current", "label": "给当前联系人打标签",
             "tip": "对当前微信聊天窗口执行已录制的坐标宏",
             "hotkey": "Ctrl+Alt+T", "callback": self.tag_current},
            {"id": "copy_tag_text", "label": "复制标签文本",
             "tip": "生成可直接粘进「备注」的标签文本",
             "callback": self.copy_tag_text},
        ]

    def create_panel(self, parent=None):
        if self.panel is None:
            self.panel = TaggerPanel(self, parent)
        return self.panel

    # ------------------------------------------------------------ 数据
    def _default_data(self):
        return {
            "tags": ["意向高", "已报价", "待跟进", "已成交", "保研咨询"],
            "contacts": [],
            "macro": [],
            "settings": {"wechat_title": "微信", "search_delay": 1.0,
                         "step_delay": 0.45, "contact_delay": 1.2},
        }

    def _load(self):
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                d = json.load(f)
            base = self._default_data()
            for k, v in base.items():
                d.setdefault(k, v)
            return d
        except (OSError, json.JSONDecodeError, AttributeError):
            return self._default_data()

    def save(self):
        try:
            with open(self.file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.signals.log.emit(f"保存失败：{exc}")

    # ------------------------------------------------------------ 动作
    def open_panel(self):
        self.api.open_panel()

    def copy_tag_text(self):
        rows = self.panel.checked_contacts() if self.panel else self.data["contacts"]
        text = "\n".join(f"{c['name']}：{'、'.join(c.get('tags') or []) or '（未设标签）'}"
                         for c in rows)
        self.api.copy_text(text or "（清单为空）")
        self.signals.log.emit(f"已复制 {len(rows)} 行标签文本")
        self.api.notify("打标签助手", "标签文本已复制到剪贴板")

    def tag_current(self):
        """对当前微信窗口执行宏；把 {标签} 换成清单里第一个勾选项的标签。"""
        if not self.data.get("macro"):
            self.signals.log.emit("请先录制坐标宏")
            self.open_panel()
            return
        rows = self.panel.checked_contacts() if self.panel else self.data["contacts"]
        c = rows[0] if rows else {"name": "", "tags": list(self.data["tags"][:1])}
        self._run([c], search_contact=False)

    def start_batch(self):
        rows = self.panel.checked_contacts() if self.panel else []
        if not rows:
            self.signals.log.emit("没有勾选任何联系人")
            return
        if not self.data.get("macro"):
            self.signals.log.emit("没有坐标宏，先点「录制坐标宏」采样一次")
            return
        self._run(rows, search_contact=True)

    def stop_batch(self):
        self._stop = True
        self.signals.log.emit("已请求停止，当前联系人执行完就停")

    def _capture_hotkey(self):
        self.signals.capture.emit()

    # ------------------------------------------------------------ 执行引擎
    def _run(self, rows, search_contact=True):
        if self._running:
            self.signals.log.emit("正在执行中，请稍候或点停止")
            return
        self._running = True
        self._stop = False
        self.signals.running.emit(True)
        macro = [dict(s) for s in self.data.get("macro", [])]
        settings = self.data.get("settings", {})
        threading.Thread(target=self._worker,
                         args=(list(rows), macro, settings, search_contact),
                         daemon=True).start()

    def _worker(self, rows, macro, settings, search_contact):
        api = self.api
        snap = api.snapshot_clipboard()
        total = len(rows)
        try:
            hwnd = (api.find_window(title_contains=settings.get("wechat_title", "微信"))
                    or api.find_window(class_name="WeChatMainWndForPC")
                    or api.find_window(title_contains="企业微信"))
            if not hwnd:
                self.signals.log.emit("找不到微信窗口，请先打开微信 PC 客户端")
                return
            vk = api.VK
            for i, c in enumerate(rows):
                if self._stop:
                    self.signals.log.emit("已停止")
                    break
                self.signals.log.emit(f"[{i+1}/{total}] {c['name']} → {'、'.join(c.get('tags') or [])}")
                api.activate(hwnd)
                api.delay(0.35)
                if search_contact:
                    api.combo([vk["CTRL"]], ord("F"))
                    api.delay(0.35)
                    api.copy_text(c.get("name", ""))
                    api.combo([vk["CTRL"]], ord("V"))
                    api.delay(float(settings.get("search_delay", 1.0)))
                    api.tap(vk["ENTER"])
                    api.delay(0.7)
                for step in macro:
                    if self._stop:
                        break
                    self._exec_step(step, c, vk, float(settings.get("step_delay", 0.45)))
                c["done"] = True
                for real in self.data["contacts"]:
                    if real.get("name") == c.get("name"):
                        real["done"] = True
                self.save()
                self.signals.progress.emit(i + 1, total)
                self.signals.reload.emit()
                api.delay(float(settings.get("contact_delay", 1.2)))
            self.signals.log.emit("批量执行结束（完成状态已保存，重跑不会重复打）")
        except Exception as exc:
            self.signals.log.emit(f"执行出错：{exc}")
        finally:
            api.restore_clipboard(snap)
            self._running = False
            self.signals.running.emit(False)

    def _exec_step(self, step, contact, vk, step_delay):
        api = self.api
        typ = step.get("type", "单击")
        value = step.get("value", "") or ""
        tags = "、".join(contact.get("tags") or [])
        value = (value.replace("{标签}", tags)
                      .replace("{姓名}", contact.get("name", ""))
                      .replace("{备注}", contact.get("note", "")))
        if typ in ("单击", "双击", "右键"):
            api.click(int(step.get("x", 0)), int(step.get("y", 0)),
                      double=(typ == "双击"), right=(typ == "右键"))
            api.delay(step_delay)
        elif typ == "粘贴文本":
            api.copy_text(value)
            api.delay(0.12)
            api.paste_clipboard()
            api.delay(step_delay)
        elif typ == "按键":
            key = str(value).strip().upper()
            if key in vk:
                api.tap(vk[key])
            elif key in ("CTRL+V", "CTRLV"):
                api.paste_clipboard()
            api.delay(step_delay)
        elif typ == "等待":
            try:
                api.delay(float(value or step_delay))
            except ValueError:
                api.delay(step_delay)

    # ------------------------------------------------------------ 实验：控件定位
    def try_uiautomation(self, contact):
        """可选增强：装了 uiautomation 时按控件名找按钮；不同微信版本差异大，失败请改用坐标宏。"""
        try:
            import uiautomation as auto
        except ImportError:
            self.signals.log.emit("未安装 uiautomation（可选）。请用坐标宏，适配任何微信版本。")
            return False
        try:
            win = auto.WindowControl(searchDepth=1, ClassName="WeChatMainWndForPC")
            if not win.Exists(1, 1):
                self.signals.log.emit("控件模式没找到微信主窗口")
                return False
            edit = win.EditControl(searchDepth=12, Name="")
            self.signals.log.emit(f"已连接微信窗口：{win.Name}（控件树可用，可按需要扩展此函数）")
            return True
        except Exception as exc:
            self.signals.log.emit(f"控件模式失败：{exc}，建议改用坐标宏")
            return False


# ---------------------------------------------------------------- 面板
class TaggerPanel(QWidget):
    def __init__(self, plugin: Plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.api = plugin.api
        self._recorder = None
        self._build()
        self.reload()
        s = plugin.signals
        s.log.connect(self.log)
        s.progress.connect(lambda a, b: (self.bar.setMaximum(max(1, b)), self.bar.setValue(a)))
        s.running.connect(self._set_running)
        s.reload.connect(self.reload)

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)

        lay.addWidget(QLabel("① 标签池（选中标签可批量赋给勾选的联系人）"))
        row = QHBoxLayout()
        self.ed_tag = QLineEdit()
        self.ed_tag.setPlaceholderText("输入新标签名后回车")
        self.ed_tag.returnPressed.connect(self.add_tag)
        row.addWidget(self.ed_tag, 1)
        b_add = QPushButton("添加")
        b_add.clicked.connect(self.add_tag)
        b_del = QPushButton("删除选中")
        b_del.clicked.connect(self.del_tag)
        b_apply = QPushButton("赋给勾选联系人")
        b_apply.setObjectName("Primary")
        b_apply.clicked.connect(self.apply_tags)
        for b in (b_add, b_del, b_apply):
            row.addWidget(b)
        lay.addLayout(row)
        self.lst_tags = QListWidget()
        self.lst_tags.setFixedHeight(64)
        self.lst_tags.setFlow(QListWidget.LeftToRight)
        self.lst_tags.setWrapping(True)
        self.lst_tags.setResizeMode(QListWidget.Adjust)
        lay.addWidget(self.lst_tags)

        lay.addWidget(QLabel("② 联系人清单（双击单元格可直接改）"))
        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["选", "备注 / 昵称", "标签（顿号分隔）", "状态", "备注"])
        self.tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.cellChanged.connect(self._on_cell_changed)
        lay.addWidget(self.tbl, 1)

        row2 = QHBoxLayout()
        for label, fn in (("＋添加联系人", self.add_contact), ("删除选中", self.del_contact),
                          ("导入 CSV", self.import_csv), ("导出 CSV", self.export_csv),
                          ("清除完成标记", self.clear_done)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row2.addWidget(b)
        row2.addStretch(1)
        lay.addLayout(row2)

        lay.addWidget(QLabel("③ 执行（纯 UI 模拟人工：搜索联系人 → 回放坐标宏 → 自动确认）"))
        row3 = QHBoxLayout()
        self.btn_run = QPushButton("开始批量打标签")
        self.btn_run.setObjectName("Primary")
        self.btn_run.clicked.connect(self.plugin.start_batch)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self.plugin.stop_batch)
        b_cur = QPushButton("给当前联系人打标签")
        b_cur.clicked.connect(self.plugin.tag_current)
        b_rec = QPushButton("录制坐标宏")
        b_rec.clicked.connect(self.open_recorder)
        b_uia = QPushButton("实验：控件定位")
        b_uia.clicked.connect(self.try_uia)
        b_copy = QPushButton("生成标签文本")
        b_copy.clicked.connect(self.plugin.copy_tag_text)
        for b in (self.btn_run, self.btn_stop, b_cur, b_rec, b_uia, b_copy):
            row3.addWidget(b)
        row3.addStretch(1)
        lay.addLayout(row3)

        self.lbl_macro = QLabel(DEFAULT_MACRO_HINT)
        self.lbl_macro.setObjectName("Hint")
        self.lbl_macro.setWordWrap(True)
        lay.addWidget(self.lbl_macro)
        self.bar = QProgressBar()
        self.bar.setValue(0)
        lay.addWidget(self.bar)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumHeight(110)
        lay.addWidget(self.txt_log)

    # ------------------------------------------------------------ 标签池
    def reload(self):
        self.lst_tags.blockSignals(True)
        self.lst_tags.clear()
        for t in self.plugin.data.get("tags", []):
            it = QListWidgetItem(t)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self.lst_tags.addItem(it)
        self.lst_tags.blockSignals(False)
        self._reload_table()
        n = len(self.plugin.data.get("macro", []))
        self.lbl_macro.setText(f"当前坐标宏：{n} 个步骤。" if n else DEFAULT_MACRO_HINT)

    def _reload_table(self):
        self.tbl.blockSignals(True)
        contacts = self.plugin.data.get("contacts", [])
        self.tbl.setRowCount(len(contacts))
        for r, c in enumerate(contacts):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if c.get("checked", True) else Qt.Unchecked)
            self.tbl.setItem(r, 0, chk)
            self.tbl.setItem(r, 1, QTableWidgetItem(c.get("name", "")))
            self.tbl.setItem(r, 2, QTableWidgetItem("、".join(c.get("tags") or [])))
            done = QTableWidgetItem("✓ 已完成" if c.get("done") else "待处理")
            done.setFlags(Qt.ItemIsEnabled)
            self.tbl.setItem(r, 3, done)
            self.tbl.setItem(r, 4, QTableWidgetItem(c.get("note", "")))
        self.tbl.blockSignals(False)

    def _on_cell_changed(self, r, c):
        contacts = self.plugin.data.get("contacts", [])
        if r >= len(contacts):
            return
        item = self.tbl.item(r, c)
        val = item.text() if item else ""
        if c == 0:
            contacts[r]["checked"] = item.checkState() == Qt.Checked
        elif c == 1:
            contacts[r]["name"] = val.strip()
        elif c == 2:
            contacts[r]["tags"] = [x.strip() for x in val.replace(",", "、").split("、") if x.strip()]
        elif c == 4:
            contacts[r]["note"] = val.strip()
        self.plugin.save()

    def add_tag(self):
        t = self.ed_tag.text().strip()
        if t and t not in self.plugin.data["tags"]:
            self.plugin.data["tags"].append(t)
            self.plugin.save()
            self.reload()
        self.ed_tag.clear()

    def del_tag(self):
        for it in self.lst_tags.selectedItems():
            t = it.text()
            if t in self.plugin.data["tags"]:
                self.plugin.data["tags"].remove(t)
        self.plugin.save()
        self.reload()

    def selected_tags(self):
        return [self.lst_tags.item(i).text() for i in range(self.lst_tags.count())
                if self.lst_tags.item(i).checkState() == Qt.Checked]

    def apply_tags(self):
        tags = self.selected_tags()
        if not tags:
            self.log("先在标签池里勾选要赋的标签（点文字前的小方框）")
            return
        for c in self.selected_rows():
            merged = list(dict.fromkeys((c.get("tags") or []) + tags))
            c["tags"] = merged
        self.plugin.save()
        self._reload_table()
        self.log(f"已把 {len(tags)} 个标签赋给勾选的联系人")

    # ------------------------------------------------------------ 联系人清单
    def selected_rows(self):
        """返回勾选行的原始数据引用（用于编辑）。"""
        out = []
        for r in range(self.tbl.rowCount()):
            item = self.tbl.item(r, 0)
            if item and item.checkState() == Qt.Checked and r < len(self.plugin.data["contacts"]):
                out.append(self.plugin.data["contacts"][r])
        return out

    def checked_contacts(self):
        """返回待执行的副本（跳过已完成的）。"""
        out = []
        for r in range(self.tbl.rowCount()):
            item = self.tbl.item(r, 0)
            if item and item.checkState() == Qt.Checked:
                c = self.plugin.data["contacts"][r]
                if not c.get("done"):
                    out.append(dict(c))
        return out

    def add_contact(self):
        self.plugin.data["contacts"].append(
            {"name": "新联系人", "tags": [], "done": False, "checked": True, "note": ""})
        self.plugin.save()
        self._reload_table()

    def del_contact(self):
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True)
        for r in rows:
            if r < len(self.plugin.data["contacts"]):
                self.plugin.data["contacts"].pop(r)
        self.plugin.save()
        self._reload_table()

    def clear_done(self):
        for c in self.plugin.data["contacts"]:
            c["done"] = False
        self.plugin.save()
        self._reload_table()
        self.log("已清除完成标记，可以重跑")

    def export_csv(self):
        p, _ = QFileDialog.getSaveFileName(self, "导出联系人", "微信标签清单.csv", "CSV (*.csv)")
        if not p:
            return
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["名称", "标签", "备注", "已完成"])
            for c in self.plugin.data["contacts"]:
                w.writerow([c.get("name", ""), "、".join(c.get("tags") or []),
                            c.get("note", ""), "1" if c.get("done") else ""])
        self.log(f"已导出 {len(self.plugin.data['contacts'])} 行")

    def import_csv(self):
        p, _ = QFileDialog.getOpenFileName(self, "导入联系人", "", "CSV (*.csv);;所有文件 (*.*)")
        if not p:
            return
        count = 0
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                name = (row.get("名称") or row.get("备注 / 昵称") or row.get("name") or "").strip()
                if not name:
                    continue
                tags = (row.get("标签") or row.get("tags") or "").replace(",", "、")
                self.plugin.data["contacts"].append({
                    "name": name,
                    "tags": [t.strip() for t in tags.split("、") if t.strip()],
                    "done": (row.get("已完成") or "").strip() in ("1", "是", "true"),
                    "checked": True,
                    "note": (row.get("备注") or row.get("note") or "").strip(),
                })
                count += 1
        self.plugin.save()
        self._reload_table()
        self.log(f"已导入 {count} 个联系人")

    # ------------------------------------------------------------ 宏 / 执行
    def open_recorder(self):
        if self._recorder is None:
            self._recorder = MacroRecorder(self.plugin, self)
        self._recorder.show()
        self._recorder.raise_()

    def try_uia(self):
        rows = self.checked_contacts()
        c = rows[0] if rows else {"name": "", "tags": []}
        self.plugin.try_uiautomation(c)

    def _set_running(self, on):
        self.btn_run.setEnabled(not on)
        self.btn_stop.setEnabled(on)

    def log(self, text):
        self.txt_log.appendPlainText(text)
        self.api.log(text)


# ---------------------------------------------------------------- 宏录制器
class MacroRecorder(QDialog):
    """无模态置顶小窗：把鼠标移到目标位置，按 F8（或点按钮）采样坐标。"""

    def __init__(self, plugin: Plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle("坐标宏录制 · 把鼠标移到微信要点的位置，按 F8 采样")
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint |
                            Qt.WindowStaysOnTopHint |
                            Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        hint = QLabel("录制时保持本窗口置顶，鼠标移到微信界面上按 F8 即可记录坐标。\n"
                      "「粘贴文本」里的 {标签} 会自动替换成该联系人的标签，中文必须走粘贴。")
        hint.setWordWrap(True)
        hint.setObjectName("Hint")
        lay.addWidget(hint)

        form = QFormLayout()
        self.cb_type = QComboBox()
        self.cb_type.addItems(STEP_TYPES)
        self.ed_value = QLineEdit()
        self.ed_value.setPlaceholderText("粘贴文本：{标签} / 按键：ENTER / 等待：1.0")
        form.addRow("步骤类型", self.cb_type)
        form.addRow("参数", self.ed_value)
        lay.addLayout(form)

        row = QHBoxLayout()
        b_cap = QPushButton("采样当前鼠标位置（F8）")
        b_cap.setObjectName("Primary")
        b_cap.clicked.connect(self.sample)
        b_add = QPushButton("添加参数步骤")
        b_add.clicked.connect(self.add_value_step)
        b_del = QPushButton("删除选中")
        b_del.clicked.connect(self.del_step)
        b_up = QPushButton("上移")
        b_up.clicked.connect(lambda: self.move_step(-1))
        b_down = QPushButton("下移")
        b_down.clicked.connect(lambda: self.move_step(1))
        b_clear = QPushButton("清空")
        b_clear.setObjectName("Danger")
        b_clear.clicked.connect(self.clear_steps)
        for b in (b_cap, b_add, b_del, b_up, b_down, b_clear):
            row.addWidget(b)
        lay.addLayout(row)

        self.lst = QListWidget()
        lay.addWidget(self.lst, 1)
        b_save = QPushButton("保存宏并关闭")
        b_save.setObjectName("Primary")
        b_save.clicked.connect(self.save_and_close)
        lay.addWidget(b_save)
        self.plugin.signals.capture.connect(self.sample)
        self.refresh()

    def _steps(self):
        return self.plugin.data.setdefault("macro", [])

    def refresh(self):
        self.lst.clear()
        for i, s in enumerate(self._steps(), 1):
            if s.get("type") in ("单击", "双击", "右键"):
                desc = f"{i}. {s['type']}  ({s.get('x')}, {s.get('y')})"
            else:
                desc = f"{i}. {s.get('type')}  {s.get('value','')}"
            self.lst.addItem(desc)

    def sample(self):
        typ = self.cb_type.currentText()
        if typ not in ("单击", "双击", "右键"):
            self.cb_type.setCurrentText("单击")
            typ = "单击"
        x, y = self.plugin.api.cursor_pos()
        self._steps().append({"type": typ, "x": x, "y": y, "value": ""})
        self.refresh()
        self.plugin.signals.log.emit(f"采样 {typ}：({x}, {y})")

    def add_value_step(self):
        typ = self.cb_type.currentText()
        if typ in ("单击", "双击", "右键"):
            self.plugin.signals.log.emit("这个类型请用「采样当前鼠标位置」")
            return
        self._steps().append({"type": typ, "x": 0, "y": 0,
                              "value": self.ed_value.text().strip()})
        self.refresh()

    def del_step(self):
        r = self.lst.currentRow()
        if 0 <= r < len(self._steps()):
            self._steps().pop(r)
            self.refresh()

    def move_step(self, delta):
        r = self.lst.currentRow()
        steps = self._steps()
        nr = r + delta
        if 0 <= r < len(steps) and 0 <= nr < len(steps):
            steps[r], steps[nr] = steps[nr], steps[r]
            self.refresh()
            self.lst.setCurrentRow(nr)

    def clear_steps(self):
        self.plugin.data["macro"] = []
        self.refresh()

    def save_and_close(self):
        self.plugin.save()
        self.plugin.signals.reload.emit()
        self.plugin.signals.log.emit(f"坐标宏已保存：{len(self._steps())} 步")
        self.close()

    def closeEvent(self, e):
        try:
            self.plugin.signals.capture.disconnect(self.sample)
        except Exception:
            pass
        super().closeEvent(e)
