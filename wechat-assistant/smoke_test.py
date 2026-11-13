# -*- coding: utf-8 -*-
"""smoke_test.py —— 无头冒烟：构造极简界面五个页签，验证不崩、不进事件循环。

用法（在本包目录下）：
    runtime\\python.exe smoke_test.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

# 无头环境下屏蔽模态框，避免脚本卡住
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

from ui.simple_window import SettingsDialog, SimpleWindow  # noqa: E402
from ui.deal_signals_dialog import DealSignalsDialog  # noqa: E402
from core import deal_signals as DS  # noqa: E402


def main() -> int:
    app = QApplication.instance() or QApplication([])
    w = SimpleWindow()
    names = [b.text() for b in w.tabs]
    assert names == ["常用语", "常用文件", "客户名单", "打标签", "话术库"], names
    assert w.stack.count() == 5, w.stack.count()
    for i in range(5):
        w.stack.setCurrentIndex(i)
        app.processEvents()
    assert w.btn_auto.text() == "一键更新"
    assert w.txt_summary is not None and w.btn_import_scripts is not None
    assert w.btn_review.text() == "核对"

    # 成交识别设置在「全新安装、还没有聊天数据」时也必须能打开
    sd = SettingsDialog(w.store, w.dock, w.tracker, w, w)
    assert sd is not None
    d = DealSignalsDialog(w, w)
    assert d.lst.count() >= 1 and "本机微信标签" in d.lbl_stat.text()

    # 识别器在空数据下可用，且否定优先 / 姓氏防误伤不退化
    cfg = DS.default_config()
    assert DS.judge(wxid="x", name="示例 已成交", cfg=cfg)["deal"]
    assert not DS.judge(wxid="x", name="示例 未成交", cfg=cfg)["deal"]
    assert not DS.judge(wxid="x", name="付小明同学", cfg=cfg)["deal"]
    assert isinstance(DS.contact_book(), dict)
    assert isinstance(DS.recommend(top=5), list)

    w.show()
    app.processEvents()
    print("冒烟通过：5 个页签 + 成交识别设置 + 识别器判定均正常（空数据下也 OK）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
