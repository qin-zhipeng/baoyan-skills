# -*- coding: utf-8 -*-
"""simple_window.py —— 微信辅助器 · 极简主界面。

设计原则：一屏三个页签，每个页签只做一件事，点一下就出结果。
  常用语   → 点卡片，文字进微信输入框，你点【发送】
  常用文件 → 点卡片，文件进微信输入框，你点【发送】
  打标签   → 选名单 → 开始，程序只勾选，最后【完成】你自己点
右上角「吸附」把窗口贴到微信聊天界面右侧并跟随移动。
"""
from __future__ import annotations

import datetime
import os

from PySide6.QtCore import QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QScrollArea,
    QSizePolicy, QSpinBox, QStackedWidget, QSystemTrayIcon,
    QToolButton, QVBoxLayout, QWidget,
)

import json

from core import win32util as w
from core import auto_pilot, wx_bridge
from core.chat_radar import (
    RadarJob, discover_sources, export_csv, export_names,
    refresh_cmd, source_summary,
)
from core.config import DATA_DIR
from core.quick_sender import QuickSender
from core.quickstore import QuickStore
from core.tag_job import TagJob
from core.wechat_follow import DockToWeChat, WeChatTracker
from ui.setup_dialog import ConsentDialog, ProgressDialog

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QSS_PATH = os.path.join(ROOT, "ui", "simple.qss")


def make_icon() -> QIcon:
    ico = os.path.join(ROOT, "assets", "app.ico")
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
    p.setFont(QFont("Microsoft YaHei UI", 26, QFont.Bold))
    p.drawText(pm.rect(), Qt.AlignCenter, "微")
    p.end()
    return QIcon(pm)


def _human_size(path: str) -> str:
    try:
        n = os.path.getsize(path)
    except OSError:
        return "文件已移动"
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def _one_line(text: str, limit: int = 34) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def clean_spin(box: QSpinBox) -> QSpinBox:
    """数字框统一处理：数值居中，可直接输入，选中按 ↑ ↓ 微调。

    注意：黑边（Windows 原生上/下箭头的深色描边）由 simple.qss 用
    「透明无边框的按钮占位」解决。这里**不能**用 setButtonSymbols(NoButtons)，
    那会与本项目的 QSS 冲突、把内部 lineEdit 压成 1px，数字直接不显示。
    """
    box.setAlignment(Qt.AlignCenter)
    return box


# ------------------------------------------------------------------ 卡片
class Card(QFrame):
    """整块可点击；右侧两个小按钮只在 hover 时显眼，不会误触发发送。"""

    clicked = Signal(str)
    edited = Signal(str)
    deleted = Signal(str)

    def __init__(self, kind: str, item: dict, preview: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.kind = kind
        self.item = item
        self._title = item.get("title") or item.get("name") or "未命名"
        self._preview = preview
        self.setFixedHeight(64)
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(13, 7, 8, 7)
        lay.setSpacing(6)

        left = QVBoxLayout()
        left.setSpacing(3)
        # Ignored：不让长文字的 sizeHint 把右侧按钮挤出卡片，超长部分按宽度省略
        self.title_lbl = QLabel(self._title)
        self.title_lbl.setObjectName("cardTitle")
        self.title_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.title_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.sub_lbl = QLabel(preview)
        self.sub_lbl.setObjectName("cardPreview")
        self.sub_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.sub_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        left.addWidget(self.title_lbl)
        left.addWidget(self.sub_lbl)
        left.addStretch(1)
        lay.addLayout(left, 1)

        for text, tip, handler in (
            ("改", "修改", lambda: self.edited.emit(self.item["id"])),
            ("删", "删除", lambda: self.deleted.emit(self.item["id"])),
        ):
            btn = QToolButton()
            btn.setObjectName("tiny")
            btn.setText(text)
            btn.setToolTip(tip)
            btn.setFixedWidth(26)
            btn.clicked.connect(handler)
            lay.addWidget(btn)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for lbl, text in ((getattr(self, "title_lbl", None), self._title),
                          (getattr(self, "sub_lbl", None), self._preview)):
            if lbl is None:
                continue
            fm = QFontMetrics(lbl.font())
            lbl.setText(fm.elidedText(text, Qt.ElideRight, max(12, lbl.width())))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.item["id"])
        super().mousePressEvent(event)


# ------------------------------------------------------------------ 名单结果行
class RadarRow(QFrame):
    """客户名单里的一行：勾选框 + 姓名 + 一句话理由，鼠标悬停看完整依据。"""

    def __init__(self, row: dict, mode: str = "follow", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.row = row
        self._title = f"{row.get('rank', 0)}.  {row.get('name', '')}"
        tag = "⚠已退费" if row.get("refunded") else ("老客户" if row.get("is_deal") else "")
        why = (row.get("follow_tag") or "") if mode == "follow" else ""
        self._sub = ((why + "  |  ") if why else "") \
            + (f"{row.get('stage', '')} · 静默 {row.get('silence', 0)} 天"
               f" · 意向 {row.get('signal_score', 0)} 分"
               + (f" · {tag}" if tag else "")
               + f" · {'、'.join(row.get('signals', [])[:2]) or '无强信号'}")
        self.setFixedHeight(58)
        self.setToolTip(
            "命中：" + ("、".join(row.get("signals", [])) or "无") + "\n"
            + "风险：" + ("、".join(row.get("risks", [])) or "无") + "\n"
            + "微信标签：" + ("、".join(row.get("labels", [])) or "无") + "\n"
            + "建议：" + str(row.get("reason", "")))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(7)
        self.cb = QCheckBox()
        self.cb.setChecked(True)
        self.cb.setFixedWidth(20)
        lay.addWidget(self.cb)

        col = QVBoxLayout()
        col.setSpacing(3)
        self.title_lbl = QLabel(self._title)
        self.title_lbl.setObjectName("cardTitle")
        self.title_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.sub_lbl = QLabel(self._sub)
        self.sub_lbl.setObjectName("cardPreview")
        self.sub_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        col.addWidget(self.title_lbl)
        col.addWidget(self.sub_lbl)
        col.addStretch(1)
        lay.addLayout(col, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for lbl, text in ((getattr(self, "title_lbl", None), self._title),
                          (getattr(self, "sub_lbl", None), self._sub)):
            if lbl is None:
                continue
            fm = QFontMetrics(lbl.font())
            lbl.setText(fm.elidedText(text, Qt.ElideRight, max(12, lbl.width())))

    def is_checked(self):
        return self.cb.isChecked()

    def set_checked(self, on: bool):
        self.cb.setChecked(bool(on))


# ------------------------------------------------------------------ 添加 / 编辑常用语
class TextDialog(QDialog):
    def __init__(self, title="", content="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("常用语")
        self.setWindowIcon(make_icon())
        self.setMinimumWidth(430)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(9)
        lay.addWidget(QLabel("标题（自己看得懂就行）"))
        self.title = QLineEdit(title)
        self.title.setPlaceholderText("例如：报价说明")
        lay.addWidget(self.title)
        lay.addWidget(QLabel("内容（点一下就会整段进微信输入框）"))
        self.content = QPlainTextEdit(content)
        self.content.setMinimumHeight(150)
        lay.addWidget(self.content)
        hint = QLabel("小技巧：写 {日期} / {时间} 会自动换成当天日期时间")
        hint.setObjectName("hint")
        lay.addWidget(hint)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("soft")
        save = QPushButton("保存")
        save.setObjectName("primary")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(save)
        lay.addLayout(row)

    def values(self):
        return self.title.text().strip(), self.content.toPlainText()


# ------------------------------------------------------------------ 设置
class SettingsDialog(QDialog):
    def __init__(self, store: QuickStore, dock: DockToWeChat, tracker, window, parent=None):
        super().__init__(parent)
        self.store, self.dock, self.tracker = store, dock, tracker
        self.win = window
        self.setWindowTitle("设置")
        self.setWindowIcon(make_icon())
        self.setMinimumWidth(400)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(11)

        self.top = QCheckBox("窗口始终置顶")
        self.top.setChecked(bool(store.setting("always_on_top", True)))
        lay.addWidget(self.top)

        row = QHBoxLayout()
        self.dock_on = QCheckBox("吸附在微信聊天界面右侧")
        self.dock_on.setChecked(dock.enabled)
        row.addWidget(self.dock_on)
        self.mode = QComboBox()
        self.mode.addItem("自动（放得下贴外侧）", "auto")
        self.mode.addItem("始终贴微信外侧", "outside")
        self.mode.addItem("浮在聊天区右侧", "inside")
        idx = max(0, [self.mode.itemData(i) for i in range(self.mode.count())].index(dock.mode))
        self.mode.setCurrentIndex(idx)
        row.addWidget(self.mode)
        lay.addLayout(row)

        lay.addWidget(QLabel("点卡片之后："))
        self.put = QRadioButton("只放进输入框，我自己点【发送】（推荐）")
        self.direct = QRadioButton("直接帮我发出去（自动回车）")
        self.put.setChecked(not bool(store.setting("direct_send", False)))
        self.direct.setChecked(bool(store.setting("direct_send", False)))
        lay.addWidget(self.put)
        lay.addWidget(self.direct)

        self.click_input = QCheckBox("粘贴前先点一下微信输入框（内容跑偏时勾上）")
        self.click_input.setChecked(bool(store.setting("click_input", False)))
        lay.addWidget(self.click_input)
        cal = QPushButton("校准输入框位置")
        cal.setObjectName("soft")
        cal.clicked.connect(self.calibrate)
        lay.addWidget(cal)
        tip = QLabel("校准：点一下 → 3 秒内把鼠标停在微信输入框中间别动")
        tip.setObjectName("hint")
        lay.addWidget(tip)

        ds = QPushButton("成交客户识别设置…（改标识词 / 看核对清单）")
        ds.setObjectName("soft")
        ds.setToolTip("按昵称、备注名、标签识别已成交客户，可自定义别人微信里的标识写法")
        ds.clicked.connect(self._open_deal_signals)
        lay.addWidget(ds)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("soft")
        save = QPushButton("保存")
        save.setObjectName("primary")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self._save)
        btns.addWidget(cancel)
        btns.addWidget(save)
        lay.addLayout(btns)

    def _open_deal_signals(self):
        from ui.deal_signals_dialog import DealSignalsDialog
        DealSignalsDialog(self.win, self).exec()

    def calibrate(self):
        QMessageBox.information(
            self, "校准输入框",
            "点【确定】后，3 秒内把鼠标移到微信聊天输入框的中间，停住别动。\n"
            "（不用点，鼠标放那儿就行）")
        QTimer.singleShot(3000, self._capture)

    def _capture(self):
        try:
            p = w.get_cursor_pos()
            rect = self.tracker.rect()
            if not rect:
                QMessageBox.warning(self, "校准失败", "没找到微信窗口，先打开微信再试")
                return
            l, t, r, b = rect
            rx = round((p[0] - l) / max(1, r - l), 4)
            ry = round((p[1] - t) / max(1, b - t), 4)
            if not (0.05 < rx < 0.98 and 0.05 < ry < 0.98):
                QMessageBox.warning(self, "校准失败", "鼠标好像不在微信窗口里，再试一次")
                return
            self.store.set_setting("input_pos", {"rx": rx, "ry": ry})
            self.store.set_setting("click_input", True)
            self.click_input.setChecked(True)
            QMessageBox.information(self, "校准完成", "已记住输入框位置，以后会自动点一下再粘贴")
        except Exception as exc:
            QMessageBox.warning(self, "校准失败", str(exc))

    def _save(self):
        self.store.set_setting("always_on_top", self.top.isChecked())
        self.store.set_setting("direct_send", self.direct.isChecked())
        self.store.set_setting("click_input", self.click_input.isChecked())
        # 吸附状态下必须置顶，否则会被微信窗口盖住
        self.win._set_always_on_top(self.top.isChecked() or self.dock_on.isChecked())
        self.dock.set_mode(self.mode.currentData())
        if self.dock.enabled != self.dock_on.isChecked():
            self.dock.set_enabled(self.dock_on.isChecked())
            self.win.sync_dock_button()
        self.accept()


# ------------------------------------------------------------------ 主窗口
class SimpleWindow(QWidget):
    def __init__(self, ctx=None):
        super().__init__()
        self.ctx = ctx
        self.setObjectName("root")
        self.setWindowTitle("微信辅助器")
        self.setWindowIcon(make_icon())
        self.resize(390, 650)
        self.setMinimumSize(340, 470)
        self._force_quit = False
        self._tray_tipped = False
        self.tag_path = ""
        self.radar_paths = []
        self.radar_rows = []
        self.radar_widgets = []
        self.radar_mode = "follow"
        self.radar_proc = None
        self.pilot = auto_pilot.AutoPilot(self)
        self.pilot_progress = None
        self._pilot_consented = False
        self._first_run_done = False

        self.store = QuickStore()
        self.tracker = WeChatTracker()
        self.qs = QuickSender(self.tracker, self.store)
        self.dock = DockToWeChat(self, self.tracker, self.store)
        self.tagjob = TagJob(os.path.join(DATA_DIR, "打标签输出"))
        self.radar = RadarJob(self)

        try:
            with open(QSS_PATH, "r", encoding="utf-8") as f:
                self.setStyleSheet(f.read())
        except OSError:
            pass

        self._build_ui()
        self._wire()
        self._build_tray()
        self.refresh_all()
        self._radar_init_sources()
        self._set_always_on_top(bool(self.store.setting("always_on_top", True)))
        self.sync_dock_button()
        self.tracker.start()
        if self.dock.enabled:
            self.dock.set_enabled(True)

        # 开机即自动跑一遍全流程；没有密钥时才会弹一次授权
        self.pilot.progress.connect(self._pilot_step)
        self.pilot.need_consent.connect(self._pilot_need_consent)
        self.pilot.done.connect(self._pilot_done)
        QTimer.singleShot(700, self._auto_start)

    # ------------------------------------------------------------ 界面骨架
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部：一行只放「品牌 + 三个短按钮」。
        # 以前这里还塞了副标题，340px 吸附宽度下五个元素互相挤，文字看着乱，
        # 现在说明统一交给状态条和底栏，且所有长文字都按宽度省略。
        header = QWidget()
        header.setObjectName("header")
        header.setFixedHeight(44)
        h = QHBoxLayout(header)
        h.setContentsMargins(12, 0, 10, 0)
        h.setSpacing(6)
        dot = QLabel("●")
        dot.setFixedWidth(11)
        dot.setStyleSheet("color:#e5484d;font-size:11px;")
        brand = QLabel("微信辅助器")
        brand.setObjectName("brand")
        brand.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        h.addWidget(dot)
        h.addWidget(brand, 1)
        self.btn_auto = QPushButton("一键更新")
        self.btn_auto.setObjectName("primary")
        self.btn_auto.setFixedHeight(28)
        self.btn_auto.setToolTip(
            "自动完成：读本机微信 → 解密聊天记录 → 出今日跟进/意向名单 → 成交话术归因")
        h.addWidget(self.btn_auto)
        self.btn_dock = QPushButton("吸附")
        self.btn_dock.setObjectName("mini")
        self.btn_dock.setFixedWidth(54)
        self.btn_dock.setFixedHeight(28)
        self.btn_dock.setCheckable(True)
        self.btn_dock.setToolTip("贴到微信聊天界面右侧，跟着微信移动；切到别的应用会自动收起")
        self.btn_setting = QPushButton("设置")
        self.btn_setting.setObjectName("mini")
        self.btn_setting.setFixedWidth(48)
        self.btn_setting.setFixedHeight(28)
        h.addWidget(self.btn_dock)
        h.addWidget(self.btn_setting)
        root.addWidget(header)

        # 状态条：只报状态，操作提示放底栏，两处不再重复长句
        status_bar = QWidget()
        status_bar.setObjectName("statusBar")
        status_bar.setFixedHeight(28)
        s = QHBoxLayout(status_bar)
        s.setContentsMargins(12, 0, 10, 0)
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("status")
        self.lbl_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        s.addWidget(self.lbl_status, 1)
        root.addWidget(status_bar)

        # 页签
        tabbar = QWidget()
        tabbar.setObjectName("tabBar")
        tl = QHBoxLayout(tabbar)
        tl.setContentsMargins(8, 0, 8, 0)
        tl.setSpacing(0)
        self.tabs = []
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        for i, name in enumerate(("常用语", "常用文件", "客户名单", "打标签", "话术库")):
            b = QPushButton(name)
            b.setObjectName("tab")
            b.setCheckable(True)
            b.setChecked(i == 0)
            self.tab_group.addButton(b, i)
            tl.addWidget(b, 1)
            self.tabs.append(b)
        root.addWidget(tabbar)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_items("text"))
        self.stack.addWidget(self._page_items("file"))
        self.stack.addWidget(self._page_radar())
        self.stack.addWidget(self._page_tag())
        self.stack.addWidget(self._page_scripts())
        root.addWidget(self.stack, 1)

        footer = QWidget()
        footer.setObjectName("footer")
        footer.setFixedHeight(28)
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(12, 0, 10, 0)
        self.lbl_tip = QLabel("")
        self.lbl_tip.setObjectName("footerTip")
        self.lbl_tip.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        fl.addWidget(self.lbl_tip, 1)
        root.addWidget(footer)

        self._status_full = ""
        self._tip_full = ""
        self._set_status("正在找微信窗口…", "#2f7d43")
        self._set_tip("点一下 → 内容进微信输入框，你点发送")

    def _page_items(self, kind: str) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 11, 12, 10)
        lay.setSpacing(9)

        search = QLineEdit()
        search.setObjectName("search")
        search.setPlaceholderText("搜索" if kind == "text" else "搜文件名")
        search.setClearButtonEnabled(True)
        lay.addWidget(search)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("scrollContent")
        box = QVBoxLayout(content)
        box.setContentsMargins(1, 1, 5, 1)
        box.setSpacing(7)
        box.addStretch(1)
        area.setWidget(content)
        lay.addWidget(area, 1)

        add = QPushButton("＋ 添加常用语" if kind == "text" else "＋ 添加常发文件")
        add.setObjectName("outline")
        lay.addWidget(add)

        if kind == "text":
            self.text_search, self.text_box, self.text_add = search, box, add
        else:
            self.file_search, self.file_box, self.file_add = search, box, add
        return page

    def _page_radar(self) -> QWidget:
        """客户名单：读本机聊天记录 → 出两类名单 → 一键送进批量打标签。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        # ① 数据源
        src = QHBoxLayout()
        src.setSpacing(5)
        self.lbl_source = QLabel("正在找本机聊天记录…")
        self.lbl_source.setObjectName("cardPreview")
        self.lbl_source.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        src.addWidget(self.lbl_source, 1)
        self.btn_radar_pick = QPushButton("选文件")
        self.btn_radar_load = QPushButton("读取")
        self.btn_radar_update = QPushButton("更新")
        for b in (self.btn_radar_pick, self.btn_radar_load, self.btn_radar_update):
            b.setObjectName("srcBtn")
            b.setFixedWidth(52)
            src.addWidget(b)
        self.btn_radar_pick.setToolTip("换一个聊天记录文件（jsonl / csv）")
        self.btn_radar_load.setToolTip("重新读取并分析聊天记录")
        self.btn_radar_update.setToolTip("从本机微信重新解密导出聊天记录，并自动重出名单与归因报告")
        lay.addLayout(src)

        # ② 两类名单
        seg = QHBoxLayout()
        seg.setSpacing(6)
        self.btn_mode_follow = QPushButton("今日跟进 TOP")
        self.btn_mode_follow.setToolTip(
            "只列未成交、今天该跟进的人：客户留言待回复 / 到点该主动触达 / 高意向临门一脚。\n"
            "已成交、已退费、明确放弃、沉睡太久的都不进这个名单")
        self.btn_mode_intent = QPushButton("潜在意向客户")
        self.btn_mode_intent.setToolTip("命中成交信号、未成交、未放弃的潜在客户，可调「意向分≥」")
        self.seg_group = QButtonGroup(self)
        for i, b in enumerate((self.btn_mode_follow, self.btn_mode_intent)):
            b.setObjectName("seg")
            b.setCheckable(True)
            self.seg_group.addButton(b, i)
            seg.addWidget(b, 1)
        self.btn_mode_follow.setChecked(True)
        lay.addLayout(seg)

        # ③ 条件
        opts = QHBoxLayout()
        opts.setSpacing(5)
        opts.addWidget(QLabel("显示前"))
        self.spin_radar_top = clean_spin(QSpinBox())
        self.spin_radar_top.setRange(5, 300)
        self.spin_radar_top.setValue(20)
        self.spin_radar_top.setFixedWidth(60)
        self.spin_radar_top.setToolTip("直接输入数字，或选中后按 ↑ ↓ 微调")
        opts.addWidget(self.spin_radar_top)
        opts.addWidget(QLabel("人"))
        self.lbl_min_score = QLabel("意向分≥")
        self.spin_radar_score = clean_spin(QSpinBox())
        self.spin_radar_score.setRange(0, 60)
        self.spin_radar_score.setValue(8)
        self.spin_radar_score.setFixedWidth(52)
        self.spin_radar_score.setToolTip("直接输入数字，或选中后按 ↑ ↓ 微调")
        self.lbl_min_score.setEnabled(False)
        self.spin_radar_score.setEnabled(False)
        opts.addWidget(self.lbl_min_score)
        opts.addWidget(self.spin_radar_score)
        opts.addStretch(1)
        self.chk_radar_all = QCheckBox("全选")
        self.chk_radar_all.setChecked(True)
        opts.addWidget(self.chk_radar_all)
        lay.addLayout(opts)

        # ④ 结果
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("scrollContent")
        self.radar_box = QVBoxLayout(content)
        self.radar_box.setContentsMargins(1, 1, 5, 1)
        self.radar_box.setSpacing(6)
        self.radar_box.addStretch(1)
        area.setWidget(content)
        area.setMinimumHeight(120)
        lay.addWidget(area, 1)

        # ⑤ 导出
        tail = QHBoxLayout()
        tail.setSpacing(7)
        self.btn_radar_csv = QPushButton("导出 CSV")
        self.btn_radar_csv.setObjectName("soft")
        self.btn_radar_tag = QPushButton("→ 送去打标签")
        self.btn_radar_tag.setObjectName("primary")
        tail.addWidget(self.btn_radar_csv)
        tail.addWidget(self.btn_radar_tag, 1)
        lay.addLayout(tail)
        return page

    def _page_tag(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 11, 12, 10)
        lay.setSpacing(9)

        step = QLabel("① 微信 → 通讯录管理 → 右键标签 → 添加成员，弹窗别关\n"
                      "② 选名单 → 开始；只自动勾选，最后【完成】你自己点")
        step.setObjectName("step")
        step.setWordWrap(True)
        lay.addWidget(step)

        panel = QFrame()
        panel.setObjectName("panel")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(12, 10, 12, 10)
        pl.setSpacing(9)

        row = QHBoxLayout()
        self.btn_pick = QPushButton("选择名单…")
        self.btn_pick.setObjectName("soft")
        self.lbl_file = QLabel("还没选名单")
        self.lbl_file.setObjectName("cardPreview")
        row.addWidget(self.btn_pick)
        row.addWidget(self.lbl_file, 1)
        pl.addLayout(row)

        opts = QHBoxLayout()
        opts.addWidget(QLabel("本次只跑前"))
        self.spin_limit = clean_spin(QSpinBox())
        self.spin_limit.setRange(0, 9999)
        self.spin_limit.setValue(0)
        self.spin_limit.setToolTip("0 = 全部；首次建议先填 10 试跑；可直接输入数字或按 ↑ ↓ 微调")
        self.spin_limit.setFixedWidth(72)
        opts.addWidget(self.spin_limit)
        opts.addWidget(QLabel("人（0=全部）"))
        self.chk_fresh = QCheckBox("清空进度重跑")
        opts.addWidget(self.chk_fresh)
        opts.addStretch(1)
        pl.addLayout(opts)

        btns = QHBoxLayout()
        self.btn_check = QPushButton("检测弹窗")
        self.btn_check.setObjectName("soft")
        self.btn_run = QPushButton("开始批量勾选")
        self.btn_run.setObjectName("primary")
        btns.addWidget(self.btn_check)
        btns.addWidget(self.btn_run, 1)
        pl.addLayout(btns)

        self.bar = QProgressBar()
        self.bar.setValue(0)
        self.bar.setFormat("%v / %m")
        pl.addWidget(self.bar)
        lay.addWidget(panel)

        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        self.log_box.setPlaceholderText("运行记录会出现在这里")
        self.log_box.setMinimumHeight(84)
        lay.addWidget(self.log_box, 1)

        tail = QHBoxLayout()
        self.btn_review = QPushButton("查看复核页")
        self.btn_review.setObjectName("soft")
        self.btn_outdir = QPushButton("输出目录")
        self.btn_outdir.setObjectName("soft")
        self.btn_clear = QPushButton("清空进度")
        self.btn_clear.setObjectName("soft")
        tail.addWidget(self.btn_review)
        tail.addWidget(self.btn_outdir)
        tail.addStretch(1)
        tail.addWidget(self.btn_clear)
        lay.addLayout(tail)
        return page

    def _page_scripts(self) -> QWidget:
        """成交话术归因结果：一个摘要 + 三个按钮，保持极简。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 11, 12, 10)
        lay.setSpacing(9)

        tip = QLabel("自动从成交客户的真实聊天里，挖出打赢过的话术、异议应答和成交链关键动作。")
        tip.setObjectName("step")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.lbl_pilot = QLabel("点右上角「一键更新」开始，或等开机自动完成。")
        self.lbl_pilot.setObjectName("cardPreview")
        self.lbl_pilot.setWordWrap(True)
        lay.addWidget(self.lbl_pilot)

        self.txt_summary = QPlainTextEdit()
        self.txt_summary.setObjectName("logBox")
        self.txt_summary.setReadOnly(True)
        self.txt_summary.setPlaceholderText("归因结果会显示在这里")
        lay.addWidget(self.txt_summary, 1)

        tail = QHBoxLayout()
        tail.setSpacing(6)
        self.btn_review = QPushButton("核对")
        self.btn_review.setObjectName("soft")
        self.btn_review.setFixedWidth(50)
        self.btn_review.setToolTip("打开「成交客户核对清单.csv」，逐人查看判定依据")
        self.btn_report = QPushButton("报告")
        self.btn_report.setObjectName("soft")
        self.btn_import_scripts = QPushButton("→ 存入常用语")
        self.btn_import_scripts.setObjectName("primary")
        self.btn_outdir2 = QPushButton("目录")
        self.btn_outdir2.setObjectName("soft")
        self.btn_outdir2.setFixedWidth(50)
        tail.addWidget(self.btn_review)
        tail.addWidget(self.btn_report)
        tail.addWidget(self.btn_import_scripts, 1)
        tail.addWidget(self.btn_outdir2)
        lay.addLayout(tail)
        return page

    # ------------------------------------------------------------ 信号
    def _wire(self):
        self.tab_group.idClicked.connect(self.stack.setCurrentIndex)
        self.tab_group.idClicked.connect(self._on_tab_changed)
        self.text_search.textChanged.connect(lambda: self._reload("text"))
        self.file_search.textChanged.connect(lambda: self._reload("file"))
        self.text_add.clicked.connect(lambda: self._add_text())
        self.file_add.clicked.connect(self._add_files)
        self.btn_setting.clicked.connect(self._open_settings)
        self.btn_dock.clicked.connect(self._toggle_dock)
        self.btn_auto.clicked.connect(self._manual_update)
        self.btn_report.clicked.connect(self._open_report)
        self.btn_review.clicked.connect(self._open_review)
        self.btn_import_scripts.clicked.connect(self._import_scripts)
        self.btn_outdir2.clicked.connect(lambda: self._open_path(auto_pilot.OUT_DIR))
        self.tracker.state_changed.connect(self._on_wx_state)
        self.qs.delivered.connect(self._on_delivered)
        self.radar.done.connect(self._radar_done)

        # 客户名单
        self._radar_debounce = QTimer(self)
        self._radar_debounce.setSingleShot(True)
        self._radar_debounce.setInterval(350)
        self._radar_debounce.timeout.connect(self._radar_load)
        self.btn_radar_pick.clicked.connect(self._radar_pick)
        self.btn_radar_load.clicked.connect(self._radar_load)
        self.btn_radar_update.clicked.connect(self._radar_update)
        self.btn_radar_csv.clicked.connect(self._radar_csv)
        self.btn_radar_tag.clicked.connect(self._radar_to_tag)
        self.chk_radar_all.toggled.connect(self._radar_all)
        self.seg_group.idClicked.connect(self._radar_mode)
        self.spin_radar_top.valueChanged.connect(lambda _: self._radar_debounce.start())
        self.spin_radar_score.valueChanged.connect(lambda _: self._radar_debounce.start())

        self.btn_pick.clicked.connect(self._pick_list)
        self.btn_check.clicked.connect(self._check_tag_window)
        self.btn_run.clicked.connect(self._run_tag)
        self.btn_review.clicked.connect(lambda: self._open_path(self.tagjob.review))
        self.btn_outdir.clicked.connect(lambda: self._open_path(self.tagjob.out_dir))
        self.btn_clear.clicked.connect(self._clear_progress)
        self.tagjob.log.connect(self._append_log)
        self.tagjob.progress.connect(self._on_progress)
        self.tagjob.running_changed.connect(self._on_running)
        self.tagjob.finished.connect(self._on_tag_finished)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("微信辅助器")
        menu = QMenu()
        menu.addAction("显示 / 隐藏", self.toggle_visible)
        menu.addSeparator()
        menu.addAction("退出", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.toggle_visible()
            if reason == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    # ------------------------------------------------------------ 列表
    def refresh_all(self):
        self._reload("text")
        self._reload("file")

    def _clear_box(self, box: QVBoxLayout):
        while box.count():
            item = box.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()

    def _reload(self, kind: str):
        box = self.text_box if kind == "text" else self.file_box
        query = (self.text_search if kind == "text" else self.file_search).text()
        self._clear_box(box)
        items = self.store.texts(query) if kind == "text" else self.store.files(query)
        if not items:
            empty = QLabel("这里还是空的，点下面的「＋ 添加」建一条吧")
            empty.setObjectName("emptyHint")
            empty.setAlignment(Qt.AlignCenter)
            empty.setMinimumHeight(70)
            box.addWidget(empty)
        for it in items:
            if kind == "text":
                preview = _one_line(it["content"], 40) or "（空内容）"
            else:
                preview = f"{_human_size(it['path'])}  ·  {_one_line(it['path'], 30)}"
            card = Card(kind, it, preview)
            card.clicked.connect(lambda i=it["id"], k=kind: self._use(k, i))
            card.edited.connect(lambda i=it["id"], k=kind: self._edit(k, i))
            card.deleted.connect(lambda i=it["id"], k=kind: self._delete(k, i))
            box.addWidget(card)
        box.addStretch(1)

    # ------------------------------------------------------------ 常用语 / 文件操作
    def _use(self, kind: str, item_id: str):
        item = self.store.get(kind, item_id)
        if not item:
            return
        if kind == "text":
            self.qs.deliver_text(item.get("content", ""), item_id)
        else:
            self.qs.deliver_files([item.get("path", "")], item_id)

    def _add_text(self):
        dlg = TextDialog(parent=self)
        if dlg.exec() == QDialog.Accepted:
            title, content = dlg.values()
            if not content.strip():
                QMessageBox.warning(self, "没保存", "内容不能是空的")
                return
            self.store.add_text(title or content.strip()[:12], content)
            self._reload("text")

    def _edit(self, kind: str, item_id: str):
        item = self.store.get(kind, item_id)
        if not item:
            return
        if kind == "file":
            name, ok = QInputDialog.getText(self, "改显示名", "显示名", text=item["name"])
            if ok and name.strip():
                self.store.rename_file(item_id, name)
                self._reload("file")
            return
        dlg = TextDialog(item["title"], item["content"], self)
        if dlg.exec() == QDialog.Accepted:
            title, content = dlg.values()
            self.store.update_text(item_id, title or "未命名", content)
            self._reload("text")

    def _delete(self, kind: str, item_id: str):
        item = self.store.get(kind, item_id)
        if not item:
            return
        name = item.get("title") or item.get("name")
        if QMessageBox.question(self, "删除", f"确定删掉「{name}」？",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.store.delete(kind, item_id)
        self._reload(kind)

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选常发的文件（可多选）", "", "所有文件 (*.*)")
        if not paths:
            return
        n = self.store.add_files(paths)
        self._reload("file")
        if n < len(paths):
            self._flash(f"添加了 {n} 个文件（重复的跳过）")

    # ------------------------------------------------------------ 客户名单
    def _on_tab_changed(self, idx: int):
        self._set_tip((
            "点一下 → 内容进微信输入框，你点发送",
            "点一下 → 文件进微信输入框，你点发送",
            "勾好名单 → 点【送去打标签】，自动填名单",
            "程序只负责勾选，最后【完成】你自己点",
            "成交话术已归因 → 点【存入常用语】直接用",
        )[min(idx, 4)])

    def _radar_init_sources(self):
        self.radar_paths = discover_sources()
        self._radar_show_source()
        if self.radar_paths:
            self._radar_load()
        else:
            self._radar_empty("没找到聊天记录，点「选文件」挑一个 jsonl / csv")

    def _radar_show_source(self):
        self.lbl_source.setText(source_summary(self.radar_paths))
        self.lbl_source.setToolTip("\n".join(self.radar_paths) or "点「选文件」挑聊天记录文件")

    def _radar_load(self):
        if not self.radar_paths:
            self._radar_empty("还没选聊天记录，点「选文件」")
            return
        if self.radar.is_running():
            return
        self.lbl_source.setText("正在读聊天记录…")
        self.btn_radar_load.setEnabled(False)
        self.radar.start(self.radar_paths, self.radar_mode,
                         self.spin_radar_top.value(), self.spin_radar_score.value())

    def _radar_done(self, ok: bool, msg: str, rows: list):
        self.btn_radar_load.setEnabled(True)
        self._radar_show_source()
        if not ok:
            self._radar_empty(msg)
            self._flash(msg, False)
            return
        self.radar_rows = rows
        self._radar_render()
        self._flash(msg, True)

    def _radar_render(self):
        self._clear_box(self.radar_box)
        self.radar_widgets = []
        if not self.radar_rows:
            self._radar_empty("这个条件下没筛到人：把「意向分≥」调低，或换一类名单")
            return
        for r in self.radar_rows:
            row = RadarRow(r, self.radar_mode)
            row.set_checked(self.chk_radar_all.isChecked())
            self.radar_box.addWidget(row)
            self.radar_widgets.append(row)
        self.radar_box.addStretch(1)

    def _radar_empty(self, msg: str):
        self._clear_box(self.radar_box)
        self.radar_widgets = []
        empty = QLabel(msg)
        empty.setObjectName("emptyHint")
        empty.setAlignment(Qt.AlignCenter)
        empty.setWordWrap(True)
        empty.setMinimumHeight(72)
        self.radar_box.addWidget(empty)
        self.radar_box.addStretch(1)

    def _radar_all(self, on: bool):
        for row in self.radar_widgets:
            row.set_checked(on)

    def _radar_mode(self, idx: int):
        self.radar_mode = "follow" if idx == 0 else "intent"
        intent = idx == 1
        self.lbl_min_score.setEnabled(intent)
        self.spin_radar_score.setEnabled(intent)
        self._radar_load()

    def _radar_pick(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选聊天记录文件（可多选）", "",
            "聊天记录 (*.jsonl *.ndjson *.json *.csv *.tsv);;所有文件 (*.*)")
        if not paths:
            return
        self.radar_paths = list(paths)
        self._radar_show_source()
        self._radar_load()

    def _radar_checked(self):
        return [w.row for w in self.radar_widgets if w.is_checked()]

    def _radar_to_tag(self):
        picked = self._radar_checked()
        if not picked:
            QMessageBox.information(self, "先勾人", "在名单里勾上要打标签的人，再点这里")
            return
        prefix = "今日跟进" if self.radar_mode == "follow" else "潜在意向"
        path = export_names(picked, os.path.join(DATA_DIR, "打标签名单"), prefix)
        self.tag_path = path
        self.lbl_file.setText(os.path.basename(path))
        self.lbl_file.setToolTip(path)
        self._append_log(f"客户名单已生成 {len(picked)} 人：{os.path.basename(path)}")
        self.tabs[3].setChecked(True)
        self.stack.setCurrentIndex(3)
        self._on_tab_changed(3)
        self._flash(f"名单 {len(picked)} 人已填好：先「检测弹窗」，再「开始批量勾选」")

    def _radar_csv(self):
        picked = self._radar_checked() or list(self.radar_rows)
        if not picked:
            QMessageBox.information(self, "还没有名单", "先点「读取」生成名单")
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        default = os.path.join(DATA_DIR, "客户名单", f"客户名单_{stamp}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "导出客户名单", default, "CSV (*.csv)")
        if not path:
            return
        export_csv(picked, path)
        self._flash(f"已导出 {len(picked)} 人：{os.path.basename(path)}")
        self._open_path(path)

    def _radar_update(self):
        """「更新」直接走内置全自动流水线，不再依赖任何外部脚本或配置。"""
        self._manual_update()

    def _radar_updated(self, code: int, _status):
        self.btn_radar_update.setEnabled(True)
        if code == 0:
            self.radar_paths = discover_sources()
            self._radar_show_source()
            self._radar_load()
            self._flash("聊天记录已更新，正在重新出名单")
        else:
            self._flash("导出脚本没跑成功，现有记录仍可正常使用", False)

    # ------------------------------------------------------------ 状态与设置
    def _on_wx_state(self, found: bool, title: str):
        self._wx_found = found
        self._restore_status()

    def _restore_status(self):
        if getattr(self, "_wx_found", False):
            self._set_status("● 微信已就绪 · 点一下就送进输入框", "#2f7d43")
        else:
            self._set_status("○ 还没看到微信窗口，先打开聊天", "#a06616")

    def _flash(self, msg: str, ok: bool = True):
        self._set_status(("✓ " if ok else "! ") + msg,
                         "#2f7d43" if ok else "#c0392b", bold=True)
        QTimer.singleShot(3200, self._restore_status)

    # ---- 长文字统一按当前宽度省略，绝不把布局撑乱 ----
    def _set_status(self, text: str, color: str, bold: bool = False):
        self._status_full = text
        self.lbl_status.setStyleSheet(
            f"color:{color};font-size:12px;" + ("font-weight:600;" if bold else ""))
        self._elide_labels()

    def _set_tip(self, text: str):
        self._tip_full = text
        self._elide_labels()

    def _elide_labels(self):
        for lbl, full in (
            (getattr(self, "lbl_status", None), getattr(self, "_status_full", "")),
            (getattr(self, "lbl_tip", None), getattr(self, "_tip_full", "")),
        ):
            if lbl is None or not full:
                continue
            fm = QFontMetrics(lbl.font())
            lbl.setText(fm.elidedText(full, Qt.ElideRight, max(20, lbl.width() - 4)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide_labels()

    def _on_delivered(self, ok: bool, msg: str):
        self._flash(msg, ok)

    def _open_settings(self):
        SettingsDialog(self.store, self.dock, self.tracker, self, self).exec()

    def sync_dock_button(self):
        self.btn_dock.setChecked(self.dock.enabled)
        self.btn_dock.setText("已吸附" if self.dock.enabled else "吸附")

    def _toggle_dock(self, on: bool = None):
        target = (not self.dock.enabled) if on is None else bool(on)
        self.dock.set_enabled(target)
        self.sync_dock_button()
        self._flash("已贴到微信右侧 · 切走自动收起" if target else "已取消吸附", True)

    def _set_always_on_top(self, on: bool):
        flag = bool(self.windowFlags() & Qt.WindowStaysOnTopHint)
        if flag == bool(on):
            return
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        if self.isVisible():
            self.show()

    # ------------------------------------------------------------ 打标签
    def _pick_list(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选名单文件", "", "名单文件 (*.csv *.tsv *.txt *.md *.xlsx *.xlsm);;所有文件 (*.*)")
        if not path:
            return
        self.tag_path = path
        self.lbl_file.setText(os.path.basename(path))
        self.lbl_file.setToolTip(path)
        self._append_log(f"已选名单：{os.path.basename(path)}")

    def _check_tag_window(self):
        r = self.tagjob.check()
        self._append_log(r["msg"])
        self._flash(r["msg"], r["ok"])

    def _run_tag(self):
        if self.tagjob.is_running():
            self.tagjob.stop()
            return
        if not self.tag_path:
            QMessageBox.information(self, "先选名单", "点「选择名单…」挑一个含微信备注的文件")
            return
        r = self.tagjob.check()
        if not r["ok"]:
            QMessageBox.information(self, "弹窗没找到", r["msg"])
            return
        self._append_log(r["msg"])
        self.log_box.clear()
        self._append_log("开始批量勾选，按 F12 可随时停")
        self.tagjob.start({
            "path": self.tag_path,
            "limit": self.spin_limit.value(),
            "fresh": self.chk_fresh.isChecked(),
            "speed": "normal",
        })

    def _append_log(self, text: str):
        self.log_box.appendPlainText(str(text))
        if self.log_box.document().blockCount() > 300:
            self.log_box.clear()
            self.log_box.appendPlainText("（日志过长已清空，详细结果见复核页）")

    def _on_progress(self, done: int, total: int, stats: dict):
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)
        brief = "  ".join(f"{k}{v}" for k, v in stats.items())
        if brief:
            self.bar.setFormat(f"%v / %m   {brief}")

    def _on_running(self, running: bool):
        self.btn_run.setText("停止" if running else "开始批量勾选")
        self.btn_pick.setEnabled(not running)
        self.btn_check.setEnabled(not running)

    def _on_tag_finished(self, ok: bool, msg: str):
        self._flash(msg, ok)
        if ok:
            QMessageBox.information(
                self, "勾选完成",
                "已把名单里的人逐个勾进弹窗。\n\n"
                "请在微信弹窗右侧核对一遍，确认无误后自己点【完成】。\n"
                "需要看每个人当时搜出来的是谁，点「查看复核页」。")

    def _clear_progress(self):
        if QMessageBox.question(self, "清空进度",
                                "清空后下次会从名单第一个重新开始，确定？",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.tagjob.clear_progress()
        self.bar.setValue(0)

    # ------------------------------------------------------------ 全自动流水线
    def _auto_start(self):
        """开机自动跑：有本机密钥就完全无感，没有才弹一次授权。"""
        self._start_pilot(first_run=True)

    def _manual_update(self):
        if self.pilot.is_running():
            self._flash("正在处理，稍等一下", False)
            return
        self._start_pilot(first_run=False)

    def _start_pilot(self, first_run: bool):
        self.btn_auto.setEnabled(False)
        self.lbl_pilot.setText("正在自动处理本机微信数据…")
        if not self.pilot_progress:
            self.pilot_progress = ProgressDialog(self)
        self.pilot_progress.step("正在检查环境…", 0, 7)
        self.pilot_progress.show()
        self.pilot_progress.raise_()
        self.pilot.start(allow_capture=True, consented=self._pilot_consented)

    def _pilot_step(self, text: str, done: int, total: int):
        if self.pilot_progress and self.pilot_progress.isVisible():
            self.pilot_progress.step(text, done, total or 7)
        self.lbl_pilot.setText(text)

    def _pilot_need_consent(self, reason: str):
        """唯一一次人工动作：重启微信 + 手机确认登录（微信机制要求）。"""
        if self.pilot_progress:
            self.pilot_progress.hide()
        dlg = ConsentDialog(reason, self)
        if dlg.exec() != QDialog.Accepted:
            self.btn_auto.setEnabled(True)
            self.lbl_pilot.setText(
                "已跳过自动接入。已有聊天记录仍可正常使用；\n"
                "想接入时点右上角「一键更新」。")
            self._flash("已跳过接入，其他功能照常可用", False)
            return
        self._pilot_consented = True
        wx_bridge.save_state({"consented": True})
        if self.pilot_progress:
            self.pilot_progress.show()
            self.pilot_progress.lbl.setText("等待手机上确认微信登录…")
            self.pilot_progress.hint.setText("请在手机上点微信的「确认登录」，其余自动完成。")
        self.pilot.start(allow_capture=True, consented=True)

    def _pilot_done(self, ok: bool, msg: str, result: dict):
        self.btn_auto.setEnabled(True)
        if self.pilot_progress and self.pilot_progress.isVisible():
            self.pilot_progress.finish(ok, msg)
        self.lbl_pilot.setText(msg)
        if not ok:
            self._flash(msg, False)
            return
        self._flash("自动处理完成")
        # 刷新客户名单与常用语
        self.radar_paths = discover_sources()
        self._radar_show_source()
        self._radar_load()
        self._reload("text")

        lines = [
            "本机微信数据：%s" % result.get("account", ""),
            "会话 %d 个 · 消息 %d 条" % (result.get("chats", 0), result.get("messages", 0)),
            "今日跟进 %d 人 · 潜在意向 %d 人" % (result.get("follow", 0),
                                          result.get("intent", 0)),
            "识别成交客户 %d 单 · 未成交 %d 个" % (result.get("deals", 0),
                                          result.get("others", 0)),
        ]
        if result.get("median_cycle") is not None:
            lines.append("成交周期中位数 %d 天" % result["median_cycle"])
        lines += [
            "提炼可复用成交话术 %d 条" % result.get("top_scripts", 0),
            "可一键入库话术 %d 条" % result.get("scripts", 0),
            "",
            "成交识别：本机微信标签 %d 个 · 疑似待核对 %d 人 · 标识冲突 %d 人" % (
                result.get("labels_all", 0), result.get("deal_suspect", 0),
                result.get("deal_conflict", 0)),
            "系统推荐了 %d 个可能的新标识词（设置 → 成交客户识别）" %
            result.get("suggestions", 0),
            "点【核对】可看每个客户为什么被判成交；改法在设置里。",
            "",
            "名单已自动放进「打标签名单」目录，可直接送去打标签。",
            "报告位置：输出/成交话术归因报告_*.md",
        ]
        self.txt_summary.setPlainText("\n".join(lines))
        self._last_report = result.get("report", "")

    def _open_review(self):
        p = os.path.join(auto_pilot.OUT_DIR, "成交客户核对清单.csv")
        if os.path.isfile(p):
            self._open_path(p)
        else:
            QMessageBox.information(
                self, "还没有核对清单",
                "点右上角「一键更新」跑一次分析，就会生成\n"
                "输出/成交客户核对清单.csv（逐人列出成交判定依据）。")

    def _open_report(self):
        p = getattr(self, "_last_report", "") or ""
        if not p or not os.path.isfile(p):
            import glob
            found = sorted(glob.glob(os.path.join(auto_pilot.OUT_DIR,
                                                  "成交话术归因报告_*.md")))
            p = found[-1] if found else ""
        if p:
            self._open_path(p)
        else:
            QMessageBox.information(self, "还没有报告",
                                    "点右上角「一键更新」跑一次就有了。")

    def _import_scripts(self):
        """把归因出来的高转化话术写进常用语库（去重）。"""
        p = os.path.join(ROOT, "data", "归因话术.json")
        if not os.path.isfile(p):
            QMessageBox.information(self, "还没有话术",
                                    "点右上角「一键更新」跑一次归因，话术会自动生成。")
            return
        try:
            items = json.load(open(p, encoding="utf-8"))
        except Exception as exc:
            QMessageBox.warning(self, "读取失败", str(exc))
            return
        exist = {str(x.get("content") or "").strip() for x in self.store.texts()}
        n = 0
        for it in items or []:
            content = str(it.get("content") or "").strip()
            if not content or content in exist:
                continue
            try:
                self.store.add_text(str(it.get("title") or "成交复用话术"), content)
                exist.add(content)
                n += 1
            except Exception:
                continue
        self._reload("text")
        QMessageBox.information(self, "已存入常用语",
                                "新增 %d 条成交复用话术，切到「常用语」页即可点用。\n"
                                "重复执行不会重复添加。" % n)

    @staticmethod
    def _open_path(path):
        try:
            if os.path.exists(str(path)):
                os.startfile(str(path))
            else:
                QMessageBox.information(None, "还没有", "跑一次之后才有内容可以看")
        except Exception as exc:
            QMessageBox.warning(None, "打不开", str(exc))

    # ------------------------------------------------------------ 兼容旧入口 / 托盘
    def _apply_window_flags(self):
        self._set_always_on_top(bool(self.store.setting("always_on_top", True)))

    def toggle_visible(self):
        if self.isVisible():
            self.dock.note_manual_hide()   # 用户自己收的，别自动弹回来
            self.hide()
        else:
            self.dock.note_manual_show()   # 手动呼出给宽限，不立刻被自动隐藏
            self.show()
            self.raise_()
            self.activateWindow()

    def quick_search(self):
        self.dock.note_manual_show()
        self.show()
        self.raise_()
        self.stack.setCurrentIndex(0)
        self.tabs[0].setChecked(True)
        self._on_tab_changed(0)
        self.text_search.setFocus()
        self.text_search.selectAll()

    def send_first(self):
        items = self.store.texts()
        if items:
            self.qs.deliver_text(items[0]["content"], items[0]["id"])

    def send_phrase(self, phrase: dict):
        """兼容旧版热键：旧话术也能直接送进输入框。"""
        self.qs.deliver_text(str(phrase.get("content", "")))

    def quit_app(self):
        self._force_quit = True
        self.tracker.stop()
        if self.ctx:
            try:
                self.ctx.shutdown()
            except Exception:
                pass
        self.tray.hide()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    def closeEvent(self, event):
        if self._force_quit:
            event.accept()
            return
        event.ignore()
        self.dock.note_manual_hide()
        self.hide()
        if not self._tray_tipped:
            self._tray_tipped = True
            self.tray.showMessage("微信辅助器", "我缩到右下角托盘了，双击图标再叫我出来",
                                  QSystemTrayIcon.MessageIcon.Information, 3000)
