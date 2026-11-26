"""Claude Code の会話の履歴から、研究者（ユーザー）の発言を日付つきで取り出す。

使い方:
  python -X utf8 claude_history.py --list
  python -X utf8 claude_history.py --project <作業ディレクトリ> [--subdirs]
         [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--max-chars 400] [--out FILE]

- 履歴は読むだけで、書き換えない。
- 履歴の場所は ~/.claude/projects/<作業ディレクトリを記号で置き換えた名前>/*.jsonl。
- 分岐した会話（fork）は同じ発言を写して持つので、発言の uuid で重複を除く。
- 研究者の発言として数えるのは、研究者が打った発言、Claude が作業している間に送った発言、
  スラッシュコマンドの名前と引数。サブエージェントへの指示（sidechain）や、ほかの会話からのメッセージは除く。
- 出力には研究の中身がそのまま入る。git に入れない場所に書くこと。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REMINDER_RX = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
SKIP_PREFIXES = (
    "<system-reminder", "<local-command", "<side-sessions",
    "<task-notification", "[Request interrupted", "<ci-monitor-event", "<user-prompt-submit-hook",
)


def encode(path: str) -> str:
    """Claude Code が作業ディレクトリから履歴のフォルダ名を作る規則（英数字以外を - に）。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def norm(p: str) -> str:
    return str(Path(p).resolve()).lower().rstrip("\\/")


def local_time(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError, TypeError):
        return None


def clean(content) -> str:
    """先に <system-reminder> の囲みを除いてから、機械が足した行かを判定する。"""
    if isinstance(content, list):  # tool_result だけの行は本文が無い
        t = " ".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
    else:
        t = content if isinstance(content, str) else ""
    t = REMINDER_RX.sub("", t).strip()
    if not t or "This session is being continued" in t[:300]:
        return ""
    if "<command-name>" in t:
        # スラッシュコマンド：引数のあるものだけを、名前と引数で残す。
        # 引数の無いもの（/clear、/exit、/model など）は、研究者の発言として数えない
        name = re.search(r"<command-name>(.*?)</command-name>", t, re.S)
        args = re.search(r"<command-args>(.*?)</command-args>", t, re.S)
        if not (args and args.group(1).strip()):
            return ""
        return " ".join(x.group(1).strip() for x in (name, args) if x and x.group(1).strip())
    if t.startswith(SKIP_PREFIXES):
        return ""
    return t


def user_text(d: dict) -> str:
    if d.get("isSidechain"):
        return ""  # サブエージェントへの指示は、研究者の発言ではない
    if d.get("type") == "attachment":
        a = d.get("attachment") or {}
        origin = (a.get("origin") or {}).get("kind")
        if (a.get("type") == "queued_command" and a.get("commandMode") == "prompt"
                and not a.get("isMeta") and origin in (None, "human")):
            return clean(a.get("prompt"))
        return ""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary"):
        return ""
    return clean((d.get("message") or {}).get("content"))


def first_cwd(f: Path):
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("cwd"):
                return d["cwd"]
    return None


def scan(files):
    rows, seen_uuid, seen_text = [], set(), set()
    for f in files:
        sid = f.stem[:8]
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = user_text(d)
                if not t:
                    continue
                dt = local_time(d.get("timestamp", ""))
                if dt is None:
                    continue
                u = d.get("uuid")
                if u:
                    if u in seen_uuid:
                        continue
                    seen_uuid.add(u)
                else:  # uuid の無い古い行だけは、時刻（秒まで）と本文で重複を除く
                    key = (dt.isoformat(), t)
                    if key in seen_text:
                        continue
                    seen_text.add(key)
                rows.append((dt, sid, f.parent.name, t, d.get("type") == "attachment"))
    # 作業中に送った発言が、前後 10 分以内に普通の発言としても記録されていれば、普通の発言の方だけを残す。
    # 普通の発言どうしは、同じ本文（「はい」など）が続いても消さない
    said = {}
    for dt, sid, _, t, queued in rows:
        if not queued:
            said.setdefault((sid, re.sub(r"\s+", " ", t)), []).append(dt)
    out = [r[:4] for r in rows
           if not (r[4] and any(abs((x - r[0]).total_seconds()) < 600
                                for x in said.get((r[1], re.sub(r"\s+", " ", r[3])), [])))]
    out.sort(key=lambda r: r[0])
    return out


def cmd_list(base: Path):
    dirs = sorted(p for p in base.iterdir() if p.is_dir())
    print(f"履歴の場所: {base}")
    for p in dirs:
        js = list(p.glob("*.jsonl"))
        if not js:
            continue
        mt = [datetime.fromtimestamp(j.stat().st_mtime) for j in js]
        size = sum(j.stat().st_size for j in js) / 1e6
        print(f"{p.name}\n    会話 {len(js)}、{size:.1f} MB、最終更新 {min(mt):%Y-%m-%d} 〜 {max(mt):%Y-%m-%d}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--list", action="store_true", help="履歴のあるフォルダの一覧を出す")
    ap.add_argument("--project", help="Claude Code を開いていた作業ディレクトリ")
    ap.add_argument("--subdirs", action="store_true", help="その下のフォルダ・ワークツリーで開いた会話も含める")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--max-chars", type=int, default=400)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()

    if not a.base.exists():
        sys.exit(f"履歴の場所が無い: {a.base}")
    if a.list:
        cmd_list(a.base)
        return
    if not a.project:
        sys.exit("--list か --project を指定する")

    enc = encode(a.project).lower()  # Windows ではフォルダの名前の大文字・小文字がそろわないことがある
    dirs = [p for p in a.base.iterdir() if p.is_dir()
            and (p.name.lower() == enc or (a.subdirs and p.name.lower().startswith(enc + "-")))]
    if not dirs:
        sys.exit(f"該当する履歴が無い: {encode(a.project)}（--list で名前を確かめる）")
    files = [f for d in dirs for f in d.glob("*.jsonl")]
    if a.subdirs:
        # フォルダの名前では「\」と「-」を見分けられない（…\hub と …\hub-old）。会話を開いた場所で確かめる
        root = norm(a.project)
        def under(c):
            if not c:
                return False
            n = norm(c)
            return n == root or n.startswith(root + "\\") or n.startswith(root + "/")
        files = [f for f in files if under(first_cwd(f))]
    rows = scan(files)
    if a.since:
        rows = [r for r in rows if r[0].strftime("%Y-%m-%d") >= a.since]
    if a.until:
        rows = [r for r in rows if r[0].strftime("%Y-%m-%d") <= a.until]

    out = []
    days = Counter(r[0].strftime("%Y-%m-%d") for r in rows)
    out.append(f"# 研究者の発言（{a.project}）")
    out.append("")
    out.append(f"- 対象のフォルダ: {', '.join(sorted(d.name for d in dirs))}")
    out.append(f"- 会話 {len(files)}、発言 {len(rows)}（重複を除く）")
    out.append("")
    out.append("## 日ごとの発言の数")
    out.append("")
    out += [f"- {d}: {n}" for d, n in sorted(days.items())]
    cur = None
    for dt, sid, folder, t in rows:
        day = dt.strftime("%Y-%m-%d")
        if day != cur:
            out += ["", f"## {day}", ""]
            cur = day
        body = t.replace("\r", "").replace("\n", " / ")
        if len(body) > a.max_chars:
            body = body[: a.max_chars] + "…"
        out.append(f"- {dt:%H:%M} [{sid}] {body}")
    text = "\n".join(out) + "\n"
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"書いた: {a.out}（発言 {len(rows)}、日数 {len(days)}）")
    else:
        print(text)


if __name__ == "__main__":
    main()
