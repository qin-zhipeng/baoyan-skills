# -*- coding: utf-8 -*-
"""ui/widgets.py —— 可复用的界面零件：高亮委托、话术编辑框、变量填值框、设置页、插件页。"""
from __future__ import annotations

import html
import os
import re

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QColor, QFont, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QStyledItemDelegate, QStyle, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QMessageBox,
)

from core import win32util as w
from core.config import PLUGINS_DIR

HTML_ROLE = Qt.UserRole + 7
DATA_ROLE = Qt.UserRole + 8


def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def highlight(text: str, words) -> str:
    out = esc(text)
    for word in sorted({x for x in words if x}, key=len, reverse=True):
        if not word:
            continue
        pattern = re.compile(re.escape(esc(word)), re.IGNORECASE)
        out = pattern.sub(lambda m: f'<span style="background:#7b3a30;color:#ffe1b0;">{m.group(0)}</span>', out)
    return out


def phrase_html(p: dict, words=()) -> str:
    title = highlight(p.get("title", ""), words)
    tags = "".join(f'<span style="color:#7f8aa3;"> #{esc(t)}</span>'
                   for t in (p.get("tags") or [])[:4])
    hk = p.get("hotkey") or ""
    hk_html = (f'<span style="color:#ffb3b8;"> ⌨ {esc(hk)}</span>' if hk else "")
    star = '<span style="color:#ffc53d;"> ★</span>' if p.get("bookmark") else ""
    content = (p.get("content") or "").replace("\n", " ").replace("|||", " ⏎ ")
    preview = highlight(content[:72] + ("…" if len(content) > 72 else ""), words)
    files = p.get("files") or []
    file_html = (f'<span style="color:#7aa2ff;"> 📎{len(files)}</span>' if files else "")
    usage = p.get("usage") or 0
    usage_html = (f'<span style="color:#6b7280;"> · 用过{usage}次</span>' if usage else "")
    return (f'<div><span style="font-size:14px;font-weight:600;">{title}</span>'
            f'{star}{tags}{hk_html}{file_html}{usage_html}</div>'
            f'<div style="color:#98a0b3;margin-top:3px;">{preview}</div>')


class HighlightDelegate(QStyledItemDelegate):
    """让 QListWidget 的条目支持富文本 + 搜索关键词高亮。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.words = []
        self._doc = QTextDocument()

    def set_words(self, words):
        self.words = list(words or [])

    def paint(self, painter, option, index):
        painter.save()
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor("#402b33"))
            painter.fillRect(option.rect.adjusted(0, 0, -option.rect.width() + 3, 0),
                             QColor("#e5484d"))
        text = index.data(HTML_ROLE) or esc(index.data(Qt.DisplayRole) or "")
        self._doc.setHtml(text)
        self._doc.setDefaultFont(QFont("Microsoft YaHei UI", 10))
        self._doc.setTextWidth(max(20, option.rect.width() - 16))
        painter.translate(option.rect.left() + 8, option.rect.top() + 5)
        self._doc.drawContents(painter)
        painter.restore()

    def sizeHint(self, option, index):
        text = index.data(HTML_ROLE) or ""
        self._doc.setHtml(text)
        self._doc.setTextWidth(max(20, option.rect.width() - 16) if option.rect.width() > 0 else 300)
        return QSize(option.rect.width(), int(self._doc.size().height()) + 12)


# ---------------------------------------------------------------- 话术编辑
class PhraseEditDialog(QDialog):
    def __init__(self, library, phrase: dict = None, categories=None, parent=None):
        super().__init__(parent)
        self.library = library
        self.phrase = phrase or {}
        self.setWindowTitle("编辑话术" if phrase else "新建话术")
        self.resize(620, 560)
        cats = categories if categories is not None else library.categories()

        self.ed_title = QLineEdit(self.phrase.get("title", ""))
        self.ed_title.setPlaceholderText("给这条话术起个好搜的名字")
        self.cb_cat = QComboBox()
        self.cb_cat.addItem("（未分类）", "")
        for c in cats:
            self.cb_cat.addItem(c["name"], c["id"])
        cur = self.phrase.get("category_id", "")
        idx = self.cb_cat.findData(cur)
        self.cb_cat.setCurrentIndex(max(0, idx))

        self.ed_content = QPlainTextEdit(self.phrase.get("content", ""))
        self.ed_content.setPlaceholderText(
            "支持变量 {姓名} {日期}；用 ||| 分段发送（一条一条发出，避免刷屏）")
        self.ed_tags = QLineEdit(" ".join(self.phrase.get("tags") or []))
        self.ed_tags.setPlaceholderText("用空格分隔，例如：报价 重点")
        self.ed_keywords = QLineEdit(self.phrase.get("keywords", ""))
        self.ed_keywords.setPlaceholderText("额外搜索词 / 拼音，例如 baojia 多少钱")
        self.ed_hotkey = QLineEdit(self.phrase.get("hotkey", ""))
        self.ed_hotkey.setPlaceholderText("例如 Ctrl+Alt+1 / F9（留空表示不绑定）")
        self.ck_bookmark = QCheckBox("收藏（置顶显示）")
        self.ck_bookmark.setChecked(bool(self.phrase.get("bookmark")))
        self.sp_order = QSpinBox()
        self.sp_order.setRange(-9999, 9999)
        self.sp_order.setValue(int(self.phrase.get("order", 999)))

        self.lst_files = QListWidget()
        for f in self.phrase.get("files") or []:
            self.lst_files.addItem(f)
        btn_add_file = QPushButton("添加图片 / 文件")
        btn_add_file.clicked.connect(self._add_files)
        btn_del_file = QPushButton("移除选中")
        btn_del_file.clicked.connect(self._del_file)

        form = QFormLayout()
        form.addRow("标题", self.ed_title)
        form.addRow("分类", self.cb_cat)
        form.addRow("标签", self.ed_tags)
        form.addRow("搜索词", self.ed_keywords)
        form.addRow("专属热键", self.ed_hotkey)
        form.addRow("排序", self.sp_order)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.ck_bookmark)
        lay.addWidget(QLabel("话术内容"))
        lay.addWidget(self.ed_content, 3)
        lay.addWidget(QLabel("附件（点击发送时会一并粘贴；微信里可能需再点一次「发送」）"))
        lay.addWidget(self.lst_files, 1)
        row = QHBoxLayout()
        row.addWidget(btn_add_file)
        row.addWidget(btn_del_file)
        row.addStretch(1)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.button(QDialogButtonBox.Save).setObjectName("Primary")
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择图片或文件", "", "所有文件 (*.*)")
        for p in paths:
            if p and not self.lst_files.findItems(p, Qt.MatchExactly):
                self.lst_files.addItem(p)

    def _del_file(self):
        for it in self.lst_files.selectedItems():
            self.lst_files.takeItem(self.lst_files.row(it))

    def _on_accept(self):
        hk = self.ed_hotkey.text().strip()
        if hk and not w.parse_hotkey(hk)[1]:
            QMessageBox.warning(self, "热键格式不对",
                                "用 Ctrl+Alt+1 / Ctrl+Shift+F9 / F9 这种写法。\n"
                                "支持的修饰键：Ctrl / Alt / Shift / Win")
            return
        if not self.ed_title.text().strip() and not self.ed_content.toPlainText().strip():
            QMessageBox.warning(self, "内容是空的", "至少填标题或内容")
            return
        self.accept()

    def values(self) -> dict:
        return {
            "title": self.ed_title.text().strip() or "未命名",
            "category_id": self.cb_cat.currentData(),
            "content": self.ed_content.toPlainText(),
            "tags": [t for t in re.split(r"[\s,，]+", self.ed_tags.text().strip()) if t],
            "keywords": self.ed_keywords.text().strip(),
            "hotkey": self.ed_hotkey.text().strip(),
            "files": [self.lst_files.item(i).text() for i in range(self.lst_files.count())],
            "bookmark": self.ck_bookmark.isChecked(),
            "order": self.sp_order.value(),
        }


# ---------------------------------------------------------------- 变量填值
class VariableDialog(QDialog):
    def __init__(self, names, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("填写话术变量")
        self.resize(420, 60 + 44 * max(1, len(names)))
        self.config = config
        self.edits = {}
        form = QFormLayout()
        for n in names:
            ed = QLineEdit(str(config.get(f"variables.{n}", "") or ""))
            ed.setPlaceholderText(f"填入 {{{n}}} 的内容，留空则保留原样")
            self.edits[n] = ed
            form.addRow(n, ed)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("这段话术里有变量，填完点发送："))
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("发送")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.button(QDialogButtonBox.Ok).setObjectName("Primary")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _save(self):
        for n, ed in self.edits.items():
            if ed.text().strip():
                self.config.set(f"variables.{n}", ed.text().strip(), save=False)
        self.config.save()
        self.accept()

    def values(self):
        return {n: ed.text().strip() for n, ed in self.edits.items()}


# ---------------------------------------------------------------- 设置页
class SettingsPage(QWidget):
    changed = Signal()

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        cfg = ctx.cfg
        tabs = QTabWidget()

        # --- 发送 ---
        send = QWidget()
        f = QFormLayout(send)
        self.cb_mode = QComboBox()
        for key, label in (("paste_enter", "粘贴并回车（直接发出）"),
                           ("paste", "只粘贴不发送（可再编辑）"),
                           ("copy", "仅复制到剪贴板")):
            self.cb_mode.addItem(label, key)
        self.cb_mode.setCurrentIndex(max(0, self.cb_mode.findData(cfg.get("send.mode"))))
        self.cb_mode.currentIndexChanged.connect(
            lambda: (cfg.set("send.mode", self.cb_mode.currentData()), self.changed.emit()))
        self.sp_delay = self._spin("send.delay_ms", 0, 3000, "粘贴后等待多久再回车（ms）", f)
        self.sp_paste = self._spin("send.paste_delay_ms", 0, 2000, "复制后等待多久再粘贴（ms）", f)
        self.sp_seg = self._spin("send.segment_delay_ms", 0, 5000, "分段发送间隔（ms）", f)
        self._spin("send.restore_delay_ms", 200, 4000, "剪贴板恢复延迟（ms）", f)
        self.ed_sep = QLineEdit(str(cfg.get("send.segment_separator", "|||")))
        self.ed_sep.editingFinished.connect(
            lambda: (cfg.set("send.segment_separator", self.ed_sep.text()), self.changed.emit()))
        f.addRow("分段符", self.ed_sep)
        self._check("send.split_segments", "启用分段发送（用分段符把长话术拆成多条）", f)
        self._check("send.enter_after_paste", "粘贴后自动回车", f)
        self._check("send.restore_clipboard", "发送后恢复剪贴板原有内容", f)
        tabs.addTab(send, "发送")

        # --- 热键 ---
        hk = QWidget()
        hf = QFormLayout(hk)
        self.ed_hk = {}
        for key, label in (("toggle_window", "显示 / 隐藏主窗口"),
                           ("quick_search", "呼出并聚焦搜索框"),
                           ("send_first", "发送搜索结果第一条")):
            ed = QLineEdit(str(cfg.get("hotkeys." + key, "")))
            ed.setPlaceholderText("Ctrl+Shift+Space")
            ed.editingFinished.connect(lambda k=key, e=ed: self._save_hotkey(k, e))
            self.ed_hk[key] = ed
            hf.addRow(label, ed)
        hf.addRow(QLabel("单条话术的专属热键在话术「编辑」里设置；\n"
                         "也支持序列热键（先 Ctrl+A 再 Ctrl+B），见下方表格。"))
        self.tbl_seq = QTableWidget(0, 3)
        self.tbl_seq.setHorizontalHeaderLabels(["名称", "按键序列（逗号分隔）", "动作 ID"])
        self.tbl_seq.horizontalHeader().setStretchLastSection(True)
        for s in cfg.get("sequences", []) or []:
            self._seq_add_row(s)
        seq_row = QHBoxLayout()
        b1 = QPushButton("添加序列")
        b1.clicked.connect(self._seq_add)
        b2 = QPushButton("删除选中")
        b2.clicked.connect(self._seq_del)
        b3 = QPushButton("保存序列")
        b3.setObjectName("Primary")
        b3.clicked.connect(self._seq_save)
        for b in (b1, b2, b3):
            seq_row.addWidget(b)
        seq_row.addStretch(1)
        hf.addRow(self.tbl_seq)
        hf.addRow(seq_row)
        tabs.addTab(hk, "热键")

        # --- 窗口 ---
        win = QWidget()
        wf = QFormLayout(win)
        self._check("window.always_on_top", "窗口置顶", wf)
        self._check("window.dock", "智能吸附到聊天窗口旁边", wf)
        self._check("window.no_activate", "点击窗口不抢焦点（高级：开启后搜索框需手动点两下）", wf)
        self.cb_side = QComboBox()
        self.cb_side.addItem("聊天窗口右侧", "right")
        self.cb_side.addItem("聊天窗口左侧", "left")
        self.cb_side.setCurrentIndex(max(0, self.cb_side.findData(cfg.get("window.dock_side"))))
        self.cb_side.currentIndexChanged.connect(
            lambda: (cfg.set("window.dock_side", self.cb_side.currentData()), self.changed.emit()))
        wf.addRow("吸附位置", self.cb_side)
        self.sp_gap = self._spin("window.dock_gap", 0, 80, "与聊天窗口的间距（px）", wf)
        self.sp_opacity = self._spin("window.opacity", 40, 100, "窗口不透明度（%）", wf)
        tabs.addTab(win, "窗口")

        # --- 常规 ---
        gen = QWidget()
        gf = QFormLayout(gen)
        self.btn_color = QPushButton("选择强调色")
        self.btn_color.clicked.connect(self._pick_color)
        gf.addRow("主题强调色", self.btn_color)
        self._check("general.minimize_to_tray", "关闭时最小化到托盘", gf)
        self.ck_autostart = QCheckBox("开机自动启动")
        self.ck_autostart.setChecked(bool(cfg.get("general.autostart", False)))
        self.ck_autostart.stateChanged.connect(
            lambda v: (cfg.set("general.autostart", bool(v)),
                       self.ctx.set_autostart(bool(v)), self.changed.emit()))
        gf.addRow(self.ck_autostart)
        self._check("general.confirm_variables", "发送含变量的话术时先弹窗填值", gf)
        btn_open = QPushButton("打开数据目录")
        btn_open.clicked.connect(lambda: os.startfile(os.path.dirname(self.ctx.cfg.path)))
        gf.addRow("数据", btn_open)
        row = QHBoxLayout()
        for label, fn in (("导出 CSV", self._export), ("导入 CSV", self._import),
                          ("备份话术库", self._backup), ("从备份恢复", self._restore)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        gf.addRow("数据管理", self._wrap(row))
        tabs.addTab(gen, "常规")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.addWidget(tabs)

    # --- helpers ---
    @staticmethod
    def _wrap(layout):
        wgt = QWidget()
        wgt.setLayout(layout)
        return wgt

    def _spin(self, key, lo, hi, tip, form):
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setValue(int(self.ctx.cfg.get(key, lo)))
        sp.setToolTip(tip)
        sp.valueChanged.connect(
            lambda v, k=key: (self.ctx.cfg.set(k, v), self.changed.emit()))
        form.addRow(tip.split("（")[0], sp)
        return sp

    def _check(self, key, label, form):
        cb = QCheckBox(label)
        cb.setChecked(bool(self.ctx.cfg.get(key, False)))
        cb.stateChanged.connect(
            lambda v, k=key: (self.ctx.cfg.set(k, bool(v)), self.changed.emit()))
        form.addRow(cb)
        return cb

    def _save_hotkey(self, key, ed):
        val = ed.text().strip()
        if val and not w.parse_hotkey(val)[1]:
            QMessageBox.warning(self, "格式不对", "例如 Ctrl+Shift+Space / F9 / Alt+1")
            ed.setText(str(self.ctx.cfg.get("hotkeys." + key, "")))
            return
        self.ctx.cfg.set("hotkeys." + key, val)
        self.ctx.apply_hotkeys()
        self.changed.emit()

    def _seq_add_row(self, s=None):
        r = self.tbl_seq.rowCount()
        self.tbl_seq.insertRow(r)
        for c, v in enumerate([s.get("id", ""), ",".join(s.get("keys", [])),
                               s.get("action", "")] if s else ["", "", ""]):
            self.tbl_seq.setItem(r, c, QTableWidgetItem(v))

    def _seq_add(self):
        self._seq_add_row()

    def _seq_del(self):
        r = self.tbl_seq.currentRow()
        if r >= 0:
            self.tbl_seq.removeRow(r)
            self._seq_save()

    def _seq_save(self):
        out = []
        for r in range(self.tbl_seq.rowCount()):
            vals = [self.tbl_seq.item(r, c).text().strip() if self.tbl_seq.item(r, c) else ""
                    for c in range(3)]
            keys = [k.strip() for k in vals[1].replace("，", ",").split(",") if k.strip()]
            if vals[0] and len(keys) >= 2 and vals[2]:
                out.append({"id": vals[0], "keys": keys, "action": vals[2]})
        self.ctx.cfg.set("sequences", out)
        self.ctx.apply_hotkeys()
        self.changed.emit()

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(str(self.ctx.cfg.get("general.accent", "#e5484d"))),
                                  self, "选择强调色")
        if c.isValid():
            self.ctx.cfg.set("general.accent", c.name())
            self.ctx.apply_style()

    def _export(self):
        p, _ = QFileDialog.getSaveFileName(self, "导出 CSV", "话术库.csv", "CSV (*.csv)")
        if p:
            n = self.ctx.library.export_csv(p)
            QMessageBox.information(self, "完成", f"已导出 {n} 条话术到\n{p}")

    def _import(self):
        p, _ = QFileDialog.getOpenFileName(self, "导入 CSV", "", "CSV (*.csv);;所有文件 (*.*)")
        if p:
            n = self.ctx.library.import_csv(p)
            self.ctx.window.refresh_all()
            QMessageBox.information(self, "完成", f"已导入 {n} 条话术")

    def _backup(self):
        p, _ = QFileDialog.getSaveFileName(self, "备份", "话术库备份.json", "JSON (*.json)")
        if p and self.ctx.library.backup(p):
            QMessageBox.information(self, "完成", "备份成功")

    def _restore(self):
        p, _ = QFileDialog.getOpenFileName(self, "恢复", "", "JSON (*.json)")
        if p and self.ctx.library.restore(p):
            self.ctx.window.refresh_all()
            QMessageBox.information(self, "完成", "恢复成功")


# ---------------------------------------------------------------- 插件管理页
class PluginPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        head = QHBoxLayout()
        title = QLabel("插件管理")
        title.setObjectName("Title")
        head.addWidget(title)
        head.addStretch(1)
        b_reload = QPushButton("重新扫描")
        b_reload.clicked.connect(self.reload)
        b_folder = QPushButton("打开插件目录")
        b_folder.clicked.connect(lambda: os.startfile(PLUGINS_DIR))
        head.addWidget(b_reload)
        head.addWidget(b_folder)
        lay.addLayout(head)
        self.list = QListWidget()
        lay.addWidget(self.list, 1)
        hint = QLabel("把插件文件夹放进 plugins/ 后点「重新扫描」。每个插件都是普通 Python 文件，改完即时生效。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.reload()

    def reload(self):
        self.list.clear()
        pm = self.ctx.plugins
        for p in pm.enabled_plugins():
            item = QListWidgetItem(self.list)
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(8, 6, 8, 6)
            cb = QCheckBox(f"{p.name}  v{p.instance.version}")
            cb.setChecked(True)
            cb.stateChanged.connect(lambda v, pid=p.id: (
                self.ctx.cfg.set_plugin_enabled(pid, bool(v)), self.ctx.reload_plugins()))
            info = QLabel(p.instance.description or "（无描述）")
            info.setObjectName("Hint")
            info.setWordWrap(True)
            txt = QVBoxLayout()
            txt.addWidget(cb)
            txt.addWidget(info)
            h.addLayout(txt, 1)
            for a in p.instance.actions() or []:
                b = QPushButton(a.get("label", a.get("id", "")))
                b.setToolTip(a.get("tip", ""))
                b.clicked.connect(lambda _, cb_=a.get("callback"): self._run(cb_))
                h.addWidget(b)
            if p.instance.has_panel:
                b = QPushButton("打开面板")
                b.clicked.connect(lambda _, pid=p.id: self.ctx.window.open_toolbox(pid))
                h.addWidget(b)
            item.setSizeHint(row.sizeHint())
            self.list.setItemWidget(item, row)
        for f in pm.failures:
            self.list.addItem(f"✗ {os.path.basename(f['path'])}：{f['error']}")
        for info in pm.infos():
            if not info["enabled"] and not any(p.id == info["folder"] for p in pm.enabled_plugins()):
                item = QListWidgetItem(self.list)
                row = QWidget()
                h = QHBoxLayout(row)
                cb = QCheckBox(info["folder"] + "（已禁用）")
                cb.stateChanged.connect(lambda v, pid=info["folder"]: (
                    self.ctx.cfg.set_plugin_enabled(pid, bool(v)), self.ctx.reload_plugins()))
                h.addWidget(cb)
                h.addStretch(1)
                item.setSizeHint(row.sizeHint())
                self.list.setItemWidget(item, row)

    @staticmethod
    def _run(cb):
        if callable(cb):
            try:
                cb()
            except Exception as e:
                QMessageBox.warning(None, "插件动作出错", str(e))
