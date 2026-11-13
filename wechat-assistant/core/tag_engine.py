# -*- coding: utf-8 -*-
"""
微信「通讯录管理 -> 标签 -> 添加成员」批量勾选助手

工作方式（只模拟人工，不读取/改写微信数据库，不调用任何非公开接口）：
  1. 读取名单（Markdown 表格 / CSV / TXT / XLSX），取「微信备注/昵称」列作为搜索词
  2. 逐个把搜索词输入弹窗左上角搜索框
  3. 用截图像素判断：搜索框是否真的输入了文字、结果区有没有人、这一行是不是已经打过绿勾
  4. 只有「有结果且未勾选」才点击该行；多条结果默认跳过（可开启宽松勾选），
     点击后再用绿勾 + 右侧计数区变化双重确认
  5. 每个名字留一张结果行截图 + 一条 CSV 记录 + 一张 HTML 复核页
  6. 全程不点【完成】/【取消】。跑完由你核对右侧列表，自己点【完成】

安全设计：
  - 已勾选（绿勾）的行会跳过，避免误点造成「取消勾选」
  - 搜索无结果不点击任何地方，记入「未命中」
  - 断点续跑（progress.json）、F12 或鼠标甩到屏幕左上角急停
  - 分批暂停 + 随机延时，避免高频操作

依赖：Python 3.8+ / Windows / Pillow（仅截图用）。其余全部为标准库 ctypes。
      Pillow 缺失时：pip install pillow
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import html
import json
import random
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_DIR = ROOT / "logs"
SHOT_DIR = LOG_DIR / "shots"
RESULT_CSV = LOG_DIR / "result.csv"
PROGRESS_JSON = LOG_DIR / "progress.json"
REVIEW_HTML = LOG_DIR / "review.html"
CHECK_PNG = LOG_DIR / "check_regions.png"

# ---------------------------------------------------------------- Win32 基础
u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    u32.SetProcessDPIAware()
except Exception:
    pass

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

u32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
u32.EnumWindows.restype = wintypes.BOOL
u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
u32.GetWindowTextLengthW.restype = ctypes.c_int
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowTextW.restype = ctypes.c_int
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.IsWindowVisible.restype = wintypes.BOOL
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u32.GetWindowRect.restype = wintypes.BOOL
u32.GetForegroundWindow.restype = wintypes.HWND
u32.SetForegroundWindow.argtypes = [wintypes.HWND]
u32.SetForegroundWindow.restype = wintypes.BOOL
u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
u32.GetAsyncKeyState.restype = ctypes.c_short
u32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                            wintypes.DWORD, ctypes.c_size_t]
u32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
u32.OpenClipboard.argtypes = [wintypes.HWND]
u32.OpenClipboard.restype = wintypes.BOOL
u32.EmptyClipboard.argtypes = []
u32.EmptyClipboard.restype = wintypes.BOOL
u32.GetClipboardData.argtypes = [wintypes.UINT]
u32.GetClipboardData.restype = ctypes.c_void_p
u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
u32.SetClipboardData.restype = ctypes.c_void_p
u32.CloseClipboard.argtypes = []
u32.CloseClipboard.restype = wintypes.BOOL
u32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
u32.SendInput.restype = wintypes.UINT
u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
u32.GetAncestor.restype = wintypes.HWND
u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetClassNameW.restype = ctypes.c_int
k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
k32.GlobalUnlock.restype = wintypes.BOOL

VK_CTRL, VK_A, VK_V = 0x11, 0x41, 0x56
VK_DELETE, VK_F8, VK_F9, VK_F12 = 0x2E, 0x77, 0x78, 0x7B
# 安全约定：全局 Delete 键已禁用。Ctrl+A 之后直接粘贴/输入即可覆盖选中内容，
# 不需要 Delete；即使焦点判断失效，也不会把桌面或其它窗口的内容删掉。
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def tap(vk: int) -> None:
    u32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.03)


def ctrl_combo(vk: int) -> None:
    u32.keybd_event(VK_CTRL, 0, 0, 0)
    time.sleep(0.03)
    u32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    u32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.02)
    u32.keybd_event(VK_CTRL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)


def click(x: float, y: float, delay: float = 0.10) -> None:
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.06)
    u32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    u32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(delay)


GA_ROOT = 2
BLOCKED_FOCUS_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "SHELLDLL_DefView"}


def assert_safe_focus(hwnd) -> None:
    """发送全局按键前的焦点守卫。

    2026-11-10 事故：点击搜索框落空后，Ctrl+A + Delete 打到了桌面，
    把桌面 34 个可见条目全选删进回收站（含公共桌面快捷方式）。
    现在要求前台窗口与目标窗口同属一个根窗口，且绝不能是桌面/任务栏；
    不满足就抛错让上层停止，宁可中断也不发任何全局按键。
    """
    fg = u32.GetForegroundWindow()
    if not fg:
        raise RuntimeError("没有前台窗口，已中止：拒绝发送全局按键")
    cls = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(fg, cls, 256)
    if cls.value in BLOCKED_FOCUS_CLASSES:
        raise RuntimeError("焦点在桌面/任务栏，已中止：拒绝发送 Ctrl+A 等全局按键")
    if u32.GetAncestor(fg, GA_ROOT) != u32.GetAncestor(hwnd, GA_ROOT):
        raise RuntimeError("微信窗口已失去焦点，已中止：拒绝发送全局按键")


def get_clipboard_text() -> str:
    if not u32.OpenClipboard(None):
        return ""
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = k32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            k32.GlobalUnlock(h)
    finally:
        u32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        raise RuntimeError("GlobalAlloc 失败")
    p = k32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    k32.GlobalUnlock(h)
    for _ in range(12):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("剪贴板被其他程序占用，请关闭占用剪贴板的软件后重试")
    try:
        u32.EmptyClipboard()
        u32.SetClipboardData(CF_UNICODETEXT, h)
    finally:
        u32.CloseClipboard()


def send_unicode_word(word: str) -> None:
    """逐字发送 Unicode 字符，微信自绘输入框也能收到；不碰剪贴板。"""
    units = word.encode("utf-16-le")
    for i in range(0, len(units), 2):
        code = units[i] | (units[i + 1] << 8)
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = 1
            inp.u.ki.wVk = 0
            inp.u.ki.wScan = code
            inp.u.ki.dwFlags = flags
            u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.008)


def list_windows():
    out = []

    def cb(hwnd, lparam):
        if u32.IsWindowVisible(hwnd):
            n = u32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((hwnd, buf.value))
        return True

    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def get_rect(hwnd):
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def find_target(keyword: str):
    for hwnd, title in list_windows():
        if keyword in title:
            rect = get_rect(hwnd)
            if rect[2] - rect[0] > 300 and rect[3] - rect[1] > 200:
                return hwnd, rect
    return None, None


u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.ShowWindow.restype = wintypes.BOOL
u32.BringWindowToTop.argtypes = [wintypes.HWND]
u32.BringWindowToTop.restype = wintypes.BOOL
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.GetWindowThreadProcessId.restype = wintypes.DWORD
u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
u32.AttachThreadInput.restype = wintypes.BOOL
k32.GetCurrentThreadId.argtypes = []
k32.GetCurrentThreadId.restype = wintypes.DWORD


def force_foreground(hwnd) -> bool:
    """把目标窗口抢到前台：ALT 技巧 + AttachThreadInput 兜底。"""
    if u32.GetForegroundWindow() == hwnd:
        return True
    u32.ShowWindow(hwnd, 9)  # SW_RESTORE
    u32.keybd_event(0x12, 0, 0, 0)  # VK_MENU 按下，解除前台锁
    u32.SetForegroundWindow(hwnd)
    u32.BringWindowToTop(hwnd)
    u32.keybd_event(0x12, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.15)
    if u32.GetForegroundWindow() == hwnd:
        return True
    fg = u32.GetForegroundWindow()
    t_fg = u32.GetWindowThreadProcessId(fg, None)
    t_tg = u32.GetWindowThreadProcessId(hwnd, None)
    t_me = k32.GetCurrentThreadId()
    a = u32.AttachThreadInput(t_me, t_fg, True) if t_fg and t_fg != t_me else False
    b = u32.AttachThreadInput(t_me, t_tg, True) if t_tg and t_tg != t_me else False
    u32.SetForegroundWindow(hwnd)
    u32.BringWindowToTop(hwnd)
    time.sleep(0.15)
    ok = u32.GetForegroundWindow() == hwnd
    if b:
        u32.AttachThreadInput(t_me, t_tg, False)
    if a:
        u32.AttachThreadInput(t_me, t_fg, False)
    return ok


def stop_signal() -> str:
    if u32.GetAsyncKeyState(VK_F12) & 0x8000:
        return "F12"
    p = wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(p))
    if p.x <= 2 and p.y <= 2:
        return "鼠标左上角"
    return ""


def ensure_front(hwnd, wait: float = 12) -> bool:
    if force_foreground(hwnd):
        return True
    print("  [!] 微信「添加成员」窗口暂时抢不到前台，正在自动重试", flush=True)
    for _ in range(int(wait * 10)):
        time.sleep(0.1)
        if force_foreground(hwnd):
            return True
        if stop_signal():
            return False
    return False


# ---------------------------------------------------------------- 截图与像素判定
# ---- PrintWindow：后台抓窗口内容，被其它窗口遮挡也能拿到完整画面 ----
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFO),
                            wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
u32.GetWindowDC.argtypes = [wintypes.HWND]
u32.GetWindowDC.restype = ctypes.c_void_p
u32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
u32.PrintWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
u32.PrintWindow.restype = wintypes.BOOL


def _grab_printwindow(hwnd):
    rect = get_rect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if w <= 0 or h <= 0:
        return None
    hdc = u32.GetWindowDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(memdc, bmp)
    ok = u32.PrintWindow(hwnd, memdc, 2)   # 2 = PW_RENDERFULLCONTENT
    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h            # 自上而下
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(memdc, bmp, 0, h, ctypes.cast(buf, ctypes.c_void_p),
                          ctypes.byref(bi), 0)
    gdi32.SelectObject(memdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    u32.ReleaseDC(hwnd, hdc)
    if not ok or not got:
        return None
    from PIL import Image
    return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).convert("RGB")


def _not_blank(img) -> bool:
    small = img.resize((40, 40))
    return len(small.getcolors(maxcolors=2000) or []) > 8


def grab(hwnd):
    img = _grab_printwindow(hwnd)
    if img is not None and _not_blank(img):
        return img
    from PIL import ImageGrab
    rect = get_rect(hwnd)
    try:
        img = ImageGrab.grab(bbox=rect, all_screens=True)
    except TypeError:
        img = ImageGrab.grab(bbox=rect)
    return img.convert("RGB")


def abs_point(rect, p):
    return (rect[0] + int(p[0]), rect[1] + int(p[1]))


def _rgb_stream(area):
    """按 RGB 逐像素产出，避免 Pillow 版本差异与弃用告警。"""
    data = area.tobytes()
    for i in range(0, len(data), 3):
        yield data[i], data[i + 1], data[i + 2]


def nonbg_pixels(img, reg, bg, tol: int = 26) -> int:
    """区域内与背景色明显不同的像素数：用来判断搜索结果区有没有内容。"""
    area = img.crop(tuple(map(int, reg)))
    return sum(1 for r, g, b in _rgb_stream(area)
               if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) > tol * 3)


def sample_bg(img, points) -> tuple:
    """多点采样取「亮度中位数」的颜色当背景色。

    2026-11-11 修复：旧版只取 [60,500] 一个点，缩放后 1px 之差就可能
    落在联系人头像或复选框圆圈上，采样色一错，结果区/第二行像素数全部
    失真（实测曾把 672px 算成 12126px），造成「未命中」等误判。
    """
    cols = []
    for p in points:
        try:
            cols.append(img.getpixel((int(p[0]), int(p[1])))[:3])
        except Exception:
            continue
    if not cols:
        return (255, 255, 255)
    return sorted(cols, key=sum)[len(cols) // 2]


def green_pixels(img, reg) -> int:
    """区域内绿色像素数：微信选中态是绿色对勾。"""
    area = img.crop(tuple(map(int, reg)))
    return sum(1 for r, g, b in _rgb_stream(area)
               if g > 90 and g > r * 1.35 and g > b * 1.35)


def text_pixels(img, reg, bg, tol: int = 100) -> int:
    """搜索框内「真正输入的文字」像素数（跨深浅主题、跨窗口宽度稳定）。

    空的搜索框里只有暗色占位提示「搜索」和放大镜，与底色反差很小；
    真正输入后文字是白字（深色主题）或黑字（浅色主题），与底色反差很大。
    绿色像素要排除：聚焦时的绿色描边、搜索结果里的绿色高亮都不是输入文字。

    2026-11-11 修复：旧版只靠搜索框右端「清除 × 图标」判断有没有输入，
    但弹窗从 770 宽变成 794 宽时图标右移了约 28px，检测框按线性缩放
    只右移 9px，完全错过图标 -> 明明搜出了人却判定「未输入」而不敢点击。
    """
    area = img.crop(tuple(map(int, reg)))
    base = sum(bg) / 3.0
    n = 0
    for r, g, b in _rgb_stream(area):
        if g > 90 and g > r * 1.35 and g > b * 1.35:
            continue
        if abs((r + g + b) / 3.0 - base) > tol:
            n += 1
    return n


def diff_pixels(a, b, tol: int = 25) -> int:
    from PIL import ImageChops
    d = ImageChops.difference(a, b).convert("L")
    return sum(1 for v in d.tobytes() if v > tol)


def safe_filename(s: str, maxlen: int = 26) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s)
    return s[:maxlen] or "empty"


def save_shot(img, reg, idx: int, status: str, word: str) -> str:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    fn = f"{idx:04d}_{status}_{safe_filename(word)}.png"
    img.crop(tuple(map(int, reg))).save(SHOT_DIR / fn)
    return fn


# ---------------------------------------------------------------- 名单解析
def clean_name(s: str) -> str:
    s = s.strip().strip("|").strip()
    s = re.sub(r"[*`_]", "", s)
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _row_ok(name: str) -> bool:
    if not name or len(name) < 2:
        return False
    # 纯数字多半是表格序号；但 11 位手机号是真实存在的微信备注名，要放行
    if name.isdigit() and not re.fullmatch(r"1[3-9]\d{9}", name):
        return False
    if name in ("微信备注/昵称", "备注", "昵称", "姓名", "名字"):
        return False
    return True


def _read_xlsx_stdlib(path: Path):
    """纯标准库读 xlsx 第一张表（不装 openpyxl 也能用 Excel 名单）。"""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
        sheet = next((n for n in z.namelist()
                      if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")), None)
        if not sheet:
            return []
        root = ET.fromstring(z.read(sheet))
    out = []
    for row in root.iter(f"{ns}row"):
        cells = []
        for c in row.findall(f"{ns}c"):
            ref = c.get("r", "")
            letters = "".join(ch for ch in ref if ch.isalpha()).upper()
            idx = 0
            for ch in letters:
                idx = idx * 26 + (ord(ch) - 64)
            idx = max(0, idx - 1)
            while len(cells) <= idx:
                cells.append(None)
            ctype = c.get("t", "")
            if ctype == "inlineStr":
                node = c.find(f"{ns}is")
                value = "".join(t.text or "" for t in node.iter(f"{ns}t")) if node is not None else ""
            else:
                v = c.find(f"{ns}v")
                value = (v.text or "") if v is not None else ""
                if ctype == "s" and value:
                    try:
                        value = shared[int(value)]
                    except (ValueError, IndexError):
                        value = ""
            cells[idx] = value
        out.append(cells)
    return out


def load_names(path: Path, grade: str = "", min_records: int = 0):
    """返回 [{'name','grade','records','silence'}]，保持原顺序并去重。"""
    suffix = path.suffix.lower()
    rows = []

    if suffix == ".md":
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        header = None
        col = {}
        for line in lines:
            st = line.strip()
            if not st.startswith("|"):
                header = None
                continue
            cells = [c.strip() for c in st.strip("|").split("|")]
            if header is None:
                joined = "".join(cells)
                if any(k in joined for k in ("备注", "昵称", "姓名", "名字", "客户")):
                    header = cells
                    for i, c in enumerate(cells):
                        if any(k in c for k in ("备注", "昵称", "姓名", "名字", "客户")):
                            col["name"] = i
                        elif c == "级" or ("级" in c and len(c) <= 4):
                            col["grade"] = i
                        elif "记录" in c:
                            col["records"] = i
                        elif "静默" in c:
                            col["silence"] = i
                continue
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if "name" not in col or col["name"] >= len(cells):
                continue
            name = clean_name(cells[col["name"]])
            if not _row_ok(name):
                continue
            rec = cells[col["records"]] if col.get("records", 99) < len(cells) else ""
            sil = cells[col["silence"]] if col.get("silence", 99) < len(cells) else ""
            grd = cells[col["grade"]] if col.get("grade", 99) < len(cells) else ""
            rows.append({"name": name, "grade": grd, "records": rec, "silence": sil})

    elif suffix in (".csv", ".tsv"):
        delim = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            data = list(csv.reader(f, delimiter=delim))
        if not data:
            return []
        head = data[0]
        idx = next((i for i, c in enumerate(head)
                    if any(k in c for k in ("备注", "昵称", "姓名", "名字", "客户", "name", "remark"))), 0)
        for cells in data[1:]:
            if idx >= len(cells):
                continue
            name = clean_name(cells[idx])
            if _row_ok(name):
                rows.append({"name": name, "grade": "", "records": "", "silence": ""})

    elif suffix == ".txt":
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name = clean_name(line.split(",")[0].split("\t")[0])
            if _row_ok(name) and not name.startswith("#"):
                rows.append({"name": name, "grade": "", "records": "", "silence": ""})

    elif suffix in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            data = _read_xlsx_stdlib(path)
        else:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb[wb.sheetnames[0]]
            data = list(ws.values)
            wb.close()
        head = [str(x or "") for x in data[0]] if data else []
        idx = next((i for i, c in enumerate(head)
                    if any(k in c for k in ("备注", "昵称", "姓名", "名字", "客户", "name", "remark"))), 0)
        for cells in data[1:]:
            if idx >= len(cells) or cells[idx] is None:
                continue
            name = clean_name(str(cells[idx]))
            if _row_ok(name):
                rows.append({"name": name, "grade": "", "records": "", "silence": ""})
    else:
        raise SystemExit(f"不支持的文件类型：{suffix}（支持 .md / .csv / .tsv / .txt / .xlsx）")

    # 去重 + 可选筛选
    seen, out = set(), []
    for r in rows:
        key = re.sub(r"\s+", "", r["name"]).lower()
        if key in seen:
            continue
        seen.add(key)
        if grade and str(r["grade"]).strip() != str(grade).strip():
            continue
        if min_records:
            try:
                if int(float(r["records"] or 0)) < min_records:
                    continue
            except ValueError:
                continue
        out.append(r)
    return out


def load_alias(path: Path) -> dict:
    """别名表：名单里的名字 -> 微信通讯录里真实的备注名（两列 CSV）。"""
    if not path.exists():
        return {}
    out = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0].strip() and not row[0].strip().startswith("#"):
                k, v = clean_name(row[0]), clean_name(row[1])
                if k and v and k not in ("名单关键词", "备注", "昵称"):
                    out[k] = v
    return out


def build_words(name: str, cfg) -> list:
    v = cfg.get("variants", {})
    words = [name]
    if v.get("nospace", True):
        w = re.sub(r"[\s,，、]+", "", name)
        if w and w not in words:
            words.append(w)
    if v.get("short_prefix"):
        core = re.sub(r"[\s,，、]+", "", name)[:int(v.get("short_len", 10))]
        if len(core) >= 6 and core not in words:
            words.append(core)
    return words[:3]


def is_excluded(name: str, cfg) -> bool:
    norm = lambda s: re.sub(r"\s+", "", clean_name(s))
    n = norm(name)
    if any(norm(x) == n for x in cfg.get("exclude_names", [])):
        return True
    return any(k in name for k in cfg.get("exclude_keywords", []))


# ---------------------------------------------------------------- 日志与复核页
def load_progress(fresh: bool) -> set:
    if fresh or not PROGRESS_JSON.exists():
        return set()
    try:
        return set(json.loads(PROGRESS_JSON.read_text(encoding="utf-8")).get("done", []))
    except Exception:
        return set()


def save_progress(done: set) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_JSON.write_text(
        json.dumps({"done": sorted(done), "updated": datetime.now().isoformat(timespec="seconds")},
                   ensure_ascii=False, indent=2), encoding="utf-8")


def append_result(rec: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    new = not RESULT_CSV.exists()
    with RESULT_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["时间", "序号", "状态", "名单关键词", "实际搜索词", "截图文件", "说明"])
        w.writerow([rec["time"], rec["idx"], rec["status"], rec["name"],
                    rec["word"], rec["shot"], rec["note"]])


COLORS = {"成功": "#2e7d32", "已存在": "#6b7280", "未命中": "#c0392b",
          "待复核": "#e67e22", "已排除": "#8e44ad", "已中断": "#c0392b"}


def build_review(rows: list, stats: Counter) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cards = "".join(
        f'<div class="card"><b style="color:{COLORS.get(k, "#333")}">{k}</b><div class="num">{v}</div></div>'
        for k, v in stats.items())
    trs = []
    for r in rows:
        shot = (f'<img src="shots/{html.escape(r["shot"])}" loading="lazy">'
                if r["shot"] else "-")
        c = COLORS.get(r["status"], "#333")
        trs.append(
            f'<tr><td>{r["idx"]}</td><td style="color:{c};font-weight:600">{html.escape(r["status"])}</td>'
            f'<td>{html.escape(r["name"])}</td><td class="dim">{html.escape(r["word"])}</td>'
            f'<td>{shot}</td><td class="dim">{html.escape(r["note"])}</td></tr>')
    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>微信标签批量勾选 · 复核页</title>
<style>
body{{background:#f6f7f9;color:#1f2328;font:14px/1.6 "Microsoft YaHei",sans-serif;margin:24px}}
h1{{font-size:20px;border-left:4px solid #c0392b;padding-left:10px}}
.warn{{background:#fff4f4;border:1px solid #f0c8c8;color:#96281b;padding:10px 14px;border-radius:6px;margin:12px 0}}
.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}
.card{{background:#fff;border:1px solid #e3e6ea;border-radius:8px;padding:10px 18px;min-width:90px}}
.num{{font-size:22px;font-weight:700}}
table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{border:1px solid #e3e6ea;padding:6px 9px;vertical-align:middle;text-align:left}}
th{{background:#fafafa;position:sticky;top:0}}
img{{height:74px;border:1px solid #ddd;border-radius:3px}}
.dim{{color:#6b7280;font-size:12px}}
</style></head><body>
<h1>微信标签批量勾选 · 复核页</h1>
<div class="warn"><b>点【完成】之前请做两件事：</b>
① 在微信弹窗右侧滚动核对已选名单；② 在下面重点看红色「未命中」和橙色「待复核」，
截图里就是当时搜索出来的那一行，确认没有勾错人。确认无误后再手动点【完成】。</div>
<div class="cards">{cards}</div>
<table><thead><tr><th>#</th><th>状态</th><th>名单关键词</th><th>实际搜索词</th><th>搜索结果截图</th><th>判定依据</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table>
<p class="dim">生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · 截图目录：logs/shots/</p>
</body></html>"""
    REVIEW_HTML.write_text(doc, encoding="utf-8")


# ---------------------------------------------------------------- 单人处理
def type_into_search(hwnd, rect, word: str, cfg, mode: str) -> None:
    tm = cfg["timing"]
    x, y = abs_point(rect, cfg["points"]["search_box"])
    click(x, y, 0.12)
    # 点击可能落空 / 窗口可能被切走：发任何全局按键前都必须复核焦点
    assert_safe_focus(hwnd)
    ctrl_combo(VK_A)          # 全选旧搜索词；随后粘贴或输入会直接覆盖，无需 Delete
    assert_safe_focus(hwnd)
    if mode == "type":
        send_unicode_word(word)
    else:
        set_clipboard_text(word)
        time.sleep(0.06)
        assert_safe_focus(hwnd)
        ctrl_combo(VK_V)
    time.sleep(tm["search_wait"] + random.random() * tm["jitter"])


def process_one(hwnd, rect, name: str, cfg, args, idx: int) -> tuple:
    th, reg, tm = cfg["thresholds"], cfg["regions"], cfg["timing"]
    words = build_words(name, cfg)
    pref = cfg.get("_alias", {}).get(name)
    if pref and pref not in words:
        words.insert(0, pref)
    last_fn = ""
    for word in words:
        sig = stop_signal()
        if sig:
            return ("已中断", word, "", f"急停：{sig}")
        if not ensure_front(hwnd):
            return ("已中断", word, "", "窗口长时间不在前台")
        rect = get_rect(hwnd)   # 窗口可能被移动，每次重新取
        type_into_search(hwnd, rect, word, cfg, args.input_mode)
        img = grab(hwnd)
        pts = cfg.get("bg_samples")
        bg = sample_bg(img, pts) if pts else img.getpixel(tuple(cfg["bg_sample"]))[:3]
        sbg = img.getpixel(tuple(cfg["search_bg_sample"]))[:3]
        nres = nonbg_pixels(img, reg["result_text"], bg)
        ngreen = green_pixels(img, reg["checkbox"])
        filled = nonbg_pixels(img, reg["clear_button"], sbg, tol=12)
        # 主信号：直接看搜索框里有没有真正输入的文字
        text_reg = reg.get("search_text")
        typed = text_pixels(img, text_reg, sbg) if text_reg else 0
        typed_th = th.get("search_text_pixels", 15)
        icon_strong_th = th.get("search_icon_strong_pixels", 100)
        multi = nonbg_pixels(img, reg["second_row"], bg)
        last_fn = save_shot(img, reg["shot"], idx, "搜索后", word)

        # 保险 1：搜索词没进去时，左侧是默认通讯录列表，绝不能点第一行。
        # 文字信号为主；清除图标只在「明显命中」时兜底，避免聚焦空框右端的
        # 灰色小图标被当成已输入（实测空框 74px / 真图标 145px）。
        if typed < typed_th and filled < icon_strong_th:
            return ("待复核", word, last_fn,
                    f"搜索框疑似未输入成功(文字{typed}px/清除图标{filled}px)，未点击；"
                    "可试 --input-mode type")
        if ngreen > th["green_pixels"]:
            return ("已存在", word, last_fn,
                    f"结果行已是绿勾({ngreen}px)，跳过以免误取消勾选")
        if nres < th["result_pixels"]:
            continue    # 搜索无结果：绝不点击，换下一个候选词
        # 保险 2：多条结果默认跳过防勾错人；界面勾「多条结果也勾第一条」后放行，
        # 这些名字会额外写进待人工处理清单，点【完成】前重点复核。
        multi_flag = multi > th["second_row_pixels"]
        if multi_flag and not getattr(args, "multi_ok", False):
            return ("待复核", word, last_fn,
                    f"疑似多条搜索结果(第二行{multi}px)，为防勾错人未点击，请看截图；"
                    "如需自动勾选第一条，请在界面勾「多条结果也勾第一条」")
        multi_note = "；⚠ 多条结果已勾第一条，请重点复核截图" if multi_flag else ""

        before = img.crop(tuple(map(int, reg["count"])))
        click(*abs_point(rect, cfg["points"]["result_row"]), delay=tm["click_wait"])
        # 关键：点击后必须把鼠标移开。微信列表的 hover 高亮也是绿勾+绿字，
        # 不移开会把「鼠标停在那儿」误判成「已勾选」（实测 hover 仅 19px，真选中 192px）。
        u32.SetCursorPos(*abs_point(rect, cfg["points"].get("idle_mouse", [200, 500])))
        time.sleep(0.35)
        img2 = grab(hwnd)
        ngreen2 = green_pixels(img2, reg["checkbox"])
        changed = diff_pixels(before, img2.crop(tuple(map(int, reg["count"]))))
        fn2 = save_shot(img2, reg["shot"], idx, "勾选后", word)
        if ngreen2 > th["green_pixels"] or changed > th["count_changed_pixels"]:
            return ("成功", word, fn2,
                    f"绿勾 {ngreen}→{ngreen2}px / 计数区变化 {changed}px{multi_note}")
        return ("待复核", word, fn2,
                f"移开鼠标后绿勾 {ngreen2}px / 计数区变化 {changed}px；"
                "多半是此人已在该标签内或微信不允许添加，请看截图" + multi_note)
    return ("未命中", words[0], last_fn, "搜索无结果（结果区为空），未点击任何位置")


# ---------------------------------------------------------------- 子命令
def do_check(cfg, image_path=None) -> int:
    hwnd = None
    if image_path:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        rect = (0, 0, img.size[0], img.size[1])
        print(f"离线标注模式：{image_path}（不读取实时窗口）")
    else:
        hwnd, rect = find_target(cfg["window_title_keyword"])
        if not hwnd:
            print("没找到「添加成员」窗口。请先在微信：通讯录管理 → 目标标签右键 → 添加成员。")
            return 1
        img = grab(hwnd)
    from PIL import ImageDraw
    ann = img.copy()
    d = ImageDraw.Draw(ann)
    boxes = [
        ("RESULT_TEXT", cfg["regions"]["result_text"], "#ff5555"),
        ("CHECKBOX", cfg["regions"]["checkbox"], "#55ff55"),
        ("COUNT", cfg["regions"]["count"], "#55aaff"),
        ("CLEAR_ICON", cfg["regions"]["clear_button"], "#00e5ff"),
        ("SECOND_ROW", cfg["regions"]["second_row"], "#ff8800"),
        ("SHOT", cfg["regions"]["shot"], "#ffcc55"),
        ("DONE (script never clicks)", cfg["regions"]["done_button"], "#ff55ff"),
    ]
    for label, r, color in boxes:
        r = tuple(map(int, r))
        d.rectangle(r, outline=color, width=2)
        d.text((r[0] + 2, max(0, r[1] - 12)), label, fill=color)
    for label, p in cfg["points"].items():
        x, y = int(p[0]), int(p[1])
        d.ellipse((x - 6, y - 6, x + 6, y + 6), outline="#ffffff", width=2)
        d.line((x - 10, y, x + 10, y), fill="#ffffff", width=1)
        d.line((x, y - 10, x, y + 10), fill="#ffffff", width=1)
        d.text((x + 8, y + 6), label, fill="#ffffff")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ann.save(CHECK_PNG)

    pts = cfg.get("bg_samples")
    bg = sample_bg(img, pts) if pts else img.getpixel(tuple(cfg["bg_sample"]))[:3]
    th = cfg["thresholds"]
    print(f"窗口 hwnd={hwnd if hwnd else '离线'}  rect={rect}  截图尺寸={img.size}")
    print(f"基准窗口尺寸={cfg['base_size']}，当前宽度差={(img.size[0]-cfg['base_size'][0])}px；"
          f"差异超过 20px 建议跑 --calibrate")
    print(f"背景采样色={bg}（取自 {cfg['bg_sample']}）")
    sbg = img.getpixel(tuple(cfg["search_bg_sample"]))[:3]
    print(f"搜索框底色={sbg}（取自 {cfg['search_bg_sample']}）")
    print(f"结果区非背景像素 = {nonbg_pixels(img, cfg['regions']['result_text'], bg)}"
          f"  （有结果阈值 > {th['result_pixels']}）")
    print(f"勾选框绿色像素   = {green_pixels(img, cfg['regions']['checkbox'])}"
          f"  （已勾选阈值 > {th['green_pixels']}）")
    print(f"清除图标像素     = {nonbg_pixels(img, cfg['regions']['clear_button'], sbg, tol=12)}"
          f"  （搜索词已输入阈值 > {th['search_filled_pixels']}）")
    print(f"第二行非背景像素 = {nonbg_pixels(img, cfg['regions']['second_row'], bg)}"
          f"  （多条结果阈值 > {th['second_row_pixels']}，越小越好）")
    print(f"区域标注图已保存：{CHECK_PNG}")
    print("请打开标注图确认各个框都套对位置；不对就改 config.json 里的坐标后重跑 --check。")
    return 0


def do_calibrate(cfg) -> int:
    hwnd, _ = find_target(cfg["window_title_keyword"])
    if not hwnd:
        print("没找到「添加成员」窗口，先把它打开。")
        return 1
    print("标定：把鼠标移到下面两个位置，各按一次 F8，全部取完按 F9")
    print("  1) 左上角搜索框中心")
    print("  2) 第一条搜索结果行的文字处（先随便搜出一个联系人）")
    print("  提示：先让微信窗口在前台，取点时不要点鼠标，只移动 + 按 F8")
    pts, prev = [], False
    while True:
        if u32.GetAsyncKeyState(VK_F9) & 0x8000:
            break
        f8 = bool(u32.GetAsyncKeyState(VK_F8) & 0x8000)
        if f8 and not prev:
            p = wintypes.POINT()
            u32.GetCursorPos(ctypes.byref(p))
            rect = get_rect(hwnd)
            rel = [p.x - rect[0], p.y - rect[1]]
            pts.append(rel)
            print(f"  已记录点 {len(pts)}：{rel}")
            time.sleep(0.35)
        prev = f8
        time.sleep(0.05)
    if len(pts) < 2:
        print("取点不足 2 个，config.json 未改动。")
        return 1
    old_row = cfg["points"]["result_row"]
    shift = (pts[1][0] - old_row[0], pts[1][1] - old_row[1])
    cfg["points"]["search_box"] = pts[0]
    cfg["points"]["result_row"] = pts[1]
    for key in ("result_text", "checkbox", "shot"):
        r = cfg["regions"][key]
        cfg["regions"][key] = [r[0] + shift[0], r[1] + shift[1],
                               r[2] + shift[0], r[3] + shift[1]]
    r = get_rect(hwnd)
    cfg["base_size"] = [r[2] - r[0], r[3] - r[1]]
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已更新 {CONFIG_PATH}（结果行整体平移 {shift}px）。建议再跑一次 --check 确认。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="微信标签「添加成员」批量勾选助手（最后【完成】由你手动点）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="名单文件路径，默认读 config.json 的 source_file")
    ap.add_argument("--check", action="store_true", help="只读自检：截图标注各区域，不点击、不输入")
    ap.add_argument("--check-image", help="用已有的窗口截图做离线自检/标注，不需要微信弹窗打开")
    ap.add_argument("--calibrate", action="store_true", help="重新标定搜索框和结果行坐标")
    ap.add_argument("--dry-run", action="store_true", help="只解析名单并打印预览，不碰微信")
    ap.add_argument("--limit", type=int, default=0, help="本次只处理前 N 个（试跑用）")
    ap.add_argument("--fresh", action="store_true", help="忽略断点记录，从头重跑")
    ap.add_argument("--grade", default="", help="只处理指定年级，如 24 / 25 / 26")
    ap.add_argument("--min-records", type=int, default=0, help="只处理记录数 >= N 的行")
    ap.add_argument("--input-mode", choices=["clipboard", "type"], default="clipboard",
                    help="中文输入方式：clipboard=粘贴(默认) / type=逐字 Unicode 输入")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if args.check or args.check_image:
        return do_check(cfg, args.check_image)
    if args.calibrate:
        return do_calibrate(cfg)

    path = Path(args.file or cfg["source_file"])
    if not path.exists():
        print(f"名单文件不存在：{path}\n请改 config.json 的 source_file，或用 --file 指定。")
        return 1
    cfg["_alias"] = load_alias(ROOT / "alias.csv")
    items = load_names(path, args.grade, args.min_records)
    if not items:
        print("名单解析结果为空，检查文件编码/表头是否含「备注」「昵称」等字眼。")
        return 1
    done = load_progress(args.fresh)
    todo = [it for it in items if it["name"] not in done]
    if args.limit:
        todo = todo[:args.limit]

    print(f"名单文件：{path}")
    print(f"解析到 {len(items)} 个名字（去重后），已完成 {len(done)} 个，本次待处理 {len(todo)} 个")
    if cfg["_alias"]:
        print(f"别名映射：{len(cfg['_alias'])} 条（alias.csv），优先按映射后的备注名搜索")
    if args.grade:
        print(f"筛选：仅 {args.grade} 级")
    if args.min_records:
        print(f"筛选：记录数 >= {args.min_records}")
    if args.dry_run:
        print("\n--dry-run 预览前 15 个（未做任何界面操作）：")
        for i, it in enumerate(todo[:15], 1):
            print(f"  {i:>3}. {it['name']}")
        print("\n确认无误后去掉 --dry-run 正式运行。")
        return 0

    hwnd, rect = find_target(cfg["window_title_keyword"])
    if not hwnd:
        print("没找到「添加成员」窗口。请先：通讯录管理 → 目标标签右键 → 添加成员，保持弹窗打开。")
        return 1

    print("""
运行前提（重要）：
  1. 微信「添加成员」弹窗保持打开，弹窗里已有的已选成员不会被清空
  2. 5 秒倒计时后开始，请把该窗口切到前台；运行期间不要切走、不要动鼠标
  3. 急停：按 F12，或把鼠标甩到屏幕左上角；进度会保存，下次自动续跑
  4. 脚本不会点【完成】/【取消】；跑完由你核对右侧列表后手动点【完成】
""", flush=True)
    for i in range(5, 0, -1):
        print(f"  {i} 秒后开始…", flush=True)
        time.sleep(1)
    ensure_front(hwnd)
    print("-" * 68, flush=True)

    clip_backup = ""
    if args.input_mode == "clipboard":
        clip_backup = get_clipboard_text()

    stats, records = Counter(), []
    tm = cfg["timing"]
    try:
        for idx, it in enumerate(todo, 1):
            name = it["name"]
            sig = stop_signal()
            if sig:
                print(f"[!] 急停：{sig}，已保存进度。", flush=True)
                break
            if is_excluded(name, cfg):
                status, word, shot, note = "已排除", "", "", "命中排除名单（需人工确认）"
            else:
                status, word, shot, note = process_one(hwnd, get_rect(hwnd), name, cfg, args, idx)
            rec = {"time": datetime.now().strftime("%H:%M:%S"), "idx": idx,
                   "status": status, "name": name, "word": word,
                   "shot": shot, "note": note}
            append_result(rec)
            records.append(rec)
            stats[status] += 1
            done.add(name)
            save_progress(done)
            print(f"[{idx:>3}/{len(todo)}] {status:<4} | {name[:24]:<24} | {note}", flush=True)
            if status == "已中断":
                break
            if idx % int(tm["batch_size"]) == 0 and idx < len(todo):
                print(f"    已处理 {idx} 个，休息 {tm['batch_pause']} 秒…", flush=True)
                time.sleep(tm["batch_pause"])
    except KeyboardInterrupt:
        print("\n[!] Ctrl+C 中断，进度已保存。", flush=True)
    finally:
        if args.input_mode == "clipboard" and clip_backup:
            try:
                set_clipboard_text(clip_backup)
            except Exception:
                pass

    build_review(records, stats)
    manual = [r["name"] for r in records if r["status"] in ("未命中", "待复核", "已排除")]
    manual_txt = LOG_DIR / "待人工处理.txt"
    manual_txt.write_text("\n".join(manual) + ("\n" if manual else ""), encoding="utf-8")
    print("-" * 68)
    print("本次结果：" + "  ".join(f"{k}={v}" for k, v in stats.items()))
    print(f"明细 CSV：{RESULT_CSV}")
    print(f"复核页  ：{REVIEW_HTML}（用浏览器打开，重点看红色/橙色行）")
    print(f"待人工  ：{manual_txt}（{len(manual)} 个未命中/待复核/已排除）")
    print(f"过程截图：{SHOT_DIR}")
    print("请先在微信右侧滚动核对已选成员，确认无误后【自己点完成】。脚本不会替你提交。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
