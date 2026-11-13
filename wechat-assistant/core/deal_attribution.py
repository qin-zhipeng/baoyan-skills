# -*- coding: utf-8 -*-
"""deal_attribution.py —— 成交客户话术归因（全自动，纯标准库）。

输入：wx_db.export_all 产出的 all_messages.jsonl（本机解密所得）
输出：
  1. 成交客户识别（签约证据 / 成交关键词 / 外部名单）
  2. 「成交组 vs 未成交组」对照：成交链证据覆盖率、倍数差、发言结构、成交周期
  3. 异议 → 我的应答 配对（只从成交会话里挖，保证是打赢过的仗）
  4. 跨会话复用话术 TOP（归一化后聚类，保留原句），可一键回填常用语库
  5. 阶段关卡流失：每个阶段推进到下一阶段的比例
  6. Markdown 报告 + CSV 话术库 + JSON 摘要

所有数据只在本机处理，报告写在本机「输出」目录，不联网、不上传。
"""
from __future__ import annotations

import collections
import csv
import datetime
import json
import os
import re

from . import deal_signals as DS

TZ = datetime.timezone(datetime.timedelta(hours=8))

# ---------------------------------------------------------------- 词典
DEAL_EVIDENCE = {
    "甲方信息表": ["甲方信息", "甲方姓名", "甲方：", "填写甲方"],
    "e签宝/合同": ["esign", "e签宝", "E签宝", "签署链接", "签合同", "电子合同", "合同链接"],
    "对公收款账户": ["对公账户", "对公账号", "户名：", "开户行", "收款码", "支付宝账户"],
    "转账/定金": ["[转账]", "收到转账", "定金", "已付款", "款已收到", "缴费成功"],
}
DEAL_WORDS = ["已成交", "报名成功", "已付款", "定金已付", "合同已签", "已签合同",
              "缴费成功", "已缴费", "已报名", "款已收到", "恭喜上岸"]
OBJECTIONS = {
    "价格异议": ["太贵", "有点贵", "贵", "优惠", "便宜", "打折", "预算", "价格", "费用高"],
    "拖延犹豫": ["再考虑", "考虑一下", "考虑考虑", "再看看", "不急", "过几天", "回头",
             "商量", "商量一下", "再说吧", "等孩子"],
    "对比竞品": ["对比", "别家", "其他机构", "哪家", "另一家"],
    "效果质疑": ["不成功", "没上岸", "保障", "保底", "退费", "退款", "风险", "承诺", "靠谱"],
}
CLOSING = ["名额", "最后", "截止", "尽快", "月底", "抓紧", "锁定", "保留", "定下来",
           "敲定", "今天定", "申请优惠", "给答复", "确定下来"]
GUARANTEE = ["退费", "退还", "不满意", "随时退出", "保障", "全额", "协议写明"]
STAGE_KWS = [
    ("S1 破冰建档", ["信息采集", "哪年入学", "本科院校", "自我介绍", "规划师", "基本情况"]),
    ("S2 诊断定档", ["保研政策", "竞赛", "绩点", "排名", "目标院校", "论文", "科研", "夏令营", "推免"]),
    ("S3 方案报价", ["价格", "费用", "课时", "方案", "多少钱", "报价", "分期", "项目卡"]),
    ("S4 破异议", ["优惠", "太贵", "对比", "别家", "退费", "保障", "成功率", "考虑一下"]),
    ("S5 逼单", ["名额", "最后一批", "截止", "月底", "抓紧", "定下来", "敲定", "锁定"]),
    ("S6 签约交付", ["甲方信息", "e签", "签署", "合同链接", "对公", "账户", "定金", "付款"]),
]
_PUNCT = set("，。！？、；：（）【】[].,!?:;()~～\"“”‘’·—… \t\r\n")

STOP_FRAGMENTS = ("[图片]", "[语音", "[视频", "[表情", "[通话", "系统消息", "[消息卡片]")


def norm(t: str) -> str:
    t = re.sub(r"\s+", "", t or "")
    t = re.sub(r"\d+", "#", t)
    return "".join(c for c in t if c not in _PUNCT)


def _dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(s)[:19], fmt).replace(tzinfo=TZ)
        except Exception:
            continue
    return None


def load_chats(paths) -> dict:
    """按 chat_wxid 分组，返回 {wxid: {'name','msgs':[...]}}。"""
    chats = collections.OrderedDict()
    seen = set()
    for p in paths or []:
        if not os.path.isfile(p):
            continue
        try:
            if os.path.getsize(p) > 200 * 1024 * 1024:
                continue
        except OSError:
            continue
        with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                wxid = str(r.get("chat_wxid") or r.get("wxid") or r.get("chat") or "").strip()
                name = str(r.get("chat") or wxid or "").strip()
                if not wxid:
                    continue
                content = str(r.get("content") or "")
                ts = str(r.get("ts") or "")
                is_self = bool(r.get("is_self"))
                key = (wxid, ts, is_self, content[:80])
                if key in seen:
                    continue
                seen.add(key)
                c = chats.setdefault(wxid, {
                    "name": name, "wxid": wxid, "msgs": [],
                    "remark": str(r.get("remark") or ""),
                    "nick": str(r.get("nick") or ""),
                    "alias": str(r.get("alias") or ""),
                    "labels": list(r.get("labels") or []),
                })
                # 昵称/备注/标签可能只出现在部分记录里，逐条补全
                for k in ("remark", "nick", "alias"):
                    if not c.get(k) and r.get(k):
                        c[k] = str(r[k])
                if not c.get("labels") and r.get("labels"):
                    c["labels"] = list(r["labels"])
                c["msgs"].append({"ts": ts, "is_self": is_self, "content": content})
    for c in chats.values():
        c["msgs"].sort(key=lambda m: m["ts"] or "")
    return chats


def is_internal(name: str, wxid: str, msgs: list) -> bool:
    if wxid.endswith("@chatroom") or wxid.startswith("gh_"):
        return True
    low = (name or "").lower()
    if any(k in low for k in ("bot", "助手", "服务号", "客服", "微信团队", "文件传输")):
        return True
    if any(k in name for k in ("老师", "规划师", "渠道", "同行", "中介")) and not any(
            k in name for k in ("大学", "学院", "级", "届")):
        return True
    other = "\n".join(m["content"] for m in msgs if not m["is_self"])
    return sum(1 for k in ("师资群", "内部", "咱们内部", "教务教研", "交付同学",
                           "能转走", "跟进进度") if k in other) >= 2


def _self_blocks(msgs: list, min_len: int = 12) -> list:
    """把连续的我方消息合成一个话术块（客户视角收到的就是一整段）。"""
    blocks, buf = [], []
    for m in msgs:
        if m["is_self"]:
            t = (m["content"] or "").strip()
            if t and not t.startswith(STOP_FRAGMENTS):
                buf.append(t)
        else:
            if buf:
                blocks.append("\n".join(buf).strip())
                buf = []
    if buf:
        blocks.append("\n".join(buf).strip())
    return [b for b in blocks if len(norm(b)) >= min_len and len(b) <= 1200]


def classify(chats: dict, deal_wxids=None, deal_names=None,
             judges_out: dict = None) -> tuple[list, list]:
    """分成成交组 / 未成交组。

    成交判定优先看「微信备注名 / 昵称 / 标签」里的标识（deal_signals），
    再叠加原有的聊天内容签约证据与外部名单，保证加严口径的同时不丢召回。
    judges_out 传入一个空 dict 时会回填每个会话的判定依据，供核对清单使用。
    """
    deal_wxids = set(x.strip() for x in (deal_wxids or []) if x.strip())
    deal_names = set(x.strip() for x in (deal_names or []) if x.strip())
    cfg = DS.load_config()
    deals, others = [], []
    for wxid, c in chats.items():
        if is_internal(c["name"], wxid, c["msgs"]):
            continue
        mine = "\n".join(m["content"] for m in c["msgs"] if m["is_self"])
        alltext = "\n".join(m["content"] for m in c["msgs"])
        evid = [label for label, kws in DEAL_EVIDENCE.items()
                if any(k in mine for k in kws)]
        old_deal = (wxid in deal_wxids or c["name"] in deal_names
                    or any(k in alltext for k in DEAL_WORDS)
                    or len(evid) >= 2
                    or ("S6 签约交付" in _stages(c["msgs"]) and len(evid) >= 1))
        j = DS.judge(
            wxid=wxid, name=c["name"], remark=c.get("remark", ""),
            nick=c.get("nick", ""), alias=c.get("alias", ""),
            labels=c.get("labels"), text=alltext, cfg=cfg,
            extra_deal=bool(old_deal))
        if judges_out is not None:
            judges_out[wxid] = j
        # 备注/标签里明确写了退费、放弃的，即使聊天里有签约证据也不算成交
        rejected = j["flags"]["refunded"] or j["flags"]["give_up"]
        deal = (j["deal"] or bool(old_deal)) and not rejected
        c["signal"] = j
        c["evidence"] = evid
        c["deal"] = bool(deal)
        (deals if deal else others).append(c)
    return deals, others


def _stages(msgs: list) -> list:
    text = "\n".join(m["content"] for m in msgs)
    return [name for name, kws in STAGE_KWS if any(k in text for k in kws)]


def _cycle(c: dict):
    msgs = c["msgs"]
    first = _dt(msgs[0]["ts"]) if msgs else None
    deal_ts = None
    for m in msgs:
        if any(any(k in m["content"] for k in kws)
               for kws in DEAL_EVIDENCE.values()):
            d = _dt(m["ts"])
            if d:
                deal_ts = d
                break
    if not first or not deal_ts:
        return None
    return max(0, (deal_ts - first).days)


def analyze(paths, out_dir: str, deal_wxids=None, deal_names=None,
            top: int = 30, progress=None, judges_out: dict = None) -> dict:
    """跑完整归因，写报告 + 话术库 CSV + 摘要 JSON，返回摘要。"""
    def tick(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    tick("正在读取本机聊天记录…")
    chats = load_chats(paths)
    deals, others = classify(chats, deal_wxids, deal_names, judges_out=judges_out)
    now = datetime.datetime.now(TZ)

    # ---- 成交链证据覆盖（成交组 vs 未成交组）----
    ev_rows = []
    for label in DEAL_EVIDENCE:
        a = sum(1 for c in deals if label in c["evidence"])
        b = sum(1 for c in others if label in c["evidence"])
        ra = a / len(deals) if deals else 0
        rb = b / len(others) if others else 0
        ev_rows.append({"label": label, "deal_n": a, "deal_rate": round(ra, 3),
                        "other_n": b, "other_rate": round(rb, 3),
                        "lift": round(ra / rb, 1) if rb > 0 else (99.0 if ra else 0)})

    # ---- 发言结构 / 成交周期 ----
    def struct(group):
        tot = sum(len(c["msgs"]) for c in group)
        mine = sum(sum(1 for m in c["msgs"] if m["is_self"]) for c in group)
        voice = sum(sum(1 for m in c["msgs"] if m["content"].startswith("[语音"))
                    for c in group)
        return {"chats": len(group), "messages": tot,
                "self_rate": round(mine / tot, 3) if tot else 0,
                "avg_msgs": round(tot / len(group)) if group else 0,
                "voice": voice,
                "avg_voice": round(voice / len(group), 1) if group else 0}
    dstruct, ostruct = struct(deals), struct(others)
    cycles = [x for x in (_cycle(c) for c in deals) if x is not None]
    cycles.sort()
    median_cycle = cycles[len(cycles) // 2] if cycles else None
    fast = sum(1 for x in cycles if x <= 7)
    long = sum(1 for x in cycles if x > 30)

    # ---- 异议 → 我的应答（只从成交会话挖）----
    tick("正在挖成交会话里的异议应答…")
    pairs = []
    for c in deals:
        msgs = c["msgs"]
        for i, m in enumerate(msgs):
            if m["is_self"]:
                continue
            txt = (m["content"] or "").strip()
            if not (4 <= len(txt) <= 90) or txt.startswith(STOP_FRAGMENTS):
                continue
            kinds = [k for k, kws in OBJECTIONS.items() if any(x in txt for x in kws)]
            if not kinds:
                continue
            reply = None
            for j in range(i + 1, min(i + 6, len(msgs))):
                if msgs[j]["is_self"] and len(norm(msgs[j]["content"])) >= 8:
                    reply = msgs[j]["content"].strip()
                    break
            if reply:
                pairs.append({"kinds": kinds, "objection": txt[:90],
                              "reply": reply[:400], "stage": _stages(msgs[i:i + 6])})
    by_kind = collections.Counter(k for p in pairs for k in p["kinds"])

    # ---- 跨会话复用话术 TOP ----
    tick("正在统计跨成交会话复用话术…")
    cluster = collections.defaultdict(lambda: {"n": 0, "sample": "", "chats": set(),
                                               "closing": 0, "guarantee": 0})
    for c in deals:
        for b in _self_blocks(c["msgs"]):
            key = norm(b)[:120]
            if len(key) < 12:
                continue
            e = cluster[key]
            e["n"] += 1
            e["chats"].add(c["wxid"])
            if not e["sample"] or len(b) > len(e["sample"]):
                e["sample"] = b
            if any(k in b for k in CLOSING):
                e["closing"] += 1
            if any(k in b for k in GUARANTEE):
                e["guarantee"] += 1
    hot = sorted(cluster.values(), key=lambda x: (-len(x["chats"]), -x["n"]))[:top]
    hot = [{"reuse": len(e["chats"]), "times": e["n"], "text": e["sample"][:600],
            "closing": bool(e["closing"]), "guarantee": bool(e["guarantee"])}
           for e in hot if len(e["chats"]) >= 2]

    # 控制权转移话术（逼单 / 风险逆转）：单独列出，最适合回填常用语
    closing_blocks = []
    for c in deals:
        for b in _self_blocks(c["msgs"]):
            if any(k in b for k in CLOSING) or any(k in b for k in GUARANTEE):
                if 20 <= len(b) <= 400:
                    closing_blocks.append(b)
    cb = collections.defaultdict(lambda: {"n": 0, "text": ""})
    for b in closing_blocks:
        k = norm(b)[:100]
        cb[k]["n"] += 1
        if not cb[k]["text"]:
            cb[k]["text"] = b
    close_top = sorted((v for v in cb.values()), key=lambda x: -x["n"])[:15]

    # ---- 阶段流失 ----
    stage_hit = collections.Counter()
    for c in list(deals) + list(others):
        hit = _stages(c["msgs"])
        for s in hit:
            stage_hit[s] += 1
        c["max_stage"] = hit[-1] if hit else "S0 未开始"
    stage_rows = []
    for name, _ in STAGE_KWS:
        reached = sum(1 for c in list(deals) + list(others)
                      if _stages(c["msgs"]) and name in _stages(c["msgs"]))
        d = sum(1 for c in deals if name in _stages(c["msgs"]))
        o = sum(1 for c in others if name in _stages(c["msgs"]))
        stage_rows.append({"stage": name, "reached": reached, "deal": d, "other": o,
                           "deal_rate": round(d / (d + o), 3) if (d + o) else 0})

    os.makedirs(out_dir, exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M")
    csv_path = os.path.join(out_dir, "成交话术库_%s.csv" % stamp)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["复用成交会话数", "出现次数", "类型", "话术原文"])
        for h in hot:
            wr.writerow([h["reuse"], h["times"],
                         "逼单/成交话术" if h["closing"] else
                         "风险逆转" if h["guarantee"] else "标准话术", h["text"]])
        for v in close_top:
            wr.writerow([1, v["n"], "临门一脚", v["text"]])

    md_path = os.path.join(out_dir, "成交话术归因报告_%s.md" % stamp)
    lines = [
        "# 成交客户话术归因报告", "",
        "> 由本机微信聊天记录自动分析生成 · %s" % now.strftime("%Y-%m-%d %H:%M"),
        "> 数据仅存本机，未上传。所有话术均为「我方在成交会话里真实发出过的原话」。", "",
        "## 一、样本概况", "",
        "| 指标 | 成交组 | 未成交组 |", "| --- | ---: | ---: |",
        "| 会话数 | %d | %d |" % (dstruct["chats"], ostruct["chats"]),
        "| 消息总数 | %d | %d |" % (dstruct["messages"], ostruct["messages"]),
        "| 平均每会话消息 | %d | %d |" % (dstruct["avg_msgs"], ostruct["avg_msgs"]),
        "| 我方发言占比 | %.0f%% | %.0f%% |" % (dstruct["self_rate"] * 100,
                                              ostruct["self_rate"] * 100),
        "| 人均语音条 | %.1f | %.1f |" % (dstruct["avg_voice"], ostruct["avg_voice"]),
        "",
    ]
    if median_cycle is not None:
        lines += [
            "**成交周期**：中位数 %d 天；≤7 天闪电单 %d 单（%.0f%%）；"
            ">30 天长线单 %d 单（%.0f%%）。" % (
                median_cycle, fast, fast / len(cycles) * 100,
                long, long / len(cycles) * 100),
            "近一半单子是「跟出来的」，当场没成不等于没戏。", "",
        ]
    lines += ["## 二、成交链证据覆盖（这是最该抄的作业）", "",
              "| 关键动作 | 成交组 | 未成交组 | 倍数差 |",
              "| --- | ---: | ---: | ---: |"]
    for r in ev_rows:
        lines.append("| %s | %.0f%% (%d) | %.0f%% (%d) | %s |" % (
            r["label"], r["deal_rate"] * 100, r["deal_n"],
            r["other_rate"] * 100, r["other_n"],
            ("%.1f×" % r["lift"]) if 0 < r["lift"] < 90 else "—"))
    lines += ["", "> 覆盖率差距大的动作，就是最值得固化的标准件。", "",
              "## 三、异议 → 我怎么答（只收录打赢过的会话）", ""]
    for kind, n in by_kind.most_common():
        lines += ["### %s（%d 例）" % (kind, n), ""]
        shown = 0
        for p in pairs:
            if kind in p["kinds"] and shown < 6:
                lines += ["- 客户：「%s」" % p["objection"].replace("\n", " "),
                          "  - 我方：「%s」" % p["reply"].replace("\n", " / ")[:220], ""]
                shown += 1
    lines += ["## 四、跨成交会话复用话术 TOP", "",
              "| 复用会话数 | 类型 | 原话（节选） |", "| ---: | --- | --- |"]
    for h in hot[:20]:
        t = re.sub(r"\s+", " ", h["text"])[:150].replace("|", "/")
        lines.append("| %d | %s | %s |" % (
            h["reuse"], "逼单" if h["closing"] else "风险逆转" if h["guarantee"] else "标准件", t))
    lines += ["", "## 五、阶段关卡与流失", "",
              "| 阶段 | 触达会话 | 最终成交 | 未成交 | 该阶段后成交率 |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for r in stage_rows:
        lines.append("| %s | %d | %d | %d | %.0f%% |" % (
            r["stage"], r["reached"], r["deal"], r["other"], r["deal_rate"] * 100))
    lines += ["", "## 六、怎么用这份报告", "",
              "1. 打开界面「常用语」页，把第四节 TOP 话术存进去，客户走到哪步就点哪条。",
              "2. 第二节倍数差最大的动作，检查当前在谈客户有没有做到，没做就是漏单点。",
              "3. 第三节按客户异议类型检索，直接复用打赢过的应答。",
              "4. 报告与话术库 CSV 都在本机「输出」目录，可随时重新生成。", ""]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    summary = {
        "generated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "chats_total": len(chats), "deals": len(deals), "others": len(others),
        "deal_struct": dstruct, "other_struct": ostruct,
        "median_cycle": median_cycle, "fast_deals": fast, "long_deals": long,
        "evidence": ev_rows, "stages": stage_rows,
        "objection_pairs": len(pairs), "objection_kinds": dict(by_kind),
        "hot_scripts": len(hot), "top_scripts": hot[:10],
        "report": md_path, "csv": csv_path,
    }
    with open(os.path.join(out_dir, "归因摘要_%s.json" % stamp), "w",
              encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    tick("归因报告已生成")
    return summary


def scripts_for_quickstore(summary: dict, limit: int = 12) -> list:
    """把归因结果转成可直接写入常用语库的条目。"""
    out = []
    for h in (summary.get("top_scripts") or [])[:limit]:
        t = (h.get("text") or "").strip()
        if len(t) < 12:
            continue
        kind = "逼单" if h.get("closing") else "风险逆转" if h.get("guarantee") else "成交复用"
        out.append({"title": "【成交复用 %d 单】%s" % (h.get("reuse", 1), kind),
                    "content": t[:600],
                    "keywords": "成交话术 " + kind,
                    "tags": ["成交归因"]})
    return out
