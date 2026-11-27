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

import sys

# 古い Python では、この先の書き方を読めずに分かりにくい失敗をするので、先に知らせて終わる
if sys.version_info < (3, 10):
    if "--hook" in sys.argv:
        sys.exit(0)
    sys.exit("claude_history: Python 3.10 以降が要る（いまは %d.%d）" % sys.version_info[:2])

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

SKIP_PREFIXES = (
    "<system-reminder", "<local-command", "<side-sessions",
    "<task-notification", "[Request interrupted", "<ci-monitor-event", "<user-prompt-submit-hook",
)
# Claude Code がスラッシュコマンドの発言に書く印。いまの版は <command-name> から始め、<command-message>、<command-args> と続く
COMMAND_TAGS = ("<command-name>", "<command-message>", "<command-args>")


def strip_reminders(t: str) -> str:
    """<system-reminder> 〜 </system-reminder> の囲みを除く（re.sub の「.*?」と同じ結果）。
    str.find で探す（閉じていない始まりの印が多く並んでも、印ごとに終わりまで読み直さないため）。"""
    o, c = "<system-reminder>", "</system-reminder>"
    if o not in t:
        return t
    out, pos = [], 0
    while True:
        i = t.find(o, pos)
        if i == -1:
            break
        j = t.find(c, i + len(o))
        if j == -1:
            break  # 閉じていない（後ろの始まりの印も閉じていない）
        out.append(t[pos:i])
        pos = j + len(c)
    out.append(t[pos:])
    return "".join(out)


def tag_text(t: str, name: str):
    """<name> 〜 </name> の間の字（re.search の「<name>(.*?)</name>」と同じ）。無ければ None。
    閉じた印のある最初の始まりの印は、いつも最初の始まりの印なので、str.find で一度だけ探す。"""
    o, c = f"<{name}>", f"</{name}>"
    i = t.find(o)
    if i == -1:
        return None
    j = t.find(c, i + len(o))
    return None if j == -1 else t[i + len(o):j]


def encode(path: str) -> str:
    """Claude Code が作業ディレクトリから履歴のフォルダ名を作る規則（英数字以外を - に）。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def norm(p: str) -> str:
    """作業ディレクトリを比べる形。大文字と小文字は、Windows でだけ同じとみなす（os.path.normcase）。"""
    return os.path.normcase(str(Path(p).resolve())).rstrip("\\/")


def fold_name(s: str) -> str:
    """履歴のフォルダの名前を比べる形。Windows ではフォルダの名前の大文字・小文字がそろわないことがあるので、Windows でだけ小文字に。"""
    return s.lower() if os.name == "nt" else s


def local_time(ts: str) -> datetime | None:
    """1970 年より前の時刻は、Windows では astimezone が OSError になるので、読めない時刻として扱う。"""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError, TypeError, OSError, OverflowError):
        return None


def clean(content) -> str:
    """先に <system-reminder> の囲みを除いてから、機械が足した行かを判定する。"""
    if isinstance(content, list):  # tool_result だけの行は本文が無い
        t = " ".join(x["text"] for x in content
                     if isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str))
    else:
        t = content if isinstance(content, str) else ""
    t = strip_reminders(t).strip()
    if not t or "This session is being continued" in t[:300]:
        return ""
    if t.startswith(COMMAND_TAGS):
        # スラッシュコマンド（Claude Code が書く印で始まる発言だけ。印を本文に貼った発言は、ふつうの発言として残す）：
        # 引数のあるものだけを、名前と引数で残す。
        # 引数の無いもの（/clear、/exit、/model など）は、研究者の発言として数えない
        name, args = tag_text(t, "command-name"), tag_text(t, "command-args")
        if not (args and args.strip()):
            return ""
        return " ".join(x.strip() for x in (name, args) if x and x.strip())
    if t.startswith(SKIP_PREFIXES):
        return ""
    return t


def user_text(d: dict) -> str:
    if d.get("isSidechain"):
        return ""  # サブエージェントへの指示は、研究者の発言ではない
    if d.get("type") == "attachment":
        a = d.get("attachment")
        a = a if isinstance(a, dict) else {}
        o = a.get("origin")
        origin = o.get("kind") if isinstance(o, dict) else None
        if (a.get("type") == "queued_command" and a.get("commandMode") == "prompt"
                and not a.get("isMeta") and origin in (None, "human")):
            return clean(a.get("prompt"))
        return ""
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary"):
        return ""
    m = d.get("message")
    return clean(m.get("content") if isinstance(m, dict) else None)


def records(fh):
    """履歴の行を順に読む。JSON でない行と、JSON でもオブジェクトでない行（[] など）は読み飛ばす。"""
    for line in fh:
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(d, dict):
            yield d


def first_cwd(f: Path):
    with open(f, encoding="utf-8", errors="replace") as fh:
        for d in records(fh):
            if isinstance(d.get("cwd"), str) and d["cwd"]:
                return d["cwd"]
    return None


def scan(files):
    rows, seen_uuid, seen_text = [], set(), set()
    for f in files:
        sid = f.stem[:8]
        with open(f, encoding="utf-8", errors="replace") as fh:
            for d in records(fh):
                t = user_text(d)
                if not t:
                    continue
                dt = local_time(d.get("timestamp", ""))
                if dt is None:
                    continue
                u = d.get("uuid") if isinstance(d.get("uuid"), str) else None
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
    # 作業中に送った発言が、その時刻から 10 分以内に、同じ会話で同じ本文の普通の発言として後に記録されていれば、
    # 普通の発言の方だけを残す。一つの普通の発言で除けるのは、作業中に送った発言の一つだけ（続けて送った「はい」を、
    # まとめて消さないため）。前にある普通の発言では除かない。普通の発言どうしは、同じ本文が続いても消さない
    said = {}
    for dt, sid, _, t, queued in rows:
        if not queued:
            said.setdefault((sid, re.sub(r"\s+", " ", t)), []).append(dt)
    for v in said.values():
        v.sort()
    used, drop = set(), set()
    # 時刻の順に、まだ使っていない、いちばん早い普通の発言と組にする
    for k in sorted((k for k, r in enumerate(rows) if r[4]), key=lambda k: (rows[k][0], k)):
        key = (rows[k][1], re.sub(r"\s+", " ", rows[k][3]))
        for n, x in enumerate(said.get(key, [])):
            d = (x - rows[k][0]).total_seconds()
            if d >= 600:
                break
            if d >= 0 and (key, n) not in used:
                used.add((key, n))
                drop.add(k)
                break
    out = [r[:4] for k, r in enumerate(rows) if k not in drop]
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
        # フォルダの名前からは元のパスに戻せない（記号がすべて - になる）ので、会話に記録された作業ディレクトリを出す。
        # hub/session-sources.txt にそのまま写せるように。新しい会話から順に、記録のあるものを使う
        cwd = None
        for j in sorted(js, key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                cwd = first_cwd(j)
            except OSError:
                cwd = None
            if cwd:
                break
        print(f"{p.name}\n    会話 {len(js)}、{size:.1f} MB、最終更新 {min(mt):%Y-%m-%d} 〜 {max(mt):%Y-%m-%d}"
              f"\n    作業ディレクトリ: {cwd or '不明'}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 書けない字（対になっていないサロゲート）は置き換える
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--list", action="store_true", help="履歴のあるフォルダと、そこで開いた作業ディレクトリの一覧を出す")
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

    enc = fold_name(encode(a.project))  # Windows ではフォルダの名前の大文字・小文字がそろわないことがある
    dirs = [p for p in a.base.iterdir() if p.is_dir()
            and (fold_name(p.name) == enc or (a.subdirs and fold_name(p.name).startswith(enc + "-")))]
    if not dirs:
        sys.exit(f"該当する履歴が無い: {encode(a.project)}（--list で名前を確かめる）")
    # フォルダの名前では、記号の違いを見分けられない（…\my-experiment と …\my_experiment、…\hub と …\hub-old は同じ名前になる）。
    # 会話に記録された作業ディレクトリがあれば、それで確かめる（--subdirs なら、その下のフォルダも含める）。
    # 記録の無い会話は、名前がそのまま同じフォルダのものだけを残す
    root = norm(a.project)

    def keep(f):
        try:
            c = first_cwd(f)
        except OSError:
            c = None
        if not c:
            return fold_name(f.parent.name) == enc
        n = norm(c)
        return n == root or (a.subdirs and (n.startswith(root + "\\") or n.startswith(root + "/")))
    files = [f for d in dirs for f in d.glob("*.jsonl") if keep(f)]
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
        a.out.write_text(text, encoding="utf-8", errors="replace", newline="\n")  # 書けない字は置き換える
        print(f"書いた: {a.out}（発言 {len(rows)}、日数 {len(days)}）")
    else:
        print(text)


if __name__ == "__main__":
    main()
