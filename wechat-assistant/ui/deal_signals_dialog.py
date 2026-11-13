# -*- coding: utf-8 -*-
"""deal_signals_dialog.py —— 成交客户识别设置（一个弹窗，勾选即用）。

给拿到安装包的人用，不需要懂配置、不需要改代码：
  * 自动列出「本机微信里的标签名」和「从备注名里学到的疑似标识」，
    勾一下就加进识别词典；
  * 也可以直接手写自己习惯的写法（成交标识 / 排除词，一行一个）；
  * 每次自动分析都会生成「成交客户核对清单.csv」，逐人给出判定依据。
"""
from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout,
)

from core import deal_signals as DS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "输出")
STATE_FILE = os.path.join(ROOT, "data", "deal_signals_state.json")
REVIEW_FILE = os.path.join(OUT_DIR, "成交客户核对清单.csv")
SUGGEST_FILE = os.path.join(OUT_DIR, "成交标识建议.csv")


def _lines(box: QPlainTextEdit) -> list:
    out = []
    for line in box.toPlainText().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


class DealSignalsDialog(QDialog):
    def __init__(self, win=None, parent=None):
        super().__init__(parent)
        self.win = win
        self.cfg = DS.load_config()
        self.setWindowTitle("成交客户识别")
        self.setModal(True)
        self.setMinimumSize(470, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(9)

        title = QLabel("谁算「已成交」，你说了算")
        title.setStyleSheet("font-size:16px;font-weight:700;color:#d63b45;")
        lay.addWidget(title)
        info = QLabel(
            "程序会自动看微信备注名、昵称、微信号和标签里的成交标识"
            "（已成交 / 付 / 全款 / 签单…），也认「未成交 / 待付款 / 退费」这类否定词。"
            "每个人的写法不同，下面可以勾、可以自己加。")
        info.setWordWrap(True)
        info.setStyleSheet("color:#555;font-size:12px;line-height:1.6;")
        lay.addWidget(info)

        # ---- 统计 ----
        self.lbl_stat = QLabel("")
        self.lbl_stat.setWordWrap(True)
        self.lbl_stat.setStyleSheet(
            "background:#f3f6fb;border:1px solid #e2e8f2;border-radius:7px;"
            "padding:8px 10px;color:#3a4a63;font-size:12px;")
        lay.addWidget(self.lbl_stat)

        # ---- 自动推荐 ----
        lay.addWidget(QLabel("本机发现的标识词（勾上就生效，不用手打）"))
        self.lst = QListWidget()
        self.lst.setMinimumHeight(150)
        self.lst.setAlternatingRowColors(True)
        lay.addWidget(self.lst, 1)

        # ---- 自定义 ----
        row = QHBoxLayout()
        col1, col2 = QVBoxLayout(), QVBoxLayout()
        col1.addWidget(QLabel("我自己的成交标识（一行一个）"))
        self.txt_include = QPlainTextEdit()
        self.txt_include.setPlaceholderText("例如：\n成交\n付\n已签\nA单")
        self.txt_include.setMaximumHeight(86)
        col1.addWidget(self.txt_include)
        col2.addWidget(QLabel("看到就不算成交（一行一个）"))
        self.txt_exclude = QPlainTextEdit()
        self.txt_exclude.setPlaceholderText("例如：\n待付\n未成\n不考虑\n同行")
        self.txt_exclude.setMaximumHeight(86)
        col2.addWidget(self.txt_exclude)
        row.addLayout(col1)
        row.addLayout(col2)
        lay.addLayout(row)

        self.chk_suspect = QCheckBox("把「疑似成交」也算进成交客户（默认不勾，更稳妥）")
        self.chk_suspect.setChecked(bool(self.cfg.get("accept_suspect")))
        lay.addWidget(self.chk_suspect)

        tip = QLabel(
            "提示：微信本地只保存标签名、不保存「谁挂了哪个标签」，"
            "所以标签识别走两条：把标签名当标识词 + 可选的标签文件"
            "（data/radar_config.json 的 contacts_file）。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8a9099;font-size:11px;")
        lay.addWidget(tip)

        btns = QHBoxLayout()
        b_review = QPushButton("核对清单")
        b_review.setObjectName("soft")
        b_sugg = QPushButton("标识建议表")
        b_sugg.setObjectName("soft")
        btns.addWidget(b_review)
        btns.addWidget(b_sugg)
        btns.addStretch(1)
        b_cancel = QPushButton("取消")
        b_cancel.setObjectName("soft")
        b_save = QPushButton("保存")
        b_save.setObjectName("primary")
        b_rerun = QPushButton("保存并重新分析")
        b_rerun.setObjectName("primary")
        btns.addWidget(b_cancel)
        btns.addWidget(b_save)
        btns.addWidget(b_rerun)
        lay.addLayout(btns)

        b_review.clicked.connect(lambda: self._open(REVIEW_FILE))
        b_sugg.clicked.connect(lambda: self._open(SUGGEST_FILE))
        b_cancel.clicked.connect(self.reject)
        b_save.clicked.connect(lambda: self._save(False))
        b_rerun.clicked.connect(lambda: self._save(True))

        self._load()

    # ------------------------------------------------------------ 数据
    def _load(self):
        state = {}
        try:
            with open(STATE_FILE, "r", encoding="utf-8-sig") as f:
                state = json.load(f)
        except Exception:
            pass
        scan = state.get("scan") or {}
        suggestions = state.get("suggestions") or []
        if not suggestions:
            try:
                suggestions = DS.recommend(top=30)
            except Exception:
                suggestions = []
        labels = DS.contact_book().get("labels_all") or []
        self.lbl_stat.setText(
            "本机微信标签 %d 个 · 上次识别成交 %d 人 · 疑似待核对 %d 人 · 标识冲突 %d 人\n"
            "推荐词 %d 个（来自本机备注名与标签，勾选后写入本机配置）"
            % (len(labels), scan.get("deals", 0), scan.get("suspect", 0),
               scan.get("conflict", 0), len(suggestions)))

        if not suggestions:
            empty = QListWidgetItem(
                "（还没跑过分析：点右上角「一键更新」，这里会自动列出你微信里的标签和标识）")
            empty.setFlags(Qt.NoItemFlags)
            self.lst.addItem(empty)
        for r in suggestions:
            text = "%s    ·    %s%s" % (
                r["word"], r["source"],
                ("    ·    %d 人" % r["count"]) if r.get("count") else "")
            it = QListWidgetItem(text)
            it.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            it.setCheckState(Qt.Unchecked)
            it.setData(Qt.UserRole, r)
            it.setToolTip(r.get("why", ""))
            self.lst.addItem(it)

        self.txt_include.setPlainText("\n".join(
            dict.fromkeys(self.cfg.get("name_include", [])
                          + self.cfg.get("label_include", []))))
        self.txt_exclude.setPlainText("\n".join(
            self.cfg.get("negative_include", [])))

    def _save(self, rerun: bool):
        names = _lines(self.txt_include)
        negs = _lines(self.txt_exclude)
        for i in range(self.lst.count()):
            it = self.lst.item(i)
            if it.checkState() == Qt.Checked:
                r = it.data(Qt.UserRole) or {}
                w = str(r.get("word") or "").strip()
                if w and w not in names:
                    names.append(w)
        self.cfg["name_include"] = list(dict.fromkeys(names))
        self.cfg["label_include"] = list(dict.fromkeys(names))
        self.cfg["negative_include"] = list(dict.fromkeys(negs))
        self.cfg["accept_suspect"] = self.chk_suspect.isChecked()
        try:
            DS.save_config(self.cfg)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self.accept()
        if rerun and self.win is not None:
            try:
                self.win._manual_update()
            except Exception:
                pass
        else:
            QMessageBox.information(
                self, "已保存", "识别词典已更新，下次点「一键更新」或重启程序后生效。")

    @staticmethod
    def _open(path: str):
        if not os.path.isfile(path):
            QMessageBox.information(
                None, "还没有这个文件",
                "点一次右上角「一键更新」，跑完分析后就会生成：\n%s"
                % os.path.basename(path))
            return
        try:
            os.startfile(path)  # noqa: S606 - Windows 本机打开
        except Exception:
            QMessageBox.information(None, "文件位置", path)
