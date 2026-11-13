# -*- coding: utf-8 -*-
"""quick_sender.py —— 一键把「常用语 / 常用文件」放进微信输入框。

默认行为（重点）：只粘贴，不按回车。用户自己点微信的【发送】。
原理：剪贴板(CF_UNICODETEXT / CF_HDROP) + 激活微信窗口 + Ctrl+V，
纯模拟人工，不注入微信、不碰数据库。发送动作放在后台线程，界面不卡。
"""
from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QObject, Signal

from . import win32util as w
from .store import Library
from .wechat_follow import find_wechat_windows


class QuickSender(QObject):
    delivered = Signal(bool, str)

    def __init__(self, tracker, store, parent=None):
        super().__init__(parent)
        self.tracker = tracker
        self.store = store
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 目标
    def target_hwnd(self) -> int:
        hwnd = self.tracker.best_target()
        if hwnd and w.is_window(hwnd):
            return hwnd
        wins = find_wechat_windows()
        return int(wins[0]["hwnd"]) if wins else 0

    # ------------------------------------------------------------ 对外 API
    def deliver_text(self, text: str, item_id: str = ""):
        text = Library.render(text or "", {})     # 支持 {日期} {时间} 等内置变量
        if not text.strip():
            self.delivered.emit(False, "这条常用语是空的")
            return
        threading.Thread(target=self._work, args=("text", text, item_id),
                         daemon=True).start()

    def deliver_files(self, paths, item_id: str = ""):
        paths = [os.path.abspath(p) for p in paths if p]
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            self.delivered.emit(False, f"文件已不在原位置：{os.path.basename(missing[0])}")
            return
        if not paths:
            self.delivered.emit(False, "没有可发送的文件")
            return
        threading.Thread(target=self._work, args=("files", paths, item_id),
                         daemon=True).start()

    def copy_text(self, text: str) -> bool:
        return w.set_clipboard_text(Library.render(text or "", {}))

    # ------------------------------------------------------------ 执行
    def _click_input_box(self, rect):
        """可选：粘贴前点一下聊天输入框，解决焦点落在会话列表/搜索框的情况。"""
        if not rect:
            return
        pos = self.store.setting("input_pos") or {}
        l, t, r, b = rect
        rx = float(pos.get("rx", 0.62))
        ry = float(pos.get("ry", 0.90))
        x = int(l + (r - l) * rx)
        y = int(t + (b - t) * ry)
        w.click(x, y)
        time.sleep(0.08)

    def _work(self, kind: str, payload, item_id: str):
        if not self._lock.acquire(blocking=False):
            self.delivered.emit(False, "上一条还在送入中，稍等一下")
            return
        snap = None
        try:
            hwnd = self.target_hwnd()
            if not hwnd:
                self.delivered.emit(False, "没找到微信窗口，先打开微信聊天界面")
                return
            problem = w.clipboard_problem()
            if problem:
                self.delivered.emit(False, problem)
                return

            snap = w.snapshot_clipboard()
            ok = w.force_foreground(hwnd)
            if not ok:
                self.delivered.emit(False, "微信窗口没能切到前台，再点一次试试")
                return
            time.sleep(0.10)

            if self.store.setting("click_input", False):
                self._click_input_box(w.get_window_rect(hwnd))

            if kind == "text":
                if not w.set_clipboard_text(payload):
                    raise OSError(w.clipboard_problem() or "剪贴板写入失败")
            else:
                if not w.set_clipboard_files(payload):
                    raise OSError(w.clipboard_problem() or "文件放进剪贴板失败")

            time.sleep(0.10)
            w.paste()                        # Ctrl+V：内容进入微信输入框
            time.sleep(0.14)
            if self.store.setting("direct_send", False):
                w.press_enter()              # 可选：直接帮他发出去（默认关）

            if item_id:
                self.store.bump("text" if kind == "text" else "file", item_id)
            tip = "已进输入框，点【发送】即可" if not self.store.setting("direct_send", False) \
                else "已发送"
            if kind == "files":
                tip = "文件已进输入框，点【发送】即可" if not self.store.setting("direct_send", False) \
                    else "文件已发送"
            self.delivered.emit(True, tip)
        except Exception as exc:
            self.delivered.emit(False, f"送入失败：{exc}")
        finally:
            # 微信已经把内容取进输入框了，恢复剪贴板不会影响待发送内容
            if snap:
                try:
                    time.sleep(0.85)
                    w.restore_clipboard(snap)
                except Exception:
                    pass
            self._lock.release()
