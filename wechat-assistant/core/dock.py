# -*- coding: utf-8 -*-
"""dock.py —— 窗口智能跟随：吸附到当前聊天窗口旁边，不遮挡输入框。

同时负责每 200ms 采集前台窗口，交给 Sender 记住"要把话术发到哪个窗口"。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

from . import win32util as w

# 常见聊天窗口类名/标题特征；为空时退化为"任意够大的前台窗口"
CHAT_HINTS = (
    "wechat", "微信", "企业微信", "qq", "千牛", "旺旺", "阿里旺旺",
    "咚咚", "拼多多", "抖店", "商务通", "百度商桥", "telegram",
    "chrome", "edge", "firefox", "钉钉", "飞书", "slack",
)
SKIP_TITLES = ("", "程序管理器", "Program Manager", "任务栏", "桌面")


class DockController(QObject):
    def __init__(self, window, config, sender, parent=None):
        super().__init__(parent)
        self.window = window
        self.cfg = config
        self.sender = sender
        self.timer = QTimer(self)
        self.timer.setInterval(220)
        self.timer.timeout.connect(self.tick)
        self._own_hwnd = 0
        self._last_placed = None

    def start(self):
        try:
            self._own_hwnd = int(self.window.winId())
        except Exception:
            self._own_hwnd = 0
        if self._own_hwnd:
            self.sender.register_self_hwnd(self._own_hwnd)
        self.timer.start()

    def stop(self):
        self.timer.stop()

    def tick(self):
        fg = w.get_foreground_window()
        if not fg:
            return
        if not self._own_hwnd:
            try:
                self._own_hwnd = int(self.window.winId())
            except Exception:
                return
            self.sender.register_self_hwnd(self._own_hwnd)
        self.sender.observe_foreground(fg)
        if fg == self._own_hwnd:
            return
        if not self.cfg.get("window.dock", False) or not self.window.isVisible():
            return
        if getattr(self.window, "_user_moving", False):
            return

        info = w.get_window_info(fg)
        rect = info.get("rect")
        if not rect or info.get("title") in SKIP_TITLES:
            return
        l, t, r, b = rect
        fw, fh = r - l, b - t
        if fw < 520 or fh < 360:
            return
        if not self._looks_like_chat(info):
            return

        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        if not area:
            return
        gap = int(self.cfg.get("window.dock_gap", 8))
        side = self.cfg.get("window.dock_side", "right")
        ww, wh = self.window.width(), self.window.height()
        if side == "left":
            x = max(area.left(), l - ww - gap)
        else:
            x = min(area.right() - ww, r + gap)
        if x + ww > area.right():
            x = max(area.left(), area.right() - ww)
        y = max(area.top(), min(t, area.bottom() - wh))
        placed = (x, y)
        if placed != self._last_placed and (self.window.x(), self.window.y()) != placed:
            self.window.move(x, y)
            self._last_placed = placed

    @staticmethod
    def _looks_like_chat(info: dict) -> bool:
        blob = ((info.get("title") or "") + " " + (info.get("class") or "")).lower()
        return any(h in blob for h in CHAT_HINTS) or bool(info.get("title"))
