# -*- coding: utf-8 -*-
"""setup_dialog.py —— 首次接入的一次性授权 + 全自动进度。

微信 4.x 的数据库密钥只在登录瞬间产生，运行中的进程内存里不留明文，
所以「装好就完全静默解密」在技术上做不到。这里把唯一一次人工动作
压缩成一个「同意并接入」按钮：之后程序自动关闭微信、挂载监听、拉起微信，
使用者只需在手机上点一下微信自己的「确认登录」。
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QVBoxLayout,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "输出")


class ConsentDialog(QDialog):
    """首次接入：把要做什么、边界是什么，一次讲清楚。"""

    def __init__(self, reason: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("首次接入本机微信")
        self.setMinimumWidth(430)
        self.setModal(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        title = QLabel("一键接入 · 只需这一次")
        title.setStyleSheet("font-size:17px;font-weight:700;color:#d63b45;")
        lay.addWidget(title)

        info = QLabel(
            "接下来程序会自动完成：定位微信 → 取本机数据库密钥 → 解密聊天记录 →\n"
            "出「今日跟进 / 潜在意向」名单 → 做成交话术归因 → 生成话术库。\n\n"
            "微信要求：需要<b>重启一次微信</b>，并在手机上点微信自己的「确认登录」。\n"
            "这是微信的安全机制（密钥只在登录瞬间产生），任何工具都无法绕过。\n"
            "拿到后密钥会<b>缓存在本机</b>，以后点「一键更新」全程自动，不用再操作。")
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        info.setStyleSheet("color:#444;line-height:1.65;font-size:12px;")
        lay.addWidget(info)

        boundary = QLabel(
            "边界：① 只读取<b>本机当前登录</b>的微信账号数据，不碰他人账号；\n"
            "② 密钥、聊天记录、报告全部只留在本机，<b>不联网、不上传</b>；\n"
            "③ 不改动微信任何文件，不后台驻留；\n"
            "④ 导出内容含客户隐私，请自行妥善保管。")
        boundary.setWordWrap(True)
        boundary.setTextFormat(Qt.RichText)
        boundary.setStyleSheet(
            "background:#fff5f5;border:1px solid #f5cfd2;border-radius:6px;"
            "padding:9px 10px;color:#7a3b3e;font-size:11px;line-height:1.6;")
        lay.addWidget(boundary)

        if reason:
            r = QLabel(reason)
            r.setWordWrap(True)
            r.setStyleSheet("color:#888;font-size:11px;")
            lay.addWidget(r)

        self.chk = QCheckBox("我已阅读并同意以上说明（仅限本人账号、数据不出本机）")
        self.chk.setStyleSheet("font-size:12px;color:#333;")
        lay.addWidget(self.chk)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_later = QPushButton("暂不接入")
        self.btn_ok = QPushButton("同意并自动接入")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setMinimumWidth(150)
        self.btn_ok.setEnabled(False)
        row.addWidget(self.btn_later)
        row.addWidget(self.btn_ok)
        lay.addLayout(row)

        self.chk.toggled.connect(self.btn_ok.setEnabled)
        self.btn_later.clicked.connect(self.reject)
        self.btn_ok.clicked.connect(self.accept)


class ProgressDialog(QDialog):
    """全自动流水线的进度窗，可收起但不打断流程。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("正在自动处理")
        self.setMinimumWidth(410)
        self.setModal(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        self.lbl = QLabel("正在检查环境…")
        self.lbl.setWordWrap(True)
        self.lbl.setStyleSheet("font-size:13px;color:#333;")
        lay.addWidget(self.lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 7)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        lay.addWidget(self.bar)

        self.hint = QLabel("首次接入时请在手机上点「确认登录」，其余步骤自动完成。")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#999;font-size:11px;")
        lay.addWidget(self.hint)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_hide = QPushButton("后台运行")
        self.btn_close = QPushButton("完成")
        self.btn_close.setVisible(False)
        row.addWidget(self.btn_hide)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self.btn_hide.clicked.connect(self.hide)
        self.btn_close.clicked.connect(self.accept)

    def step(self, text: str, done: int = 0, total: int = 7):
        self.lbl.setText(text)
        self.bar.setMaximum(max(1, total))
        self.bar.setValue(max(0, done))

    def finish(self, ok: bool, msg: str):
        self.lbl.setText(("✓ " if ok else "✗ ") + msg)
        self.lbl.setStyleSheet("font-size:13px;color:%s;font-weight:600;" %
                               ("#2e7d4f" if ok else "#c0392b"))
        self.bar.setValue(self.bar.maximum())
        self.btn_hide.setVisible(False)
        self.btn_close.setVisible(True)
