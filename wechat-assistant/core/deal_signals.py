# -*- coding: utf-8 -*-
"""deal_signals.py —— 成交客户识别（离线、纯标准库、可自定义）。

为什么单独做这一层
------------------
「谁已经成交」不能只从聊天内容猜。绝大多数销售把成交状态写在
**微信备注名 / 昵称 / 标签** 里，例如「张三 已成交」「李四-付」「【签单】王五」，
或者给联系人打「已成交」「付款」标签。每个人的写法都不一样，所以：

  1. 内置一套覆盖常见写法的成交 / 排除词典（昵称、备注、标签分开配权重）
  2. 支持在 data/deal_signals.json 里加自己的词、加强制名单、关掉默认词
  3. 自动把本机微信里的标签名、备注名片段学出来，推荐成新标识（一键启用）
  4. 否定优先：「未成交 / 没付款 / 待付 / 退费 / 放弃」绝不能被「成交 / 付款」误判
  5. 防姓氏误伤：「付」是姓，单字「付」「定」「单」「成」不进默认词典
  6. 每个判定都给依据 + 置信度，导出「成交客户核对清单.csv」供人工核对

判定口径
--------
  confirmed / likely  → 算成交（从今日跟进名单剔除、进成交归因组）
  suspect             → 疑似，不算成交，只进核对清单（可勾选启用）
  no / excluded       → 未成交 / 明确被否定词排除

隐私：只读本机文件，不联网、不上传、不写微信任何数据。
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import re

TZ = datetime.timezone(datetime.timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT, "data", "deal_signals.json")
CONTACTS_FILE = os.path.join(ROOT, "data", "聊天记录", "contacts.json")
RADAR_CONFIG = os.path.join(ROOT, "data", "radar_config.json")

# ---------------------------------------------------------------- 内置词典
# 强标识：出现在「昵称 / 备注 / 标签」里基本就能确定成交。
# 注意：刻意不收单字「付 / 定 / 单 / 成 / 款 / 签」——「付」是常见姓氏，
# 「定」可能是地名，「单」可能是「名单」。这些字只以组合词形式出现。
NAME_STRONG = [
    "已成交", "成交客户", "已成交客户", "成单", "成交单", "成交", "已签单", "签单成功",
    "已签", "签约成功", "合同已签", "已签合同", "签了合同", "签完合同", "已签约",
    "已付款", "付清", "全款已付", "已付全款", "付全款", "款已付", "款已收",
    "已打款", "已转账", "已收款", "定金已付", "已付定金", "订金已付", "已付订金",
    "尾款已付", "已付尾款", "尾款结清", "全款结清", "已结清",
    "已缴费", "缴费成功", "已交费", "费用已交", "已交齐", "学费已交",
    "报名成功", "已报名", "已开通", "已开课", "正式学员", "已入班", "已入学",
    "已交付", "已下单", "已购买", "已付费", "付费成功", "支付成功", "已支付",
    "款已到账", "已到账", "交易完成", "付款完成", "已完成付款", "已开通课程",
    "全款",
    # 单字标识（销售常写「李四-付」「王五 签」）。这两个词只在
    # _match_words 的边界规则下生效：必须前面是分隔符或后面是分隔符，
    # 且后面不能跟称谓。因此「付强」「付老师」「王付」都不会被误判。
    "付", "签",
]

# 中标识：单独出现只算「疑似」，两个及以上、或与强标识叠加才判成交。
NAME_MEDIUM = [
    "付款", "付了", "付过", "付完", "已付", "定金", "订金", "尾款", "全款",
    "缴费", "交费", "交了", "签了", "签约", "签单", "下定金", "下定", "已定",
    "定了", "下单", "拍下", "老客户", "续费", "复购", "学员", "正式", "vip",
    "已开", "已通", "售后", "交付", "结清", "到款", "到账", "打款", "转账",
    "收款", "收据", "发票", "合同", "协议书", "名额已锁", "已锁名额", "已排课",
    "已建群", "服务中", "在读", "已入学籍", "开通了", "买过", "报了名",
]

# 标签专用：微信标签是人工定义的短词，语义比昵称干净，所以收得更宽一点。
# 但「报名 / 学员 / VIP / 付费 / 正式」也可能是阶段标签，放中标识，不单独定案。
LABEL_STRONG = [
    "已成交", "成交", "成交客户", "成单", "已签", "签约客户", "签单",
    "已付款", "付款", "付清", "全款", "已付全款", "尾款已结", "已缴费",
    "缴费成功", "已付费", "付费成功", "支付成功", "已支付", "已到账",
    "报名成功", "正式学员", "已交付", "已开通", "已开课", "交易完成",
]
LABEL_MEDIUM = [
    "定金", "订金", "尾款", "已付", "付款中", "缴费", "交费", "报名", "付费",
    "学员", "正式", "vip", "VIP", "老客户", "复购", "续费", "已续费",
    "交付", "交付中", "售后", "服务中", "在读", "已入班", "A类", "A单",
    "S级", "重点客户", "已锁定", "已定", "签了", "合同",
]

# 否定 / 排除词：优先级最高。命中后先把这段从文本里「遮掉」，再跑正向匹配，
# 这样「未成交」不会被拆成「成交」、「没付款」不会被拆成「付款」。
NEGATIVE = [
    "未成交", "没成交", "未成单", "未签", "没签", "待签", "未签合同", "未签约",
    "未付款", "没付款", "待付款", "待付", "未付", "没付", "未缴费", "待缴费",
    "未缴", "未交费", "待交费", "未交", "没交", "未报名", "待报名", "没报名",
    "未开通", "未交付", "未到账", "未支付", "未付费", "未购买", "未下单",
    "退费", "退款", "已退费", "已退款", "已退", "退了", "退单", "申请退",
    "放弃", "已放弃", "不考虑", "不报了", "不学了", "无效", "无效客户", "屏蔽",
    "拉黑", "黑名单", "跑单", "流单", "丢单", "飞单", "已删", "停用",
    "意向", "潜在", "待定", "考虑中", "观望", "跟进中", "洽谈中", "咨询中",
    "未定", "没定", "待跟进", "线索", "新客", "未转化", "未成", "没成",
    "同行", "内部", "同事", "测试", "演示", "样例", "示例", "机器人",
]

# 命中否定词时推导的状态标记
REFUND_WORDS = ("退费", "退款", "已退", "退了", "退单", "申请退")
GIVEUP_WORDS = ("放弃", "不考虑", "不报了", "不学了", "无效", "屏蔽", "拉黑",
                "黑名单", "跑单", "流单", "丢单", "飞单")
INTERNAL_WORDS = ("同行", "内部", "同事", "测试", "演示", "样例", "示例", "机器人")
# 「这不是客户」和「这单没成」是两回事：前者不该让一个明确写着「已成交」的
# 联系人变成「标识冲突」，只用于内部账号过滤。
NOISE_NEGATIVE_WORDS = set(INTERNAL_WORDS)

# 来源权重：越靠前越可信。聊天内容噪声最大，权重压到最低。
SOURCE_W = {
    "label":  {"strong": 100, "medium": 72},
    "remark": {"strong": 96,  "medium": 62},
    "name":   {"strong": 88,  "medium": 54},
    "nick":   {"strong": 90,  "medium": 56},
    "alias":  {"strong": 80,  "medium": 48},
    "desc":   {"strong": 76,  "medium": 44},
    "text":   {"strong": 46,  "medium": 20},
}
SOURCE_CN = {"label": "微信标签", "remark": "备注名", "name": "会话名",
             "nick": "昵称", "alias": "微信号", "desc": "描述", "text": "聊天内容"}

# 推荐新标识时用的语义字（命中越多越像成交标识）
SEM_CHARS = set("成付付款单签定缴费报学员正已完结老交课金账到收通开续复购交付锁清")
# 切片段时的通用噪声词，不推荐给用户
NOISE_WORDS = {
    "老师", "同学", "家长", "妈妈", "爸爸", "微信", "客户", "备注", "手机",
    "电话", "大学", "学院", "年级", "学校", "先生", "女士", "咨询", "好友",
    "来源", "添加", "已加", "未读", "群聊", "公众号", "服务号", "文件传输",
}

_FULL2HALF = {ord(c): ord(c) - 0xFEE0 for c in
              "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
              "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"}
# 抒情 / 成语昵称里常见的字，用来过滤「春暖花开」「马到成功」这类假标识
POETIC_CHARS = set("花开心想风月山水云阳光静暖清简栀梦诗远方老马功事")
_SEP = re.compile(r"[\s\-_/|｜:：,，、;；+~～!！?？()（）\[\]【】{}<>《》·．.]+")
_TAG = re.compile(r"<[^>]*>")


def normalize(s) -> str:
    """全角转半角、括号/分隔符变空格、小写。保留空格用于边界判断。"""
    t = str(s or "").translate(_FULL2HALF).lower()
    t = _TAG.sub(" ", t)
    t = _SEP.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- 配置
def default_config() -> dict:
    return {
        "name_include": [],        # 追加：昵称/备注里的成交标识
        "label_include": [],       # 追加：微信标签名（可直接填自己的标签）
        "negative_include": [],    # 追加：否定词
        "manual_deal": [],         # 强制算成交（wxid 或备注名）
        "manual_not_deal": [],     # 强制不算成交
        "disabled": [],            # 关掉某个内置词
        "accept_suspect": False,   # 把「疑似」也算成交
    }


def load_config(path: str = None) -> dict:
    cfg = default_config()
    try:
        with open(path or CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, dict):
            for k in cfg:
                if k in d:
                    cfg[k] = d[k]
    except Exception:
        pass
    for k in ("name_include", "label_include", "negative_include",
              "manual_deal", "manual_not_deal", "disabled"):
        v = cfg.get(k)
        if not isinstance(v, list):
            v = []
        cfg[k] = [str(x).strip() for x in v if str(x).strip()]
    cfg["accept_suspect"] = bool(cfg.get("accept_suspect"))
    return cfg


def save_config(cfg: dict, path: str = None) -> str:
    d = default_config()
    d.update({k: cfg.get(k, d[k]) for k in d})
    p = path or CONFIG_FILE
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    return p


def lexicons(cfg: dict = None) -> dict:
    """内置 + 自定义 - 禁用，返回按词长降序的四组词典。"""
    cfg = cfg or load_config()
    off = set(normalize(x) for x in cfg.get("disabled", []))

    def build(base, extra, tier):
        words = {normalize(w) for w in list(base) + list(extra or [])}
        return sorted((w for w in words if w and w not in off),
                      key=len, reverse=True), tier

    strong_name, _ = build(NAME_STRONG, cfg.get("name_include"), "strong")
    med_name, _ = build(NAME_MEDIUM, [], "medium")
    strong_label, _ = build(LABEL_STRONG, cfg.get("label_include"), "strong")
    med_label, _ = build(LABEL_MEDIUM, cfg.get("label_include"), "medium")
    neg, _ = build(NEGATIVE, cfg.get("negative_include"), "negative")
    return {
        "name": [(w, "strong") for w in strong_name] + [(w, "medium") for w in med_name],
        "label": [(w, "strong") for w in strong_label] + [(w, "medium") for w in med_label],
        "negative": neg,
    }


# ---------------------------------------------------------------- 联系人簿
_BOOK = None


def contact_book(refresh: bool = False) -> dict:
    """读解密导出的 contacts.json，并合并外部标签文件。

    返回 {"contacts": {wxid: {...}}, "labels_all": [...]}。
    微信 4.x 本地库只存标签定义、不存「联系人↔标签」关联，所以这里支持
    两种精确补标签的方式：
      * 在 contacts.json 里给某个 wxid 手工加 "labels": ["已成交"]
      * data/radar_config.json 的 contacts_file 指向 JSON 数组
        [{"username":"wxid_xxx","labels":["已成交"]}, ...]
    """
    global _BOOK
    if _BOOK is not None and not refresh:
        return _BOOK
    contacts, labels_all = {}, []
    try:
        with open(CONTACTS_FILE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        contacts = d.get("contacts") or {}
        labels_all = [str(x) for x in (d.get("labels_all") or []) if str(x).strip()]
    except Exception:
        pass
    try:
        with open(RADAR_CONFIG, "r", encoding="utf-8-sig") as f:
            p = str((json.load(f) or {}).get("contacts_file") or "").strip()
        if p and os.path.isfile(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            items = data.values() if isinstance(data, dict) else data
            for c in items or []:
                if not isinstance(c, dict):
                    continue
                uid = str(c.get("username") or c.get("wxid") or "").strip()
                if not uid:
                    continue
                e = contacts.setdefault(uid, {"remark": "", "nick": "",
                                              "alias": "", "desc": "", "labels": []})
                labs = c.get("labels") or []
                if isinstance(labs, str):
                    labs = [labs]
                e["labels"] = list(dict.fromkeys(list(e.get("labels") or [])
                                                 + [str(x) for x in labs if str(x).strip()]))
                for k in ("remark", "nick", "alias", "desc"):
                    if c.get(k) and not e.get(k):
                        e[k] = str(c[k])
                if c.get("refunded"):
                    e["labels"] = list(e["labels"]) + ["已退费"]
    except Exception:
        pass
    _BOOK = {"contacts": contacts, "labels_all": labels_all}
    return _BOOK


def meta_for(wxid: str, rec: dict = None) -> dict:
    """优先用消息记录里带的字段，缺的从联系人簿补。"""
    rec = rec or {}
    b = contact_book().get("contacts", {}).get(str(wxid), {})
    out = {}
    for k in ("remark", "nick", "alias", "desc"):
        out[k] = str(rec.get(k) or b.get(k) or "")
    labs = list(rec.get("labels") or b.get("labels") or [])
    out["labels"] = [str(x) for x in labs if str(x).strip()]
    return out


# ---------------------------------------------------------------- 匹配核心
def _mask_negatives(text: str, neg_words: list) -> tuple:
    """把否定片段遮成空格，返回（遮后文本, 命中的否定词列表）。"""
    hits = []
    t = text
    for w in neg_words:
        if w and w in t:
            hits.append(w)
            t = t.replace(w, " " * len(w))
    return t, hits


def _match_words(text: str, words: list, source: str) -> list:
    """在文本里找词；同一个词在同一来源只算一次，按词长降序。"""
    out = []
    if not text:
        return out
    for w, tier in words:
        if not w or w not in text:
            continue
        # 自定义单字词防误伤：只认「出现在末尾或紧贴分隔符」的位置，
        # 避免把姓氏「付」「定」之类当成成交标识。
        if len(w) == 1:
            # 单字标识只在「看起来像标注」的位置生效：
            #   ✓ 李四 付 / 付 2026 / 付25级 / 付学生25 / 单独一个「付」
            #   ✗ 付老师、付强、付小明同学（姓）、王付（名字尾字）
            # 关键：紧跟数字或「已全定款费了清完过学」这类语义字时也算标注，
            # 紧跟普通汉字则一律当姓名，不放行。
            positions = [i for i in range(len(text)) if text.startswith(w, i)]
            ok = False
            for i in positions:
                nxt = text[i + 1:i + 2]
                if any(text[i + 1:i + 3].startswith(s) for s in
                       ("老师", "同学", "先生", "女士", "经理", "总")):
                    continue
                looks_tag = (not nxt or nxt == " " or nxt.isdigit()
                             or nxt in "已全定款费了清完过学")
                boundary = ((i > 0 and text[i - 1] == " ")
                            or (i == 0 and looks_tag)
                            or (nxt == " "))
                if boundary:
                    ok = True
                    break
            if not ok:
                continue
        out.append({"source": source, "word": w, "tier": tier,
                    "weight": SOURCE_W[source][tier]})
    return out


def judge(*, wxid: str = "", name: str = "", remark: str = "", nick: str = "",
          alias: str = "", desc: str = "", labels=None, text: str = "",
          cfg: dict = None, extra_deal: bool = False) -> dict:
    """判定一个联系人是否成交，返回带依据与置信度的字典。

    extra_deal: 上游原有的「聊天内容签约证据」判定，作为一路弱证据并入。
    """
    cfg = cfg or load_config()
    lex = lexicons(cfg)
    labels = [str(x) for x in (labels or []) if str(x).strip()]
    fields = [
        ("label", " ".join(labels)),
        ("remark", remark), ("name", name), ("nick", nick),
        ("alias", alias), ("desc", desc),
        ("text", (text or "")[-8000:]),
    ]

    all_hits, negatives = [], []
    for source, raw in fields:
        t = normalize(raw)
        if not t:
            continue
        words = lex["label"] if source == "label" else lex["name"]
        # 关键：否定词只作用于「标识字段」。聊天里说「退费怎么算」
        # 「我差点放弃」只是讨论，不是客户状态；否则会把大量成交客户
        # 误标成退费/放弃（真机实测会让成交数从 68 掉到 29）。
        if source == "text":
            all_hits.extend(_match_words(t, words, source))
            continue
        masked, neg = _mask_negatives(t, lex["negative"])
        for n in neg:
            if n not in negatives:
                negatives.append(n)
        all_hits.extend(_match_words(masked, words, source))

    # 同一个词可能既在「会话名」又在「备注名」里出现（两者常常相同），
    # 也可能同时被收进强/中两组词典。只保留权重最高的一次，避免重复刷分。
    best = {}
    for h in all_hits:
        k = h["word"]
        prev = best.get(k)
        if prev is None:
            best[k] = h
        elif h["tier"] == "strong" and prev["tier"] != "strong":
            best[k] = h
        elif h["tier"] == prev["tier"] and h["weight"] > prev["weight"]:
            best[k] = h
    hits = sorted(best.values(), key=lambda x: -x["weight"])

    # 只有「标识字段」能定案，聊天内容只作加权：我方话术里经常出现
    # 「合同 / 付款 / 定金」，光看聊天内容会把一大票还在谈的人误判成成交。
    id_hits = [h for h in hits if h["source"] != "text"]
    text_hits = [h for h in hits if h["source"] == "text"]
    strong_from_id = [h for h in id_hits if h["tier"] == "strong"]
    score = (sum(h["weight"] for h in id_hits[:6])
             + min(30, sum(h["weight"] for h in text_hits[:3]))
             + (40 if extra_deal else 0))
    score = min(100, score)

    # 状态否定（未成交/退费/放弃）才影响成交判定；
    # 噪声否定（示例/测试/同行）只用于识别内部账号。
    state_neg = [n for n in negatives if n not in NOISE_NEGATIVE_WORDS]
    flags = {
        "refunded": any(any(r in n for r in REFUND_WORDS) for n in state_neg),
        "give_up": any(any(r in n for r in GIVEUP_WORDS) for n in state_neg),
        "internal": any(any(r in n for r in INTERNAL_WORDS) for n in negatives),
    }
    # 标签本身也可能直接写「已退费」「已放弃」
    blob = " ".join(labels)
    if any(r in blob for r in REFUND_WORDS):
        flags["refunded"] = True
    if any(r in blob for r in GIVEUP_WORDS):
        flags["give_up"] = True

    manual_yes = {normalize(x) for x in cfg.get("manual_deal", [])}
    manual_no = {normalize(x) for x in cfg.get("manual_not_deal", [])}
    ids = {normalize(x) for x in (wxid, name, remark, nick, alias) if x}

    level = "no"
    if any(x and (x in ids or x == normalize(wxid)) for x in manual_no):
        level = "excluded"
    elif any(x and (x in ids or x == normalize(wxid)) for x in manual_yes):
        level = "confirmed"
    elif strong_from_id:
        # 昵称/备注/标签里有强标识就定案；同时命中否定词则标 conflict 交人工核对
        level = "conflict" if state_neg else "confirmed"
    elif flags["refunded"] or flags["give_up"]:
        level = "no"
    elif extra_deal:
        # 沿用原有口径：聊天里有明确签约证据也算成交（置信度较低，进核对清单）
        level = "likely"
    elif score >= 55 and len(id_hits) >= 2:
        level = "likely"
    elif id_hits:
        level = "suspect"

    deal = level in ("confirmed", "likely", "conflict")
    if level not in ("confirmed", "excluded") and cfg.get("accept_suspect") \
            and level == "suspect":
        deal = True
        level = "likely"

    why = []
    for h in hits[:5]:
        why.append("%s「%s」" % (SOURCE_CN.get(h["source"], h["source"]), h["word"]))
    if extra_deal:
        why.append("聊天内容有签约证据")
    if negatives:
        why.append("排除词：" + "、".join(negatives[:3]))
    if not why:
        why.append("未在昵称/备注/标签里发现成交标识")

    return {
        "wxid": wxid, "name": name or remark or nick or wxid,
        "remark": remark, "nick": nick, "alias": alias, "labels": labels,
        "deal": bool(deal), "level": level, "confidence": int(score),
        "hits": hits, "negatives": negatives, "flags": flags,
        "reason": "；".join(why),
        "basis": "、".join("%s:%s" % (SOURCE_CN.get(h["source"], h["source"]), h["word"])
                          for h in hits[:6]),
    }


# ---------------------------------------------------------------- 自动学习
def _fragments(s: str) -> list:
    t = normalize(s)
    if not t:
        return []
    out = []
    for part in t.split(" "):
        if not part:
            continue
        # 去掉纯数字 / 日期 / 金额 / 手机号形态的片段
        if re.fullmatch(r"[\d.#￥¥wWkK元块年月日\-]+", part):
            continue
        if not re.search(r"[\u4e00-\u9fa5]", part):
            continue
        if 2 <= len(part) <= 14 and part not in NOISE_WORDS:
            out.append(part)
    return out


def recommend(contacts: dict = None, labels_all=None, cfg: dict = None,
              top: int = 30) -> list:
    """从本机备注名 / 昵称 / 标签里学出「可能是成交标识」的新词。

    微信里每个人写法不同：有人写「已签」、有人写「付清」、有人打「A单」。
    这里把反复出现的短片段和本机标签名列出来，让对方勾一下就能用，
    不需要懂配置、不需要联网、不需要改代码。
    """
    cfg = cfg or load_config()
    book = contact_book()
    contacts = contacts if contacts is not None else book["contacts"]
    labels_all = labels_all if labels_all is not None else book["labels_all"]
    lex = lexicons(cfg)
    known = set(w for w, _ in lex["name"]) | set(w for w, _ in lex["label"]) \
        | set(lex["negative"]) | set(cfg.get("name_include", [])) \
        | set(cfg.get("label_include", [])) | set(cfg.get("disabled", []))

    counts, where = {}, {}
    for uid, c in (contacts or {}).items():
        c = c or {}
        seen = set()
        for key in ("remark", "nick", "alias", "desc"):
            for frag in _fragments(c.get(key) or ""):
                seen.add(frag)
        for lab in (c.get("labels") or []):
            for frag in _fragments(lab):
                seen.add(frag)
        for frag in seen:
            if frag in known or frag in NOISE_WORDS:
                continue
            counts[frag] = counts.get(frag, 0) + 1
            where.setdefault(frag, set()).add("备注/昵称")

    out = []
    for word, n in counts.items():
        # 用「不同语义字」计数：叠字「简简单单」只算 1 个「单」，不当标识。
        sem = len({ch for ch in word if ch in SEM_CHARS})
        poetic = sum(1 for ch in word if ch in POETIC_CHARS) / max(1, len(word))
        # 至少 2 个交易语义字、足够短、且不是抒情成语，才像
        # 「已付 / 结清 / 签单 / 已成交」这类真标识。
        if sem < 2 or len(word) > 8 or poetic >= 0.3:
            continue
        sc = sem * 4 + min(n, 30) * 2 + (4 if len(word) <= 4 else 0)
        if word.startswith(("已", "未", "待", "完", "结", "正", "付", "签")):
            sc += 5
        out.append({"word": word, "count": n, "source": "备注/昵称片段",
                    "score": sc, "why": "含 %d 个交易语义字，出现在 %d 个联系人" % (sem, n)})

    for lab in (labels_all or []):
        w = normalize(lab)
        if not w or w in known or len(w) > 14:
            continue
        sem = sum(1 for ch in w if ch in SEM_CHARS)
        # 标签是人工定义的，语义弱的也列出来，但排在后面由人判断
        sc = sem * 4 + (12 if sem >= 2 else 3)
        out.append({"word": lab, "count": 0, "source": "微信标签",
                    "score": sc, "why": "本机微信里的标签名%s" %
                    ("，含 %d 个交易语义字" % sem if sem else "")})

    # 同一个词可能同时来自两处，合并保留高分
    merged = {}
    for r in out:
        k = normalize(r["word"])
        if k not in merged or r["score"] > merged[k]["score"]:
            merged[k] = r
    return sorted(merged.values(), key=lambda x: (-x["score"], -x["count"],
                                                  x["word"]))[:max(1, top)]


def scan(judges: dict, labels_all=None) -> dict:
    """汇总判定结果，给设置界面显示。"""
    lv = {}
    for j in (judges or {}).values():
        lv[j["level"]] = lv.get(j["level"], 0) + 1
    return {
        "total": len(judges or {}),
        "levels": lv,
        "deals": sum(1 for j in (judges or {}).values() if j["deal"]),
        "suspect": lv.get("suspect", 0),
        "conflict": lv.get("conflict", 0),
        "refunded": sum(1 for j in (judges or {}).values()
                        if j["flags"].get("refunded")),
        "labels_all": list(labels_all or contact_book()["labels_all"]),
    }


# ---------------------------------------------------------------- 核对清单
REVIEW_COLS = ["微信备注", "昵称", "wxid", "判定", "置信度", "命中依据",
               "排除词", "微信标签", "来源说明", "是否成交(可改)", "备注"]


def export_review(judges: dict, path: str, only_uncertain: bool = False,
                  include_all: bool = False) -> str:
    """导出「成交客户核对清单.csv」——每个判定都带依据，方便人工复核。

    默认只列需要复核的：已判成交、疑似、标识冲突、退费/放弃。
    全量名单上千行没有核对价值，需要时传 include_all=True。
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    cn = {"confirmed": "已确认成交", "likely": "较大概率成交",
          "suspect": "疑似，待确认", "conflict": "标识冲突，待确认",
          "no": "未成交", "excluded": "手工排除"}
    rows = []
    for j in (judges or {}).values():
        if only_uncertain and j["level"] not in ("suspect", "conflict"):
            continue
        if not include_all and not only_uncertain and not (
                j["deal"] or j["level"] in ("suspect", "conflict")
                or j["flags"]["refunded"] or j["flags"]["give_up"]):
            continue
        rows.append([
            j.get("remark") or j.get("name", ""), j.get("nick", ""), j.get("wxid", ""),
            cn.get(j["level"], j["level"]), j.get("confidence", 0),
            j.get("basis", ""), "、".join(j.get("negatives", [])[:5]),
            "、".join(j.get("labels", [])), j.get("reason", ""),
            "是" if j.get("deal") else "否", "",
        ])
    rows.sort(key=lambda r: (r[9] != "是", -int(r[4] or 0), r[0]))
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(REVIEW_COLS)
        wr.writerows(rows)
    return path


def export_suggestions(items: list, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["建议标识词", "来源", "出现联系人数", "推荐分", "理由",
                     "启用为成交标识(填 是)"])
        for r in items or []:
            wr.writerow([r["word"], r["source"], r["count"], r["score"],
                         r["why"], ""])
    return path


# ---------------------------------------------------------------- 批量判定
def judge_chats(chats: dict, cfg: dict = None, text_of=None) -> dict:
    """给 load_chats() 产出的会话批量判定。

    chats: {wxid: {"name"/"chat", "msgs":[...], 可选 remark/nick/alias/labels}}
    text_of: 可选 fn(c) -> str，默认拼最后 60 条消息。
    """
    cfg = cfg or load_config()
    out = {}
    for wxid, c in (chats or {}).items():
        m = meta_for(wxid, c)
        msgs = c.get("msgs") or []
        text = text_of(c) if text_of else "\n".join(
            str(x.get("content") or "") for x in msgs[-60:])
        out[wxid] = judge(
            wxid=wxid, name=c.get("name") or c.get("chat") or wxid,
            remark=m["remark"], nick=m["nick"], alias=m["alias"], desc=m["desc"],
            labels=m["labels"], text=text, cfg=cfg)
    return out
