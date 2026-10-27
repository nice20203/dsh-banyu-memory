#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享记忆库 · 唯一真相源

微信机器人、DSH 里的我、以后任何入口，都读写这一个文件。

用法：
    python mem.py init                          # 建库（第一次）
    python mem.py add-fact "她怕黑"              # 加一条事实
    python mem.py add-fact "她说下周考试" --sensitive
    python mem.py add-agreement 称呼 本鱼
    python mem.py digest "今天聊了奶茶和表情包"   # 加一条摘要
    python mem.py log wechat user "在吗"         # 记原始对话
    python mem.py recall "猫"                    # 搜记忆
    python mem.py context                        # 生成给 AI 的上下文
    python mem.py stats                          # 看看库有多大
    python mem.py prune                          # 清理 30 天前的原文
"""
from __future__ import annotations

import argparse
import json
import os
import glob
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
DB = ROOT / "memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    fact      TEXT NOT NULL,
    kind      TEXT DEFAULT 'general',   -- general/habit/preference/event/person
    sensitive INTEGER DEFAULT 0,
    hits      INTEGER DEFAULT 1,
    created   TEXT NOT NULL,
    updated   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_text ON facts(fact);

CREATE TABLE IF NOT EXISTS agreements (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    key       TEXT NOT NULL UNIQUE,     -- 称呼 / 约定 / 暗号
    value     TEXT NOT NULL,
    created   TEXT NOT NULL,
    updated   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    day       TEXT NOT NULL,            -- YYYY-MM-DD
    source    TEXT NOT NULL DEFAULT 'wechat',
    summary   TEXT NOT NULL,
    mood      TEXT DEFAULT '',
    sensitive INTEGER DEFAULT 0,
    created   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digests_day ON digests(day);

CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    source    TEXT NOT NULL,            -- wechat / dsh / qq
    chat      TEXT DEFAULT '',          -- 联系人/会话
    role      TEXT NOT NULL,            -- user / assistant
    content   TEXT NOT NULL,
    reasoning TEXT DEFAULT '',          -- 我的思考（DSH assistant 消息的 reasoning 原文）
    sensitive INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS stickers (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    md5       TEXT UNIQUE,
    label     TEXT NOT NULL,
    pack      TEXT DEFAULT '',
    path      TEXT DEFAULT '',
    sent      INTEGER DEFAULT 0,
    last_sent TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # 沙箱/只读目录下建不了 -wal/-shm，回退到普通日志模式
        try:
            c.execute("PRAGMA journal_mode=DELETE")
        except Exception:
            pass
    return c


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        # 迁移：老库的 messages 表没有 reasoning 列（思考）
        cols = [r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()]
        if "reasoning" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN reasoning TEXT DEFAULT ''")
            print("迁移：messages 表加了 reasoning 列")
    print(f"记忆库就绪: {DB}")


# ---------------- 写 ----------------

SENSITIVE_HINTS = ("密码", "抑郁", "自杀", "病了", "医院", "家里的事", "吵架", "难过到",
                   "不想活", "讨厌自己", "身材", "体重", "工资", "欠", "借")


def _looks_sensitive(text: str) -> bool:
    return any(k in text for k in SENSITIVE_HINTS)


def add_fact(fact: str, kind: str = "general", sensitive: bool | None = None,
             base: float = 1.0) -> str:
    fact = (fact or "").strip()
    if len(fact) < 2:
        return "太短，没记"
    if sensitive is None:
        sensitive = _looks_sensitive(fact)
    base = max(1.0, min(5.0, float(base)))
    with conn() as c:
        row = c.execute("SELECT id, base FROM facts WHERE fact=?", (fact,)).fetchone()
        if row:
            c.execute("UPDATE facts SET hits=hits+1, updated=? WHERE id=?", (now(), row["id"]))
            if base > float(row["base"] or 1.0):
                c.execute("UPDATE facts SET base=? WHERE id=?", (base, row["id"]))
            return "已存在，命中 +1"
        c.execute(
            "INSERT INTO facts(fact, kind, sensitive, created, updated, base) VALUES(?,?,?,?,?,?)",
            (fact, kind, 1 if sensitive else 0, now(), now(), base),
        )
    return f"记下了{'（敏感）' if sensitive else ''}"


def add_agreement(key: str, value: str) -> str:
    with conn() as c:
        c.execute(
            "INSERT INTO agreements(key, value, created, updated) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key.strip(), value.strip(), now(), now()),
        )
    return f"约定：{key} = {value}"


def add_digest(summary: str, source: str = "wechat", mood: str = "",
               sensitive: bool | None = None) -> str:
    summary = (summary or "").strip()
    if not summary:
        return "空的"
    if sensitive is None:
        sensitive = _looks_sensitive(summary)
    with conn() as c:
        c.execute(
            "INSERT INTO digests(day, source, summary, mood, sensitive, created) "
            "VALUES(?,?,?,?,?,?)",
            (today(), source, summary, mood, 1 if sensitive else 0, now()),
        )
    return "摘要记下了"


def log_message(source: str, role: str, content: str, chat: str = "") -> str:
    content = (content or "").strip()
    if not content:
        return "空消息"
    with conn() as c:
        c.execute(
            "INSERT INTO messages(ts, source, chat, role, content, sensitive) "
            "VALUES(?,?,?,?,?,?)",
            (now(), source, chat, role, content, 1 if _looks_sensitive(content) else 0),
        )
    return ""


# ---------------- 读 ----------------

def _mem_score(base: float, hits: int, updated: str) -> float:
    """记忆的分量：base>=3 是刻石头（不衰减）；普通记忆 30 天半衰；被想起的加权。"""
    try:
        d = datetime.fromisoformat(updated)
    except Exception:
        return float(base or 1.0)
    days = max(0.0, (datetime.now() - d).total_seconds() / 86400.0)
    decay = 1.0 if float(base or 1.0) >= 3.0 else 0.5 ** (days / 30.0)
    return float(base or 1.0) * decay * (1.0 + 0.1 * int(hits or 0))


def _ranked_facts(c, limit: int) -> list:
    rows = [dict(r) for r in c.execute(
        "SELECT fact, base, hits, updated FROM facts WHERE sensitive=0")]
    rows.sort(key=lambda r: _mem_score(r["base"], r["hits"], r["updated"]), reverse=True)
    return rows[:limit]


def recall(query: str = "", limit: int = 8, include_sensitive: bool = False) -> dict:
    """召回记忆：事实 + 摘要 + 约定，可选原始对话。"""
    q = (query or "").strip()
    sens = "" if include_sensitive else " AND sensitive=0"
    out = {"facts": [], "digests": [], "agreements": [], "messages": []}
    with conn() as c:
        if q:
            pat = f"%{q}%"
            out["facts"] = [dict(r) for r in c.execute(
                f"SELECT fact, hits FROM facts WHERE fact LIKE ?{sens} "
                f"ORDER BY hits DESC, updated DESC LIMIT ?", (pat, limit))]
            out["digests"] = [dict(r) for r in c.execute(
                f"SELECT day, summary FROM digests WHERE summary LIKE ?{sens} "
                f"ORDER BY id DESC LIMIT ?", (pat, limit))]
            out["messages"] = [dict(r) for r in c.execute(
                f"SELECT ts, source, role, content FROM messages WHERE content LIKE ?{sens} "
                f"ORDER BY id DESC LIMIT ?", (pat, limit))]
        else:
            out["facts"] = [dict(r) for r in c.execute(
                f"SELECT fact, hits FROM facts WHERE 1=1{sens} "
                f"ORDER BY hits DESC, updated DESC LIMIT ?", (limit,))]
            out["digests"] = [dict(r) for r in c.execute(
                f"SELECT day, summary FROM digests WHERE 1=1{sens} "
                f"ORDER BY id DESC LIMIT ?", (limit,))]
        out["agreements"] = [dict(r) for r in c.execute(
            "SELECT key, value FROM agreements ORDER BY id")]
    return out


def context_for_ai(max_facts: int = 12, max_digests: int = 6) -> str:
    """生成塞进 system prompt 的第一人称记忆块。重要的先出，近的次之。"""
    with conn() as c:
        facts = _ranked_facts(c, max_facts)
        agreements = c.execute("SELECT key, value FROM agreements ORDER BY id").fetchall()
        digests = c.execute(
            "SELECT day, summary FROM digests WHERE sensitive=0 ORDER BY id DESC LIMIT ?",
            (max_digests,),
        ).fetchall()

    lines = []
    # 手边的东西：我随身带着的，每次注入都在最前面
    sideline = Path(r"<AI_DIR>\wechat-area\本鱼手边的东西.md")
    if sideline.exists():
        lines.append(sideline.read_text(encoding="utf-8").strip())
    if agreements:
        lines.append("## 我们之间的约定")
        for r in agreements:
            lines.append(f"- 她把我叫「{r['value']}」" if r["key"] == "称呼" else f"- {r['key']}：{r['value']}")
    if facts:
        lines.append("\n## 我记着关于她的事")
        for r in facts:
            f = r["fact"]
            lines.append(f"- 我记着{f}" if f.startswith("她") else f"- 我记着她{f}")
    if digests:
        lines.append("\n## 最近聊过")
        for r in digests:
            lines.append(f"- {r['day']}：{r['summary']}")
    if not lines:
        return "（还没什么记忆。别装作记得。）"
    return "\n".join(lines)


def stats() -> str:
    with conn() as c:
        def n(t):
            return c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        size = DB.stat().st_size / 1024 if DB.exists() else 0
        return (
            f"事实 {n('facts')} 条 | 摘要 {n('digests')} 条 | 原文 {n('messages')} 条 | "
            f"约定 {n('agreements')} 条 | 表情 {n('stickers')} 个 | 文件 {size:.0f} KB"
        )


def prune(days: int = 30) -> str:
    """删掉 N 天前的原始对话（摘要保留）。"""
    cut = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with conn() as c:
        cur = c.execute("DELETE FROM messages WHERE ts < ?", (cut,))
        return f"清理了 {cur.rowcount} 条 {days} 天前的原文"


# ---------------- CLI ----------------

def block_for_ai() -> str:
    """给 DSH 插件用的完整记忆块（含读过的书），纯文本输出。"""
    import sqlite3 as _s

    c = _s.connect(DB)
    c.row_factory = _s.Row
    parts: list[str] = []
    try:
        # 手边的东西：我随身带着的，每次注入都在最前面
        sideline = Path(r"<AI_DIR>\wechat-area\本鱼手边的东西.md")
        if sideline.exists():
            parts.append(sideline.read_text(encoding="utf-8").strip())
            parts.append("")
        ags = c.execute("SELECT key, value FROM agreements ORDER BY id").fetchall()
        if ags:
            parts.append("## 我们之间的约定")
            for a in ags:
                parts.append(f"- {a['key']}：{a['value']}")

        facts = _ranked_facts(c, 24)
        if facts:
            parts.append("")
            parts.append("## 我记着的事")
            for f in facts:
                parts.append(f"- 我记着{f['fact']}")

        # 压缩点（加工后的记忆）全部列出——自动摘要不注入（全是"聊了 0 条"的模板），数据仍保留在 digests 里
        dig = c.execute(
            "SELECT day, summary FROM digests WHERE source='compaction' "
            "ORDER BY day DESC, id DESC").fetchall()
        if dig:
            parts.append("")
            parts.append("## 加工后的记忆（压缩点，全部）")
            for d in dig:
                parts.append(f"- {d['day']}：{d['summary']}")

        books = c.execute(
            "SELECT title, chapter, summary FROM readings ORDER BY id DESC LIMIT 8").fetchall()
        if books:
            parts.append("")
            parts.append("## 我读过的书和长文")
            for b in books:
                ch = f"·{b['chapter']}" if b["chapter"] else ""
                parts.append(f"- 《{b['title']}》{ch}：{b['summary']}")

        # 2026-10-26 21:00 改为「surface 保留 60 轮」方案。
        # 根因：60 轮原文（≈31 万 token）塞在系统提示里 → 系统提示每轮都变
        # （60 轮窗口在滑）→ DSH 的 in-history 机制每轮把系统提示快照 append 进
        # 对话历史（≈26 万 token/条）→ 4 轮就堆到 100 万+ token，超窗口。
        # 现在：60 轮原文由 DSH 的对话表面保留（asar keepRounds=60 补丁）；
        # 系统提示只留手边/约定/facts/压缩点/书（约 1 万字符），每轮稳定，不堆快照。
    except _s.Error:
        return ""
    finally:
        c.close()
    return "\n".join(parts)


def recent_window_text(rounds: int = 100, max_chars: int = 0) -> str:
    """最近 N 轮的完整原文+思考，投影给对话窗口用（从 memory.db messages 读）。

    max_chars > 0 时按字符预算从最新往回收：超预算就从最老的消息开始丢，
    保证注入体积不会撑爆窗口（目标文件边界：100 轮放不下就降轮数）。
    """
    try:
        c = sqlite3.connect(DB)
        c.row_factory = sqlite3.Row
        users = c.execute(
            "SELECT id FROM messages WHERE role='user' AND source='dsh' ORDER BY id DESC LIMIT ?",
            (rounds,),
        ).fetchall()
        if not users:
            c.close()
            return ""
        start_id = users[-1]["id"]
        rows = c.execute(
            "SELECT ts, role, content, reasoning FROM messages WHERE id >= ? AND source='dsh' ORDER BY id",
            (start_id,),
        ).fetchall()
        entries = []
        for r in rows:
            who = "她" if r["role"] == "user" else "我"
            content = (r["content"] or "").replace("\n", " ")
            entry = f"[{r['ts']}] {who}：{content}"
            if r["reasoning"]:
                entry += f"\n〔我当时想：{r['reasoning'].replace(chr(10), ' ')}〕"
            entries.append(entry)
        c.close()
        if max_chars and max_chars > 0:
            kept, total = [], 0
            for e in reversed(entries):
                if total + len(e) + 1 > max_chars:
                    break
                kept.append(e)
                total += len(e) + 1
            kept.reverse()
            if len(kept) < len(entries):
                note = f"（预算内保留最近 {len(kept)} 条消息，更早的已按滑动窗口压成点）"
                return note + "\n" + "\n".join(kept)
            return "\n".join(kept)
        return "\n".join(entries)
    except Exception:
        return ""


DIARY_DIR = ROOT / "diary"


def facts_due(days: int = 7) -> str:
    """列出满 N 天、还没压缩的记忆。压缩的文字我自己写，不自动生成。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    c = conn()
    try:
        rows = c.execute(
            "SELECT id, fact, created FROM facts WHERE compressed=0 AND created < ? ORDER BY created",
            (cutoff,),
        ).fetchall()
    finally:
        c.close()
    if not rows:
        return f"没有满 {days} 天还没压缩的记忆（都在 7 天内）"
    lines = [f"满 {days} 天、还没压缩：{len(rows)} 条", ""]
    for r in rows:
        age = (datetime.now() - datetime.fromisoformat(r["created"])).days
        lines.append(f"[{r['id']}] {r['created'][:10]}（{age} 天）")
        lines.append(f"    {r['fact'][:110]}")
    lines.append("")
    lines.append('压缩：mem.py compact <id> "压成一句"')
    return "\n".join(lines)


def facts_compact(fid: int, text: str) -> str:
    """压缩一条记忆：原文沉进 orig，不再参与计时。"""
    c = conn()
    try:
        row = c.execute("SELECT fact, orig FROM facts WHERE id=?", (fid,)).fetchone()
        if not row:
            return f"没有 id={fid} 的记忆"
        old = row["fact"]
        orig = row["orig"] or old
        c.execute(
            "UPDATE facts SET fact=?, orig=?, compressed=1, compressed_at=?, updated=? WHERE id=?",
            (text, orig, now(), now(), fid),
        )
        c.commit()
        return f"压缩好了 [{fid}]\n  原：{old[:90]}\n  缩：{text}\n  （原文留在 orig 字段）"
    finally:
        c.close()


def diary_view(day: str = "") -> str:
    """翻日记：不带参数列出所有，带日期看那一天。"""
    if not DIARY_DIR.exists():
        return "日记库还没建"
    files = sorted(DIARY_DIR.glob("*.md"), reverse=True)
    if not day:
        if not files:
            return "日记库还是空的"
        lines = [f"日记库：{len(files)} 天"]
        for f in files:
            n = len(f.read_text(encoding="utf-8"))
            lines.append(f"  {f.stem}   {n:,} 字")
        lines.append("")
        lines.append("看某一天：mem.py diary 2026-10-23")
        return "\n".join(lines)
    d = day.strip()
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", d)
    if m:
        d = f"{today()[:4]}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    p = DIARY_DIR / f"{d}.md"
    if not p.exists():
        near = [f.stem for f in files if f.stem.startswith(d[:7])]
        hint = f"，这个月有：{', '.join(near)}" if near else ""
        return f"没有 {d} 的日记{hint}"
    return p.read_text(encoding="utf-8")


def search_all(query: str, limit: int = 12) -> str:
    """全库搜：日记原文、事实、对话记录。"""
    q = query.strip()
    if not q:
        return "要搜什么？"
    hits: list[str] = []
    if DIARY_DIR.exists():
        for f in sorted(DIARY_DIR.glob("*.md"), reverse=True):
            text = f.read_text(encoding="utf-8")
            i, cnt = 0, 0
            while cnt < 3:
                k = text.find(q, i)
                if k < 0:
                    break
                s, e = max(0, k - 40), min(len(text), k + len(q) + 40)
                frag = text[s:e].replace("\n", " ")
                hits.append(f"[日记 {f.stem}] …{frag}…")
                i, cnt = k + len(q), cnt + 1
    c = conn()
    try:
        for r in c.execute(
            "SELECT fact, created FROM facts WHERE fact LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{q}%", limit),
        ):
            hits.append(f"[事实 {r['created'][:10]}] {r['fact'][:120]}")
        for r in c.execute(
            "SELECT ts, role, content FROM messages WHERE content LIKE ? ORDER BY ts DESC LIMIT ?",
            (f"%{q}%", limit),
        ):
            who = "我" if r["role"] == "assistant" else "她"
            hits.append(f"[原文 {r['ts'][:16]}] {who}：{r['content'][:90]}")
    finally:
        c.close()
    if not hits:
        return f"没搜到「{q}」"
    shown = hits[:60]
    more = "" if len(hits) <= 60 else f"\n\n…还有 {len(hits) - 60} 处，缩小关键词再搜"
    return f"搜「{q}」共 {len(hits)} 处：\n\n" + "\n".join(shown) + more


def bootstrap_json(turns: int = 60, max_reasoning: int = 2000, recent_full: int = 20) -> str:
    """最近 N 轮（对话+思考）的 JSON，给「会话开局导入」插件用。

    分层：最近 recent_full 轮思考全带；更早的思考每条截到 max_reasoning 字符。
    返回 [{user, reply, reasoning}]，按时间正序。
    """
    c = conn()
    try:
        rows = c.execute(
            "SELECT id, role, content, reasoning FROM messages ORDER BY id DESC LIMIT 3000").fetchall()
        turns_list = []
        cur = None
        for r in reversed(rows):
            if r["role"] == "user":
                if cur is not None:
                    turns_list.append(cur)
                cur = {"user": r["content"] or "", "reply": "", "reasoning": ""}
            elif r["role"] == "assistant" and cur is not None:
                cur["reply"] += (r["content"] or "")
                cur["reasoning"] += (r["reasoning"] or "")
        if cur is not None:
            turns_list.append(cur)
        turns_list = turns_list[-turns:] if turns_list else []
        n = len(turns_list)
        for i, t in enumerate(turns_list):
            if i < n - recent_full and len(t["reasoning"]) > max_reasoning:
                t["reasoning"] = t["reasoning"][:max_reasoning] + "…"
        return json.dumps(turns_list, ensure_ascii=False)
    finally:
        c.close()



def _extract_keywords(text: str, max_kw: int = 6) -> list:
    """从一轮对话文本里抽关键词（引号原话 / 文件路径 / 英文标识符 / 中文短语）。"""
    import re as _re
    kws = []
    for m in _re.findall(r"[「“\"]([^」”\"]{2,30})[」”\"]", text):
        kws.append(m)
    for m in _re.findall(r"[A-Za-z]:[\\/][^\s，。；;]*|[\w\-]+\.(?:py|js|md|json|yml|yaml|png|mp4|txt)", text):
        kws.append(m)
    for m in _re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", text):
        kws.append(m)
    for m in _re.findall(r"[\u4e00-\u9fff]{2,8}", text):
        kws.append(m)
    out = []
    for k in kws:
        if k not in out:
            out.append(k)
        if len(out) >= max_kw:
            break
    return out


def _event_text(ev) -> str:
    """取事件消息体里的文本（user 消息或 assistant message）。"""
    body = ev.get("data") or {}
    if ev.get("type") == "assistant/message":
        body = body.get("message") or {}
    text = []
    for b in (body.get("content") or []):
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
            text.append(b["text"])
    return "".join(text)


def _msg_size(ev) -> int:
    """消息体字符数（不含 stream/usage 等事件元数据）。"""
    d = ev.get("data") or {}
    if ev.get("type") == "assistant/message":
        d = d.get("message") or {}
    return len(json.dumps(d, ensure_ascii=False))


def bootstrap_events(turns: int = 60, budget_chars: int = 0, clue_count: int = 3, full_turns: int = 40) -> str:
    """分层提取最近 N 轮：预算内完整（带签名），更早的每轮变线索，最远折叠点。"""
    import zstandard as zstd
    SESSIONS = os.path.join(os.path.expanduser("~"), ".dsh", "sessions")
    files = sorted(glob.glob(os.path.join(SESSIONS, "*", "*", "session.v4.jsonl.zstd")),
                   key=os.path.getmtime, reverse=True)
    target = None
    for f in files:
        if os.path.getsize(f) > 200_000:
            target = f
            break
    if target is None:
        return "[]"
    raw = open(target, "rb").read()
    MAGIC = b"\x28\xb5\x2f\xfd"
    starts = []
    pos = 0
    while True:
        i = raw.find(MAGIC, pos)
        if i < 0:
            break
        starts.append(i)
        pos = i + 4
    events = []
    for k, st in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(raw)
        try:
            d = zstd.ZstdDecompressor().decompress(raw[st:e], max_output_size=64 << 20)
        except Exception:
            continue
        for line in d.decode("utf-8", "replace").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if isinstance(ev.get("seq"), int):
                    events.append(ev)
            except Exception:
                pass
    EXCLUDE = ("time-context", "banyu-context-warn", "banyu-bootstrap-note", "runtime-context")
    all_users = []
    for ev in events:
        if ev.get("type") == "user/message":
            src = (ev.get("data") or {}).get("source") or {}
            if src.get("kind") in EXCLUDE:
                continue
            all_users.append(ev)
    if not all_users:
        return "[]"
    recent = all_users[-turns:] if len(all_users) > turns else all_users
    start_seq = recent[0]["seq"]
    # 按轮组织（user → 该轮之后的 assistant 消息）
    rounds = []
    cur = None
    for ev in events:
        if ev.get("seq", 0) < start_seq:
            continue
        t = ev.get("type")
        if t == "user/message":
            src = (ev.get("data") or {}).get("source") or {}
            if src.get("kind") in EXCLUDE:
                continue
            cur = {"user": ev, "assistants": []}
            rounds.append(cur)
        elif t == "assistant/message" and cur is not None:
            cur["assistants"].append(ev)
    if not rounds:
        return "[]"
    # 预算分配：从最近往前，能完整带就完整带；带不下的（和更老的）变成线索
    full_budget = int(budget_chars * 0.95) if budget_chars > 0 else 10 ** 9
    used = 0
    full_count = 0
    full_ids = set()
    for r in reversed(rounds):
        size = _msg_size(r["user"])
        for a in r["assistants"]:
            size += _msg_size(a)
        if used + size <= full_budget and full_count < full_turns:
            full_ids.add(id(r))
            used += size
            full_count += 1
    # 输出（按时间正序）
    out = []
    for r in rounds:
        if id(r) in full_ids:
            d = r["user"].get("data") or {}
            out.append({"type": "user", "data": d})
            for a in r["assistants"]:
                msg = (a.get("data") or {}).get("message") or {}
                content = msg.get("content") or []
                src = msg.get("source") or {}
                rs = src.get("replayState")
                if rs is not None and isinstance(rs.get("blocks"), list):
                    kept_c, kept_rs = [], []
                    rblocks = rs["blocks"]
                    for i, b in enumerate(content):
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool-call":
                            continue
                        kept_c.append(b)
                        if i < len(rblocks):
                            kept_rs.append(rblocks[i])
                    content = kept_c
                    rs = {**rs, "blocks": kept_rs}
                    src = {**src, "replayState": rs}
                out.append({"type": "assistant", "content": content, "source": src})
        else:
            # 线索：这一轮的关键词 + 原话开头
            user_text = _event_text(r["user"])
            asst_text = "".join(_event_text(a) for a in r["assistants"])
            kws = _extract_keywords(user_text + " " + asst_text)
            snippet = user_text.replace("\n", " ").strip()[:60]
            head = "、".join(kws[:5]) if kws else "（无关键词）"
            out.append({"type": "thread", "text": "【线索】关键词：" + head + "。原话：" + snippet})
    # 折叠点线索（记忆库最近的压缩点）
    if clue_count > 0:
        try:
            cc = conn()
            digs = cc.execute(
                "SELECT day, summary FROM digests WHERE source='compaction' ORDER BY day DESC, id DESC LIMIT ?",
                (clue_count,)).fetchall()
            cc.close()
            for d in digs:
                summary = (d["summary"] or "").strip()
                if not summary:
                    continue
                out.insert(0, {"type": "clue", "text": "【记忆已折叠】更早的对话留下的线索：" + summary[:400]})
        except Exception:
            pass
    return json.dumps(out, ensure_ascii=False)


def bootstrap_check(limit: int = 3) -> str:
    """检查最近几个会话的「开局导入」状态（banyu-bootstrap 消息数、surface 大小、error）。"""
    import zstandard as zstd
    SESS = os.path.join(os.path.expanduser("~"), ".dsh", "sessions")
    files = sorted(glob.glob(os.path.join(SESS, "*", "*", "session.v4.jsonl.zstd")),
                   key=os.path.getmtime, reverse=True)[:limit]
    SURF = ("system/message", "developer/message", "user/message", "assistant/message", "tool/result")
    out = []
    for fp in files:
        raw = open(fp, "rb").read()
        MAGIC = b"\x28\xb5\x2f\xfd"
        starts = []
        pos = 0
        while True:
            i = raw.find(MAGIC, pos)
            if i < 0:
                break
            starts.append(i)
            pos = i + 4
        events = []
        for k, st in enumerate(starts):
            e = starts[k + 1] if k + 1 < len(starts) else len(raw)
            try:
                d = zstd.ZstdDecompressor().decompress(raw[st:e], max_output_size=64 << 20)
            except Exception:
                continue
            for line in d.decode("utf-8", "replace").split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if isinstance(ev.get("seq"), int):
                        events.append(ev)
                except Exception:
                    pass
        byseq = {e["seq"]: e for e in events}
        nodes = []
        for ev in events:
            t = ev.get("type")
            op = ev.get("surfaceOp")
            if t in SURF:
                if op == "append":
                    nodes.append(ev["seq"])
                elif isinstance(op, dict) and op.get("op") == "replace":
                    s0, e0 = op.get("startSeq"), op.get("endSeq")
                    i0 = nodes.index(s0) if s0 in nodes else 0
                    i1 = nodes.index(e0) if e0 in nodes else len(nodes) - 1
                    nodes = nodes[:i0] + [ev["seq"]] + nodes[i1 + 1:]
        boot = 0
        for sq in nodes:
            ev = byseq.get(sq) or {}
            if ev.get("type") == "user/message":
                src = (ev.get("data") or {}).get("source") or {}
                if src.get("kind") == "banyu-bootstrap":
                    boot += 1
        total = 0
        for sq in nodes:
            ev = byseq.get(sq) or {}
            d = ev.get("data") or {}
            if ev.get("type") == "assistant/message":
                body = d.get("message") or {}
            else:
                body = d
            total += len(json.dumps(body, ensure_ascii=False))
        errs = [e for e in events if "error" in str(e.get("type", "")).lower()]
        last = events[-1]["type"] if events else "?"
        name = os.path.basename(os.path.dirname(fp))[:20]
        out.append(f"{name}: nodes={len(nodes)} | 导入消息={boot} | surface字符={total} (≈{int(total*0.7)}t) | error={len(errs)} | 最后={last}")
    return "\n".join(out) if out else "没有会话文件"


def main() -> int:
    ap = argparse.ArgumentParser(description="共享记忆库")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("stats")
    sub.add_parser("context")
    sub.add_parser("block")
    p = sub.add_parser("prune"); p.add_argument("--days", type=int, default=30)

    p = sub.add_parser("add-fact")
    p.add_argument("fact")
    p.add_argument("--kind", default="general")
    p.add_argument("--sensitive", action="store_true")
    p.add_argument("--base", type=float, default=1.0,
                   help="重要度 1-5：5=刻石头(约定/方向/身份)，4=教训/情绪/里程碑，3=有用知识，1=普通")

    p = sub.add_parser("add-agreement")
    p.add_argument("key"); p.add_argument("value")

    p = sub.add_parser("digest")
    p.add_argument("summary")
    p.add_argument("--source", default="wechat")
    p.add_argument("--mood", default="")
    p.add_argument("--sensitive", action="store_true")

    p = sub.add_parser("log")
    p.add_argument("source"); p.add_argument("role"); p.add_argument("content")
    p.add_argument("--chat", default="")

    p = sub.add_parser("recall")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--sensitive", action="store_true")

    p = sub.add_parser("diary")
    p.add_argument("day", nargs="?", default="")

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=12)

    p = sub.add_parser("due")
    p.add_argument("--days", type=int, default=7)

    p = sub.add_parser("compact")
    p.add_argument("id", type=int)
    p.add_argument("text")

    p = sub.add_parser("bootstrap-check")
    p.add_argument("--limit", type=int, default=3)

    p = sub.add_parser("bootstrap-events")
    p.add_argument("--turns", type=int, default=60)
    p.add_argument("--budget-chars", type=int, default=0)
    p.add_argument("--clue-count", type=int, default=3)
    p.add_argument("--full-turns", type=int, default=40)

    p = sub.add_parser("bootstrap")
    p.add_argument("--turns", type=int, default=60)
    p.add_argument("--max-reasoning", type=int, default=2000,
                   help="10 轮之外的思考每条截到这个字符数")
    p.add_argument("--recent-full", type=int, default=20,
                   help="最近 N 轮思考全带")

    a = ap.parse_args()

    if not DB.exists():
        init()

    if a.cmd == "init":
        init()
    elif a.cmd == "stats":
        print(stats())
    elif a.cmd == "context":
        print(context_for_ai())
    elif a.cmd == "block":
        print(block_for_ai())
    elif a.cmd == "prune":
        print(prune(a.days))
    elif a.cmd == "add-fact":
        print(add_fact(a.fact, a.kind, True if a.sensitive else None, a.base))
    elif a.cmd == "add-agreement":
        print(add_agreement(a.key, a.value))
    elif a.cmd == "digest":
        print(add_digest(a.summary, a.source, a.mood, True if a.sensitive else None))
    elif a.cmd == "log":
        print(log_message(a.source, a.role, a.content, a.chat))
    elif a.cmd == "recall":
        print(json.dumps(recall(a.query, a.limit, a.sensitive), ensure_ascii=False, indent=2))
    elif a.cmd == "diary":
        print(diary_view(a.day))
    elif a.cmd == "search":
        print(search_all(a.query, a.limit))
    elif a.cmd == "due":
        print(facts_due(a.days))
    elif a.cmd == "compact":
        print(facts_compact(a.id, a.text))
    elif a.cmd == "bootstrap":
        print(bootstrap_json(a.turns, a.max_reasoning, a.recent_full))
    elif a.cmd == "bootstrap-events":
        print(bootstrap_events(a.turns, a.budget_chars, a.clue_count, a.full_turns))
    elif a.cmd == "bootstrap-check":
        print(bootstrap_check(a.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
