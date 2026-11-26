"""Claude Code の会話の履歴から、研究者（ユーザー）の発言を日付つきで取り出す。

使い方:
  python -X utf8 claude_history.py --list
  python -X utf8 claude_history.py --project <作業ディレクトリ> [--subdirs]
         [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--max-chars 400] [--out FILE]

- 履歴は読むだけで、書き換えない。
- 履歴の場所は ~/.claude/projects/<作業ディレクトリを記号で置き換えた名前>/*.jsonl。
- 分岐した会話（fork）で同じ発言が重複するので、時刻と本文の先頭で重複を除く。
- 出力には研究の中身がそのまま入る。git に入れない場所に書くこと。
"""
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SKIP_PREFIXES = (
    "<system-reminder", "<local-command", "<command-", "<side-sessions",
    "<task-notification", "[Request interrupted", "<ci-monitor-event",
)


def encode(path: str) -> str:
    """Claude Code が作業ディレクトリから履歴のフォルダ名を作る規則（英数字以外を - に）。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def local_time(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None


def user_text(d: dict) -> str:
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary"):
        return ""
    c = d.get("message", {}).get("content")
    if isinstance(c, list):  # tool_result のみの行は飛ばす
        t = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    else:
        t = c or ""
    t = t.strip()
    if not t or t.startswith(SKIP_PREFIXES) or "This session is being continued" in t[:300]:
        return ""
    t = re.sub(r"<system-reminder>.*?</system-reminder>", "", t, flags=re.S).strip()
    return t


def scan(files):
    rows, seen = [], set()
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
                key = (dt.strftime("%Y-%m-%dT%H:%M"), t[:120])
                if key in seen:
                    continue
                seen.add(key)
                rows.append((dt, sid, f.parent.name, t))
    rows.sort(key=lambda r: r[0])
    return rows


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

    enc = encode(a.project)
    dirs = [p for p in a.base.iterdir() if p.is_dir()
            and (p.name == enc or (a.subdirs and p.name.startswith(enc + "-")))]
    if not dirs:
        sys.exit(f"該当する履歴が無い: {enc}（--list で名前を確かめる）")
    files = [f for d in dirs for f in d.glob("*.jsonl")]
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
        a.out.write_text(text, encoding="utf-8")
        print(f"書いた: {a.out}（発言 {len(rows)}、日数 {len(days)}）")
    else:
        print(text)


if __name__ == "__main__":
    main()
