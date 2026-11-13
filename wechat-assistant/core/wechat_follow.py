# -*- coding: utf-8 -*-
"""wechat_follow.py —— 找到微信窗口 + 把辅助器吸附到微信聊天界面右侧。

两个探测器分工：
  WeChatTracker  低频(500ms)枚举顶层窗口，只负责「找到微信 / 记住最近用过的聊天窗」
  DockToWeChat   高频(25ms)只对同一个 hwnd 调 GetWindowRect，负责跟手跟随

为什么这样改（修「拖动慢悠悠卡顿」+「缩放不适配、排版乱」）：
  1. 旧版每 350ms 枚举全部窗口才更新一次位置 → 拖动时肉眼可见滞后；
     现在 25ms 只查一个矩形，微秒级，跟手。
  2. 旧版每次都用 setGeometry 连位置带尺寸一起改 → 每帧触发 Qt 全量布局重排
     （每张卡片还要重算省略号），所以拖起来一顿一顿；
     现在拖动期间只 move()，零布局重排。
  3. 尺寸只在微信「停下来」180ms 后适配一次，且高度按 24px 量化、宽度固定，
     不再随微信每一像素缩放反复拉扯内部排版。
  4. 外侧/内侧切换加 350ms 迟滞，避免微信缩放经过临界宽度时左右横跳。

只做窗口层面的定位，不注入、不读取微信数据。
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from . import win32util as w

WX_CLASS_HINTS = ("wechat", "weixin", "mmui", "chatframe")
WX_TITLE_HINTS = ("微信", "wechat")


def _is_wechat(info: dict) -> bool:
    cls = (info.get("class") or "").lower()
    title = (info.get("title") or "").lower()
    rect = info.get("rect")
    if not rect:
        return False
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    if width < 460 or height < 320:          # 过滤小程序/通知小窗
        return False
    if any(h in cls for h in WX_CLASS_HINTS):
        return True
    return any(h in title for h in WX_TITLE_HINTS) and width >= 600


def find_wechat_windows() -> list:
    """按面积从大到小返回所有看得见的微信主窗口。"""
    me = os.getpid()
    out = []
    for info in w.list_windows(visible_only=True):
        try:
            if info.get("pid") == me or not _is_wechat(info):
                continue
            r = info["rect"]
            info["area"] = (r[2] - r[0]) * (r[3] - r[1])
            out.append(info)
        except Exception:
            continue
    return sorted(out, key=lambda x: -x["area"])


class WeChatTracker(QObject):
    """低频发现微信窗口；updated 每次探测都发，供吸附器对账 / 界面显示状态。"""

    updated = Signal(dict)
    state_changed = Signal(bool, str)
    window_switched = Signal()

    def __init__(self, interval: int = 500, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(interval)
        self._timer.timeout.connect(self.tick)
        self.current: dict | None = None
        self.last_chat_hwnd = 0       # 用户最近点过的微信窗口（含独立聊天窗）
        self._last_state = None

    def start(self):
        self.tick()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def tick(self):
        fg = w.get_foreground_window()
        fg_info = w.get_window_info(fg) if fg else {}
        if _is_wechat(fg_info) and fg_info.get("pid") != os.getpid():
            self.last_chat_hwnd = int(fg)

        wins = find_wechat_windows()
        # 优先跟随"用户刚刚正在用的那个微信窗口"，否则用最大的主窗口
        cur = next((x for x in wins if x["hwnd"] == self.last_chat_hwnd), None) \
            or (wins[0] if wins else None)
        switched = bool(cur and self.current and cur["hwnd"] != self.current["hwnd"])
        self.current = cur

        found = bool(cur)
        title = (cur or {}).get("title", "") or ""
        state = (found, title)
        if state != self._last_state:
            self._last_state = state
            self.state_changed.emit(found, title)
        self.updated.emit(dict(cur) if cur else {})

        # 换了窗口：让吸附器立刻完整对齐一次（含尺寸）
        if switched:
            self.window_switched.emit()

    def rect(self):
        return (self.current or {}).get("rect")

    def best_target(self) -> int:
        """投送时该把内容送进哪个窗口：最近聊天窗 > 主窗口。"""
        if self.last_chat_hwnd and w.is_window(self.last_chat_hwnd):
            return self.last_chat_hwnd
        return int((self.current or {}).get("hwnd") or 0)


class DockToWeChat(QObject):
    """把辅助器贴到微信窗口右侧，跟着微信移动、缩放、最小化。"""

    MODES = ("auto", "outside", "inside")
    FAST_MS = 25            # 跟随刷新间隔
    RESIZE_SETTLE = 0.18    # 微信停住多久后才改尺寸（秒）
    SIDE_HOLD = 0.35        # 外侧/内侧判定要连续保持多久才切换（秒）

    # 高度贴合微信窗口：外侧上下各留 6px；内侧顶部避开微信标题栏、
    # 底部避开聊天输入框和【发送】键
    TOP_PAD = 6
    BOTTOM_PAD = 6
    INSIDE_TOP_PAD = 46
    INSIDE_BOTTOM_PAD = 130

    # 只有微信（或辅助器自己）在前台时才显示吸附窗，切到别的应用自动收起
    FG_GRACE = 0.4          # 焦点短暂跳走（粘贴、系统弹窗）不立刻收起
    MANUAL_HOLD = 6.0       # 用户手动从托盘呼出后至少显示这么久

    def __init__(self, window, tracker: WeChatTracker, store, parent=None):
        super().__init__(parent)
        self.window = window
        self.tracker = tracker
        self.store = store
        self.enabled = bool(store.setting("dock_wechat", False))
        self.mode = str(store.setting("dock_mode", "auto"))
        if self.mode not in self.MODES:
            self.mode = "auto"

        # 吸附态宽度固定，不用「当前宽度」，避免被 Qt 最小宽度或用户拖拽带偏
        self.width = max(340, min(int(window.width() or 390), 460))

        self._hwnd = 0
        self._wx_pid = 0
        self._own_pid = os.getpid()
        self._last_rect = None
        self._last_target = None
        self._moved_at = 0.0
        self._pending = False
        self._side = None
        self._prefer = None
        self._prefer_at = 0.0
        self._auto_hidden = False       # 只有自己收起的才自动弹回来
        self._fg_lost_at = 0.0
        self._manual_until = 0.0

        self._fast = QTimer(self)
        self._fast.setInterval(self.FAST_MS)
        self._fast.setTimerType(Qt.PreciseTimer)
        self._fast.timeout.connect(self._fast_tick)

        tracker.updated.connect(self.tick)
        tracker.window_switched.connect(self._on_switched)

    # ------------------------------------------------------------ 开关
    def set_enabled(self, on: bool):
        self.enabled = bool(on)
        self.store.set_setting("dock_wechat", self.enabled)
        self._reset()
        if self.enabled:
            self.window._set_always_on_top(True)
            self._fast.start()
            self.tick(self.tracker.current or {})
        else:
            self._fast.stop()
            self._restore()              # 取消吸附时窗口要还给用户，别留在隐藏态
            self.window._set_always_on_top(
                bool(self.store.setting("always_on_top", True)))

    # ------------------------------------------------------------ 显隐
    def note_manual_show(self):
        """用户从托盘 / 热键手动呼出：给一段宽限，别马上又被自动收起。"""
        self._auto_hidden = False
        self._manual_until = time.monotonic() + self.MANUAL_HOLD

    def note_manual_hide(self):
        """用户自己收起的窗口，不自动弹回来。"""
        self._auto_hidden = False
        self._fg_lost_at = 0.0

    def _wechat_active(self) -> bool:
        fg = w.get_foreground_window()
        if not fg:
            return False
        if fg == self._hwnd or fg == int(self.tracker.last_chat_hwnd or 0):
            return True
        pid = w.get_window_pid(fg)
        # 辅助器自己（含设置框、文件框、消息框）和微信进程的任何窗口都算「在用微信」
        return bool(pid and (pid == self._own_pid or
                             (self._wx_pid and pid == self._wx_pid)))

    def _auto_hide(self):
        if not self._auto_hidden and self.window.isVisible():
            self._auto_hidden = True
            self.window.hide()

    def _restore(self):
        if self._auto_hidden:
            self._auto_hidden = False
            # 不 raise / 不 activateWindow：别抢微信的键盘焦点
            self.window.show()

    def _follow_foreground(self):
        # 还没认到微信窗口时不干预显隐，免得用户只是想用常用语库却被收起
        if not self._wx_pid or not self._hwnd:
            return
        now = time.monotonic()
        if now < self._manual_until:
            self._fg_lost_at = 0.0
            self._restore()
            return
        if self._wechat_active():
            self._fg_lost_at = 0.0
            self._restore()
            return
        if self._auto_hidden:
            return
        if not self._fg_lost_at:
            self._fg_lost_at = now
        elif now - self._fg_lost_at >= self.FG_GRACE:
            self._auto_hide()

    def set_mode(self, mode: str):
        if mode in self.MODES:
            self.mode = mode
            self.store.set_setting("dock_mode", mode)
            self._side = None
            self._prefer = None
            if self.enabled:
                self.tick(self.tracker.current or {})

    def _reset(self):
        self._last_rect = None
        self._last_target = None
        self._pending = False
        self._side = None
        self._prefer = None
        self._prefer_at = 0.0

    # ------------------------------------------------------------ 布局
    @staticmethod
    def _sane(rect) -> bool:
        """过滤最小化 / 隐藏窗口的 -32000 坐标。"""
        if not rect or len(rect) != 4:
            return False
        l, t, r, b = rect
        return l > -10000 and t > -10000 and r - l >= 300 and b - t >= 240

    def _screen_geometry(self, rect):
        if rect:
            dpr = float(self.window.devicePixelRatioF() or 1.0)
            c = QPoint(int((rect[0] + rect[2]) / 2 / dpr),
                       int((rect[1] + rect[3]) / 2 / dpr))
            scr = QGuiApplication.screenAt(c)
            if scr:
                return scr.availableGeometry()
        scr = QGuiApplication.primaryScreen()
        return scr.availableGeometry() if scr else None

    def compute(self, rect, sg, win_w: int = None, win_h_max: int = None):
        """返回 (x, y, w, h)，全部是 Qt 逻辑坐标。win_w / win_h_max 可覆盖（测试用）。"""
        # Win32 GetWindowRect 是物理像素，Qt 用逻辑像素
        dpr = float(self.window.devicePixelRatioF() or 1.0)
        l, t, r, b = [v / dpr for v in rect]
        sg_l, sg_t, sg_r, sg_b = sg.left(), sg.top(), sg.right(), sg.bottom()
        win_w = int(win_w or self.width)

        gap = 8
        x_outside = r + gap
        x_inside = r - win_w - 14
        prefer = "outside" if x_outside + win_w <= sg_r - 4 else "inside"

        # 迟滞：微信缩放经过临界宽度时，不立刻左右横跳（横跳会改 bottom_reserve
        # 导致高度突变，是"排版乱"的主因）
        now = time.monotonic()
        if self.mode in ("outside", "inside"):
            side = self.mode
            self._side = side
        else:
            if self._side is None:
                self._side = prefer
            if prefer != self._side:
                if prefer != self._prefer:
                    self._prefer, self._prefer_at = prefer, now
                elif now - self._prefer_at >= self.SIDE_HOLD:
                    self._side = prefer
            else:
                self._prefer = prefer
            side = self._side

        x = x_outside if side == "outside" else x_inside
        inside = side == "inside"

        # ---- 高度：贴着微信窗口的上下边缘，微信多高就多高 ----
        # 旧版取 min(屏幕高-150, 720) 当固定高度，微信一拉高就差一大截，
        # 看着像「没适配」。现在直接由微信矩形算，顶部/底部只留固定 padding。
        top = max(t, sg_t)
        bottom = min(b, sg_b)
        if inside:
            # 浮在微信内侧时：顶部避开微信标题栏，底部必须给输入框 +【发送】键留白
            top_pad, bottom_pad = self.INSIDE_TOP_PAD, self.INSIDE_BOTTOM_PAD
        else:
            top_pad, bottom_pad = self.TOP_PAD, self.BOTTOM_PAD

        # Qt 的实际下限可能来自布局（打标签页撑到约 480），取两者较大值，
        # 免得算出来的高度被 Qt 悄悄顶回去，看起来又「不适配」
        min_h = int(self.window.minimumHeight() or 470)
        lay = self.window.layout()
        if lay is not None:
            min_h = max(min_h, int(lay.minimumSize().height()))
        min_h = max(min_h, 380)

        y = top + top_pad
        h = max(min_h, int(bottom - bottom_pad - y))
        cap = int(win_h_max) if win_h_max else int(sg_b - sg_t - 8)
        h = min(h, max(min_h, cap))
        # 不做高度量化：尺寸只在微信停稳后改一次，_align 还有 8px 阈值防抖；
        # 量化反而会让窗口比微信矮几像素，正是用户反馈的「高度不适配」。
        y = max(sg_t + 4, min(y, sg_b - h - 4))

        x = max(sg_l + 4, min(x, sg_r - win_w - 4))
        return int(x), int(y), int(win_w), int(h)

    # ------------------------------------------------------------ 跟随
    def tick(self, info: dict = None):
        """低频对账：启用、切模式、换微信窗口时，立刻做一次完整对齐。"""
        if not self.enabled:
            return
        info = info if info is not None else (self.tracker.current or {})
        if isinstance(info, dict):
            rect = info.get("rect")
            self._hwnd = int(info.get("hwnd") or self._hwnd or 0)
            if self._hwnd:
                self._wx_pid = w.get_window_pid(self._hwnd) or self._wx_pid
        else:
            rect, self._hwnd = None, 0
        if not rect or not self._sane(rect):
            return                       # 微信最小化/关掉时保持原位，不乱跳
        self._last_rect = rect
        self._pending = False
        self._align(rect, with_size=True)

    def _on_switched(self):
        if self.enabled:
            self._reset()
            self.tick(self.tracker.current or {})

    def _fast_tick(self):
        """高频跟随：只查一个 hwnd 的矩形，拖动中只 move，不碰布局。"""
        if not self.enabled:
            return
        # 前台不是微信就收起，避免置顶窗口挡住用户正在用的其他应用
        self._follow_foreground()
        if not self._hwnd:
            return
        now = time.monotonic()
        try:
            rect = w.get_window_rect(self._hwnd)
        except Exception:
            return
        if not self._sane(rect):
            self._auto_hide()        # 微信最小化 / 关掉，跟着一起收起
            return
        if rect != self._last_rect:
            self._last_rect = rect
            self._moved_at = now
            self._pending = True
            self._align(rect, with_size=False)     # 只移动 → 丝滑
        elif self._pending and now - self._moved_at >= self.RESIZE_SETTLE:
            self._pending = False
            self._align(rect, with_size=True)      # 微信停稳 → 一次性适配宽高

    def _align(self, rect, with_size: bool):
        sg = self._screen_geometry(rect)
        if not sg:
            return
        try:
            target = self.compute(rect, sg)
            cur = self.window.geometry()
        except Exception:
            return
        tx, ty, tw, th = target
        if cur.x() != tx or cur.y() != ty:
            self.window.move(tx, ty)
        if with_size:
            if abs(cur.width() - tw) >= 8 or abs(cur.height() - th) >= 8:
                self.window.resize(tw, th)
                self.window.move(tx, ty)   # 某些系统 resize 后会顶回原位，再贴一次
        self._last_target = target
