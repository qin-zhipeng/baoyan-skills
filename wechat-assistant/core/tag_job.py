# -*- coding: utf-8 -*-
"""tag_job.py —— 批量打标签：把 11-10 已真机验证的「添加成员弹窗批量勾选」封装成 Qt 任务。

界面只需要：选名单 → 检测弹窗 → 开始 → 看进度 → 自己点【完成】。
脚本只负责"搜索 + 勾选"，绝不点【完成】/【取消】，不改微信数据库。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal

from . import tag_engine as t
from .config import ROOT

DEFAULT_CFG = {
    "window_title_keyword": "添加成员",
    "base_size": [770, 610],
    "bg_sample": [60, 500],
    "bg_samples": [[60, 500], [340, 300], [340, 450], [200, 250], [300, 110]],
    "search_bg_sample": [250, 60],
    "points": {
        "search_box": [180, 60], "result_row": [150, 148], "idle_mouse": [200, 500],
    },
    "regions": {
        "result_text": [80, 125, 330, 172], "checkbox": [44, 132, 72, 164],
        "clear_button": [297, 49, 315, 71], "search_text": [72, 51, 290, 71],
        "second_row": [80, 178, 330, 225], "count": [610, 45, 740, 80],
        "shot": [35, 118, 340, 235], "done_button": [421, 530, 533, 563],
    },
    "thresholds": {
        "result_pixels": 600, "green_pixels": 80, "count_changed_pixels": 20,
        "search_filled_pixels": 25, "search_text_pixels": 15,
        "search_icon_strong_pixels": 100, "second_row_pixels": 120,
    },
    "timing": {"search_wait": 0.9, "click_wait": 0.45, "jitter": 0.35,
               "batch_size": 30, "batch_pause": 6},
    "variants": {"nospace": True, "short_prefix": False, "short_len": 10},
    "exclude_names": [], "exclude_keywords": [],
}


def scaled_cfg(w: int, h: int, speed: str = "normal") -> dict:
    """按当前弹窗尺寸缩放坐标与像素阈值（换分辨率/换电脑不用重新标定）。"""
    sx, sy = w / 770.0, h / 610.0
    c = json.loads(json.dumps(DEFAULT_CFG))
    c["points"] = {k: [round(v[0] * sx, 1), round(v[1] * sy, 1)]
                   for k, v in c["points"].items()}
    c["regions"] = {k: [round(v[0] * sx, 1), round(v[1] * sy, 1),
                        round(v[2] * sx, 1), round(v[3] * sy, 1)]
                    for k, v in c["regions"].items()}
    dx = max(-80, min(160, w - 770))
    c["regions"]["clear_button"] = [297 + dx, round(49 * sy, 1),
                                    318 + dx, round(71 * sy, 1)]
    for k in ("bg_sample", "search_bg_sample"):
        c[k] = [int(c[k][0] * sx), int(c[k][1] * sy)]
    c["bg_samples"] = [[int(p[0] * sx), int(p[1] * sy)] for p in c["bg_samples"]]
    area = sx * sy
    c["thresholds"] = {k: max(5, int(v * area)) for k, v in c["thresholds"].items()}
    c["base_size"] = [w, h]
    if speed == "slow":
        c["timing"].update(search_wait=1.3, click_wait=0.65, jitter=0.5,
                           batch_size=20, batch_pause=10)
    return c


class TagJob(QObject):
    log = Signal(str)
    progress = Signal(int, int, dict)
    finished = Signal(bool, str)
    running_changed = Signal(bool)

    def __init__(self, out_dir: str, parent=None):
        super().__init__(parent)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.shots = self.out_dir / "shots"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.review = self.out_dir / "复核.html"
        self.manual = self.out_dir / "待人工处理.txt"
        t.LOG_DIR = self.out_dir
        t.SHOT_DIR = self.shots
        t.RESULT_CSV = self.out_dir / "result.csv"
        t.PROGRESS_JSON = self.out_dir / "progress.json"
        t.REVIEW_HTML = self.review
        t.CHECK_PNG = self.out_dir / "check_regions.png"
        self._stop = False
        self._running = False
        self.clip_backup = ""

    # ------------------------------------------------------------ 检测
    def check(self) -> dict:
        hwnd, rect = t.find_target(DEFAULT_CFG["window_title_keyword"])
        if not hwnd:
            return {"ok": False,
                    "msg": "没找到弹窗：微信 → 通讯录管理 → 右键标签 → 添加成员，保持弹窗打开"}
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        ratio = w / 770.0
        tip = "" if 0.85 <= ratio <= 1.18 else "（窗口尺寸偏差较大，建议把弹窗拉回默认大小）"
        return {"ok": True, "w": w, "h": h,
                "msg": f"弹窗已找到 {w}×{h}，坐标自动适配{tip}"}

    # ------------------------------------------------------------ 控制
    def is_running(self):
        return self._running

    def stop(self):
        self._stop = True
        self.log.emit("已收到停止：当前这个名字跑完就停，进度已保存")

    def clear_progress(self):
        try:
            t.PROGRESS_JSON.unlink(missing_ok=True)
            t.RESULT_CSV.unlink(missing_ok=True)
            self.log.emit("进度已清空，下次从头开始")
        except Exception as exc:
            self.log.emit(f"清空失败：{exc}")

    def start(self, opts: dict):
        if self._running:
            self.log.emit("正在运行中")
            return
        self._stop = False
        self._running = True
        self.running_changed.emit(True)
        threading.Thread(target=self._worker, args=(dict(opts),), daemon=True).start()

    # ------------------------------------------------------------ 主循环
    def _worker(self, opts: dict):
        ok = False
        msg = ""
        try:
            raw = str(opts.get("path") or "").strip().strip('"')
            path = Path(raw)
            if not path.is_file():
                raise ValueError("请先选一个名单文件（.csv / .txt / .md / .xlsx）")
            hwnd, rect = t.find_target(DEFAULT_CFG["window_title_keyword"])
            if not hwnd:
                raise ValueError("没找到「添加成员」弹窗，请先在微信里打开")

            w, h = rect[2] - rect[0], rect[3] - rect[1]
            cfg = scaled_cfg(w, h, opts.get("speed", "normal"))
            alias_csv = Path(ROOT) / "alias.csv"
            cfg["_alias"] = t.load_alias(alias_csv) if alias_csv.exists() else {}

            items = t.load_names(path)
            if not items:
                raise ValueError("名单里没解析出名字：表头请包含「备注 / 昵称 / 姓名」之类列名")
            done = t.load_progress(bool(opts.get("fresh")))
            todo = [it for it in items if it["name"] not in done]
            limit = int(opts.get("limit", 0) or 0)
            if limit > 0:
                todo = todo[:limit]
            if not todo:
                raise ValueError("这批名字都跑过了；勾「清空进度重跑」再来一次")

            self.log.emit(f"名单 {path.name}：共 {len(items)} 人，本次处理 {len(todo)} 人")
            self.log.emit("开始自动勾选。按 F12 或把鼠标甩到屏幕左上角可随时停")
            self.progress.emit(0, len(todo), {})
            if opts.get("input_mode", "clipboard") == "clipboard":
                self.clip_backup = t.get_clipboard_text()

            args = SimpleNamespace(input_mode=opts.get("input_mode", "clipboard"),
                                   multi_ok=bool(opts.get("multi_ok")))
            records, stats = [], Counter()
            tm = cfg["timing"]
            for idx, it in enumerate(todo, 1):
                if self._stop or t.stop_signal():
                    self.log.emit("已停止，进度已保存")
                    break
                if not t.ensure_front(hwnd):
                    self.log.emit("微信弹窗长时间抢不到前台，已停止")
                    break
                name = it["name"]
                if t.is_excluded(name, cfg):
                    status, word, shot, note = "已排除", "", "", "命中排除名单"
                else:
                    status, word, shot, note = t.process_one(
                        hwnd, t.get_rect(hwnd), name, cfg, args, idx)
                rec = {"time": datetime.now().strftime("%H:%M:%S"), "idx": idx,
                       "status": status, "name": name, "word": word,
                       "shot": shot, "note": note}
                t.append_result(rec)
                records.append(rec)
                stats[status] += 1
                done.add(name)
                t.save_progress(done)
                self.log.emit(f"[{idx}/{len(todo)}] {status}  {name}  {note}")
                self.progress.emit(idx, len(todo), dict(stats))
                if status == "已中断":
                    break
                if idx % int(tm["batch_size"]) == 0 and idx < len(todo):
                    self.log.emit(f"已处理 {idx} 人，歇 {tm['batch_pause']} 秒")
                    for _ in range(int(tm["batch_pause"]) * 5):
                        if self._stop:
                            break
                        time.sleep(0.2)

            t.build_review(records, stats)
            manual = [r["name"] for r in records
                      if r["status"] in ("未命中", "待复核", "已排除")
                      or "多条结果" in r["note"]]
            self.manual.write_text("\n".join(manual) + ("\n" if manual else ""),
                                   encoding="utf-8")
            self.log.emit("本次结果：" + "  ".join(f"{k}={v}" for k, v in stats.items()))
            self.log.emit("请到微信弹窗右侧核对名单，确认后自己点【完成】；程序不会替你提交")
            ok, msg = True, "批量勾选完成，请回微信核对并点【完成】"
        except SystemExit as exc:
            msg = str(exc)
            self.log.emit(msg)
        except Exception as exc:
            msg = str(exc)
            self.log.emit(f"出错了：{msg}")
        finally:
            if self.clip_backup:
                try:
                    t.set_clipboard_text(self.clip_backup)
                except Exception:
                    pass
                self.clip_backup = ""
            self._running = False
            self.running_changed.emit(False)
            self.finished.emit(ok, msg)
