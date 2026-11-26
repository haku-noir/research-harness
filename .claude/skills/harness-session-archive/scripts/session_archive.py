"""Claude Code の会話を、研究ハブに残す。

- 生の記録（JSONL）を records/sessions/raw/<会話の ID>.jsonl に写す（git の外。.gitignore）。
- 要約するための読める形を records/sessions/raw/digest/<会話の ID>.md に書く（git の外）。
  研究者の発言・Claude の返答の本文・道具の呼び出しの 1 行・選択肢での質問と回答を、時刻と uuid つきで載せる。
  分岐した会話は、各発言を元の会話（記録の各行の sessionId）に持たせ、同じ発言を二重に載せない。
- 要約のファイル records/sessions/<開始日>_<ID の先頭 8 文字>.md（git で管理）を用意する。
  - 無ければ、ひな形（中身は「要約待ち」）を作る。
  - あれば、<!-- auto:start --> 〜 <!-- auto:end --> の間（会話の ID、期間、生の記録への参照、ハブのコミット）だけを書き直す。
  - 要約の本文は Claude が書く（.claude/skills/harness-session-archive/SKILL.md）。このスクリプトは書かない。
- 一覧 records/sessions/README.md を作り直す。要約が要る会話（未要約、または要約した範囲の後に発言が増えた会話）を示す。

対象の会話は hub/session-sources.txt に書いた作業ディレクトリ（その下のフォルダを含む）で開いた会話。
「! <会話の ID の先頭>」の行で、その会話を除ける。

使い方:
  python -X utf8 session_archive.py            （全部を更新。変わった会話だけを読み直す）
  python -X utf8 session_archive.py --pending  （更新して、要約が要る会話を出す）
  python -X utf8 session_archive.py --hook     （Claude Code のフックから。標準入力は読み捨て、失敗しても 0 で終わる）
  python -X utf8 session_archive.py --list     （対象の会話を一覧するだけ）
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HUB = Path(__file__).resolve().parents[4]
OUT = HUB / "records" / "sessions"
RAW = OUT / "raw"
DIGEST = RAW / "digest"
CACHE = RAW / ".index.json"
CACHE_VERSION = 2  # 読み方を変えたら上げる。上がると、すべての会話を一度読み直す
SOURCES = HUB / "hub" / "session-sources.txt"
PROJECTS = Path.home() / ".claude" / "projects"
TRAILER = "Hub-Session"

REMINDER_RX = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
SKIP_PREFIXES = (
    "<system-reminder", "<local-command", "<side-sessions", "<task-notification",
    "[Request interrupted", "<ci-monitor-event", "<user-prompt-submit-hook",
)
CHORE_RX = re.compile(r"^chore\((sessions|links)\)!?:")
SECRET = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<メールアドレス>"),
    (re.compile(r"\b(sk-ant-[\w-]{10,}|sk-[\w-]{20,}|ghp_\w{20,}|github_pat_\w{20,}|xox[abpr]-[\w-]{10,})"), "<秘密の値>"),
]


def redact(t: str) -> str:
    for rx, rep in SECRET:
        t = rx.sub(rep, t)
    return t


def encode(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def norm(p: str) -> str:
    return str(Path(p).resolve()).lower().rstrip("\\/")


def load_sources():
    dirs, excludes = [], []
    if not SOURCES.exists():
        return [str(HUB)], []
    # BOM 付きでも読めるように utf-8-sig。行の途中の「 #」より後は注釈として読み飛ばす
    for line in SOURCES.read_text(encoding="utf-8-sig").splitlines():
        line = re.split(r"\s#", line, maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            x = line[1:].strip()
            if x:  # 「!」だけの行で、すべての会話を除いてしまわないように
                excludes.append(x)
        else:
            dirs.append(line)
    if str(HUB) not in dirs:
        dirs.insert(0, str(HUB))
    return dirs, excludes


def local(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError, TypeError):
        return None


def candidate_files(dirs):
    """会話のファイル。Claude Code のフォルダにあるものを優先し、消えたもの（既定で 30 日）はハブに写した生の記録を使う。"""
    files = {}
    if PROJECTS.exists():
        encs = [encode(d).lower() for d in dirs]  # Windows ではフォルダの名前の大文字・小文字がそろわないことがある
        for p in PROJECTS.iterdir():
            n = p.name.lower()
            if p.is_dir() and any(n == e or n.startswith(e + "-") for e in encs):
                for f in p.glob("*.jsonl"):
                    files[f.stem] = f
    if RAW.exists():
        for f in RAW.glob("*.jsonl"):
            files.setdefault(f.stem, f)
    return list(files.values())


def message_text(content):
    if isinstance(content, list):
        return " ".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    return content if isinstance(content, str) else ""


def user_text(content):
    """研究者の発言の本文。先に <system-reminder> の囲みを除いてから、機械が足した行かを判定する。"""
    t = REMINDER_RX.sub("", message_text(content)).strip()
    if not t:
        return ""
    if "<command-name>" in t:  # スラッシュコマンド：コマンドの名前と引数を残す
        name = re.search(r"<command-name>(.*?)</command-name>", t, re.S)
        args = re.search(r"<command-args>(.*?)</command-args>", t, re.S)
        return " ".join(x.group(1).strip() for x in (name, args) if x and x.group(1).strip())
    if t.startswith(SKIP_PREFIXES):
        return ""
    return t


def one_line(s, n=120):
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_summary(b):
    name, inp = b.get("name", "?"), b.get("input", {}) or {}
    if name == "AskUserQuestion":
        return None  # 質問は別に載せる
    for key in ("description", "file_path", "path", "pattern", "query", "url", "title", "skill"):
        if inp.get(key):
            return f"{name}（{one_line(inp[key])}）"
    if inp.get("command"):
        return f"{name}（{one_line(inp['command'], 80)}）"
    return name


def ask_text(inp):
    out = []
    for q in inp.get("questions", []) or []:
        opts = " / ".join(o.get("label", "") for o in q.get("options", []) or [])
        out.append(f"- 問い：{one_line(q.get('question', ''), 300)}（選択肢：{opts}）")
    return "\n".join(out)


def tool_result_text(c):
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return str(c or "")


def parse(path: Path):
    """会話を、発言の並び（uuid、種類、時刻、本文）に直す。"""
    sid, cwd, title_c, title_a = path.stem, None, None, None
    items, ask_ids = [], {}
    cur = None  # 続いている Claude の返答
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = d.get("type")
            if typ == "custom-title" and d.get("customTitle"):
                title_c = d["customTitle"]
            elif typ == "ai-title" and d.get("aiTitle"):
                title_a = d["aiTitle"]
            if d.get("cwd") and not cwd:
                cwd = d["cwd"]
            if typ == "attachment" and not d.get("isSidechain"):
                # Claude が作業している間に研究者が送った発言。ほかの会話（peer）や作業の知らせは除く
                a = d.get("attachment") or {}
                origin = (a.get("origin") or {}).get("kind")
                if (a.get("type") == "queued_command" and a.get("commandMode") == "prompt"
                        and not a.get("isMeta") and origin in (None, "human")):
                    t = user_text(a.get("prompt"))
                    if t:
                        items.append({"uuid": d.get("uuid"), "label": d.get("sessionId") or sid, "kind": "user",
                                      "ts": d.get("timestamp") or a.get("timestamp"), "text": t, "queued": True})
                        cur = None
                continue
            if d.get("isSidechain") or typ not in ("user", "assistant"):
                continue
            ts, uuid, label = d.get("timestamp"), d.get("uuid"), d.get("sessionId") or sid
            msg = d.get("message", {}) or {}
            content = msg.get("content")
            if typ == "user":
                if d.get("isCompactSummary"):
                    items.append({"uuid": uuid, "label": label, "kind": "note", "ts": ts, "text": "（ここで会話が要約され、続きの会話になった）"})
                    cur = None
                    continue
                if d.get("isMeta"):
                    continue
                if isinstance(content, list):
                    for x in content:
                        if isinstance(x, dict) and x.get("type") == "tool_result" and x.get("tool_use_id") in ask_ids:
                            ans = tool_result_text(x.get("content"))
                            items.append({"uuid": f"{uuid}-a", "label": label, "kind": "answer", "ts": ts,
                                          "text": ask_ids[x["tool_use_id"]] + "\n- 研究者の回答：" + one_line(ans, 2000)})
                            cur = None
                t = user_text(content)
                if t and not t.startswith("This session is being continued"):
                    items.append({"uuid": uuid, "label": label, "kind": "user", "ts": ts, "text": t})
                    cur = None
            else:
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                        ask_ids[b.get("id")] = ask_text(b.get("input", {}) or {})
                    if b.get("type") not in ("text", "tool_use"):
                        continue
                    if cur is None:
                        cur = {"uuid": uuid, "label": label, "kind": "claude", "ts": ts, "text": [], "tools": []}
                        items.append(cur)
                    cur["te"] = ts or cur.get("te")  # 返答が続いた最後の時刻（期間と、要約が要るかの判定に使う）
                    if b["type"] == "text" and b.get("text", "").strip():
                        cur["text"].append(b["text"].strip())
                    elif b["type"] == "tool_use":
                        s = tool_summary(b)
                        if s:
                            cur["tools"].append(s)
    for it in items:
        if it["kind"] == "claude":
            it["text"] = "\n\n".join(it["text"])
    items = [it for it in items if it["kind"] != "claude" or it["text"] or it["tools"]]
    # 作業中に送った発言は、あとで普通の発言としても記録されることがある。同じ本文があれば、その方を残す
    said = {re.sub(r"\s+", " ", i["text"]).strip() for i in items if i["kind"] == "user" and not i.get("queued")}
    items = [i for i in items if not (i.get("queued") and re.sub(r"\s+", " ", i["text"]).strip() in said)]
    title = title_c or title_a or next((one_line(i["text"], 40) for i in items if i["kind"] == "user"), sid[:8])
    times = [local(i.get("te") or i["ts"]) for i in items]
    first = next((local(i["ts"]) for i in items if local(i["ts"])), None)
    last = max((t for t in times if t), default=None)
    return {"id": sid, "cwd": cwd, "title": title, "items": items, "first": first, "last": last}


def demote(md: str) -> str:
    out, fence = [], False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and re.match(r"#{1,6} ", line):
            line = "**" + line.lstrip("#").strip() + "**"
        out.append(line)
    return "\n".join(out)


def quote(md: str) -> str:
    return "\n".join("> " + l if l.strip() else ">" for l in md.splitlines())


def hub_commit_map():
    """会話の ID → その会話のハブのコミット（「短い ID 日付 件名」）。git log は 1 回だけ起動する。"""
    try:
        # git の出力の文字コードの設定（i18n.logOutputEncoding）によらず UTF-8 で受け取る
        r = subprocess.run(["git", "-c", "i18n.logOutputEncoding=UTF-8", "-C", str(HUB), "log", "--all",
                            "--date=short", "--format=%x1e%h %ad%x1f%s%x1f%B"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    found = {}
    trailer_rx = re.compile(rf"^{TRAILER}:\s*(\S+)\s*$", re.M)
    for rec in (r.stdout or "").split("\x1e"):
        parts = rec.split("\x1f", 2)
        if len(parts) < 3:
            continue
        head, subject, body = parts[0].strip(), parts[1].strip(), parts[2]
        # 会話の記録だけを更新するコミット（chore(sessions):）は載せない。載せると記録の更新が終わらないため
        if CHORE_RX.match(subject):
            continue
        for sid in trailer_rx.findall(body):
            found.setdefault(sid, []).append(f"{head} {subject}")
    return found


def render_digest(s, items, parent_title):
    L = [f"# 会話（要約するための読める形）：{redact(s['title'])}", "",
         f"- 会話の ID：`{s['id']}`",
         f"- 作業ディレクトリ：`{s['cwd'] or '不明'}`",
         f"- 生の記録：`records/sessions/raw/{s['id']}.jsonl`（`<!-- uuid: … -->` の値で行を探せる）"]
    if parent_title:
        L.append(f"- 分岐元：{parent_title}。分岐元と同じ部分は省いた")
    L += ["", "道具の結果は載せていない（選択肢での質問と回答は載せる）。git の外のファイル。", ""]
    day = None
    for it in items:
        dt = local(it["ts"])
        if dt and f"{dt:%Y-%m-%d}" != day:
            day = f"{dt:%Y-%m-%d}"
            L += [f"## {day}", ""]
        hm = f"{dt:%H:%M:%S}" if dt else "--:--:--"
        who = {"user": "研究者", "answer": "選択肢での質問と回答", "note": "注記", "claude": "Claude"}[it["kind"]]
        L += [f"### {hm} {who}", f"<!-- uuid: {it['uuid']} -->", ""]
        if it["kind"] == "user":
            L += [quote(redact(it["text"])), ""]
        elif it["kind"] in ("answer", "note"):
            L += [redact(it["text"]), ""]
        else:
            if it["text"]:
                L += [demote(redact(it["text"])), ""]
            if it["tools"]:
                L += ["- 道具：" + " ／ ".join(redact(x) for x in it["tools"]), ""]
    return "\n".join(L).rstrip() + "\n"


AUTO_START = "<!-- auto:start（harness-session-archive が作る。人も Claude も直接書き換えない） -->"
AUTO_START_RX = re.compile(r"<!-- auto:start[^>]*-->")
AUTO_END = "<!-- auto:end -->"
SUMMARY_BODY = """
- 要約した範囲：（要約待ち）

## 何をした会話か

## 流れ

## 決まったこと

## 未決のまま残ったこと

## 作ったもの・変えたもの

## 関連
"""


def summary_body():
    """要約のひな形。研究の層（hub/templates/session-summary.md）があればそちらを使う。"""
    for c in (HUB / "hub" / "templates" / "session-summary.md", HUB / "harness" / "templates" / "session-summary.md"):
        if c.exists():
            return "\n" + c.read_text(encoding="utf-8").lstrip("\n")
    return SUMMARY_BODY


def auto_block(s, r, parent, commits):
    L = [AUTO_START,
         f"- 会話の ID：`{s['id']}`",
         f"- 作業ディレクトリ：`{s.get('cwd') or '不明'}`"]
    if r["first"] and r["last"]:
        L.append(f"- 期間：{r['first']:%Y-%m-%d %H:%M} 〜 {r['last']:%Y-%m-%d %H:%M}")
    if parent:
        L.append(f"- 分岐元：[{redact(parent['title'])}]({parent['file']})")
    L += [f"- 生の記録：`records/sessions/raw/{s['id']}.jsonl`（git の外。この PC だけ）",
          f"- 読める形：`records/sessions/raw/digest/{s['id']}.md`（git の外）"]
    if commits:
        L.append(f"- この会話でのハブのコミット（{TRAILER}）：")
        L += [f"  - {c}" for c in commits]
    L.append(AUTO_END)
    return "\n".join(L)


def summarized_until(text):
    m = re.search(r"^- 要約した範囲：.*?(\d{4}-\d{2}-\d{2}) (\d{1,2}):(\d{2})", text, re.M)
    return f"{m.group(1)} {int(m.group(2)):02d}:{m.group(3)}" if m else None


def is_linked_worktree():
    """ハブのワークツリー（.claude/worktrees/ など）の中で動いているか。そのときは何も書かない。
    そこで開いた会話は、元のハブで動くときに、ハブの下の会話として取り込まれる。"""
    return (HUB / ".git").is_file()


def write_text(path: Path, text: str):
    """改行は LF にそろえ、一時ファイルに書いてから入れ替える（途中で止まっても壊れたファイルを残さない）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def load_cache():
    try:
        c = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # 無い、または壊れている：すべてを読み直す
    if not isinstance(c, dict) or c.get("_v") != CACHE_VERSION:
        return {}
    return c


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--hook", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--pending", action="store_true")
    a = ap.parse_args()
    if a.hook:
        try:
            sys.stdin.read()
        except OSError:
            pass
    if not (HUB / "hub").exists():
        return  # 導入前のひな形では何もしない
    if is_linked_worktree():
        return
    dirs, excludes = load_sources()
    roots = [norm(d) for d in dirs]
    for d in (OUT, RAW, DIGEST):
        d.mkdir(parents=True, exist_ok=True)
    cache = load_cache()
    raw_dir = RAW.resolve()

    def in_scope(cwd):
        if not cwd:
            return False
        n = norm(cwd)
        return any(n == r or n.startswith(r + "\\") or n.startswith(r + "/") for r in roots)

    sessions = []
    for f in candidate_files(dirs):
        sid = f.stem
        if any(sid.startswith(x) for x in excludes):
            continue
        st = f.stat()
        key = [st.st_size, int(st.st_mtime)]
        c = cache.get(sid)
        # 対象かどうかは、記録した作業ディレクトリ（cwd）を、いまの hub/session-sources.txt と毎回照らして決め直す。
        # 作業ディレクトリを足したり外したりしても、会話のファイルを読み直さずに反映されるようにするため
        if c and c.get("key") == key:
            if "labels" in c:
                if in_scope(c.get("cwd")):
                    sessions.append({"id": sid, "path": f, "parsed": None, **c})
                else:
                    cache[sid] = {"key": key, "skip": True, "cwd": c.get("cwd"), "talk": True}
                continue
            if c.get("skip") and "cwd" in c and not (c.get("talk") and in_scope(c["cwd"])):
                continue
        p = parse(f)
        talk = any(i["kind"] in ("user", "answer") for i in p["items"])
        if not talk or not in_scope(p["cwd"]):
            cache[sid] = {"key": key, "skip": True, "cwd": p["cwd"], "talk": talk}
            continue
        entry = {"key": key, "title": p["title"], "cwd": p["cwd"],
                 "uuids": [i["uuid"] for i in p["items"]], "labels": [i["label"] for i in p["items"]],
                 "kinds": [i["kind"] for i in p["items"]], "ts": [i["ts"] for i in p["items"]],
                 "te": [i.get("te") or i["ts"] for i in p["items"]], "changed": True}
        cache[sid] = entry
        sessions.append({"id": sid, "path": f, "parsed": p, **entry})
        # 生の記録をハブに写す（一覧だけのときと、すでにハブの写しから読んだときは写さない）
        if not a.list and f.resolve().parent != raw_dir:
            shutil.copy2(f, RAW / f.name)

    # 各発言の持ち主：発言に記された会話がその発言を持っていればその会話、無ければ最も早い会話
    def first_ts(s):
        return min((x for x in s["ts"] if x), default="")
    sessions.sort(key=first_ts)
    holders = {}
    for s in sessions:
        for u, lab in zip(s["uuids"], s["labels"]):
            holders.setdefault(u, []).append((s["id"], lab))
    owner = {}
    for u, hs in holders.items():
        ids = [h[0] for h in hs]
        owner[u] = hs[0][1] if hs[0][1] in ids else ids[0]

    rows = {}
    for s in sessions:
        own = [i for i, u in enumerate(s["uuids"]) if owner.get(u) == s["id"]]
        te, kinds = s.get("te") or s["ts"], s.get("kinds") or [""] * len(s["ts"])
        ts_own = sorted(s["ts"][i] for i in own if s["ts"][i])
        te_own = sorted(te[i] for i in own if te[i])
        talk_own = sorted(s["ts"][i] for i in own if s["ts"][i] and kinds[i] in ("user", "answer"))
        first = local(ts_own[0]) if ts_own else None
        last = local(te_own[-1]) if te_own else None
        s["own_idx"] = own
        rows[s["id"]] = {"id": s["id"], "title": s.get("title", ""), "first": first, "last": last,
                         "last_talk": local(talk_own[-1]) if talk_own else None,
                         "cwd": s.get("cwd"), "n_own": len(own),
                         "file": (f"{first:%Y-%m-%d}_{s['id'][:8]}.md" if first else None)}

    if a.list:
        for s in sessions:
            r = rows[s["id"]]
            when = f"{r['first']:%Y-%m-%d %H:%M}" if r["first"] else "（自分の発言なし）"
            print(f"{when}  {s['id'][:8]}  own={r['n_own']:5d}  {r['title'][:50]}")
        return

    commits_of = hub_commit_map()
    for s in sessions:
        r = rows[s["id"]]
        others = {}
        for u in s["uuids"]:
            o = owner.get(u)
            if o and o != s["id"] and rows[o]["file"]:
                others[o] = others.get(o, 0) + 1
        parent = None
        if others:
            pid = max(others, key=others.get)
            parent = {"title": rows[pid]["title"], "file": rows[pid]["file"]}
        r["parent"] = parent
        if not r["file"]:
            cache[s["id"]].pop("changed", None)
            continue
        dg = DIGEST / f"{s['id']}.md"
        # 読める形は、会話のファイルが変わったときだけでなく、持ち主の決まり方（分岐元）が変わったときにも作り直す
        own_sig = hashlib.sha1(("\n".join(s["uuids"][i] or "" for i in s["own_idx"]) + "|" +
                                (parent["file"] if parent else "")).encode("utf-8")).hexdigest()
        if s.get("changed") or not dg.exists() or cache[s["id"]].get("own_sig") != own_sig:
            p = s["parsed"] or parse(s["path"])
            own_set = {s["uuids"][i] for i in s["own_idx"]}
            items = [i for i in p["items"] if i["uuid"] in own_set]
            write_text(dg, render_digest(p, items, parent["title"] if parent else None))
            cache[s["id"]]["own_sig"] = own_sig
        # 要約のファイル：名前が変わっていたら移す（中身は Claude が書いたものなので消さない）
        out = OUT / r["file"]
        if not out.exists():
            olds = [x for x in OUT.glob(f"*_{s['id'][:8]}.md") if x != out]
            if olds:
                olds[0].rename(out)
        block = auto_block(s, r, parent, commits_of.get(s["id"], []))
        if out.exists():
            text = out.read_text(encoding="utf-8")
            m = AUTO_START_RX.search(text)
            if m and AUTO_END in text[m.end():]:
                new = text[: m.start()] + block + text[text.index(AUTO_END, m.end()) + len(AUTO_END):]
            else:
                new = text
        else:
            new = f"# 会話の要約：{redact(r['title'])}\n\n{block}\n{summary_body()}"
        if not out.exists() or out.read_text(encoding="utf-8") != new:
            write_text(out, new)
        until = summarized_until(new)
        r["until"] = until
        # 要約が要るのは、要約した範囲の後に研究者の発言があるとき（Claude の最後の返答だけでは要らない）
        lt = r["last_talk"]
        r["pending"] = (until is None) or (lt is not None and f"{lt:%Y-%m-%d %H:%M}" > until)
        cache[s["id"]].pop("changed", None)

    rows_l = [rows[s["id"]] for s in sessions]
    idx = ["# 会話の記録", "",
           "研究に関係する Claude Code の会話の要約。**履歴の記録**なので、書かれた決定はその時点のもの（現在の決定は `hub/decisions.md`）。",
           "", "- 対象：`hub/session-sources.txt` の作業ディレクトリで開いた会話",
           "- 要約は Claude が書く（`harness-session-archive`）。「要約が要る」の会話は、統括か、その会話自身が要約する",
           f"- 決定と会話のつながり：`hub/decisions.md` の「会話」の欄、またはコミットの `{TRAILER}:` の行",
           "- 生の記録（JSONL）と読める形：`records/sessions/raw/`（git の外。この PC だけ）", "",
           "| 開始 | 題 | 会話の ID | 要約 | 要約した範囲 |", "|---|---|---|---|---|"]
    for r in rows_l:
        when = f"{r['first']:%Y-%m-%d %H:%M}" if r["first"] else ""
        title = redact(r["title"]).replace("|", "／") + ("（分岐）" if r.get("parent") else "")
        if r["file"]:
            link = f"[{r['file']}]({r['file']})"
            state = (r.get("until") or "未要約") + ("（**要約が要る**）" if r.get("pending") else "")
        else:
            par = r.get("parent")
            link = f"自分の発言なし。中身は [{par['file']}]({par['file']})" if par else "自分の発言なし"
            state = "―"
        idx.append(f"| {when} | {title} | `{r['id'][:8]}` | {link} | {state} |")
    write_text(OUT / "README.md", "\n".join(idx) + "\n")
    cache["_v"] = CACHE_VERSION
    write_text(CACHE, json.dumps(cache, ensure_ascii=False))
    pend = [r for r in rows_l if r.get("pending")]
    if a.pending:
        for r in pend:
            print(f"{r['id'][:8]}  要約：records/sessions/{r['file']}  読める形：records/sessions/raw/digest/{r['id']}.md  "
                  f"要約した範囲：{r.get('until') or 'なし'}  最後の発言：{(r['last_talk'] or r['last']):%Y-%m-%d %H:%M}")
    elif not a.hook:
        print(f"会話 {len(rows_l)} 件を更新した。要約が要る会話：{len(pend)} 件（--pending で一覧）")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # フックでは会話の終了を妨げない
        if "--hook" in sys.argv:
            print(f"session_archive: {e}", file=sys.stderr)
            sys.exit(0)
        raise
