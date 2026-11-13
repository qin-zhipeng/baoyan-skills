# -*- coding: utf-8 -*-
"""auto_pilot.py —— 一键全自动编排：定位 → 取密钥 → 解密 → 导出 → 名单 → 归因。

首次运行：只需一次「同意并接入」（微信要求重启 + 手机确认登录，无法绕过）。
之后每次：点一下「一键更新」，或直接启动程序，全部自动跑完，无需任何配置。

所有步骤在后台线程执行，通过 Qt 信号回传进度，不卡界面。
"""
from __future__ import annotations

import datetime
import json
import os
import threading

from PySide6.QtCore import QObject, Signal

from . import wx_bridge, wx_db, deal_attribution, chat_radar, deal_signals as DS

TZ = datetime.timezone(datetime.timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT_DIR = os.path.join(ROOT, "data", "聊天记录")
OUT_DIR = os.path.join(ROOT, "输出")
CONFIG_FILE = os.path.join(ROOT, "data", "radar_config.json")


def chat_paths() -> list:
    out = []
    p = os.path.join(CHAT_DIR, "all_messages.jsonl")
    if os.path.isfile(p):
        out.append(p)
    # 已有的外部导出数据也一并纳入（只是合并，不动原文件）
    try:
        cfg = json.load(open(CONFIG_FILE, encoding="utf-8-sig"))
        for d in (cfg.get("datasets") or []):
            q = os.path.join(str(d), "all_messages.jsonl")
            if os.path.isfile(q) and os.path.abspath(q) != os.path.abspath(p):
                out.append(q)
    except Exception:
        pass
    return out


def _extra_meta() -> tuple[list, list]:
    try:
        cfg = json.load(open(CONFIG_FILE, encoding="utf-8-sig"))
        return (cfg.get("deal_wxids") or [], cfg.get("deal_names") or [])
    except Exception:
        return [], []


class AutoPilot(QObject):
    """全自动流水线。"""

    progress = Signal(str, int, int)          # 阶段说明, 已完成, 总数
    need_consent = Signal(str)                # 需要一次人工授权（首次捕获密钥）
    done = Signal(bool, str, dict)            # 成功?, 说明, 摘要

    STAGES = ("检查环境", "定位微信数据", "准备密钥", "解密导出聊天记录",
              "分析客户名单", "成交话术归因", "生成名单与话术库")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False

    def is_running(self):
        return self._busy

    # ------------------------------------------------------------ 入口
    def start(self, allow_capture: bool = True, consented: bool = False):
        """启动全流程。allow_capture=False 时缺密钥直接报错，不打扰用户。"""
        if self._busy:
            return False
        self._busy = True
        threading.Thread(target=self._run, args=(allow_capture, consented),
                         daemon=True).start()
        return True

    def _emit(self, idx: int, detail: str = "", done: int = 0, total: int = 0):
        stage = self.STAGES[min(idx, len(self.STAGES) - 1)]
        text = ("%s · %s" % (stage, detail)) if detail else stage
        self.progress.emit(text, done, total)

    def _fail(self, msg: str):
        self._busy = False
        self.done.emit(False, msg, {})

    # ------------------------------------------------------------ 主流程
    def _run(self, allow_capture: bool, consented: bool):
        try:
            # ① 环境
            self._emit(0, "检查解密组件", 1, 7)
            ok, msg = wx_db.deps_ok()
            if not ok:
                self._fail(msg + "（请重新运行「一键安装.bat」）")
                return
            state = wx_bridge.load_state()
            os.makedirs(CHAT_DIR, exist_ok=True)
            os.makedirs(OUT_DIR, exist_ok=True)

            # ② 定位微信与数据目录
            self._emit(1, "自动定位", 2, 7)
            exe = state.get("wechat_exe") or wx_bridge.find_wechat_exe()
            roots = wx_bridge.find_data_roots()
            if state.get("data_root") and os.path.isdir(state["data_root"]):
                roots = [state["data_root"]] + [r for r in roots
                                               if r != state["data_root"]]
            if not roots:
                self._fail("没找到微信数据目录。请确认电脑版微信已登录过，"
                           "且聊天记录保存在本机。")
                return
            wx_bridge.save_state({"wechat_exe": exe, "data_root": roots[0]})

            # ③ 密钥：优先本机已有 → 校验 → 否则申请一次授权
            self._emit(2, "检查本机密钥", 3, 7)
            key = ""
            found = wx_bridge.find_existing_keys()
            for p, k in found:
                if wx_bridge.probe_key_against_roots(k, roots):
                    key = k
                    if not wx_bridge.has_key():
                        wx_bridge.store_key(k, "本机已有密钥，自动复用")
                    break
            if not key and wx_bridge.has_key():
                k = wx_bridge.read_key()
                if wx_bridge.probe_key_against_roots(k, roots):
                    key = k

            if not key:
                if not (allow_capture and consented):
                    self._busy = False
                    self.need_consent.emit(
                        "首次接入需要重启一次微信并在手机上确认登录，"
                        "这是微信自身的安全机制（密钥只在登录瞬间产生）。"
                        "确认后密钥会缓存在本机，以后全自动、不用再操作。")
                    return
                self._emit(2, "等待手机确认登录", 3, 7)
                if not exe:
                    self._fail("找不到微信主程序 Weixin.exe，请在设置里手动指定。")
                    return
                key = wx_bridge.capture_key(exe, log=lambda m: self._emit(2, m, 3, 7))
                wx_bridge.store_key(key, "本机登录时自动捕获")
                roots = wx_bridge.find_data_roots() or roots
                if not wx_bridge.probe_key_against_roots(key, roots):
                    self._fail("密钥拿到了，但解不开本机聊天库。"
                               "请确认微信已完成登录、且数据目录正确。")
                    return

            # ④ 解密导出
            self._emit(3, "正在解密本机聊天库", 4, 7)
            root = roots[0]
            stat = wx_db.export_all(
                key, root, CHAT_DIR, since_days=365,
                kinds=("单聊",),
                progress=lambda stage, d, t: self._emit(3, stage, 4, 7))
            if not stat.get("messages"):
                self._fail("解密成功但没读到消息。请确认微信里已有聊天记录。")
                return
            wx_bridge.save_state({
                "key_ready": True,
                "last_export": datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "last_export_stat": {"chats": stat["chats"], "messages": stat["messages"]},
            })
            # 读刚导出的联系人簿（备注名 / 昵称 / 本机标签名），供成交识别使用
            DS.contact_book(refresh=True)

            # ⑤ 客户名单
            self._emit(4, "今日跟进 TOP / 潜在意向", 5, 7)
            paths = chat_paths()
            follow = chat_radar.analyze(paths, mode="follow", top=30)
            intent = chat_radar.analyze(paths, mode="intent", top=30, min_score=8)
            chat_radar.export_csv(follow, os.path.join(OUT_DIR, "今日跟进名单.csv"))
            chat_radar.export_csv(intent, os.path.join(OUT_DIR, "潜在意向名单.csv"))
            chat_radar.export_names(follow, os.path.join(ROOT, "data", "打标签名单"),
                                    "今日跟进")
            chat_radar.export_names(intent, os.path.join(ROOT, "data", "打标签名单"),
                                    "潜在意向")

            # ⑥ 成交话术归因
            self._emit(5, "正在做成交归因", 6, 7)
            dw, dn = _extra_meta()
            judges = {}
            summary = deal_attribution.analyze(
                paths, OUT_DIR, deal_wxids=dw, deal_names=dn,
                progress=lambda m: self._emit(5, m, 6, 7),
                judges_out=judges)

            # ⑥b 成交客户核对清单 + 从本机备注/标签里学到的新标识建议
            scan = DS.scan(judges)
            review_path = DS.export_review(
                judges, os.path.join(OUT_DIR, "成交客户核对清单.csv"))
            suggestions = DS.recommend(top=30)
            DS.export_suggestions(
                suggestions, os.path.join(OUT_DIR, "成交标识建议.csv"))
            with open(os.path.join(ROOT, "data", "deal_signals_state.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"scan": scan, "suggestions": suggestions,
                           "updated": datetime.datetime.now(TZ).strftime(
                               "%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=2)

            # ⑦ 话术库落盘（供一键回填常用语）
            scripts = deal_attribution.scripts_for_quickstore(summary)
            with open(os.path.join(ROOT, "data", "归因话术.json"), "w",
                      encoding="utf-8") as f:
                json.dump(scripts, f, ensure_ascii=False, indent=2)
            self._emit(6, "全部完成", 7, 7)

            result = {
                "account": stat.get("account", ""),
                "chats": stat["chats"], "messages": stat["messages"],
                "follow": len(follow), "intent": len(intent),
                "deals": summary["deals"], "others": summary["others"],
                "median_cycle": summary.get("median_cycle"),
                "top_scripts": summary["hot_scripts"],
                "report": summary["report"], "csv": summary["csv"],
                "csv_follow": os.path.join(OUT_DIR, "今日跟进名单.csv"),
                "scripts": len(scripts),
                "deal_review": review_path, "deal_levels": scan["levels"],
                "deal_suspect": scan["suspect"], "deal_conflict": scan["conflict"],
                "suggestions": len(suggestions),
                "labels_all": len(scan["labels_all"]),
            }
            self._busy = False
            self.done.emit(True, "已完成：%d 个会话 / %d 条消息，"
                           "跟进 %d 人、意向 %d 人、识别成交 %d 单"
                           "（另有 %d 人疑似成交待核对）" % (
                               stat["chats"], stat["messages"], len(follow),
                               len(intent), summary["deals"], scan["suspect"]), result)
        except Exception as exc:
            self._fail("%s：%s" % (type(exc).__name__, exc))
