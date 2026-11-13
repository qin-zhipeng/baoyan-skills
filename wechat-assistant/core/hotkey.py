# -*- coding: utf-8 -*-
"""hotkey.py —— 全局热键（RegisterHotKey + 独立消息泵线程）。

支持：
  * 单组合热键：Ctrl+Shift+F9 / Alt+1 / F12 / Win+Q
  * 顺序组合热键：先 Ctrl+A 再 Ctrl+B（同类工具的"混合触发"），800ms 内有效
  * 运行时动态改键、卸载，不需要重启
"""
from __future__ import annotations

import queue
import threading
import time

from PySide6.QtCore import QObject, Signal

from . import win32util as w


class HotkeyManager(QObject):
    fired = Signal(str)          # action_id 或 "seq:<id>"
    error = Signal(str)

    SEQ_WINDOW = 1.2             # 序列热键的有效时间窗（秒）
    STANDALONE_DELAY = 0.45     # 序列前缀同时绑定了单键动作时的等待时间

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cmd: queue.Queue = queue.Queue()
        self._thread = None
        self._running = False
        self._next_id = 1
        self._registered = {}    # hotkey_id -> (combo, action_id)
        self._action_by_combo = {}  # combo -> action_id
        self._by_action = {}     # action_id -> combo
        self._sequences = []     # [{"id","keys":[combo,...],"action"}]
        self._seq_prefixes = set()
        self._recent = []        # [(combo_normalized, ts)]
        self._pending = []       # [(action_id, expire_ts)] 延迟发出的单键动作
        self._callbacks = {}     # action_id -> callable（插件注册的热键）
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 生命周期
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="HotkeyPump", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._cmd.put(("stop", None))
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    # ------------------------------------------------------------ 配置
    def rebind(self, bindings: dict):
        """bindings: {action_id: "Ctrl+Shift+Space", ...}"""
        with self._lock:
            wanted = {a: c for a, c in (bindings or {}).items() if c and w.parse_hotkey(c)[1]}
            invalid = [f"{a}={c}" for a, c in (bindings or {}).items()
                       if c and not w.parse_hotkey(c)[1]]
            self._by_action = wanted
        if invalid:
            self.error.emit("无法识别的热键：" + ", ".join(invalid))
        with self._lock:
            merged = dict(wanted)
            for aid, (c, _) in self._callbacks.items():
                merged.setdefault(aid, c)
        self._cmd.put(("sync", merged))

    def set_sequences(self, sequences: list):
        seqs = []
        prefixes = set()
        for s in sequences or []:
            keys = [w.normalize_hotkey(k) for k in s.get("keys", [])]
            keys = [k for k in keys if k]
            if len(keys) >= 2:
                seqs.append({"id": s.get("id", ""), "keys": keys,
                             "action": s.get("action", "")})
                prefixes.add(keys[0])
        with self._lock:
            self._sequences = seqs
            self._seq_prefixes = prefixes
            combos = set(self._by_action.values())
            combos |= {k for s in seqs for k in s["keys"]}
        self._cmd.put(("sync_combos", combos))

    def register_callback(self, action_id: str, combo: str, callback):
        """插件用：直接把一个热键绑到 python 回调。"""
        if not w.parse_hotkey(combo)[1]:
            return False
        with self._lock:
            self._callbacks[action_id] = (combo, callback)
            extra = dict(self._by_action)
            for aid, (c, _) in self._callbacks.items():
                extra.setdefault(aid, c)
        self._cmd.put(("sync", extra))
        return True

    # ------------------------------------------------------------ 消息泵
    def _loop(self):
        msg = w.MSG()
        while self._running:
            try:
                while True:
                    cmd, payload = self._cmd.get_nowait()
                    if cmd == "stop":
                        self._unregister_all()
                        return
                    elif cmd == "sync":
                        self._sync(payload)
                    elif cmd == "sync_combos":
                        self._sync_combos(payload)
            except queue.Empty:
                pass

            while w.user32.PeekMessageW(w.C.byref(msg), None, 0, 0, w.PM_REMOVE):
                if msg.message == w.WM_HOTKEY:
                    combo = self._registered.get(int(msg.wParam), (None, None))[0]
                    if combo:
                        self._on_hotkey(combo)
                w.user32.TranslateMessage(w.C.byref(msg))
                w.user32.DispatchMessageW(w.C.byref(msg))

            self._flush_pending()
            time.sleep(0.02)

    def _unregister_all(self):
        for hid in list(self._registered):
            w.user32.UnregisterHotKey(None, hid)
        self._registered.clear()
        self._action_by_combo.clear()

    def _sync(self, by_action: dict):
        """按 action_id 重建注册表（同一 action 只保留最新组合键）。"""
        self._unregister_all()
        for action_id, combo in by_action.items():
            self._add(combo, action_id)

    def _sync_combos(self, combos: set):
        """额外注册序列热键里出现的组合，避免漏掉未绑定单键动作的前缀。"""
        existing = {c for c, _ in self._registered.values()}
        for combo in combos:
            if combo not in existing and w.parse_hotkey(combo)[1]:
                self._add(combo, "")
            else:
                # 组合已存在但 action 为空，补上行
                pass

    def _add(self, combo: str, action_id: str):
        mods, vk = w.parse_hotkey(combo)
        if not vk:
            return
        # 同一个物理组合键只注册一次，避免 "Ctrl+Shift+Space" 与规范化写法重复
        for _, (c, a) in self._registered.items():
            m2, v2 = w.parse_hotkey(c)
            if v2 == vk and (m2 & ~w.MOD_NOREPEAT) == (mods & ~w.MOD_NOREPEAT):
                if action_id and not a:
                    self._action_by_combo[combo] = action_id
                return
        hid = self._next_id
        self._next_id += 1
        if w.user32.RegisterHotKey(None, hid, mods, vk):
            self._registered[hid] = (combo, action_id)
            self._action_by_combo[combo] = action_id
        else:
            self.error.emit(f"热键注册失败（可能被其它软件占用）：{combo}")

    # ------------------------------------------------------------ 触发
    def _on_hotkey(self, combo: str):
        now = time.time()
        key = w.normalize_hotkey(combo)
        if not key:
            key = combo
        with self._lock:
            self._recent = [(k, t) for k, t in self._recent if now - t < self.SEQ_WINDOW]
            self._recent.append((key, now))
            recent_keys = [k for k, _ in self._recent]
            sequences = list(self._sequences)
            prefixes = set(self._seq_prefixes)
            action = self._action_by_combo.get(combo, "") or self._by_action.get(combo, "")
            callbacks = dict(self._callbacks)

        matched = None
        for seq in sequences:
            n = len(seq["keys"])
            if len(recent_keys) >= n and recent_keys[-n:] == seq["keys"]:
                matched = seq
                break
        if matched:
            self._recent.clear()
            self._pending.clear()
            self._emit("seq:" + matched["id"], matched.get("action", ""), callbacks)
            return

        if key in prefixes:
            if action:
                self._pending.append((action, now + self.STANDALONE_DELAY))
            return

        self._emit(action, action, callbacks)

    def _flush_pending(self):
        now = time.time()
        with self._lock:
            due = [a for a, t in self._pending if t <= now]
            self._pending = [(a, t) for a, t in self._pending if t > now]
            callbacks = dict(self._callbacks)
        for action in due:
            if action:
                self._emit(action, action, callbacks)

    def _emit(self, signal_value: str, action: str, callbacks: dict):
        if action in callbacks:
            try:
                callbacks[action][1]()
            except Exception as exc:
                self.error.emit(f"插件热键回调出错：{exc}")
        if signal_value and signal_value != "seq:":
            self.fired.emit(signal_value)
