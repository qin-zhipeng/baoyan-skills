# -*- coding: utf-8 -*-
"""sender.py —— 发送引擎：剪贴板 + 模拟人工按键，把文字/图片/文件送到聊天窗口。

关键点：
  * 不注入任何聊天软件，不读写它的内存，只做"复制 + 切焦点 + Ctrl+V + 回车"；
  * 因此天然兼容微信 / QQ / 企业微信 / 千牛 / 拼多多 / 浏览器网页聊天等一切接受粘贴的窗口；
  * 支持分段发送（长话术拆成多条，避免刷屏）、变量替换、剪贴板原样恢复；
  * 发送动作放在工作线程，界面不卡顿。
"""
from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QObject, Signal

from . import win32util as w
from .store import Library


class Sender(QObject):
    log = Signal(str)
    done = Signal(bool, str)
    target_changed = Signal(dict)

    # 这些窗口不作为发送目标
    SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow"}

    def __init__(self, config, library: Library, parent=None):
        super().__init__(parent)
        self.cfg = config
        self.lib = library
        self._auto_target = 0
        self._pinned_target = 0
        self._self_hwnds = set()
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self.done.connect(lambda *_: None)

    # ------------------------------------------------------------ 目标窗口
    def register_self_hwnd(self, hwnd: int):
        if hwnd:
            self._self_hwnds.add(int(hwnd))

    def observe_foreground(self, hwnd: int):
        """由主窗口的定时器调用：记录"最近一次不是本程序的窗口"作为发送目标。"""
        if not hwnd or hwnd in self._self_hwnds:
            return
        if w.get_window_pid(hwnd) == self._pid:   # 跳过本程序的所有弹窗/面板
            return
        cls = w.get_window_class(hwnd)
        if cls in self.SKIP_CLASSES or not w.get_window_title(hwnd):
            return
        with self._lock:
            changed = hwnd != self._auto_target
            self._auto_target = int(hwnd)
        if changed:
            self.target_changed.emit(w.get_window_info(hwnd))

    def pin_target(self, hwnd: int = 0):
        with self._lock:
            self._pinned_target = int(hwnd or 0)
        self.target_changed.emit(self.target_info())

    def target(self) -> int:
        with self._lock:
            if self._pinned_target and w.is_window(self._pinned_target):
                return self._pinned_target
            if self._auto_target and w.is_window(self._auto_target):
                return self._auto_target
            return 0

    def target_info(self) -> dict:
        return w.get_window_info(self.target())

    def activate(self, hwnd: int = None) -> bool:
        return w.force_foreground(int(hwnd or self.target() or 0))

    # ------------------------------------------------------------ 对外 API（插件也用）
    def send_text(self, text: str, enter: bool = None, target: int = None,
                  restore_clipboard: bool = None, block: bool = False):
        if block:
            self._send_text_worker(text, enter, target, restore_clipboard)
        else:
            threading.Thread(target=self._send_text_worker,
                             args=(text, enter, target, restore_clipboard),
                             daemon=True).start()

    def send_files(self, paths, target: int = None, block: bool = False):
        if block:
            self._send_files_worker(list(paths), target)
        else:
            threading.Thread(target=self._send_files_worker,
                             args=(list(paths), target), daemon=True).start()

    def copy_text(self, text: str) -> bool:
        ok = w.set_clipboard_text(text)
        self.log.emit(f"已复制到剪贴板：{text[:30]}…" if ok else
                      (w.clipboard_problem() or "剪贴板写入失败"))
        return ok

    def click(self, x=None, y=None, double=False, right=False):
        w.click(x, y, double=double, right=right)

    def tap_vk(self, vk: int):
        w.tap(vk)

    def combo(self, modifiers, vk: int):
        w.combo(modifiers, vk)

    def type_text(self, text: str):
        w.type_unicode(text)

    def delay(self, seconds: float):
        time.sleep(max(0.0, float(seconds)))

    def cursor_pos(self):
        return w.get_cursor_pos()

    # ------------------------------------------------------------ 话术发送
    def send_phrase(self, phrase: dict, values: dict = None, mode: str = None,
                    target: int = None):
        values = values or {}
        text = Library.render(phrase.get("content", ""), values)
        files = [p for p in (phrase.get("files") or []) if p]
        mode = mode or self.cfg.get("send.mode", "paste_enter")
        threading.Thread(target=self._send_phrase_worker,
                         args=(phrase, text, files, mode, target, values),
                         daemon=True).start()

    def _send_phrase_worker(self, phrase, text, files, mode, target, values):
        try:
            s = self.cfg.section("send")
            target = int(target or self.target() or 0)
            if mode == "copy":
                self.copy_text(text)
                self.lib.record_usage(phrase["id"])
                self.done.emit(True, "已复制")
                return

            if not target:
                self.log.emit("没有可用的发送目标：请先点一下聊天窗口，或用「锁定目标」")
                self.done.emit(False, "没有目标窗口")
                return

            restore = (s.get("restore_clipboard", True)
                       if not phrase.get("_no_restore") else False)
            snap = w.snapshot_clipboard() if restore else []

            self.log.emit(f"→ {w.get_window_title(target) or '目标窗口'}")
            w.force_foreground(target)
            time.sleep(0.08)

            segments = [text]
            if s.get("split_segments", True) and s.get("segment_separator"):
                segments = [x.strip() for x in text.split(s["segment_separator"]) if x.strip()]

            for idx, seg in enumerate(segments):
                if idx:
                    time.sleep(s.get("segment_delay_ms", 320) / 1000.0)
                    w.force_foreground(target)
                if not w.set_clipboard_text(seg):
                    raise OSError(w.clipboard_problem() or "剪贴板写入失败")
                time.sleep(s.get("paste_delay_ms", 90) / 1000.0)
                w.paste()
                time.sleep(s.get("delay_ms", 120) / 1000.0)
                if mode == "paste_enter" and s.get("enter_after_paste", True):
                    w.press_enter()

            for path in files:
                time.sleep(s.get("segment_delay_ms", 320) / 1000.0)
                self._put_files(path)

            if restore and snap:
                time.sleep(s.get("restore_delay_ms", 900) / 1000.0)
                w.restore_clipboard(snap)

            self.lib.record_usage(phrase["id"])
            self.done.emit(True, f"已发送：{phrase.get('title', '')}")
        except Exception as exc:  # 不让工作线程静默死掉
            self.log.emit(f"发送失败：{exc}")
            self.done.emit(False, str(exc))

    def _send_text_worker(self, text, enter, target, restore_clipboard):
        try:
            s = self.cfg.section("send")
            if enter is None:
                enter = s.get("enter_after_paste", True)
            if restore_clipboard is None:
                restore_clipboard = s.get("restore_clipboard", True)
            snap = w.snapshot_clipboard() if restore_clipboard else []
            target = int(target or self.target() or 0)
            if not target:
                self.log.emit("没有可用的发送目标")
                self.done.emit(False, "没有目标窗口")
                return
            w.force_foreground(target)
            # 必须检查返回值：写不进去时若继续 Ctrl+V，会把剪贴板里的旧内容发出去
            if not w.set_clipboard_text(text):
                raise OSError(w.clipboard_problem() or "剪贴板写入失败")
            time.sleep(s.get("paste_delay_ms", 90) / 1000.0)
            w.paste()
            time.sleep(s.get("delay_ms", 120) / 1000.0)
            if enter:
                w.press_enter()
            if restore_clipboard and snap:
                time.sleep(s.get("restore_delay_ms", 900) / 1000.0)
                w.restore_clipboard(snap)
            self.done.emit(True, "文本已发送")
        except Exception as exc:
            self.log.emit(f"发送失败：{exc}")
            self.done.emit(False, str(exc))

    def _send_files_worker(self, paths, target):
        try:
            s = self.cfg.section("send")
            target = int(target or self.target() or 0)
            snap = w.snapshot_clipboard()
            if target:
                w.force_foreground(target)
            if self._put_files(paths):
                time.sleep(0.12)
                w.paste()
                self.log.emit(f"已粘贴 {len(paths)} 个文件（如微信弹出确认请点发送）")
                self.done.emit(True, "文件已粘贴")
            else:
                self.done.emit(False, w.clipboard_problem() or "文件路径无效或剪贴板不可用")
            time.sleep(s.get("restore_delay_ms", 900) / 1000.0)
            w.restore_clipboard(snap)
        except Exception as exc:
            self.log.emit(f"文件发送失败：{exc}")
            self.done.emit(False, str(exc))

    def _put_files(self, paths) -> bool:
        if isinstance(paths, str):
            paths = [paths]
        paths = [os.path.abspath(p) for p in paths if p]
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            self.log.emit(f"文件不存在，已跳过：{missing[0]}")
        return w.set_clipboard_files(paths)
