# -*- coding: utf-8 -*-
"""
win32util.py —— 纯 ctypes 的 Windows API 封装（零第三方依赖）。

覆盖四类能力：
  1. 窗口 / 焦点    ：前台窗口、标题类名、窗口矩形、强制激活、置顶
  2. 剪贴板         ：CF_UNICODETEXT 文本、CF_HDROP 文件列表、快照与恢复
  3. 键鼠模拟       ：SendInput 按键、组合键、鼠标点击、坐标
  4. 全局热键       ：RegisterHotKey / UnregisterHotKey

设计原则：所有函数都是"模拟人工操作"，不注入任何进程、不 hook 任何协议。
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import time

user32 = C.WinDLL("user32", use_last_error=True)
kernel32 = C.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- 基础类型
ULONG_PTR = C.c_ulonglong if C.sizeof(C.c_void_p) == 8 else C.c_ulong
WPARAM_T = ULONG_PTR
LPARAM_T = C.c_ssize_t
HANDLE_T = C.c_void_p


class POINT(C.Structure):
    _fields_ = [("x", C.c_long), ("y", C.c_long)]


class RECT(C.Structure):
    _fields_ = [("left", C.c_long), ("top", C.c_long),
                ("right", C.c_long), ("bottom", C.c_long)]

    def as_tuple(self):
        return self.left, self.top, self.right, self.bottom


class MSG(C.Structure):
    _fields_ = [
        ("hwnd", W.HWND), ("message", C.c_uint), ("wParam", WPARAM_T),
        ("lParam", LPARAM_T), ("time", C.c_uint), ("pt", POINT),
        ("lPrivate", C.c_uint),
    ]


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", C.c_long), ("dy", C.c_long), ("mouseData", C.c_uint32),
                ("dwFlags", C.c_uint32), ("time", C.c_uint32),
                ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", C.c_uint16), ("wScan", C.c_uint16),
                ("dwFlags", C.c_uint32), ("time", C.c_uint32),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [("uMsg", C.c_uint32), ("wParamL", C.c_uint16),
                ("wParamH", C.c_uint16)]


class _INPUTUNION(C.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(C.Structure):
    _fields_ = [("type", C.c_uint32), ("u", _INPUTUNION)]


class DROPFILES(C.Structure):
    """CF_HDROP 的头部，后面紧跟 \0 分隔、\0\0 结尾的 UTF-16 文件名列表。"""
    _fields_ = [("pFiles", C.c_uint32), ("pt", POINT),
                ("fNC", C.c_int32), ("fWide", C.c_int32)]


# ---------------------------------------------------------------- 常量
INPUT_MOUSE, INPUT_KEYBOARD, INPUT_HARDWARE = 0, 1, 2
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010

CF_TEXT, CF_UNICODETEXT, CF_HDROP = 1, 13, 15
CF_BITMAP, CF_METAFILEPICT, CF_ENHMETAFILE, CF_PALETTE = 2, 3, 14, 9
GMEM_MOVEABLE, GMEM_ZEROINIT = 0x0002, 0x0040

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 1, 2, 4, 8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
PM_REMOVE = 0x0001

SW_RESTORE, SW_SHOW = 9, 5
# SetWindowPos 的 hWndInsertAfter 是"伪句柄"，用无符号写法避免 ctypes 负数转换问题
HWND_TOPMOST = 0xFFFFFFFFFFFFFFFF     # -1
HWND_NOTOPMOST = 0xFFFFFFFFFFFFFFFE   # -2
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

VK = {
    "BACK": 0x08, "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D,
    "SHIFT": 0x10, "CTRL": 0x11, "CONTROL": 0x11, "ALT": 0x12,
    "PAUSE": 0x13, "CAPSLOCK": 0x14, "ESC": 0x1B, "ESCAPE": 0x1B,
    "SPACE": 0x20, "PGUP": 0x21, "PGDN": 0x22, "END": 0x23, "HOME": 0x24,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "INSERT": 0x2D, "DELETE": 0x2E, "LWIN": 0x5B, "WIN": 0x5B,
    "APPS": 0x5D, "NUMLOCK": 0x90, "SCROLLLOCK": 0x91,
}
for _i in range(1, 25):
    VK[f"F{_i}"] = 0x6F + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
    VK[_c] = ord(_c)
VK.update({
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
})

# ---------------------------------------------------------------- 函数签名
user32.GetForegroundWindow.restype = W.HWND
user32.SetForegroundWindow.argtypes = [W.HWND]
user32.SetForegroundWindow.restype = C.c_int
user32.BringWindowToTop.argtypes = [W.HWND]
user32.ShowWindow.argtypes = [W.HWND, C.c_int]
user32.IsIconic.argtypes = [W.HWND]
user32.IsWindow.argtypes = [W.HWND]
user32.IsWindowVisible.argtypes = [W.HWND]
user32.GetWindowRect.argtypes = [W.HWND, C.POINTER(RECT)]
user32.GetWindowTextLengthW.argtypes = [W.HWND]
user32.GetWindowTextLengthW.restype = C.c_int
user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
user32.GetWindowTextW.restype = C.c_int
user32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
user32.GetClassNameW.restype = C.c_int
user32.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(C.c_uint32)]
user32.GetWindowThreadProcessId.restype = C.c_uint32
user32.AttachThreadInput.argtypes = [C.c_uint32, C.c_uint32, C.c_int]
user32.AttachThreadInput.restype = C.c_int
user32.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int,
                                C.c_int, C.c_int, C.c_uint]
if C.sizeof(C.c_void_p) == 8:
    user32.GetWindowLongPtrW.argtypes = [W.HWND, C.c_int]
    user32.GetWindowLongPtrW.restype = C.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [W.HWND, C.c_int, C.c_ssize_t]
    user32.SetWindowLongPtrW.restype = C.c_ssize_t
    _GetWindowLong, _SetWindowLong = user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
else:
    user32.GetWindowLongW.argtypes = [W.HWND, C.c_int]
    user32.GetWindowLongW.restype = C.c_ssize_t
    user32.SetWindowLongW.argtypes = [W.HWND, C.c_int, C.c_ssize_t]
    user32.SetWindowLongW.restype = C.c_ssize_t
    _GetWindowLong, _SetWindowLong = user32.GetWindowLongW, user32.SetWindowLongW

user32.OpenClipboard.argtypes = [W.HWND]
user32.OpenClipboard.restype = C.c_int
user32.CloseClipboard.restype = C.c_int
user32.EmptyClipboard.restype = C.c_int
user32.GetClipboardData.argtypes = [C.c_uint]
user32.GetClipboardData.restype = HANDLE_T
user32.SetClipboardData.argtypes = [C.c_uint, HANDLE_T]
user32.SetClipboardData.restype = HANDLE_T
user32.EnumClipboardFormats.argtypes = [C.c_uint]
user32.EnumClipboardFormats.restype = C.c_uint
user32.IsClipboardFormatAvailable.argtypes = [C.c_uint]

kernel32.GlobalAlloc.argtypes = [C.c_uint, C.c_size_t]
kernel32.GlobalAlloc.restype = HANDLE_T
kernel32.GlobalLock.argtypes = [HANDLE_T]
kernel32.GlobalLock.restype = C.c_void_p
kernel32.GlobalUnlock.argtypes = [HANDLE_T]
kernel32.GlobalSize.argtypes = [HANDLE_T]
kernel32.GlobalSize.restype = C.c_size_t
kernel32.GlobalFree.argtypes = [HANDLE_T]
kernel32.GetCurrentThreadId.restype = C.c_uint32

user32.SendInput.argtypes = [C.c_uint, C.POINTER(INPUT), C.c_int]
user32.SendInput.restype = C.c_uint
user32.GetCursorPos.argtypes = [C.POINTER(POINT)]
user32.SetCursorPos.argtypes = [C.c_int, C.c_int]
user32.mouse_event.argtypes = [C.c_uint32, C.c_uint32, C.c_uint32,
                               C.c_uint32, ULONG_PTR]
user32.PeekMessageW.argtypes = [C.POINTER(MSG), W.HWND, C.c_uint,
                                C.c_uint, C.c_uint]
user32.PeekMessageW.restype = C.c_int
user32.TranslateMessage.argtypes = [C.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [C.POINTER(MSG)]
user32.RegisterHotKey.argtypes = [W.HWND, C.c_int, C.c_uint, C.c_uint]
user32.RegisterHotKey.restype = C.c_int
user32.UnregisterHotKey.argtypes = [W.HWND, C.c_int]
user32.UnregisterHotKey.restype = C.c_int


# ---------------------------------------------------------------- 窗口 / 焦点
def get_foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def is_window(hwnd: int) -> bool:
    return bool(hwnd and user32.IsWindow(W.HWND(hwnd)))


def get_window_title(hwnd: int) -> str:
    if not is_window(hwnd):
        return ""
    n = user32.GetWindowTextLengthW(W.HWND(hwnd))
    buf = C.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(W.HWND(hwnd), buf, n + 2)
    return buf.value


def get_window_class(hwnd: int) -> str:
    if not is_window(hwnd):
        return ""
    buf = C.create_unicode_buffer(256)
    user32.GetClassNameW(W.HWND(hwnd), buf, 256)
    return buf.value


def get_window_rect(hwnd: int):
    r = RECT()
    if is_window(hwnd) and user32.GetWindowRect(W.HWND(hwnd), C.byref(r)):
        return r.as_tuple()
    return None


def get_window_pid(hwnd: int) -> int:
    pid = C.c_uint32()
    if hwnd:
        user32.GetWindowThreadProcessId(W.HWND(hwnd), C.byref(pid))
    return int(pid.value)


def get_window_info(hwnd: int) -> dict:
    pid = C.c_uint32()
    user32.GetWindowThreadProcessId(W.HWND(hwnd), C.byref(pid)) if hwnd else None
    return {
        "hwnd": int(hwnd or 0),
        "title": get_window_title(hwnd),
        "class": get_window_class(hwnd),
        "rect": get_window_rect(hwnd),
        "pid": pid.value,
        "visible": bool(hwnd and user32.IsWindowVisible(W.HWND(hwnd))),
    }


_WNDENUMPROC = C.WINFUNCTYPE(C.c_int, W.HWND, LPARAM_T)
user32.EnumWindows.argtypes = [_WNDENUMPROC, LPARAM_T]
user32.EnumWindows.restype = C.c_int


def list_windows(visible_only: bool = True):
    """枚举所有顶层窗口，插件可用它按标题/类名找到微信等目标窗口。"""
    out = []

    def _cb(hwnd, _lparam):
        h = hwnd if isinstance(hwnd, int) else (getattr(hwnd, "value", 0) or 0)
        if visible_only and not user32.IsWindowVisible(W.HWND(h)):
            return True
        title = get_window_title(h)
        if title:
            out.append(get_window_info(h))
        return True

    try:
        user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    except Exception:
        pass
    return out


def find_window(title_contains: str = "", class_name: str = ""):
    """按标题片段 / 类名找窗口，返回 hwnd（0 表示没找到）。"""
    for info in list_windows():
        if title_contains and title_contains.lower() not in (info["title"] or "").lower():
            continue
        if class_name and class_name.lower() not in (info["class"] or "").lower():
            continue
        return info["hwnd"]
    return 0


def force_foreground(hwnd: int) -> bool:
    """尽量把某个窗口切到前台。Windows 有前台锁，这里用 AttachThreadInput 绕过。"""
    if not is_window(hwnd):
        return False
    if user32.IsIconic(W.HWND(hwnd)):
        user32.ShowWindow(W.HWND(hwnd), SW_RESTORE)
    fg = get_foreground_window()
    if fg == hwnd:
        return True
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(W.HWND(fg), None) if fg else 0
    attached = False
    if fg_tid and fg_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        user32.BringWindowToTop(W.HWND(hwnd))
        user32.SetForegroundWindow(W.HWND(hwnd))
        user32.SetWindowPos(W.HWND(hwnd), W.HWND(0), 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    time.sleep(0.06)
    return get_foreground_window() == hwnd


def set_topmost(hwnd: int, on: bool):
    if is_window(hwnd):
        user32.SetWindowPos(W.HWND(hwnd),
                            W.HWND(HWND_TOPMOST if on else HWND_NOTOPMOST),
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def set_no_activate(hwnd: int, on: bool):
    """WS_EX_NOACTIVATE：窗口被点击时不抢走当前聊天窗口的键盘焦点。"""
    if not is_window(hwnd):
        return
    style = _GetWindowLong(W.HWND(hwnd), GWL_EXSTYLE)
    style = (style | WS_EX_NOACTIVATE) if on else (style & ~WS_EX_NOACTIVATE)
    _SetWindowLong(W.HWND(hwnd), GWL_EXSTYLE, style)


# ---------------------------------------------------------------- 剪贴板
def _alloc_global(data: bytes):
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        raise OSError("GlobalAlloc 失败")
    p = kernel32.GlobalLock(h)
    if not p:
        kernel32.GlobalFree(h)
        raise OSError("GlobalLock 失败")
    C.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    return h


def _open_clipboard(retries: int = 8):
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def clipboard_problem() -> str:
    """探测剪贴板是否可用：返回空串表示可用，否则返回可直接展示给用户的原因。

    常见于 Windows 已锁定 / 处于 UAC 安全桌面（错误 5），或某个程序
    调用 OpenClipboard 后异常退出、没释放（错误 0）。这两种都不是本程序的缺陷。
    """
    if user32.OpenClipboard(None):
        user32.CloseClipboard()
        return ""
    code = C.get_last_error()
    if code == 5:
        return ("剪贴板被系统拒绝访问（Win32 错误 5）：Windows 会话可能已锁定或处于安全桌面，"
                "请解锁回到桌面后重试")
    if code == 0:
        return "剪贴板正被其他程序独占，请稍后重试（必要时重启那个程序）"
    return f"剪贴板不可用（Win32 错误 {code}）"


def set_clipboard_text(text: str) -> bool:
    """把 Unicode 文本写进剪贴板（直接交出 HGLOBAL 所有权，不用 Qt 延迟渲染）。"""
    data = text.encode("utf-16-le") + b"\x00\x00"
    h = _alloc_global(data)
    if not _open_clipboard():
        kernel32.GlobalFree(h)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            return False
        return True  # 成功后 h 归系统所有，不能释放
    finally:
        user32.CloseClipboard()


def get_clipboard_text() -> str:
    if not _open_clipboard():
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return C.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_clipboard_files(paths) -> bool:
    """把若干文件/图片放进剪贴板（CF_HDROP），微信/QQ 里 Ctrl+V 即可发送。"""
    import os
    paths = [os.path.abspath(p) for p in paths if p and os.path.exists(p)]
    if not paths:
        return False
    names = "\x00".join(paths) + "\x00\x00"
    header = DROPFILES(C.sizeof(DROPFILES), POINT(0, 0), 0, 1)
    data = bytes(header) + names.encode("utf-16-le")
    h = _alloc_global(data)
    if not _open_clipboard():
        kernel32.GlobalFree(h)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_HDROP, h):
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()


_SKIP_FORMATS = {CF_BITMAP, CF_METAFILEPICT, CF_ENHMETAFILE, CF_PALETTE}


def snapshot_clipboard():
    """保存剪贴板里可复制的格式，发送完话术后用来恢复，不打扰用户原有内容。"""
    result = []
    if not _open_clipboard():
        return result
    try:
        fmt = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if not fmt or fmt in _SKIP_FORMATS:
                break
            h = user32.GetClipboardData(fmt)
            if not h:
                continue
            size = kernel32.GlobalSize(h)
            p = kernel32.GlobalLock(h)
            if p and size:
                result.append((fmt, C.string_at(p, size)))
                kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()
    return result


def restore_clipboard(snapshot) -> bool:
    if not snapshot or not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        for fmt, data in snapshot:
            try:
                h = _alloc_global(data)
                user32.SetClipboardData(fmt, h)
            except OSError:
                pass
        return True
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------- 键鼠模拟
def _send(*inputs: INPUT) -> int:
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, C.sizeof(INPUT))


def key_down(vk: int, unicode_mode: bool = False) -> int:
    # 普通按键用 wVk；Unicode 直输用 wScan + KEYEVENTF_UNICODE
    ki = (KEYBDINPUT(0, vk, KEYEVENTF_UNICODE, 0, 0) if unicode_mode
          else KEYBDINPUT(vk, 0, 0, 0, 0))
    return _send(INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=ki)))


def key_up(vk: int, unicode_mode: bool = False) -> int:
    ki = (KEYBDINPUT(0, vk, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
          if unicode_mode else KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0))
    return _send(INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=ki)))


def tap(vk: int, delay: float = 0.012):
    key_down(vk)
    time.sleep(delay)
    key_up(vk)


def combo(modifiers, vk: int, delay: float = 0.02):
    """如 combo([VK['CTRL'], VK['SHIFT']], ord('V'))"""
    for m in modifiers:
        key_down(m)
        time.sleep(0.006)
    tap(vk, delay)
    for m in reversed(modifiers):
        key_up(m)
        time.sleep(0.006)


def paste():
    combo([VK["CTRL"]], ord("V"))


def press_enter():
    tap(VK["ENTER"])


def type_unicode(text: str, delay: float = 0.004):
    """SendInput 的 KEYEVENTF_UNICODE，可直接输入中文，不依赖输入法。"""
    for ch in text:
        code = ord(ch)
        key_down(code, unicode_mode=True)
        key_up(code, unicode_mode=True)
        time.sleep(delay)


def get_cursor_pos():
    p = POINT()
    user32.GetCursorPos(C.byref(p))
    return p.x, p.y


def click(x: int = None, y: int = None, button: str = "left",
          double: bool = False, right: bool = False):
    if x is not None and y is not None:
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
    down = MOUSEEVENTF_RIGHTDOWN if right else MOUSEEVENTF_LEFTDOWN
    up = MOUSEEVENTF_RIGHTUP if right else MOUSEEVENTF_LEFTUP
    n = 2 if double else 1
    for _ in range(n):
        user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.02)
        user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.05)


# ---------------------------------------------------------------- 热键解析
_MOD_MAP = {"ALT": MOD_ALT, "CTRL": MOD_CONTROL, "CONTROL": MOD_CONTROL,
            "SHIFT": MOD_SHIFT, "WIN": MOD_WIN, "SUPER": MOD_WIN, "META": MOD_WIN}


def parse_hotkey(combo_str: str):
    """'Ctrl+Shift+F9' -> (modifiers, vk)；解析失败返回 (0, 0)。"""
    if not combo_str:
        return 0, 0
    parts = [p.strip().upper() for p in str(combo_str).replace(" ", "").split("+") if p.strip()]
    if not parts:
        return 0, 0
    mods, key = 0, None
    for p in parts[:-1]:
        if p in _MOD_MAP:
            mods |= _MOD_MAP[p]
        else:
            return 0, 0
    key = VK.get(parts[-1])
    if key is None:
        return 0, 0
    return mods | MOD_NOREPEAT, key


def normalize_hotkey(combo_str: str) -> str:
    """用于序列热键比较：按修饰键固定顺序输出。"""
    mods, vk = parse_hotkey(combo_str)
    if not vk:
        return ""
    names = []
    for bit, name in ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"),
                      (MOD_SHIFT, "Shift"), (MOD_WIN, "Win")):
        if mods & bit:
            names.append(name)
    inv = {v: k for k, v in VK.items() if k in
           ("SPACE", "TAB", "ENTER", "ESC") or len(k) == 1 or k.startswith("F")}
    names.append(inv.get(vk, f"VK{vk}"))
    return "+".join(names)


def struct_sizes():
    """自检用：x64 下 INPUT 应为 40 字节，MSG 48 字节，DROPFILES 20 字节。"""
    return {"INPUT": C.sizeof(INPUT), "MSG": C.sizeof(MSG),
            "DROPFILES": C.sizeof(DROPFILES), "bits": C.sizeof(C.c_void_p) * 8}
