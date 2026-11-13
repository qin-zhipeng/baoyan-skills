# -*- coding: utf-8 -*-
"""wx_bridge.py —— 微信数据桥：自动定位 → 自动取密钥 → 自动导出 → 自动分析。

对使用者只有一次动作：首次运行时点一次「同意并接入」（需要重启一次微信并在手机上
确认登录，这是微信自身机制，任何工具都无法绕过）。之后密钥长期缓存在本机，
每次点「更新」都是全自动：解密 → 导出 → 出名单 → 出归因报告 → 回填话术库。

边界（写死在代码里，不提供开关）：
  * 只读本机当前登录微信账号的数据，不碰他人账号、不碰网络
  * 密钥与聊天记录只写本机用户目录，绝不上传、绝不随包分发
  * 关闭 / 重启微信前必须有使用者的显式同意
  * 不修改微信任何文件，不注入后驻留
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import datetime
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_DIR = os.path.join(ROOT, "bin")
STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or
                         os.path.expanduser("~"), "微信辅助器")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
KEY_FILE = os.path.join(STATE_DIR, "key.txt")


# ---------------------------------------------------------------- 状态
def _default_state() -> dict:
    return {
        "consented": False,
        "consent_at": "",
        "wechat_exe": "",
        "data_root": "",
        "key_ready": False,
        "key_source": "",
        "last_export": "",
        "last_export_stat": {},
        "auto_export": True,
    }


def load_state() -> dict:
    s = _default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s.update(json.load(f) or {})
    except Exception:
        pass
    return s


def save_state(patch: dict) -> dict:
    s = load_state()
    s.update(patch or {})
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    return s


def has_key() -> bool:
    try:
        k = open(KEY_FILE, encoding="ascii", errors="ignore").read().strip()
        return bool(re.fullmatch(r"[0-9a-fA-F]{64}", k))
    except OSError:
        return False


def read_key() -> str:
    try:
        return open(KEY_FILE, encoding="ascii", errors="ignore").read().strip().lower()
    except OSError:
        return ""


def store_key(key_hex: str, source: str):
    key_hex = (key_hex or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", key_hex):
        raise ValueError("密钥格式不对")
    os.makedirs(STATE_DIR, exist_ok=True)
    # 密钥只落本机用户目录，且仅本人可读（Windows 下继承目录 ACL）
    with open(KEY_FILE, "w", encoding="ascii") as f:
        f.write(key_hex)
    save_state({"key_ready": True, "key_source": source,
                "consent_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


# ---------------------------------------------------------------- 自动定位
def find_wechat_exe() -> str:
    """找微信主程序：注册表 → 常见安装目录 → 正在运行的进程路径。"""
    # 1) 运行中的进程（最准，含自定义安装盘）
    try:
        import ctypes.wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                        ("th32ProcessID", w.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                        ("th32ParentProcessID", w.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
                        ("szExeFile", w.WCHAR * 260)]

        TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32 = 0x8, 0x10
        PROCESS_QUERY_INFORMATION, PROCESS_VM_READ = 0x400, 0x10

        class ME(ctypes.Structure):
            _fields_ = [("dwSize", w.DWORD), ("th32ModuleID", w.DWORD),
                        ("th32ProcessID", w.DWORD), ("GlblcntUsage", w.DWORD),
                        ("ProccntUsage", w.DWORD), ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
                        ("modBaseSize", w.DWORD), ("hModule", ctypes.c_void_p),
                        ("szModule", ctypes.c_char * 256), ("szExePath", ctypes.c_char * 260)]

        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        e = PE(); e.dwSize = ctypes.sizeof(e)
        pids = []
        if k32.Process32FirstW(snap, ctypes.byref(e)):
            while True:
                if e.szExeFile.lower() in ("weixin.exe", "wechat.exe"):
                    pids.append(e.th32ProcessID)
                if not k32.Process32NextW(snap, ctypes.byref(e)):
                    break
        k32.CloseHandle(snap)
        for pid in pids:
            h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
            if not h:
                continue
            try:
                ms = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
                me = ME(); me.dwSize = ctypes.sizeof(me)
                if k32.Module32FirstW(ms, ctypes.byref(me)):
                    p = me.szExePath.decode("gbk", "replace")
                    k32.CloseHandle(ms)
                    if os.path.isfile(p) and os.path.basename(p).lower() in (
                            "weixin.exe", "wechat.exe"):
                        return os.path.abspath(p)
                k32.CloseHandle(ms)
            except Exception:
                pass
            finally:
                k32.CloseHandle(h)
    except Exception:
        pass

    # 2) 注册表
    for hive, sub in (
            (getattr(__import__("winreg"), "HKEY_CURRENT_USER"),
             r"Software\Tencent\Weixin"),
            (getattr(__import__("winreg"), "HKEY_LOCAL_MACHINE"),
             r"SOFTWARE\Tencent\Weixin"),
            (getattr(__import__("winreg"), "HKEY_LOCAL_MACHINE"),
             r"SOFTWARE\WOW6432Node\Tencent\Weixin")):
        try:
            import winreg
            with winreg.OpenKey(hive, sub) as k:
                for name in ("InstallPath", "Path", "InstallDir", ""):
                    try:
                        v, _ = winreg.QueryValueEx(k, name)
                    except OSError:
                        continue
                    p = os.path.join(str(v), "Weixin.exe")
                    if os.path.isfile(p):
                        return os.path.abspath(p)
                    if os.path.isfile(str(v)) and str(v).lower().endswith(".exe"):
                        return os.path.abspath(str(v))
        except Exception:
            pass

    # 3) 常见目录
    home = os.path.expanduser("~")
    cands = []
    for base in (os.environ.get("PROGRAMFILES", "C:/Program Files"),
                 os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", ""), "C:", "D:"):
        if not base:
            continue
        for rel in (("Tencent", "Weixin", "Weixin.exe"),
                    ("Tencent", "WeChat", "WeChat.exe"),
                    ("Tencent", "Weixin", "WeChat.exe")):
            cands.append(os.path.join(base, *rel))
    cands.append(os.path.join(home, "AppData", "Local", "Tencent", "Weixin", "Weixin.exe"))
    for p in cands:
        if os.path.isfile(p):
            return os.path.abspath(p)
    return ""


def find_data_roots() -> list:
    """找微信数据根目录（含 wxid_xxx_xxxx 账号文件夹的那一层）。"""
    home = os.path.expanduser("~")
    cands = []
    for base in (home, os.path.join(home, "Documents"),
                 os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")):
        if not base:
            continue
        for name in ("xwechat_files", "WeChat Files", "WeChatFiles"):
            cands.append(os.path.join(base, name))
    cands.append(os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat_files"))
    # 其它盘符根目录也扫一遍（微信常被装到 D 盘并改数据目录）
    for letter in "DEFGH":
        cands.append(letter + ":/xwechat_files")
        cands.append(letter + ":/WeChat Files")
    out, seen = [], set()
    for p in cands:
        try:
            ap = os.path.abspath(p)
            if ap.lower() in seen or not os.path.isdir(ap):
                continue
            seen.add(ap.lower())
            if any(os.path.isdir(os.path.join(ap, n, "db_storage"))
                   for n in os.listdir(ap)):
                out.append(ap)
        except OSError:
            continue
    return out


def find_existing_keys() -> list:
    """自动搜本机已有密钥文件（别人之前导出过就完全无感，不用重启微信）。"""
    home = os.path.expanduser("~")
    out = []
    seen = set()

    def add(p):
        try:
            ap = os.path.abspath(p)
            if ap.lower() in seen or not os.path.isfile(ap):
                return
            seen.add(ap.lower())
            k = open(ap, encoding="ascii", errors="ignore").read().strip()
            if re.fullmatch(r"[0-9a-fA-F]{64}", k):
                out.append((ap, k.lower()))
        except OSError:
            pass

    # 明确的已知位置
    for p in (
            KEY_FILE,
            os.path.join(ROOT, "data", "key.txt"),
            os.path.join(home, ".workbuddy", "skills", "wechat-chat-export", "key.txt"),
            os.path.join(home, "Documents", "key.txt"),
            os.path.join(home, "Desktop", "key.txt"),
    ):
        add(p)

    # 已装过的微信导出工具，配置文件里通常直接写着 key_file 绝对路径
    cfg_candidates = [
        os.path.join(home, ".workbuddy", "skills", "wechat-chat-export", "config.json"),
        os.path.join(home, ".workbuddy", "skills", "baoyan-deal-chat", "config.json"),
        os.path.join(home, ".chatlog", "config.json"),
        os.path.join(home, "Documents", "chatlog", "config.json"),
    ]
    for c in cfg_candidates:
        try:
            with open(c, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            for field in ("key_file", "key_path", "db_key_file", "keyFile"):
                v = cfg.get(field)
                if isinstance(v, str) and v.strip():
                    add(v.strip())
        except Exception:
            pass

    # WeFlow / chatlog 等常见工具留下的组件目录
    for base in (os.environ.get("LOCALAPPDATA", ""),
                 os.environ.get("PROGRAMFILES", "")):
        if base:
            add(os.path.join(base, "Programs", "WeFlow", "resources", "resources",
                             "key", "win32", "x64", "key.txt"))

    # 有限深度扫描常见工作目录：只下钻名字像「密钥/微信/导出/工具」的目录，
    # 避免把整个用户盘翻一遍（慢，且容易撞上无关文件）
    keyish = ("key", "密钥", "wx", "wechat", "weixin", "chat", "export", "导出",
              "reply", "tool", "工具", "config")
    names = ("key.txt", "dbkey.txt", "wxkey.txt", "key.hex", "wechat_key.txt")
    for base, limit in ((os.path.join(home, "WorkBuddy"), 5),
                        (os.path.join(home, "Documents"), 3),
                        (os.path.join(home, "Desktop"), 3)):
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            depth = root[len(base):].count(os.sep)
            if depth >= limit:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs
                       if not d.startswith((".", "node_modules", "__pycache__",
                                           "AppData", "runtime", "site-packages"))
                       and (depth < 2 or any(k in d.lower() for k in keyish))]
            for n in names:
                if n in files:
                    add(os.path.join(root, n))
            if len(out) >= 8:
                break
        if len(out) >= 8:
            break
    return out


def probe_key_against_roots(key_hex: str, roots: list):
    """HMAC 校验：找出这把密钥能解锁的数据根目录 / 账号目录。"""
    from . import wx_db
    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        return None
    for root in roots:
        try:
            acct = wx_db.unlocked_account(key, root)
            if acct:
                return {"data_root": root, "account": acct}
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 密钥捕获
def _weixin_pids() -> list:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, encoding="gbk", errors="replace", timeout=10).stdout
        return [int(m.group(1)) for line in (out or "").splitlines()
                if "Weixin.exe" in line
                for m in [re.search(r'"(\d+)"', line)] if m]
    except Exception:
        return []


def _load_hook_api():
    dll = os.path.join(BIN_DIR, "wx_key.dll")
    if not os.path.isfile(dll):
        raise RuntimeError("缺少密钥捕获组件 bin/wx_key.dll")
    lib = ctypes.CDLL(dll)
    api = {"_lib": lib}
    try:
        api["InitializeHook"] = lib.InitializeHook
        api["InitializeHook"].argtypes = [w.DWORD]
        api["InitializeHook"].restype = ctypes.c_bool
        api["PollKeyData"] = lib.PollKeyData
        api["PollKeyData"].argtypes = [ctypes.c_char_p, ctypes.c_int]
        api["PollKeyData"].restype = ctypes.c_bool
        api["GetStatusMessage"] = lib.GetStatusMessage
        api["GetStatusMessage"].argtypes = [ctypes.c_char_p, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_int)]
        api["GetStatusMessage"].restype = ctypes.c_bool
        api["CleanupHook"] = lib.CleanupHook
        api["CleanupHook"].restype = ctypes.c_bool
        lib.GetLastErrorMsg.restype = ctypes.c_char_p
    except AttributeError as e:
        raise RuntimeError("密钥捕获组件接口不匹配：%s" % e)
    return api


def capture_key(wechat_exe: str, poll_seconds: int = 240, log=None) -> str:
    """一次性的密钥捕获：关闭微信 → 挂 Hook → 拉起微信 → 等用户在手机上确认登录。

    !! 调用前必须已经取得使用者显式同意（会关闭并重启微信）。
    返回 64 位 hex 密钥；失败抛异常。绝不下载任何东西，组件随包自带。
    """
    def say(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    if not wechat_exe or not os.path.isfile(wechat_exe):
        raise RuntimeError("找不到微信主程序，请手动指定 Weixin.exe 路径")
    api = _load_hook_api()

    existing = _weixin_pids()
    if existing:
        say("正在关闭微信（已获得同意）…")
        subprocess.run(["taskkill", "/IM", "Weixin.exe", "/F", "/T"],
                       check=False, capture_output=True)
        end = time.time() + 25
        while time.time() < end and _weixin_pids():
            time.sleep(0.5)
        if _weixin_pids():
            raise RuntimeError("微信没能正常关闭，请手动关闭微信后重试")

    say("正在重新启动微信，请在手机上点「确认登录」…")
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    try:
        subprocess.Popen([wechat_exe],
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
    except Exception:
        subprocess.Popen([wechat_exe])

    # 微信 4.x 会先起引导进程再切主进程，等进程列表稳定再注入
    pids, last, stable_since = [], None, None
    deadline = time.time() + 90
    while time.time() < deadline:
        cur = _weixin_pids()
        if cur:
            if set(cur) == set(last or []):
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= 3:
                    pids = cur
                    break
            else:
                stable_since = None
            last = cur
        time.sleep(0.5)
    if not pids:
        raise RuntimeError("等待微信启动超时，请重试")

    ok = False
    for cand in pids:
        if api["InitializeHook"](cand):
            ok = True
            say("密钥监听已就绪，请在手机上完成登录确认…")
            break
    if not ok:
        raise RuntimeError("密钥监听启动失败（可能是微信版本更新或安全软件拦截）")

    buf = ctypes.create_string_buffer(128)
    start = time.time()
    try:
        while time.time() - start < poll_seconds:
            if api["PollKeyData"](buf, len(buf)):
                k = buf.value.decode("ascii", "ignore").strip().lower()
                if re.fullmatch(r"[0-9a-f]{64}", k):
                    say("密钥已获取并保存在本机")
                    return k
            status = ctypes.create_string_buffer(1024)
            lvl = ctypes.c_int(0)
            try:
                while api["GetStatusMessage"](status, len(status), ctypes.byref(lvl)):
                    m = status.value.decode("utf-8", "replace").strip()
                    if m:
                        say(m)
            except Exception:
                pass
            time.sleep(0.2)
    finally:
        try:
            api["CleanupHook"]()
        except Exception:
            pass
    raise RuntimeError("等待登录确认超时。请关闭微信后重试一次（先让程序就绪再登录）")
