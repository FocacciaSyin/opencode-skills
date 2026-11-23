"""從 OpenCode V2 的本機資料庫唯讀擷取 session，輸出 meta.json、精簡對話紀錄與分段清單。

供 reflect、recall、session-pickup 共用。
只使用 Python 標準函式庫；資料庫一律以 SQLite `mode=ro` 開啟，不寫入任何 OpenCode 資料。
輸出目錄必須是新的或空的目錄，避免混入前次執行的舊對話紀錄。
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
DAY_MS = 86_400_000

# 各類內容寫入對話紀錄時的截斷長度（字元）
LIMIT_USER = 2000
# 助理回覆的結論常在結尾，因此保留開頭與結尾兩段
LIMIT_ASSISTANT_HEAD = 800
LIMIT_ASSISTANT_TAIL = 400
LIMIT_TOOL_INPUT = 300
LIMIT_TOOL_ERROR = 400
LIMIT_SYNTHETIC = 400

# 敏感資訊遮蔽規則；順序有意義（Bearer 先於 JWT，避免殘留前綴）
REDACTIONS = [
    (re.compile(r"(?i)\b(password|pwd)\s*=\s*[^;\s\"']+"), r"\1=[REDACTED]"),
    # sqlcmd／bcp 的 -P 密碼、ijhttp 的 -P 私密參數，以及 --password 參數
    (re.compile(r"(?<!\S)-P\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"']+)"), "-P [REDACTED]"),
    (re.compile(r"(?i)(?<!\S)--password(?:\s+|=)(?:\"[^\"]*\"|'[^']*'|\S+)"), "--password [REDACTED]"),
    (
        re.compile(r"(?i)(\"?(?:api[_-]?key|secret|access[_-]?token|client[_-]?secret|token)\"?\s*[:=]\s*\"?)[^\"\s,;]{6,}"),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[JWT]"),
    (re.compile(r"(?:ghp_|gho_|ghu_|ghs_|github_pat_|sk-|xox[abp]-)[A-Za-z0-9_-]{16,}"), "[TOKEN]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"(?<!\d)09\d{2}-?\d{3}-?\d{3}(?!\d)"), "[PHONE]"),
    (re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?![A-Za-z0-9])"), "[ID]"),
]

# 工具輸入摘要時優先擷取的欄位
INPUT_KEYS = ["command", "path", "filePath", "pattern", "id", "agent", "description", "url", "query", "code"]


def redact(text: str) -> str:
    """遮蔽 email、密碼、token、手機與身分證號等敏感資訊。"""
    if not text:
        return text
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def clip(text: str, limit: int) -> str:
    """壓成單行並截斷，保留可讀性。"""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def clip_ends(text: str, head: int, tail: int) -> str:
    """壓成單行，超過長度時保留開頭與結尾，並標示省略的字數。"""
    text = " ".join(str(text).split())
    if len(text) <= head + tail:
        return text
    return f"{text[:head]}…（略 {len(text) - head - tail} 字）…{text[-tail:]}"


def open_db_readonly(path) -> sqlite3.Connection:
    """以唯讀模式開啟 SQLite；任何寫入都會被拒絕。"""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def norm_dir(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def is_under(directory: str | None, base: str) -> bool:
    if not directory:
        return False
    d = norm_dir(directory)
    return d == base or d.startswith(base.rstrip(os.sep) + os.sep)


def parse_json(text):
    try:
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return None


def load_jsonc(path: Path):
    """寬鬆解析 JSONC：去除字串外的註解與尾端逗號；失敗時回傳 None。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    out, i, n, in_str = [], 0, len(raw), False
    while i < n:
        ch = raw[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(raw[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif raw.startswith("//", i):
            while i < n and raw[i] != "\n":
                i += 1
            continue
        elif raw.startswith("/*", i):
            end = raw.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(ch)
        i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return parse_json(cleaned)


def ancestors(start: str) -> list[Path]:
    """由遠到近列出 start 的所有上層目錄（含自身）。"""
    p = Path(start).resolve()
    chain = [p, *p.parents]
    return list(reversed(chain))


def discover_skill_sources(directory: str, extra: list[str]) -> list[Path]:
    """依 OpenCode V2 的優先序（低到高）列出 skill 來源目錄。"""
    home = Path.home()
    config_dir = home / ".config" / "opencode"
    chain = ancestors(directory)
    sources: list[Path] = [home / ".claude" / "skills"]
    sources += [d / ".claude" / "skills" for d in chain]
    sources += [home / ".agents" / "skills"]
    sources += [d / ".agents" / "skills" for d in chain]
    sources += [config_dir / "skills"]
    sources += [d / ".opencode" / "skills" for d in chain]
    config_files = [config_dir / "opencode.json", config_dir / "opencode.jsonc"]
    for d in chain:
        config_files += [d / "opencode.json", d / "opencode.jsonc", d / ".opencode" / "opencode.json", d / ".opencode" / "opencode.jsonc"]
    for cfg in config_files:
        data = load_jsonc(cfg) if cfg.is_file() else None
        entries = data.get("skills") if isinstance(data, dict) else None
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, str) or entry.startswith(("http://", "https://")):
                    continue
                p = Path(os.path.expanduser(entry))
                sources.append(p if p.is_absolute() else Path(directory) / p)
    sources += [Path(x) for x in extra]
    seen, result = set(), []
    for s in sources:
        key = norm_dir(str(s))
        if key not in seen and s.is_dir():
            seen.add(key)
            result.append(s)
    return result


def index_skills(sources: list[Path]) -> dict[str, str]:
    """建立 skill ID → SKILL.md 路徑對照；後登記的來源覆蓋先前的同名 skill。"""
    index: dict[str, str] = {}
    for src in sources:
        for md in src.glob("*.md"):
            if md.name != "SKILL.md":
                index[md.stem] = str(md)
        base_depth = len(src.parts)
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
            if len(Path(root).parts) - base_depth > 6:
                dirs[:] = []
                continue
            if "SKILL.md" in files and Path(root) != src:
                index[Path(root).name] = str(Path(root) / "SKILL.md")
                dirs[:] = []
    return index


def discover_rule_files(directory: str) -> list[str]:
    files = [Path.home() / ".config" / "opencode" / "AGENTS.md"]
    for d in ancestors(directory):
        files += [d / "AGENTS.md", d / "CLAUDE.md"]
    return [str(f) for f in files if f.is_file()]


def summarize_input(inp) -> str:
    if not isinstance(inp, dict):
        return clip(json.dumps(inp, ensure_ascii=False), LIMIT_TOOL_INPUT)
    parts = [f"{k}={clip(inp[k], 160)}" for k in INPUT_KEYS if k in inp and inp[k] not in (None, "")]
    if not parts:
        parts = [clip(json.dumps(inp, ensure_ascii=False), LIMIT_TOOL_INPUT)]
    return clip(" ".join(parts), LIMIT_TOOL_INPUT)


def error_text(err) -> str:
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or err)
    return str(err or "")


def name_of(value) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or value.get("name") or value)
    return str(value or "")


class SessionAccumulator:
    """累積一棵 session 樹（根與所有子 session）的統計與對話紀錄。"""

    def __init__(self, exclude_skills: frozenset[str] = frozenset()):
        self.exclude_skills = exclude_skills
        self.lines: list[str] = []
        self.tool_calls = collections.Counter()
        self.tool_errors = collections.Counter()
        self.retry_after_error = collections.Counter()
        self.skills_loaded = collections.Counter()
        self.skills_mentioned = collections.Counter()
        self.errors: list[tuple[str, str]] = []
        self.counts = collections.Counter()
        self.loaded_self = False

    def add_session(self, sess: dict, messages: list[tuple], is_root: bool):
        header = "## 主 session" if is_root else "## 子 session"
        self.lines.append(f"\n{header} {sess['id']}（agent={sess.get('agent') or '?'}）\n")
        prev_tool = None
        for seq, mtype, data in messages:
            obj = parse_json(data) or {}
            prev_tool = self._render(seq, mtype, obj, prev_tool)

    def _render(self, seq, mtype, obj, prev_tool):
        add = self.lines.append
        if mtype == "user":
            self.counts["user_turns"] += 1
            skills = [name_of(s) for s in obj.get("skills") or []]
            for s in skills:
                self.skills_mentioned[s] += 1
                if s in self.exclude_skills:
                    self.loaded_self = True
            text = obj.get("text") or ""
            if any(f"@{sid}" in text for sid in self.exclude_skills):
                self.loaded_self = True
            suffix = f"（@skills: {', '.join(skills)}）" if skills else ""
            add(f"[U#{seq}] {clip(text, LIMIT_USER)}{suffix}")
        elif mtype == "assistant":
            self.counts["assistant_messages"] += 1
            for item in obj.get("content") or []:
                kind = item.get("type")
                if kind == "text" and item.get("text"):
                    add(f"[A#{seq}] {clip_ends(item['text'], LIMIT_ASSISTANT_HEAD, LIMIT_ASSISTANT_TAIL)}")
                elif kind == "tool":
                    prev_tool = self._tool(seq, item, prev_tool)
            if obj.get("error"):
                self.counts["assistant_errors"] += 1
                msg = error_text(obj["error"])
                self.errors.append(("(assistant)", msg))
                add(f"[A#{seq}] !! assistant error: {clip(msg, LIMIT_TOOL_ERROR)}")
        elif mtype == "skill":
            sid = obj.get("skill") or obj.get("name") or "?"
            self.skills_mentioned[sid] += 1
            if sid in self.exclude_skills:
                self.loaded_self = True
            add(f"[SKILL#{seq}] 使用者呼叫 skill：{sid}")
        elif mtype == "agent-switched":
            self.counts["agent_switches"] += 1
            add(f"[SWITCH#{seq}] agent {name_of(obj.get('previous'))} → {name_of(obj.get('agent'))}")
        elif mtype == "model-switched":
            self.counts["model_switches"] += 1
            add(f"[SWITCH#{seq}] model {name_of(obj.get('previous'))} → {name_of(obj.get('model'))}")
        elif mtype == "compaction":
            self.counts["compactions"] += 1
            add(f"[COMPACT#{seq}] reason={obj.get('reason') or '?'}")
        elif mtype == "synthetic":
            text = obj.get("text") or obj.get("description") or ""
            # system-reminder 多為模式提示等固定文字，只保留開頭以節省分析篇幅
            limit = 160 if text.lstrip().startswith("<system-reminder>") else LIMIT_SYNTHETIC
            add(f"[SYN#{seq}] {clip(text, limit)}")
        elif mtype == "shell":
            add(f"[SHELL#{seq}] {clip(obj.get('command') or '', LIMIT_TOOL_INPUT)} exit={obj.get('exit')}")
        elif mtype == "idle" and obj.get("error"):
            self.counts["idle_errors"] += 1
            msg = error_text(obj["error"])
            self.errors.append(("(idle)", msg))
            add(f"[IDLE#{seq}] !! {clip(msg, LIMIT_TOOL_ERROR)}")
        elif mtype == "location-switched":
            loc = obj.get("location") or {}
            add(f"[LOC#{seq}] → {loc.get('directory') if isinstance(loc, dict) else loc}")
        return prev_tool

    def _tool(self, seq, item, prev_tool):
        name = item.get("name") or "?"
        state = item.get("state") or {}
        status = state.get("status") or "?"
        inp = state.get("input")
        self.tool_calls[name] += 1
        if name == "subagent":
            self.counts["subagent_calls"] += 1
        if name == "skill" and isinstance(inp, dict) and inp.get("id"):
            self.skills_loaded[inp["id"]] += 1
            if inp["id"] in self.exclude_skills:
                self.loaded_self = True
        if prev_tool and prev_tool[1] == "error" and prev_tool[0] == name:
            self.retry_after_error[name] += 1
        line = f"[T#{seq}] {name} {status}: {summarize_input(inp)}"
        if status == "error":
            self.tool_errors[name] += 1
            msg = error_text(state.get("error"))
            self.errors.append((name, msg))
            line += f" !! {clip(msg, LIMIT_TOOL_ERROR)}"
        self.lines.append(line)
        return (name, status)


def iter_messages(conn, session_id: str):
    """逐筆讀取單一 session 的訊息；不一次載入，避免含大量工具輸出的 session 耗盡記憶體。"""
    yield from conn.execute(
        "select seq, type, data from session_message where session_id = ? order by time_created, seq",
        (session_id,),
    )


def balance_by_directory(candidates: list[tuple], sessions: dict) -> list[tuple]:
    """依主 session 的目錄分組後輪流取出：每輪各組取最新一筆，組的順序依其最新 session 由新到舊。"""
    groups: dict[str, list[tuple]] = collections.OrderedDict()
    for cand in candidates:  # candidates 已由新到舊排序
        key = norm_dir(sessions[cand[1]].get("directory") or "")
        groups.setdefault(key, []).append(cand)
    ordered, depth = [], 0
    while len(ordered) < len(candidates):
        for items in groups.values():
            if depth < len(items):
                ordered.append(items[depth])
        depth += 1
    return ordered


def cap_transcript(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.6), int(limit * 0.4)
    return f"{text[:head]}\n\n…（中間省略 {len(text) - head - tail} 字）…\n\n{text[-tail:]}"


def make_slices(records: list[dict], budget: int) -> list[dict]:
    """依時間順序切成連續分段；6 個以上 session 至少 3 段，讓模式可跨段交叉比對。"""
    n = len(records)
    if n == 0:
        return []
    total = sum(r["chars"] for r in records)
    count = max(1, math.ceil(total / budget))
    if n >= 6:
        count = max(count, 3)
    count = min(count, n)
    slices, current, acc = [], [], 0
    for i, rec in enumerate(records):
        current.append(rec)
        acc += rec["chars"]
        remaining_records = n - i - 1
        remaining_slices = count - len(slices) - 1
        target = total * (len(slices) + 1) / count
        if remaining_slices > 0 and (acc >= target or remaining_records == remaining_slices):
            slices.append(current)
            current = []
    if current:
        slices.append(current)
    return [
        {
            "index": i + 1,
            "sessions": [r["id"] for r in group],
            "transcripts": [r["transcript"] for r in group],
            "chars": sum(r["chars"] for r in group),
        }
        for i, group in enumerate(slices)
    ]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="唯讀擷取 OpenCode session，供 reflect、recall、session-pickup 分析")
    p.add_argument("--db", default=str(DEFAULT_DB), help="OpenCode 資料庫路徑")
    p.add_argument("--out", help="輸出目錄（需為新的或空的目錄）；省略時建立暫存目錄")
    p.add_argument("--label", default="session-extract", help="暫存輸出目錄的名稱前綴，通常帶呼叫的 skill 名稱")
    p.add_argument(
        "--session",
        action="append",
        default=[],
        help="只取指定的 session（可重複）；子 session 會換成所屬的主 session。指定時忽略時間窗、目錄、--max-sessions 與所有排除條件",
    )
    p.add_argument("--exclude-skill", action="append", default=[], help="排除載入或呼叫過此 skill 的 session，可重複")
    p.add_argument(
        "--directory",
        action="append",
        default=[],
        help="只取此目錄底下的 session，可重複指定多個目錄（預設目前目錄）",
    )
    p.add_argument("--all-projects", action="store_true", help="不依目錄篩選，納入所有專案")
    p.add_argument(
        "--balance-projects",
        action="store_true",
        help="依 session 目錄分組輪流選取（各組由新到舊），避免單一專案佔滿 --max-sessions",
    )
    p.add_argument("--days", type=float, default=14, help="回溯天數（預設 14）")
    p.add_argument("--since", help="起始時間（ISO 格式，如 2026-11-01）；指定時覆蓋 --days")
    p.add_argument("--max-sessions", type=int, default=40, help="最多選取的根 session 數（預設 40）")
    p.add_argument("--exclude-session", action="append", default=[], help="排除的 session ID，可重複")
    p.add_argument("--no-env-exclude", action="store_true", help="不自動排除 OPENCODE_SESSION_ID")
    p.add_argument("--max-chars", type=int, default=40000, help="單一對話紀錄上限字元數（預設 40000）")
    p.add_argument("--slice-budget", type=int, default=250000, help="單一分段上限字元數（預設 250000）")
    p.add_argument("--skills-dir", action="append", default=[], help="額外的 skill 來源目錄，可重複")
    return p


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(json.dumps({"error": f"找不到資料庫：{db}"}, ensure_ascii=False))
        return 2
    if args.out:
        out = Path(args.out)
        if out.exists() and any(out.iterdir()):
            print(json.dumps({"error": f"輸出目錄不是空的：{out}"}, ensure_ascii=False))
            return 2
        out.mkdir(parents=True, exist_ok=True)
    else:
        label = re.sub(r"[^A-Za-z0-9_.-]", "-", args.label) or "session-extract"
        out = Path(tempfile.mkdtemp(prefix=f"{label}-"))
    (out / "transcripts").mkdir(exist_ok=True)

    if args.since:
        since_ms = int(datetime.fromisoformat(args.since).timestamp() * 1000)
    else:
        since_ms = int(time.time() * 1000 - args.days * DAY_MS)
    directories = args.directory or [os.getcwd()]
    base_dirs = [norm_dir(d) for d in directories]
    excluded = set(args.exclude_session)
    env_sid = os.environ.get("OPENCODE_SESSION_ID")
    if env_sid and not args.no_env_exclude:
        excluded.add(env_sid)
    exclude_skills = frozenset(args.exclude_skill)
    explicit = list(dict.fromkeys(args.session))
    not_found: list[str] = []

    conn = open_db_readonly(db)
    try:
        cols = "id, parent_id, directory, title, agent, model, cost, tokens_input, tokens_output, time_created, time_updated"
        sessions = {row[0]: dict(zip([c.strip() for c in cols.split(",")], row)) for row in conn.execute(f"select {cols} from session_v2")}
        children = collections.defaultdict(list)
        for s in sessions.values():
            if s["parent_id"] and s["parent_id"] in sessions:
                children[s["parent_id"]].append(s["id"])

        def tree(root_id):
            order, queue = [], [root_id]
            while queue:
                sid = queue.pop(0)
                order.append(sid)
                queue.extend(sorted(children.get(sid, []), key=lambda c: sessions[c]["time_created"] or 0))
            return order

        def root_of(sid):
            seen = set()
            while sessions[sid]["parent_id"] in sessions and sid not in seen:
                seen.add(sid)
                sid = sessions[sid]["parent_id"]
            return sid

        candidates = []
        if explicit:
            # 明確指定的 session：不套用時間窗、目錄與排除條件
            roots = []
            for sid in explicit:
                if sid not in sessions:
                    not_found.append(sid)
                    continue
                root = root_of(sid)
                if root not in roots:
                    roots.append(root)
            for root in roots:
                ids = tree(root)
                candidates.append((max(sessions[i]["time_updated"] or 0 for i in ids), root, ids))
        else:
            for s in sessions.values():
                if s["parent_id"] and s["parent_id"] in sessions:
                    continue
                if not args.all_projects and not any(is_under(s["directory"], b) for b in base_dirs):
                    continue
                ids = tree(s["id"])
                last = max(sessions[i]["time_updated"] or 0 for i in ids)
                if last >= since_ms:
                    candidates.append((last, s["id"], ids))
        candidates.sort(reverse=True)
        if args.balance_projects and not explicit:
            candidates = balance_by_directory(candidates, sessions)

        stats = collections.Counter(sessions_in_window=len(candidates))
        selected = []
        for last, root_id, ids in candidates:
            if not explicit and len(selected) >= args.max_sessions:
                break
            if not explicit and root_id in excluded:
                stats["sessions_excluded"] += 1
                continue
            acc = SessionAccumulator(frozenset() if explicit else exclude_skills)
            for sid in ids:
                acc.add_session(sessions[sid], iter_messages(conn, sid), is_root=(sid == root_id))
            if acc.loaded_self:
                stats["sessions_skipped_skill"] += 1
                continue
            selected.append((last, root_id, ids, acc))
    finally:
        conn.close()

    selected.sort(key=lambda x: x[0])
    records, totals = [], {
        "tool_calls": collections.Counter(),
        "tool_errors": collections.Counter(),
        "retry_after_error": collections.Counter(),
        "skills_loaded": collections.Counter(),
        "skills_mentioned": collections.Counter(),
        "counts": collections.Counter(),
    }
    error_groups: dict[tuple[str, str], dict] = {}
    cost_total = 0.0
    for idx, (last, root_id, ids, acc) in enumerate(selected, 1):
        root = sessions[root_id]
        title = redact(root.get("title") or "")
        cost = sum(sessions[i]["cost"] or 0 for i in ids)
        cost_total += cost
        header = [
            f"# {root_id}",
            f"- 標題：{title}",
            f"- 目錄：{root.get('directory')}",
            f"- 期間：{datetime.fromtimestamp((root['time_created'] or 0) / 1000):%Y-%m-%d %H:%M} ~ "
            f"{datetime.fromtimestamp(last / 1000):%Y-%m-%d %H:%M}",
            f"- 子 session 數：{len(ids) - 1}",
            "",
            "標記：U=使用者 A=助理 T=工具呼叫 SKILL=使用者呼叫 skill SWITCH=切換 COMPACT=壓縮 SYN=系統合成 "
            "SHELL=使用者 shell IDLE=閒置錯誤；#後為訊息序號（seq），引用證據時使用。",
        ]
        body = cap_transcript(redact("\n".join(header + acc.lines)), args.max_chars)
        rel = f"transcripts/{idx:03d}-{root_id}.md"
        (out / rel).write_text(body, encoding="utf-8")

        for (tool_name, msg) in acc.errors:
            key_msg = re.sub(r"\d+", "#", clip(redact(msg.splitlines()[0] if msg else ""), 160))
            g = error_groups.setdefault((tool_name, key_msg), {"tool": tool_name, "message": key_msg, "count": 0, "sessions": []})
            g["count"] += 1
            if root_id not in g["sessions"] and len(g["sessions"]) < 3:
                g["sessions"].append(root_id)

        totals["tool_calls"].update(acc.tool_calls)
        totals["tool_errors"].update(acc.tool_errors)
        totals["retry_after_error"].update(acc.retry_after_error)
        totals["skills_loaded"].update(acc.skills_loaded)
        totals["skills_mentioned"].update(acc.skills_mentioned)
        totals["counts"].update(acc.counts)
        records.append(
            {
                "id": root_id,
                "title": title,
                "directory": root.get("directory"),
                "agent": root.get("agent"),
                "time_created": root.get("time_created"),
                "time_last": last,
                "child_sessions": len(ids) - 1,
                "cost": round(cost, 4),
                "tool_calls": dict(acc.tool_calls),
                "tool_errors": dict(acc.tool_errors),
                "retry_after_error": dict(acc.retry_after_error),
                "skills_loaded": dict(acc.skills_loaded),
                "skills_mentioned": dict(acc.skills_mentioned),
                **dict(acc.counts),
                "transcript": rel,
                "chars": len(body),
            }
        )

    slices = make_slices(records, args.slice_budget)
    skill_index = index_skills(discover_skill_sources(directories[0], args.skills_dir))
    # 規則檔涵蓋指定目錄與每個選入 session 的目錄（跨專案分析時各專案的 AGENTS.md 都要列出）
    rule_files = list(dict.fromkeys(
        f for d in [*directories, *(r["directory"] for r in records if r["directory"])] for f in discover_rule_files(d)
    ))
    by_directory = collections.Counter(r["directory"] for r in records)
    used_ids = set(totals["skills_loaded"]) | set(totals["skills_mentioned"])
    error_rate = {
        name: round(totals["tool_errors"][name] / calls, 3)
        for name, calls in totals["tool_calls"].items()
        if calls >= 5 and totals["tool_errors"][name]
    }
    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "params": {
            "db": str(db),
            "directory": None if args.all_projects else directories,
            "all_projects": args.all_projects,
            "balance_projects": args.balance_projects,
            "since": None if explicit else datetime.fromtimestamp(since_ms / 1000).isoformat(timespec="seconds"),
            "max_sessions": args.max_sessions,
            "sessions": explicit,
            "exclude_skills": sorted(exclude_skills),
        },
        "stats": {
            "sessions_in_window": stats["sessions_in_window"],
            "sessions_selected": len(records),
            "sessions_excluded": stats["sessions_excluded"],
            "sessions_skipped_skill": stats["sessions_skipped_skill"],
            "sessions_not_found": not_found,
            "sessions_by_directory": dict(by_directory.most_common()),
            "slices": len(slices),
        },
        "totals": {
            "cost": round(cost_total, 4),
            "tool_calls": dict(totals["tool_calls"].most_common()),
            "tool_errors": dict(totals["tool_errors"].most_common()),
            "tool_error_rate": dict(sorted(error_rate.items(), key=lambda kv: -kv[1])),
            "retry_after_error": dict(totals["retry_after_error"].most_common()),
            "skills_loaded": dict(totals["skills_loaded"].most_common()),
            "skills_mentioned_by_user": dict(totals["skills_mentioned"].most_common()),
            **dict(totals["counts"]),
            "top_errors": sorted(error_groups.values(), key=lambda g: -g["count"])[:20],
        },
        "skills_used": [{"id": sid, "path": skill_index.get(sid)} for sid in sorted(used_ids)],
        "skills_installed": len(skill_index),
        "rule_files": rule_files,
        "sessions": records,
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "slices.json").write_text(json.dumps(slices, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out), **meta["stats"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
