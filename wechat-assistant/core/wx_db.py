# -*- coding: utf-8 -*-
"""wx_db.py —— 微信 4.x 本地数据库解密与批量导出（完全内置，零外部 skill 依赖）。

做的事：
  1. 用 SQLCipher 4 的参数解密本机微信 message_*.db / contact.db / session.db
  2. 按会话归集消息，输出统一格式的 all_messages.jsonl + index.csv
  3. 所有读写都在本机，不联网、不上传、不改微信任何文件

设计约束：
  * 只读取，不写入微信数据目录；微信运行时也允许共享读
  * 依赖 pycryptodome / zstandard（已随安装包内置）
  * 密钥由 wx_bridge.py 提供，本模块不保存、不打印密钥
"""
from __future__ import annotations

import collections
import csv
import datetime
import hashlib
import hmac as hmac_mod
import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile

try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None
try:
    import zstandard
except ImportError:
    zstandard = None

PAGE_SZ = 4096
RESERVE = 80
TZ = datetime.timezone(datetime.timedelta(hours=8))
_dctx = zstandard.ZstdDecompressor() if zstandard else None


def deps_ok() -> tuple[bool, str]:
    missing = []
    if AES is None:
        missing.append("pycryptodome")
    if zstandard is None:
        missing.append("zstandard")
    return (not missing), ("缺少依赖：" + "、".join(missing) if missing else "解密组件就绪")


# ---------------------------------------------------------------- 共享读
def shared_read(path: str):
    """微信运行时 DB 文件被独占，必须带 FILE_SHARE_READ|WRITE|DELETE 打开。"""
    import ctypes
    import ctypes.wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.CreateFileW(path, 0x80000000, 7, None, 3, 0, None)
    if h in (-1, 0xFFFFFFFFFFFFFFFF):
        raise OSError("无法打开（被独占？）: %s err=%d" % (path, ctypes.get_last_error()))
    total = ctypes.c_longlong(0)
    k32.GetFileSizeEx(h, ctypes.byref(total))
    out = bytearray()
    buf = ctypes.create_string_buffer(1 << 20)
    got = w.DWORD(0)
    try:
        while True:
            if not k32.ReadFile(h, buf, len(buf), ctypes.byref(got), None) or got.value == 0:
                break
            out += buf.raw[:got.value]
            if len(out) >= total.value:
                break
    finally:
        k32.CloseHandle(h)
    return bytes(out)


# ---------------------------------------------------------------- SQLCipher 4
def derive_enc_key(key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", key, salt, 256000, 32)


def derive_mac_key(ek: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", ek, bytes(b ^ 0x3A for b in salt), 2, 32)


def page1_hmac_ok(key: bytes, page1: bytes) -> bool:
    salt = page1[:16]
    ek = derive_enc_key(key, salt)
    mk = derive_mac_key(ek, salt)
    h = hmac_mod.new(mk, page1[16:4032], hashlib.sha512)
    h.update(struct.pack("<I", 1))
    return h.digest() == page1[4032:4096]


def hmac_ok_file(key: bytes, path: str) -> bool:
    """用任意一页校验密钥能否解锁该文件，是「自动选账号」的关键。"""
    try:
        d = shared_read(path)
    except OSError:
        return False
    return len(d) >= PAGE_SZ and page1_hmac_ok(key, d[:PAGE_SZ])


def decrypt_page(ek: bytes, data: bytes, pgno: int) -> bytes:
    iv = data[PAGE_SZ - RESERVE:PAGE_SZ - RESERVE + 16]
    if pgno == 1:
        enc = data[16:PAGE_SZ - RESERVE]
        return (b"SQLite format 3\x00"
                + AES.new(ek, AES.MODE_CBC, iv).decrypt(enc)
                + b"\x00" * RESERVE)
    enc = data[:PAGE_SZ - RESERVE]
    return AES.new(ek, AES.MODE_CBC, iv).decrypt(enc) + b"\x00" * RESERVE


def decrypt_file(key: bytes, src: str, dst: str):
    data = shared_read(src)
    if len(data) < PAGE_SZ:
        raise ValueError("文件过小: " + src)
    salt = data[:16]
    ek = derive_enc_key(key, salt)
    if not page1_hmac_ok(key, data[:PAGE_SZ]):
        raise ValueError("HMAC 失败（密钥不对或文件损坏）: " + src)
    n = (len(data) + PAGE_SZ - 1) // PAGE_SZ
    with open(dst, "wb") as fo:
        for i in range(n):
            chunk = data[i * PAGE_SZ:(i + 1) * PAGE_SZ]
            if len(chunk) < PAGE_SZ:
                chunk += b"\x00" * (PAGE_SZ - len(chunk))
            fo.write(decrypt_page(ek, chunk, i + 1))


# ---------------------------------------------------------------- 内容解码
def as_text(v) -> str:
    d = v
    if isinstance(d, bytes) and d[:4] == b"\x28\xb5\x2f\xfd" and _dctx:
        try:
            d = _dctx.decompress(d)
        except Exception:
            pass
    if d is None:
        return ""
    if isinstance(d, bytes):
        return d.decode("utf-8", "replace")
    return str(d)


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]*>", "", s)
    for a, b in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                 ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(a, b)
    return s.strip()


def render_row(lt: int, raw) -> tuple[str, str]:
    """还原消息类型与可读文本。图片/语音等只留类型标签，不还原媒体。"""
    t16 = lt & 0xFFFF
    raw = as_text(raw)
    if t16 == 1:
        return "文本", raw or ""
    if t16 == 3:
        return "图片", "[图片]"
    if t16 == 34:
        m = re.search(r'voicelength="(\d+)"', raw)
        return "语音", ("[语音 %.1f 秒]" % (float(m.group(1)) / 1000)) if m else "[语音]"
    if t16 == 43:
        m = re.search(r'duration="?(\d+)"?', raw) or re.search(r'length="?(\d+)"?', raw)
        return "视频", ("[视频 %.1f 秒]" % (float(m.group(1)) / 1000)) if m else "[视频]"
    if t16 == 47:
        m = re.search(r'alias="([^"]*)"', raw)
        if m and m.group(1).strip():
            return "表情", "[表情：%s]" % strip_tags(m.group(1))
        return "表情", "[表情]"
    if t16 == 49:
        title = des = ""
        m = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", raw, re.S)
        title = m.group(1) if m else ""
        m = re.search(r"<des><!\[CDATA\[(.*?)\]\]></des>", raw, re.S)
        des = m.group(1) if m else ""
        if not title:
            m = re.search(r"<title>([^<]*)</title>", raw, re.S)
            title = m.group(1).strip() if m else ""
        title, des = strip_tags(title), strip_tags(des)
        shown = title
        if des and des not in shown and "请点此升级" not in des:
            shown = (shown + " - " + des) if shown else des
        return "链接/卡片", shown or "[消息卡片]"
    if t16 == 50:
        return "通话", "[视频通话]" if "视频" in raw else "[语音通话]"
    if t16 == 10000:
        return "系统", strip_tags(raw) or "系统消息"
    return "其它", raw[:200] or ("[类型%d]" % t16)


def query(sql: str, path: str):
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        cur = con.cursor()
        cur.execute(sql)
        return cur.fetchall()
    finally:
        con.close()


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s).strip()[:60] or "unnamed"


def kind_of(uname: str) -> str:
    if uname.startswith("gh_"):
        return "公众号"
    if uname.endswith("@chatroom"):
        return "群聊"
    if uname in ("filehelper", "brandsessionholder", "brandservicesessionholder"):
        return "系统"
    if uname.startswith("@placeholder"):
        return "系统"
    return "单聊"


def find_account_dirs(data_root: str) -> list:
    out = []
    try:
        for name in os.listdir(data_root):
            p = os.path.join(data_root, name)
            if os.path.isdir(p) and os.path.isdir(os.path.join(p, "db_storage")):
                out.append(p)
    except OSError:
        pass
    return out


def unlocked_account(key: bytes, data_root: str):
    """遍历账号目录，返回第一个能被这把密钥解锁的目录；密钥与账号不对应时返回 None。"""
    for d in find_account_dirs(data_root):
        for rel in ("message/message_0.db", "contact/contact.db", "session/session.db"):
            p = os.path.join(d, "db_storage", *rel.split("/"))
            if os.path.isfile(p) and hmac_ok_file(key, p):
                return d
    return None


# ---------------------------------------------------------------- 批量导出
def export_all(key_hex: str, data_root: str, out_dir: str,
               since_days: int = 180, kinds=("单聊",),
               max_per_chat: int = 5000, progress=None) -> dict:
    """一次解密全部消息库，归集成 all_messages.jsonl。返回统计字典。

    progress: 可选的回调 fn(stage:str, done:int, total:int)
    """
    ok, msg = deps_ok()
    if not ok:
        raise RuntimeError(msg)
    key_hex = (key_hex or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", key_hex):
        raise ValueError("数据库密钥格式不对（应为 64 位十六进制）")
    key = bytes.fromhex(key_hex)

    def tick(stage, done=0, total=0):
        if progress:
            try:
                progress(stage, done, total)
            except Exception:
                pass

    acct = unlocked_account(key, data_root)
    if not acct:
        raise RuntimeError("这把密钥解不开 %s 下任何账号目录（可能换了微信账号）" % data_root)
    tick("已锁定账号目录")

    base = os.path.basename(acct)
    m = re.match(r"^(.*)_[0-9a-fA-F]{2,8}$", base)
    self_wxid = m.group(1) if m else ""
    storage = os.path.join(acct, "db_storage")
    wanted = set(kinds or ())
    tmp = tempfile.mkdtemp(prefix="wxassist_")
    result = {"account": base, "chats": 0, "messages": 0, "jsonl": "", "index": ""}
    try:
        # 通讯录：备注名、昵称、别名、描述全部保留。
        # 这些字段是「离线识别成交客户」的主要依据——很多销售把成交标识
        # 写在备注名里（如「张三 已成交」「李四-付」），只留一个显示名会漏。
        cmap = {}
        label_names = []
        cp = os.path.join(storage, "contact", "contact.db")
        if os.path.isfile(cp):
            try:
                cdb = os.path.join(tmp, "contact.db")
                decrypt_file(key, cp, cdb)
                cols = {r[1] for r in query('PRAGMA table_info("contact")', cdb)}
                desc_col = '"description"' if "description" in cols else "''"
                for r in query(
                        "SELECT username, remark, nick_name, alias, %s FROM contact"
                        % desc_col, cdb):
                    cmap[r[0]] = {
                        "remark": str(r[1] or ""), "nick": str(r[2] or ""),
                        "alias": str(r[3] or ""), "desc": str(r[4] or ""),
                        "labels": [],
                    }
                # 微信 4.x 本地只存「标签定义」，不存「联系人↔标签」关联表，
                # 因此把标签名清单导出，交给识别器当候选标识词 / 供用户勾选。
                try:
                    label_names = [str(x[0]) for x in query(
                        "SELECT label_name_ FROM contact_label ORDER BY sort_order_", cdb)
                        if str(x[0] or "").strip()]
                except Exception:
                    label_names = []
            except Exception:
                pass
        tick("已读取通讯录与标签")

        # 会话最近时间
        last_ts = {}
        sp = os.path.join(storage, "session", "session.db")
        if os.path.isfile(sp):
            try:
                decrypt_file(key, sp, os.path.join(tmp, "session.db"))
                last_ts = {u: (t or 0) for u, t in
                           query("SELECT username, last_timestamp FROM SessionTable",
                                 os.path.join(tmp, "session.db"))}
            except Exception:
                pass

        cutoff = (int(datetime.datetime.now(TZ).timestamp()) - since_days * 86400
                  if since_days and since_days > 0 else 0)
        targets = {}
        cmeta = {}

        def add_target(u, info):
            info = info or {}
            targets[u] = (info.get("remark") or info.get("nick")
                          or info.get("alias") or u)
            cmeta[u] = {
                "remark": info.get("remark", ""), "nick": info.get("nick", ""),
                "alias": info.get("alias", ""), "desc": info.get("desc", ""),
                "labels": list(info.get("labels") or []),
            }

        for u, ts in last_ts.items():
            if ts < cutoff:
                continue
            if wanted and kind_of(u) not in wanted:
                continue
            add_target(u, cmap.get(u))
        # 会话表没覆盖到的联系人（有消息但无 session 记录）也从通讯录补
        for u, info in cmap.items():
            if u not in targets and (not wanted or kind_of(u) in wanted):
                if not cutoff:
                    add_target(u, info)
        tick("已筛选会话", 0, len(targets))

        msg_dir = os.path.join(storage, "message")
        try:
            db_files = sorted(f for f in os.listdir(msg_dir)
                              if re.match(r"message_\d+\.db$", f))
        except OSError:
            db_files = []
        # 反查表：把「会话 → Msg_<md5>」预计算好，解密后直接取库里真实存在的表，
        # 避免对上千个会话逐个尝试 SQL（实测能省掉一大半耗时）
        md5_to_uname = {"Msg_" + hashlib.md5(u.encode()).hexdigest(): u
                        for u in targets}
        per_chat = collections.defaultdict(list)
        for i, f in enumerate(db_files, 1):
            src = os.path.join(msg_dir, f)
            try:
                dst = os.path.join(tmp, f)
                decrypt_file(key, src, dst)   # 内含 HMAC 校验，密钥不对会直接抛
                con = sqlite3.connect("file:%s?mode=ro" % dst, uri=True)
                try:
                    cur = con.cursor()
                    tables = {t[0] for t in cur.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    if "Name2Id" not in tables:
                        continue
                    idmap = dict(cur.execute("SELECT rowid, user_name FROM Name2Id"))
                    vals = set(idmap.values())
                    if self_wxid not in vals:
                        others = {v for v in vals if v and not v.endswith("@chatroom")
                                  and not v.startswith("gh_")}
                        if len(others) == 1:
                            self_wxid = others.pop()
                    # 只遍历这个库里真实存在的会话表
                    for tbl in tables & md5_to_uname.keys():
                        uname = md5_to_uname[tbl]
                        for r in cur.execute(
                                'SELECT local_id, local_type, real_sender_id, '
                                'create_time, message_content FROM "%s"' % tbl):
                            who = idmap.get(r[2])
                            if who not in (self_wxid, uname):
                                continue
                            per_chat[uname].append((r[0], r[1], who, r[3], r[4]))
                finally:
                    con.close()
            except Exception:
                continue
            tick("正在解密消息库", i, len(db_files))
            # 及时释放，避免大账号占内存
            for x in os.listdir(tmp):
                if x.startswith("message_") and os.path.isfile(os.path.join(tmp, x)):
                    try:
                        os.remove(os.path.join(tmp, x))
                    except OSError:
                        pass

        os.makedirs(out_dir, exist_ok=True)
        jsonl_path = os.path.join(out_dir, "all_messages.jsonl")
        index_path = os.path.join(out_dir, "index.csv")
        contacts_path = os.path.join(out_dir, "contacts.json")
        labels_path = os.path.join(out_dir, "contact_labels.json")

        # 联系人元数据单独存一份：识别器、设置界面、核对清单都从这里读。
        # 用户也可以在 contacts.json 里给某个 wxid 手工补 "labels": ["已成交"]，
        # 或在 data/radar_config.json 的 contacts_file 里提供标签文件。
        with open(contacts_path, "w", encoding="utf-8") as cf:
            json.dump({"labels_all": label_names, "contacts": cmap}, cf,
                      ensure_ascii=False)
        with open(labels_path, "w", encoding="utf-8") as lf:
            json.dump(label_names, lf, ensure_ascii=False, indent=2)

        index = []
        with open(jsonl_path, "w", encoding="utf-8") as jf:
            for uname, rows in per_chat.items():
                if len(rows) < 2:
                    continue
                disp = targets.get(uname, uname)
                meta = cmeta.get(uname, {})
                rows.sort(key=lambda x: (x[3], x[0]))
                if max_per_chat and len(rows) > max_per_chat:
                    rows = rows[-max_per_chat:]
                n_self = n_other = 0
                first = last = ""
                for lid, lt, who, ct, mc in rows:
                    is_self = (who == self_wxid)
                    kind, content = render_row(lt, mc)
                    try:
                        ts = datetime.datetime.fromtimestamp(ct, TZ).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue
                    n_self += is_self
                    n_other += not is_self
                    if not first:
                        first = ts
                    last = ts
                    rec = {"chat": disp, "chat_wxid": uname,
                           "sender": "我" if is_self else disp,
                           "is_self": bool(is_self), "ts": ts,
                           "type": kind, "content": content}
                    # 昵称/备注/别名/标签是成交识别的关键依据，随每条消息带上，
                    # 下游流式读取时任意一条都能还原这个会话的标识信息。
                    for k in ("remark", "nick", "alias"):
                        if meta.get(k):
                            rec[k] = meta[k]
                    if meta.get("labels"):
                        rec["labels"] = meta["labels"]
                    jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                index.append([disp, uname, kind_of(uname), len(rows), n_self, n_other,
                              first, last])
        index.sort(key=lambda x: -x[3])
        with open(index_path, "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["会话", "wxid", "类型", "总条数", "我发出", "对方发", "起始", "最近"])
            wr.writerows(index)
        result.update({"chats": len(index), "messages": sum(r[3] for r in index),
                       "jsonl": jsonl_path, "index": index_path,
                       "contacts": contacts_path, "labels": labels_path,
                       "label_count": len(label_names)})
        tick("导出完成")
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
