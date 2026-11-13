# -*- coding: utf-8 -*-
"""chat_radar.py —— 客户名单雷达。

做的事：
  1. 读取本机已导出的聊天记录（jsonl / csv / tsv，路径在 data/radar_config.json 里配）
  2. 出「今日跟进 TOP」名单：只留未成交客户，按 待回复 + 跟进节奏 + 风险/意向 + 阶段 排序
  3. 筛「潜在意向客户」名单：命中成交信号、未成交、未放弃
  4. 把名单导出成打标签能直接吃的 txt / 可查的 csv

设计约束：
  * 只读本机文件，零第三方依赖（纯标准库），不碰微信数据库、不联网、不解密
  * 打分词典与口径集中放在本文件顶部，按自己的业务改词即可
  * 分析在后台线程跑，大文件也不卡界面
"""
from __future__ import annotations

import collections
import csv
import datetime
import json
import os
import re
import threading

from PySide6.QtCore import QObject, Signal

from . import deal_signals as DS

TZ = datetime.timezone(datetime.timedelta(hours=8))

# ---------------------------------------------------------------- 打分词典
# 打分词典：权重越高说明越接近成交；按自己的业务改这里
SIGNALS = [
    ("要合同/问签约", 5, ["合同", "协议", "e签", "签合同", "怎么签", "甲方", "身份证", "实名", "签署"]),
    ("问退费/保障", 5, ["退费", "退款", "不成功", "没上岸", "保障", "保底", "风险", "承诺"]),
    ("问价格/优惠", 5, ["优惠", "便宜", "多少钱", "费用", "价格", "报价", "分期", "贵", "预算"]),
    ("问名额/开课", 4, ["名额", "开课", "什么时候开始", "什么时候开", "还有位置", "排课", "开班"]),
    ("问师资/课时", 4, ["师资", "导师", "课时", "助教", "教授", "老师是", "谁带", "上课"]),
    ("聊学情/目标", 3, ["排名", "绩点", "专业人数", "保研率", "目标院校", "想去", "保研", "夏令营"]),
    ("对比竞品", 3, ["对比", "别家", "其他机构", "哪家", "另一家"]),
    ("提决策人", 3, ["孩子", "家长", "妈妈", "爸爸", "商量", "舅舅", "阿姨", "家里"]),
]

RISKS = [
    ("退费/投诉", 3, ["退费", "退款", "投诉", "不满意", "差评", "曝光", "维权", "举报"]),
    ("明确放弃", 3, ["不考虑", "算了", "不报了", "取消", "不继续", "放弃", "别联系"]),
    ("竞品比价", 2, ["对比", "别家", "其他机构", "哪家"]),
    ("价格受阻", 2, ["太贵", "贵", "预算不够", "负担", "便宜点", "优惠多少"]),
    ("决策拖延", 1, ["考虑一下", "考虑考虑", "再看看", "商量一下", "过几天", "回头说", "不急"]),
]

# 由晚到早，命中即停（用最近窗口判断当前位置）
STAGES = [
    ("S6 签约交付", ["甲方信息", "身份证", "e签", "签署", "合同链接", "对公", "账户", "转账", "定金", "付款", "缴费"]),
    ("S5 逼单", ["名额", "最后一批", "截止", "月底", "抓紧", "定下来", "敲定", "锁定", "今天定"]),
    ("S4 破异议", ["优惠", "太贵", "有点贵", "对比", "别家", "退费", "保障", "成功率", "考虑一下", "商量"]),
    ("S3 方案报价", ["价格", "费用", "课时", "方案", "多少钱", "报价", "分期", "项目", "课时数"]),
    ("S2 诊断定档", ["保研政策", "竞赛", "绩点", "排名", "目标院校", "论文", "科研", "夏令营", "推免"]),
    ("S1 破冰建档", ["信息采集", "哪年入学", "本科院校", "自我介绍", "规划师", "基本情况"]),
]
STAGE_RANK = {"S0": 0, "S1": 1, "S2": 2, "S3": 3, "S4": 4, "S5": 5, "S6": 6}

BOT_KW = ["clawbot", "bot", "助手", "服务号", "客服", "小助手", "公众号", "订阅号",
          "workbuddy", "元宝", "deepseek", "kimi", "豆包", "chatgpt", "claude",
          "微信团队", "文件传输", "腾讯新闻"]
PEER_KW = ["升学规划", "生涯规划", "高考规划", "志愿规划", "留学机构"]
# 渠道 / 同行 / 同事标签：分发包内一律留空，请填自己的。
# 写在 data/radar_config.json 的 channel_labels 里，例：["【同事A】", "【渠道B】"]
CHANNEL_LABELS = set()
INTERNAL_KW = ["师资群", "客诉", "加急", "接手", "教研费", "教务教研", "内部加红",
               "和你说下", "咱们内部", "我拉个群", "交付同学", "排课表",
               "来源：", "已转", "能转走", "跟进进度", "抖音私信"]
DEAL_KW = ["已成交", "报名成功", "已付款", "定金已付", "合同已签", "已签合同",
           "缴费成功", "已缴费", "已报名", "款已收到"]

STAGE_ACTION = {
    "S0 新线索/未分类": "先破冰建档，问清年级/院校/目标",
    "S1 破冰建档": "补学情信息，给一份初步规划",
    "S2 诊断定档": "出定档结论，把方案摆出来",
    "S3 方案报价": "讲清权益与交付，别只报数字",
    "S4 破异议": "针对顾虑给证据（案例/保障）",
    "S5 逼单": "给明确时间点与名额，推动今天定",
    "S6 签约交付": "发合同/收款信息，别再反复推销",
}

MAX_BYTES = 120 * 1024 * 1024      # 单个数据文件保护上限
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RADAR_CFG = os.path.join(_ROOT, "data", "radar_config.json")

_META_CACHE = None


def _cfg() -> dict:
    """读包内 data/radar_config.json；文件不存在或写坏了都当空配置。"""
    try:
        with open(RADAR_CFG, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_meta() -> dict:
    """读可选的成交 / 退费 / 内部名单，用来剔除自己人、认出老客户。

    全部来自 data/radar_config.json，缺了也不影响主流程（退化为纯聊天文本判断）。
    """
    global _META_CACHE, CHANNEL_LABELS
    if _META_CACHE is not None:
        return _META_CACHE
    cfg = _cfg()
    meta = {"contacts": {}, "deal": set(), "refunded": set(), "internal": set()}
    for key, target in (("deal_wxids", "deal"),
                        ("refunded_wxids", "refunded"),
                        ("internal_names", "internal")):
        meta[target].update(str(x).strip() for x in (cfg.get(key) or [])
                           if str(x).strip())
    CHANNEL_LABELS = set(str(x).strip() for x in (cfg.get("channel_labels") or [])
                         if str(x).strip())

    # 可选：外部联系人标签文件（JSON 数组，字段 username / remark / labels / refunded）
    p = str(cfg.get("contacts_file") or "").strip()
    if p and os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                for c in json.load(f):
                    uid = str(c.get("username") or "").strip()
                    if not uid:
                        continue
                    labels = set(str(x) for x in (c.get("labels") or []))
                    meta["contacts"][uid] = {
                        "remark": str(c.get("remark") or ""), "labels": labels}
                    if "已成交" in labels:
                        meta["deal"].add(uid)
                    if c.get("refunded") or any("退费" in x for x in labels):
                        meta["refunded"].add(uid)
        except Exception:
            pass
    _META_CACHE = meta
    return meta


# ---------------------------------------------------------------- 数据源
def discover_sources() -> list:
    """自动找聊天记录：优先一键解密导出的包内数据，其次配置里的数据集。"""
    found, seen = [], set()

    def add(p):
        p = os.path.abspath(p or "")
        if p and os.path.isfile(p) and p.lower() not in seen:
            seen.add(p.lower())
            found.append(p)

    # 一键向导自动解密导出的结果（最高优先级）
    add(os.path.join(_ROOT, "data", "聊天记录", "all_messages.jsonl"))
    cfg = _cfg()
    for p in (cfg.get("files") or []):
        add(str(p))
    for d in (cfg.get("datasets") or []):
        add(os.path.join(str(d), "all_messages.jsonl"))
    work = str(cfg.get("work_dir") or "")
    if work:
        add(os.path.join(work, "live", "all_messages.jsonl"))
        add(os.path.join(work, "all_messages.jsonl"))

    home = os.path.expanduser("~")
    for rel in ("Desktop/微信聊天记录/all_messages.jsonl",
                "Documents/微信聊天记录/all_messages.jsonl",
                "Desktop/all_messages.jsonl"):
        add(os.path.join(home, rel))
    return found


def refresh_cmd():
    """返回「重新导出聊天记录」命令，读 data/radar_config.json 的 refresh_cmd。

    没配就返回 None，界面会提示改用「选文件」。路径带空格时请写成数组。
    """
    cmd = _cfg().get("refresh_cmd")
    if isinstance(cmd, list) and cmd and all(str(x).strip() for x in cmd):
        return [str(x) for x in cmd]
    if isinstance(cmd, str) and cmd.strip():
        return cmd.strip().split()
    return None


def source_summary(paths) -> str:
    if not paths:
        return "还没找到聊天记录"
    try:
        mb = sum(os.path.getsize(p) for p in paths) / 1024 / 1024
    except OSError:
        mb = 0
    return f"{len(paths)} 个数据文件 · {mb:.1f} MB"


# ---------------------------------------------------------------- 读取
def _iter_records(paths):
    """统一产出 {'chat','wxid','is_self','ts','content'}。"""
    for p in paths or []:
        if not os.path.isfile(p):
            continue
        try:
            if os.path.getsize(p) > MAX_BYTES:
                continue
        except OSError:
            continue
        ext = os.path.splitext(p)[1].lower()
        try:
            if ext in (".jsonl", ".json", ".ndjson"):
                with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line[0] not in "{[":
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if isinstance(rec, dict):
                            yield rec
            elif ext in (".csv", ".tsv"):
                delim = "\t" if ext == ".tsv" else ","
                with open(p, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
                    reader = csv.DictReader(f, delimiter=delim)
                    headers = reader.fieldnames or []
                    col = {}
                    for i, h in enumerate(headers):
                        key = str(h or "").strip().lower()
                        for want, keys in (
                            ("chat", ("chat", "会话", "联系人", "备注", "昵称", "对方")),
                            ("wxid", ("wxid", "chat_wxid", "会话id", "对方id")),
                            ("ts", ("ts", "time", "时间", "日期")),
                            ("content", ("content", "内容", "消息", "正文")),
                            ("sender", ("sender", "发送人", "发言人")),
                            ("self", ("is_self", "是否自己", "方向")),
                        ):
                            if want not in col and any(k in key for k in keys):
                                col[want] = h
                    for raw in reader:
                        rec = {}
                        for k, h in col.items():
                            rec[k] = raw.get(h)
                        is_self = rec.get("self")
                        if is_self not in (None, ""):
                            rec["is_self"] = str(is_self).strip().lower() in (
                                "1", "true", "yes", "是", "我", "self", "自己", "发出")
                        elif rec.get("sender"):
                            rec["is_self"] = str(rec["sender"]).strip() in ("我", "self", "自己")
                        yield rec
        except Exception:
            continue


def load_chats(paths):
    """按会话分组、去重、按时间排序。返回 OrderedDict[wxid] = {'chat','msgs'}。"""
    chats = collections.OrderedDict()
    seen = set()
    for rec in _iter_records(paths):
        wxid = str(rec.get("chat_wxid") or rec.get("wxid") or rec.get("chat") or "").strip()
        chat = str(rec.get("chat") or rec.get("会话") or wxid or "").strip()
        if not wxid or not chat:
            continue
        content = str(rec.get("content") or "")
        ts = str(rec.get("ts") or "")
        key = (wxid, ts, bool(rec.get("is_self")), content[:60])
        if key in seen:
            continue
        seen.add(key)
        c = chats.setdefault(wxid, {
            "chat": chat, "wxid": wxid, "msgs": [],
            "remark": str(rec.get("remark") or ""),
            "nick": str(rec.get("nick") or ""),
            "alias": str(rec.get("alias") or ""),
            "labels": list(rec.get("labels") or []),
        })
        for k in ("remark", "nick", "alias"):
            if not c.get(k) and rec.get(k):
                c[k] = str(rec[k])
        if not c.get("labels") and rec.get("labels"):
            c["labels"] = list(rec["labels"])
        c["msgs"].append({"ts": ts, "is_self": bool(rec.get("is_self")), "content": content})
    for c in chats.values():
        c["msgs"].sort(key=lambda m: m["ts"] or "")
    return chats


# ---------------------------------------------------------------- 打分
def _dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(str(s)[:19], fmt).replace(tzinfo=TZ)
        except Exception:
            continue
    return None


def _hits(text, lexicon):
    out = []
    for name, weight, kws in lexicon:
        n = sum(1 for k in kws if k in text)
        if n:
            out.append((name, weight, n))
    return out


def _looks_peer(name: str, labels: set) -> bool:
    """名字像同行/机构老师（但带院校/届的往往是真客户）。"""
    if any(k in name for k in ("老师", "规划师")) or any(k in name for k in PEER_KW):
        if any(k in name for k in ("大学", "学院", "校区")):
            return False
        if any(k in name for k in ("级", "届")) and not any(k in name for k in PEER_KW):
            return False
        return True
    if any(k in name for k in ("cto", "CTO", "CEO", "中介", "渠道")):
        return True
    return bool(labels & CHANNEL_LABELS)


def _peer_by_my_words(msgs, name: str, min_hits: int = 2) -> bool:
    """我平时管他叫「X老师」→ 多半是渠道同行。"""
    core = re.sub(r"[^\u4e00-\u9fa5]", "", name or "")
    if not core:
        return False
    cands = {core[i:j] + "老师" for i in range(len(core))
             for j in range(i + 1, min(i + 3, len(core)) + 1)}
    mine = "\n".join(m["content"] for m in msgs if m["is_self"])
    return sum(mine.count(c) for c in cands) >= min_hits


def _valid_name(name: str) -> bool:
    """名单要能直接拿去微信里搜：至少两个汉字/字母/数字，不能只是标点。"""
    return len(re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", name or "")) >= 2


def _skip(name: str, wxid: str, msgs, meta: dict) -> bool:
    low = (name or "").lower()
    if not _valid_name(name):
        return True
    if wxid.endswith("@chatroom") or wxid.startswith("gh_"):
        return True
    if any(k in low for k in BOT_KW):
        return True
    if name in meta["internal"] or wxid in meta["internal"]:
        return True
    ct = meta["contacts"].get(wxid) or {}
    labels = ct.get("labels") or set()
    if any(k in " ".join(labels) for k in ("内部", "同事", "渠道", "同行", "师资")):
        return True
    if _looks_peer(name, labels) or _peer_by_my_words(msgs, name):
        return True
    other = "\n".join(m["content"] for m in msgs if not m["is_self"])
    return sum(1 for k in INTERNAL_KW if k in other) >= 2


def _one_chat(wxid, c, now, meta, min_msgs=3, max_silence=120, cfg=None):
    msgs = c["msgs"]
    if len(msgs) < min_msgs or _skip(c["chat"], wxid, msgs, meta):
        return None
    last = None
    for m in reversed(msgs):
        last = _dt(m["ts"])
        if last:
            break
    if not last:
        return None
    silence = max(0, (now - last).days)
    if silence > max_silence:
        return None

    other = [m for m in msgs if not m["is_self"]][-40:]
    text = "\n".join(m["content"] for m in other)
    all_text = "\n".join(m["content"] for m in msgs[-60:])
    sig = _hits(text, SIGNALS)
    sig_score = sum(weight * min(n, 3) for _n, weight, n in sig)
    risk = _hits(text, RISKS)
    risk_level = max([weight * min(n, 2) for _n, weight, n in risk], default=0)

    stage, ev = "S0 新线索/未分类", ""
    recent = "\n".join(m["content"] for m in msgs[-14:])
    for sname, kws in STAGES:
        hit = next((k for k in kws if k in recent), "")
        if hit:
            stage, ev = sname, hit
            break

    ct = meta["contacts"].get(wxid) or {}
    # 成交识别走统一口径：昵称 / 备注名 / 别名 / 微信标签 / 自定义词，
    # 否定词（未成交、待付、退费、放弃）优先，避免误把老客户留在跟进名单。
    m = DS.meta_for(wxid, c)
    labels = sorted(set(list(c.get("labels") or []) + list(m["labels"])
                        + list(ct.get("labels") or [])))
    j = DS.judge(
        wxid=wxid, name=c["chat"], remark=m["remark"], nick=m["nick"],
        alias=m["alias"], desc=m["desc"], labels=labels,
        text=all_text, cfg=cfg or DS.load_config(),
        extra_deal=any(k in all_text for k in DEAL_KW))
    deal = j["deal"] or wxid in meta["deal"]
    refunded = (wxid in meta["refunded"] or j["flags"]["refunded"]
                or any("退费" in x or "退款" in x for x in labels))
    give_up = (any(name == "明确放弃" for name, _, _ in risk)
               or j["flags"]["give_up"])
    cadence = 2 if silence <= 2 else (4 if silence <= 7 else 7)
    overdue = silence >= cadence
    srank = STAGE_RANK.get(stage[:2], 0)
    score = sig_score + risk_level * 4 + (5 if overdue else 0) \
        + min(silence, 30) * 0.2 + srank * 1.5

    # 「今天该跟谁」看球在谁手里、有没有到跟进节奏、会话还热不热。
    # 静默越久分越高是错的：那是沉睡客户，不是今日跟进。
    pending_reply = bool(msgs) and not msgs[-1]["is_self"]
    hot = max(0, 7 - silence)
    follow_score = (
        (24 if pending_reply and silence <= 2 else
         12 if pending_reply and silence <= 7 else
         5 if pending_reply else 0)
        + (10 if overdue else 0)
        + hot * 2.0
        + risk_level * 3
        + min(sig_score, 24) * 0.8
        + srank * 2
        + (6 if srank >= 5 else 0)
        - max(0, silence - 14) * 1.2
        - (8 if not pending_reply and silence <= 2 else 0)
    )
    if pending_reply and silence <= 2:
        follow_tag = "待回复 · 今天先回"
    elif pending_reply:
        follow_tag = f"待回复 · {silence} 天没回"
    elif overdue:
        follow_tag = f"该触达 · 静默 {silence} 天"
    elif silence <= 2:
        follow_tag = "刚联系过 · 等对方回"
    else:
        follow_tag = f"静默 {silence} 天"

    first = _dt(msgs[0]["ts"]) or last
    span = max(0, (last - first).days)
    kind = "闪电单" if span <= 7 and silence <= 2 else ("长线" if span > 30 else "常规")

    reasons = []
    if pending_reply and silence <= 2:
        reasons.append("客户刚留言、我方还没回 → 今天先回这一条")
    elif pending_reply:
        reasons.append(f"客户 {silence} 天前留言未回 → 尽快补一句")
    elif overdue:
        reasons.append(f"静默 {silence} 天（{kind}节奏 {cadence} 天）→ 该主动触达")
    elif silence <= 2:
        reasons.append("刚联系过、球在对方那边 → 先别重复打扰")
    if refunded:
        reasons.append("⚠ 已退费：先做关系修复，别硬推新单")
    if risk_level >= 4:
        reasons.append("风险：" + "、".join(n for n, _, _ in risk[:2]))
    if deal:
        reasons.append("老客户，走交付/续费关怀")
    if sig_score >= 8:
        reasons.append("高意向：" + "、".join(n for n, _, _ in sig[:3]))
    reasons.append(STAGE_ACTION.get(stage, "按阶段推进"))

    return {
        "name": c["chat"], "wxid": wxid, "msgs": len(msgs), "silence": silence,
        "stage": stage, "stage_ev": ev,
        "signals": [n for n, _, _ in sig], "signal_score": sig_score,
        "risks": [n for n, _, _ in risk], "risk_level": risk_level,
        "kind": kind, "is_deal": deal, "refunded": refunded,
        "deal_level": j["level"], "deal_basis": j["basis"],
        "deal_confidence": j["confidence"], "deal_reason": j["reason"],
        "labels": labels, "give_up": give_up, "overdue": overdue,
        "pending_reply": pending_reply, "follow_tag": follow_tag,
        "follow_score": round(follow_score, 1),
        "score": round(score, 1), "last_ts": last.strftime("%Y-%m-%d"),
        "reason": "；".join(reasons),
    }


def analyze(paths, mode="follow", top=20, min_score=8, min_msgs=3, max_silence=120):
    """mode='follow' 今日跟进 TOP；mode='intent' 潜在意向客户。返回 row 列表。"""
    chats = load_chats(paths)
    now = datetime.datetime.now(TZ)
    meta = load_meta()
    cfg = DS.load_config()
    rows = []
    for wxid, c in chats.items():
        try:
            r = _one_chat(wxid, c, now, meta, min_msgs, max_silence, cfg)
        except Exception:
            r = None
        if r:
            rows.append(r)

    if mode == "intent":
        rows = [r for r in rows
                if not r["is_deal"] and not r["refunded"] and not r["give_up"]
                and r["signal_score"] >= min_score]
        rows.sort(key=lambda r: (-r["signal_score"], r["silence"], -r["score"]))
    else:
        # 今日跟进 TOP：只要还在谈的客户。已成交 / 已退费 / 明确放弃一律不进名单，
        # 按「今天该不该跟」排：客户待回复 → 到点该触达 → 高意向临门一脚。
        rows = [r for r in rows
                if not r["is_deal"] and not r["refunded"] and not r["give_up"]]
        rows.sort(key=lambda r: (-r["follow_score"], -r["signal_score"],
                                 r["silence"], -r["score"]))

    rows = rows[:max(1, int(top or 20))]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


# ---------------------------------------------------------------- 导出
def export_names(rows, out_dir, prefix="名单") -> str:
    """导出打标签能直接吃的一列名单（.txt，一行一个微信备注名）。"""
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.datetime.now(TZ).strftime("%Y%m%d_%H%M")
    path = os.path.join(out_dir, f"{prefix}_{stamp}.txt")
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(f"# 微信辅助器 · {prefix} · 共 {len(rows)} 人 · {stamp}\n")
        for r in rows:
            name = re.sub(r"[\r\n\t,]+", "", str(r.get("name") or "")).strip()
            if _valid_name(name) and not name.startswith("#"):
                f.write(name + "\n")
    return path


CSV_COLS = [
    ("排名", "rank"), ("微信备注", "name"), ("阶段", "stage"),
    ("今日跟进", "follow_tag"), ("静默天数", "silence"),
    ("意向分", "signal_score"), ("今日跟进分", "follow_score"),
    ("客户待回复", lambda r: "是" if r.get("pending_reply") else "否"),
    ("优先级分", "score"), ("消息数", "msgs"),
    ("客户类型", "kind"), ("命中信号", lambda r: "、".join(r["signals"])),
    ("风险", lambda r: "、".join(r["risks"])),
    ("微信标签", lambda r: "、".join(r.get("labels", []))),
    ("已成交", lambda r: "是" if r.get("is_deal") else "否"),
    ("成交判定", "deal_level"), ("判定依据", "deal_basis"),
    ("置信度", "deal_confidence"),
    ("最近联系", "last_ts"), ("建议动作", "reason"), ("wxid", "wxid"),
]


def export_csv(rows, path) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow([c[0] for c in CSV_COLS])
        for r in rows:
            wr.writerow([(v(r) if callable(v) else r.get(v, "")) for _, v in CSV_COLS])
    return path


# ---------------------------------------------------------------- 后台任务
class RadarJob(QObject):
    """在后台线程分析，避免大文件卡住界面。"""

    done = Signal(bool, str, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False

    def is_running(self):
        return self._busy

    def start(self, paths, mode="follow", top=20, min_score=8,
              min_msgs=3, max_silence=120):
        if self._busy:
            return False
        self._busy = True

        def run():
            try:
                rows = analyze(paths, mode, top, min_score, min_msgs, max_silence)
                self.done.emit(True, f"分析完成，找到 {len(rows)} 人", rows)
            except Exception as exc:
                self.done.emit(False, f"{type(exc).__name__}: {exc}", [])
            finally:
                self._busy = False

        threading.Thread(target=run, daemon=True).start()
        return True
