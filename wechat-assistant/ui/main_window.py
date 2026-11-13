# -*- coding: utf-8 -*-
"""ui/main_window.py —— 主窗口：分类 / 搜索 / 一键发送 / 详情 / 工具箱 / 托盘。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QSystemTrayIcon, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core import win32util as w
from core.store import Library
from ui.widgets import (
    DATA_ROLE, HTML_ROLE, HighlightDelegate, PhraseEditDialog,
    PluginPage, SettingsPage, VariableDialog, esc, phrase_html,
)


def make_icon() -> QIcon:
    """优先用 assets/app.ico（桌面快捷方式同款）；文件缺失时退回程序化绘制。"""
    ico = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "assets", "app.ico")
    if os.path.isfile(ico):
        icon = QIcon(ico)
        if not icon.isNull():
            return icon
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#e5484d"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 16, 16)
    p.setPen(QColor("#ffffff"))
    f = QFont("Microsoft YaHei UI", 26, QFont.Bold)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "微")
    p.end()
    return QIcon(pm)


class MainWindow(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.library = ctx.library
        self.sender = ctx.sender
        self._current_cat = "__all"
        self._user_moving = False
        self._toolbox_index = {}
        self.setWindowTitle("微信辅助器")
        self.setWindowIcon(make_icon())
        self.setMinimumSize(820, 560)
        self._build_ui()
        self._build_tray()
        self._restore_geometry()
        self.refresh_all()
        self.sender.target_changed.connect(self._on_target_changed)
        self.ctx.log_signal.connect(self._append_log)
        self.sender.log.connect(self.log)
        self.sender.done.connect(self._on_sent)
        self._apply_window_flags()

    # ------------------------------------------------------------ 构建
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = QFrame()
        top.setObjectName("TopBar")
        th = QHBoxLayout(top)
        th.setContentsMargins(12, 9, 12, 9)
        brand = QLabel("微信<span style='color:#e5484d'>辅助器</span>")
        brand.setTextFormat(Qt.RichText)
        f = QFont("Microsoft YaHei UI", 12, QFont.Bold)
        brand.setFont(f)
        th.addWidget(brand)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("多关键词搜索，空格分隔；支持拼音首字母，如 baojia / bj")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(self.refresh_phrases)
        self.ed_search.returnPressed.connect(self.send_first)
        th.addWidget(self.ed_search, 1)
        self.btn_new = QPushButton("＋ 新建")
        self.btn_new.setObjectName("Primary")
        self.btn_new.clicked.connect(self.new_phrase)
        th.addWidget(self.btn_new)
        b_hide = QPushButton("隐藏")
        b_hide.clicked.connect(self.hide)
        th.addWidget(b_hide)
        root.addWidget(top)

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(4)
        self.sidebar = self._build_sidebar()
        self.center = self._build_center()
        self.tabs = self._build_tabs()
        split.addWidget(self.sidebar)
        split.addWidget(self.center)
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 1)
        split.setSizes([180, 400, 340])
        root.addWidget(split, 1)

        status = QFrame()
        status.setObjectName("StatusBar")
        sh = QHBoxLayout(status)
        sh.setContentsMargins(12, 7, 12, 7)
        self.lbl_target = QLabel("目标：等待识别聊天窗口…")
        self.lbl_target.setObjectName("Hint")
        sh.addWidget(self.lbl_target, 1)
        sh.addWidget(QLabel("模式"))
        self.cb_mode = QComboBox()
        for key, label in (("paste_enter", "粘贴并发送"), ("paste", "只粘贴"),
                           ("copy", "仅复制")):
            self.cb_mode.addItem(label, key)
        self.cb_mode.setCurrentIndex(max(0, self.cb_mode.findData(self.cfg.get("send.mode"))))
        self.cb_mode.currentIndexChanged.connect(
            lambda: self.cfg.set("send.mode", self.cb_mode.currentData()))
        sh.addWidget(self.cb_mode)
        self.btn_pin = QPushButton("锁定目标")
        self.btn_pin.setCheckable(True)
        self.btn_pin.clicked.connect(self._toggle_pin)
        sh.addWidget(self.btn_pin)
        self.ck_dock = QCheckBox("吸附")
        self.ck_dock.setChecked(bool(self.cfg.get("window.dock", False)))
        self.ck_dock.stateChanged.connect(
            lambda v: self.cfg.set("window.dock", bool(v)))
        sh.addWidget(self.ck_dock)
        self.ck_top = QCheckBox("置顶")
        self.ck_top.setChecked(bool(self.cfg.get("window.always_on_top", True)))
        self.ck_top.stateChanged.connect(
            lambda v: (self.cfg.set("window.always_on_top", bool(v)),
                       self._apply_window_flags()))
        sh.addWidget(self.ck_top)
        root.addWidget(status)

    def _build_sidebar(self):
        frame = QFrame()
        frame.setObjectName("Sidebar")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(9, 10, 9, 10)
        lay.addWidget(QLabel("话术分组"))
        self.lst_cat = QListWidget()
        self.lst_cat.currentItemChanged.connect(self._on_cat_changed)
        self.lst_cat.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lst_cat.customContextMenuRequested.connect(self._cat_menu)
        lay.addWidget(self.lst_cat, 1)
        row = QHBoxLayout()
        b1 = QPushButton("＋分组")
        b1.clicked.connect(self.add_category)
        b2 = QPushButton("重命名")
        b2.clicked.connect(self.rename_category)
        b3 = QPushButton("删除")
        b3.setObjectName("Danger")
        b3.clicked.connect(self.delete_category)
        for b in (b1, b2, b3):
            row.addWidget(b)
        lay.addLayout(row)
        return frame

    def _build_center(self):
        frame = QFrame()
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(9, 10, 9, 10)
        head = QHBoxLayout()
        self.lbl_count = QLabel("0 条")
        self.lbl_count.setObjectName("Hint")
        head.addWidget(self.lbl_count)
        head.addStretch(1)
        b_edit = QPushButton("编辑选中")
        b_edit.clicked.connect(self.edit_phrase)
        b_del = QPushButton("删除选中")
        b_del.setObjectName("Danger")
        b_del.clicked.connect(self.delete_phrase)
        head.addWidget(b_edit)
        head.addWidget(b_del)
        lay.addLayout(head)
        self.lst_phrase = QListWidget()
        self.delegate = HighlightDelegate(self.lst_phrase)
        self.lst_phrase.setItemDelegate(self.delegate)
        self.lst_phrase.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lst_phrase.customContextMenuRequested.connect(self._phrase_menu)
        self.lst_phrase.itemDoubleClicked.connect(lambda it: self.send_item(it))
        self.lst_phrase.currentItemChanged.connect(self._on_phrase_selected)
        lay.addWidget(self.lst_phrase, 1)
        hint = QLabel("双击 = 发送 · 右键 = 更多 · 搜索框回车 = 发第一条")
        hint.setObjectName("Hint")
        lay.addWidget(hint)
        return frame

    def _build_tabs(self):
        tabs = QTabWidget()
        # 详情
        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(10, 10, 10, 10)
        self.lbl_detail_title = QLabel("选中一条话术查看详情")
        self.lbl_detail_title.setObjectName("Title")
        self.lbl_detail_title.setWordWrap(True)
        dl.addWidget(self.lbl_detail_title)
        self.txt_detail = QPlainTextEdit()
        self.txt_detail.setReadOnly(True)
        dl.addWidget(self.txt_detail, 1)
        drow = QHBoxLayout()
        self.btn_send = QPushButton("一键发送")
        self.btn_send.setObjectName("Primary")
        self.btn_send.clicked.connect(lambda: self.send_item(self.lst_phrase.currentItem()))
        b_copy = QPushButton("复制内容")
        b_copy.clicked.connect(self.copy_current)
        b_edit = QPushButton("编辑")
        b_edit.clicked.connect(self.edit_phrase)
        for b in (self.btn_send, b_copy, b_edit):
            drow.addWidget(b)
        dl.addLayout(drow)
        tabs.addTab(detail, "详情")

        # 工具箱（插件面板）
        self.toolbox = QTabWidget()
        tabs.addTab(self.toolbox, "工具箱")
        self._tab_toolbox = 1

        self.settings_page = SettingsPage(self.ctx)
        tabs.addTab(self.settings_page, "设置")
        self._tab_settings = 2

        self.plugin_page = PluginPage(self.ctx)
        tabs.addTab(self.plugin_page, "插件")

        self.tbl_stats = QTableWidget(0, 2)
        self.tbl_stats.setHorizontalHeaderLabels(["话术", "使用次数"])
        self.tbl_stats.horizontalHeader().setStretchLastSection(True)
        tabs.addTab(self.tbl_stats, "统计")

        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(500)
        tabs.addTab(self.txt_log, "日志")
        return tabs

    def _build_tray(self):
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("微信辅助器 · 本地话术助手")
        self.tray.activated.connect(self._tray_activated)
        self.tray_menu = QMenu()
        a_show = QAction("显示 / 隐藏", self)
        a_show.triggered.connect(self.toggle_visible)
        self.tray_menu.addAction(a_show)
        self.menu_plugins = QMenu("插件动作", self.tray_menu)
        self.tray_menu.addMenu(self.menu_plugins)
        a_reload = QAction("重新加载插件", self)
        a_reload.triggered.connect(self.ctx.reload_plugins)
        self.tray_menu.addAction(a_reload)
        self.tray_menu.addSeparator()
        a_quit = QAction("退出", self)
        a_quit.triggered.connect(self.quit_app)
        self.tray_menu.addAction(a_quit)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.show()

    # ------------------------------------------------------------ 刷新
    def refresh_all(self):
        self.refresh_categories()
        self.refresh_phrases()
        self.refresh_stats()
        self.refresh_toolbox()
        self.refresh_tray_actions()
        if hasattr(self, "plugin_page"):
            self.plugin_page.reload()

    def refresh_categories(self):
        self.lst_cat.blockSignals(True)
        self.lst_cat.clear()
        for label, key in (("★ 收藏", "__bookmark"), ("全部话术", "__all"),
                           ("最近使用", "__recent")):
            it = QListWidgetItem(label)
            it.setData(DATA_ROLE, key)
            self.lst_cat.addItem(it)
        for c in self.library.categories():
            it = QListWidgetItem("● " + c["name"])
            it.setData(DATA_ROLE, c["id"])
            it.setForeground(QColor(c.get("color", "#e5484d")))
            if c.get("bookmark"):
                it.setText("★ " + c["name"])
            self.lst_cat.addItem(it)
        target = 0
        for i in range(self.lst_cat.count()):
            if self.lst_cat.item(i).data(DATA_ROLE) == self._current_cat:
                target = i
                break
        self.lst_cat.setCurrentRow(target)
        self.lst_cat.blockSignals(False)

    def refresh_phrases(self):
        query = self.ed_search.text().strip()
        words = [x for x in query.split() if x]
        self.delegate.set_words(words)
        cat = self._current_cat
        if cat == "__bookmark":
            items = self.library.phrases(query=query, bookmark_only=True)
        elif cat == "__recent":
            items = sorted(self.library.phrases(query=query),
                           key=lambda p: p.get("last_used", ""), reverse=True)[:30]
        elif cat == "__all":
            items = self.library.phrases(query=query)
        else:
            items = self.library.phrases(category_id=cat, query=query)
        self.lst_phrase.clear()
        for p in items:
            it = QListWidgetItem(p.get("title", ""))
            it.setData(HTML_ROLE, phrase_html(p, words))
            it.setData(DATA_ROLE, p)
            self.lst_phrase.addItem(it)
        self.lbl_count.setText(f"{len(items)} 条"
                               + (f" · 搜索「{query}」" if query else ""))

    def refresh_stats(self):
        s = self.library.stats()
        self.tbl_stats.setRowCount(len(s["top"]))
        for i, row in enumerate(s["top"]):
            self.tbl_stats.setItem(i, 0, QTableWidgetItem(row["title"]))
            self.tbl_stats.setItem(i, 1, QTableWidgetItem(str(row["usage"])))
        self.setToolTip(f"共 {s['total']} 条话术 / {s['sends']} 次发送")

    def refresh_toolbox(self):
        self.toolbox.clear()
        self._toolbox_index.clear()
        for p in self.ctx.plugins.enabled_plugins():
            if not p.instance.has_panel:
                continue
            panel = self.ctx.plugins.panel(p.id)
            if panel is None:
                continue
            self._toolbox_index[p.id] = self.toolbox.addTab(panel, p.name)
        if self.toolbox.count() == 0:
            placeholder = QLabel("还没有带面板的插件。\n\n到「插件」页看看，或按 PLUGIN_GUIDE.md 写一个。")
            placeholder.setAlignment(Qt.AlignCenter)
            placeholder.setObjectName("Hint")
            self.toolbox.addTab(placeholder, "空")

    def refresh_tray_actions(self):
        self.menu_plugins.clear()
        actions = self.ctx.plugins.actions()
        if not actions:
            self.menu_plugins.addAction(QAction("（暂无插件动作）", self))
            return
        for a in actions:
            act = QAction(f"{a['plugin_name']} · {a.get('label', a.get('id'))}", self)
            act.setToolTip(a.get("tip", ""))
            cb = a.get("callback")
            if callable(cb):
                act.triggered.connect(lambda _, f=cb: self._safe_call(f))
            self.menu_plugins.addAction(act)

    # ------------------------------------------------------------ 分类动作
    def _on_cat_changed(self, cur, _prev):
        if cur is None:
            return
        self._current_cat = cur.data(DATA_ROLE) or "__all"
        self.refresh_phrases()

    def _cat_menu(self, pos: QPoint):
        it = self.lst_cat.itemAt(pos)
        if not it or str(it.data(DATA_ROLE)).startswith("__"):
            return
        m = QMenu(self)
        a1 = m.addAction("重命名")
        a2 = m.addAction("设为书签分组")
        a3 = m.addAction("删除分组")
        act = m.exec(self.lst_cat.mapToGlobal(pos))
        cid = it.data(DATA_ROLE)
        if act == a1:
            self.rename_category()
        elif act == a2:
            c = next((c for c in self.library.categories() if c["id"] == cid), None)
            if c:
                self.library.update_category(cid, bookmark=not c.get("bookmark"))
                self.refresh_categories()
        elif act == a3:
            self.delete_category()

    def add_category(self):
        name, ok = QInputDialog.getText(self, "新建分组", "分组名称：")
        if ok and name.strip():
            self.library.add_category(name.strip())
            self._current_cat = next(
                c["id"] for c in self.library.categories() if c["name"] == name.strip())
            self.refresh_categories()
            self.refresh_phrases()

    def rename_category(self):
        it = self.lst_cat.currentItem()
        if not it or str(it.data(DATA_ROLE)).startswith("__"):
            return
        cid = it.data(DATA_ROLE)
        cur = next((c["name"] for c in self.library.categories() if c["id"] == cid), "")
        name, ok = QInputDialog.getText(self, "重命名分组", "新名称：", text=cur)
        if ok and name.strip():
            self.library.update_category(cid, name=name.strip())
            self.refresh_categories()

    def delete_category(self):
        it = self.lst_cat.currentItem()
        if not it or str(it.data(DATA_ROLE)).startswith("__"):
            return
        cid = it.data(DATA_ROLE)
        if QMessageBox.question(self, "确认", "删除分组？组内话术会变成「未分类」，不会丢失。") \
                == QMessageBox.Yes:
            self.library.delete_category(cid)
            self._current_cat = "__all"
            self.refresh_categories()
            self.refresh_phrases()

    # ------------------------------------------------------------ 话术动作
    def _current_phrase(self):
        it = self.lst_phrase.currentItem()
        return it.data(DATA_ROLE) if it else None

    def _on_phrase_selected(self, cur, _prev):
        p = cur.data(DATA_ROLE) if cur else None
        if not p:
            return
        self.lbl_detail_title.setText(p.get("title", ""))
        self.txt_detail.setPlainText(p.get("content", ""))

    def _phrase_menu(self, pos: QPoint):
        it = self.lst_phrase.itemAt(pos)
        if not it:
            return
        p = it.data(DATA_ROLE)
        m = QMenu(self)
        a_send = m.addAction("一键发送")
        a_copy = m.addAction("复制内容")
        a_edit = m.addAction("编辑…")
        m.addSeparator()
        a_star = m.addAction("取消收藏" if p.get("bookmark") else "设为收藏")
        a_up = m.addAction("上移")
        a_down = m.addAction("下移")
        a_dup = m.addAction("复制一份")
        m.addSeparator()
        a_del = m.addAction("删除")
        act = m.exec(self.lst_phrase.mapToGlobal(pos))
        if act == a_send:
            self.send_item(it)
        elif act == a_copy:
            self.sender.copy_text(p.get("content", ""))
        elif act == a_edit:
            self.edit_phrase()
        elif act == a_star:
            self.library.update_phrase(p["id"], bookmark=not p.get("bookmark"))
            self.refresh_phrases()
        elif act in (a_up, a_down):
            self.library.update_phrase(p["id"],
                                       order=int(p.get("order", 0)) + (-1 if act == a_up else 1))
            self.refresh_phrases()
        elif act == a_dup:
            fields = dict(p)
            fields.pop("id", None)
            fields["title"] = p.get("title", "") + "（副本）"
            self.library.add_phrase(fields)
            self.refresh_phrases()
        elif act == a_del:
            self.delete_phrase()

    def new_phrase(self):
        dlg = PhraseEditDialog(self.library, None, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self.library.add_phrase(dlg.values())
            self.ctx.apply_hotkeys()
            self.refresh_all()

    def edit_phrase(self):
        p = self._current_phrase()
        if not p:
            QMessageBox.information(self, "提示", "先选中一条话术")
            return
        dlg = PhraseEditDialog(self.library, p, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self.library.update_phrase(p["id"], **dlg.values())
            self.ctx.apply_hotkeys()
            self.refresh_all()

    def delete_phrase(self):
        p = self._current_phrase()
        if not p:
            return
        if QMessageBox.question(self, "确认", f"删除「{p.get('title','')}」？") == QMessageBox.Yes:
            self.library.delete_phrase(p["id"])
            self.ctx.apply_hotkeys()
            self.refresh_all()

    def copy_current(self):
        p = self._current_phrase()
        if p:
            self.sender.copy_text(p.get("content", ""))

    # ------------------------------------------------------------ 发送
    def send_item(self, item):
        if not item:
            return
        self.send_phrase(item.data(DATA_ROLE))

    def send_phrase(self, p: dict):
        if not p:
            return
        values = {}
        names = Library.variables(p.get("content", ""))
        names = [n for n in names if n not in Library.BUILTIN_VARS]
        if names and self.cfg.get("general.confirm_variables", True):
            dlg = VariableDialog(names, self.cfg, self)
            if dlg.exec() != QDialog.Accepted:
                return
            values = dlg.values()
        self.sender.send_phrase(p, values, mode=self.cfg.get("send.mode", "paste_enter"))

    def send_first(self):
        if self.lst_phrase.count() > 0:
            self.send_item(self.lst_phrase.item(0))

    def _on_sent(self, ok, msg):
        self.log(("✓ " if ok else "✗ ") + msg)
        if ok:
            self.refresh_stats()

    # ------------------------------------------------------------ 目标 / 窗口
    def _on_target_changed(self, info: dict):
        title = info.get("title") or "未知窗口"
        self.lbl_target.setText(f"目标：{title[:38]}  ({info.get('class','')})")

    def _toggle_pin(self, checked):
        if checked:
            hwnd = self.sender.target()
            if not hwnd:
                QMessageBox.information(self, "锁定目标",
                                        "先点一下要发送到的聊天窗口，再点「锁定目标」。")
                self.btn_pin.setChecked(False)
                return
            self.sender.pin_target(hwnd)
            self.btn_pin.setText("已锁定")
            self.log(f"已锁定目标窗口：{w.get_window_title(hwnd)}")
        else:
            self.sender.pin_target(0)
            self.btn_pin.setText("锁定目标")
            self.log("已解锁，恢复自动跟随前台窗口")

    def _apply_window_flags(self):
        flags = self.windowFlags()
        on = bool(self.cfg.get("window.always_on_top", True))
        if on:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint
        if flags != self.windowFlags():
            self.setWindowFlags(flags)
            self.show()
        opacity = int(self.cfg.get("window.opacity", 100)) / 100.0
        self.setWindowOpacity(max(0.4, min(1.0, opacity)))
        w.set_no_activate(int(self.winId()),
                          bool(self.cfg.get("window.no_activate", False)))

    def _restore_geometry(self):
        g = self.cfg.section("window")
        self.setGeometry(int(g.get("x", 120)), int(g.get("y", 120)),
                         int(g.get("w", 900)), int(g.get("h", 620)))
        screen = QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            if not area.contains(self.frameGeometry()):
                self.move(max(area.left(), min(self.x(), area.right() - self.width())),
                          max(area.top(), min(self.y(), area.bottom() - self.height())))

    def _save_geometry(self):
        g = self.geometry()
        for k, v in (("window.x", g.x()), ("window.y", g.y()),
                     ("window.w", g.width()), ("window.h", g.height())):
            self.cfg.set(k, v, save=False)
        self.cfg.save()

    def mousePressEvent(self, e):
        self._user_moving = True
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._user_moving = False
        super().mouseReleaseEvent(e)

    # ------------------------------------------------------------ 托盘 / 生命周期
    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.toggle_visible()

    def toggle_visible(self):
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def quick_search(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.ed_search.setFocus()
        self.ed_search.selectAll()

    def open_toolbox(self, plugin_id=None):
        self.showNormal()
        self.raise_()
        self.tabs.setCurrentIndex(self._tab_toolbox)
        if plugin_id and plugin_id in self._toolbox_index:
            self.toolbox.setCurrentIndex(self._toolbox_index[plugin_id])

    def _append_log(self, text: str):
        self.txt_log.appendPlainText(text)

    def log(self, text: str):
        self.ctx.log(str(text))

    def closeEvent(self, e):
        if self.cfg.get("general.minimize_to_tray", True) and self.tray.isVisible():
            self._save_geometry()
            self.hide()
            e.ignore()
            self.tray.showMessage("微信辅助器", "已收进托盘，双击图标或按热键呼出",
                                  QSystemTrayIcon.MessageIcon.Information, 2500)
        else:
            self.quit_app()

    def quit_app(self):
        self._save_geometry()
        self.ctx.shutdown()
        QApplication.quit()

    @staticmethod
    def _safe_call(fn):
        try:
            fn()
        except Exception as exc:
            QMessageBox.warning(None, "插件动作出错", str(exc))
