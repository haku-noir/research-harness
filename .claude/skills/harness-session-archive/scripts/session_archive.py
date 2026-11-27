"""Claude Code の会話を、研究ハブに残す。

- 生の記録（JSONL）を records/sessions/raw/<会話の ID>.jsonl に写す（git の外。.gitignore）。
- 要約するための読める形を records/sessions/raw/digest/<会話の ID>.md に書く（git の外）。
  研究者の発言・Claude の返答の本文・道具の呼び出しの 1 行・選択肢での質問と回答を、時刻と uuid つきで載せる。
  分岐した会話は、各発言を元の会話（記録の各行の sessionId）に持たせ、同じ発言を二重に載せない。
- 要約のファイル records/sessions/<開始日>_<ID の先頭 8 文字>.md（git で管理）を用意する。
  - 無ければ、ひな形（中身は「要約待ち」）を作る。名前は作るときに決め、後から変えない（リンクを切らないため）。
  - あれば、冒頭の <!-- auto:start --> 〜 <!-- auto:end --> の間（会話の ID、期間、生の記録への参照、ハブのコミット）だけを書き直す。
    書き直すのは、囲みが最初の「## 」の見出しより前にあり、中身がこのスクリプトの書く形の行だけのときだけ
    （組になった git のぶつかりの印の行は、スクリプトの行とみなす）。崩れていれば触らずに知らせる。
    囲みの印の行が一つも無ければ（消された）、最初の「# 」の行の後ろ（無ければいちばん上）に新しく入れる。
    印の行（始まりか終わり）が残っていて、正しい囲みになっていなければ、崩れているとして触らずに知らせる。
  - 先頭 8 文字が同じ別の会話の要約（囲みの「会話の ID」が別の会話）は使わない。その会話の要約は作らずに知らせる。
  - 要約の本文は Claude が書く（.claude/skills/harness-session-archive/SKILL.md）。このスクリプトは書かない。
- 一覧 records/sessions/README.md を作り直す。要約が要る会話（未要約、または要約した範囲の後に発言が増えた会話）を示す。
  この PC に記録の無い会話（別の PC で開いた会話）も、要約のファイルがあれば載せる。

対象の会話は hub/session-sources.txt に書いた作業ディレクトリ（その下のフォルダを含む）で開いた会話。
「! <会話の ID の先頭>」の行で、その会話を除ける。

使い方:
  python -X utf8 session_archive.py            （全部を更新。変わった会話だけを読み直す）
  python -X utf8 session_archive.py --pending  （更新して、要約が要る会話を出す）
  python -X utf8 session_archive.py --hook     （Claude Code のフックから。標準入力の transcript_path も探す場所に足す。失敗しても 0 で終わる）
  python -X utf8 session_archive.py --list     （対象の会話を一覧するだけ）
"""
import sys

# 古い Python では、この先の書き方を読めずに分かりにくい失敗をするので、先に知らせて終わる（フックでは何も言わずに終わる）
if sys.version_info < (3, 10):
    if "--hook" in sys.argv:
        sys.exit(0)
    sys.exit("session_archive: Python 3.10 以降が要る（いまは %d.%d）" % sys.version_info[:2])

import argparse
import atexit
import bisect
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from datetime import datetime
from pathlib import Path

HUB = Path(__file__).resolve().parents[4]
OUT = HUB / "records" / "sessions"
RAW = OUT / "raw"
DIGEST = RAW / "digest"
CACHE = RAW / ".index.json"
# 記録どうしのリンクのスクリプトと共有するロック。git の中（.git/）に置く（git に入らず、目にも触れない）
LOCK = (HUB / ".git" / "hub-scripts.lock") if (HUB / ".git").is_dir() else (HUB / "records" / ".hub-scripts.lock")
# これより古いロックは、止められた実行が残したものとして外す。会話の終わりのフックは 60 秒ほどで止められ、
# 手で動かした長い実行は 20 秒ごとにロックの時刻を新しくする（keep_lock）ので、これより古いものは確かに止まっている
STALE_SEC = 90
CACHE_VERSION = 5  # 読み方を変えたら上げる。上がると、すべての会話を一度読み直す
CACHE_SAVE_EVERY, CACHE_SAVE_SEC = 25, 10  # 読み直した会話がこの数になるか、この秒数がたつごとに、途中までを控えに書く
TEMP_RX = re.compile(r"^\.t(?:[0-9a-f]{2}|[0-9a-f]{8})\.tmp$")  # make_temp の名前
TEMP_STALE_SEC = 600  # これより古い一時ファイルは、止められた実行が残したもの
SOURCES = HUB / "hub" / "session-sources.txt"
PROJECTS = Path.home() / ".claude" / "projects"
TRAILER = "Hub-Session"

REMINDER_RX = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
SKIP_PREFIXES = (
    "<system-reminder", "<local-command", "<side-sessions", "<task-notification",
    "[Request interrupted", "<ci-monitor-event", "<user-prompt-submit-hook",
)
CHORE_RX = re.compile(r"^chore\((sessions|links)\)!?:")
SECRET_MARK = "<秘密の値>"
# 鍵の前後は英数字で区切る（\b は日本語も字に数えるので、「キーはghp_…」の鍵を見落とす）。中身も英数字と記号だけにする
# （日本語や、ふつうの英語の文を飲み込まないように）
_NB = r"(?<![A-Za-z0-9])"
# PEM の秘密鍵（redact_pem）
PEM_BEGIN_RX = re.compile(r"-----BEGIN (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----")
PEM_END_RX = re.compile(r"-----END (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----")
PEM_BODY_RX = re.compile(r"(?:[ \t]*\r?\n?[ \t]*[A-Za-z0-9+/=]{16,})*")
PEM_MAX = 16384  # 始まりの行から、終わりの行までの長さの上限
SECRET = [
    # 英数字だけで、前半は 64 字まで（日本語は \w に入るので、長い日本語の並びを読みに行かないように）
    (re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"), "<メールアドレス>"),
    # JSON Web Token（eyJ で始まる、base64url の 3 つの部分）
    (re.compile(_NB + r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), SECRET_MARK),
    # 「sk-」と Slack は、数字を含むものだけ（「xoxo-hugs-and-kisses」のような、ふつうの言葉の並びを伏せないように）
    (re.compile(_NB + r"(?:sk-ant-[A-Za-z0-9_-]{10,}"  # Anthropic
                r"|sk-(?=[A-Za-z0-9_-]{0,255}[0-9])[A-Za-z0-9_-]{20,}"  # OpenAI など
                r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"  # GitHub
                r"|(?:xox[abcdeprs]|xapp)-(?=[A-Za-z0-9-]{0,255}[0-9])[A-Za-z0-9-]{10,}"  # Slack
                r"|(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"  # AWS のアクセスキー ID
                r"|AIza[A-Za-z0-9_-]{35,}"  # Google の API キー
                r"|hf_[A-Za-z0-9]{30,}"  # Hugging Face
                r"|glpat-[A-Za-z0-9_-]{20,}"  # GitLab
                r"|[sr]k_live_[A-Za-z0-9]{16,})"), SECRET_MARK),  # Stripe
    # 「Bearer <トークン>」。数字を含む 20 字以上だけ（ふつうの英語の「bearer of …」を伏せないように）
    (re.compile(_NB + r"([Bb]earer[ \t]+)(?=[A-Za-z0-9._~+/=-]{0,512}[0-9])[A-Za-z0-9._~+/=-]{20,}"), r"\1" + SECRET_MARK),
]


def redact_pem(t: str) -> str:
    """PEM の秘密鍵を、始まりの行から終わりの行まで（PEM_MAX まで）伏せる。終わりの行が無ければ（途中で切れた）、
    始まりの行と、後ろに続く鍵の字の行を伏せる。終わりの行の位置は先に一度だけ探す（始まりの行が多くても遅くならないように）。"""
    if "PRIVATE KEY-----" not in t:
        return t
    ends = [(m.start(), m.end()) for m in PEM_END_RX.finditer(t)]
    end_starts = [s for s, _ in ends]
    out, pos = [], 0
    for m in PEM_BEGIN_RX.finditer(t):
        if m.start() < pos:
            continue  # 伏せた範囲の中
        i = bisect.bisect_left(end_starts, m.end())
        if i < len(ends) and ends[i][0] - m.end() <= PEM_MAX:
            end = ends[i][1]
        else:
            end = PEM_BODY_RX.match(t, m.end()).end()
        out += [t[pos:m.start()], SECRET_MARK]
        pos = end
    out.append(t[pos:])
    return "".join(out)


def redact(t: str) -> str:
    t = redact_pem(t)
    for rx, rep in SECRET:
        t = rx.sub(rep, t)
    return t


def encode(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def norm(p: str) -> str:
    """作業ディレクトリを比べる形。大文字と小文字は、Windows でだけ同じとみなす（os.path.normcase）。
    大文字と小文字を分けるファイルシステムでは、名前の大文字・小文字だけが違う二つのフォルダは別のもの。"""
    return os.path.normcase(str(Path(p).resolve())).rstrip("\\/")


def fold_name(s: str) -> str:
    """Claude Code の会話のフォルダの名前を比べる形。Windows ではフォルダの名前の大文字・小文字がそろわないことがあるので、
    Windows でだけ小文字にそろえる。"""
    return s.lower() if os.name == "nt" else s


def load_sources():
    dirs, excludes = [], []
    if not SOURCES.exists():
        return [str(HUB)], []
    # BOM 付きでも読めるように utf-8-sig。「#」は、行の最初の（空白でない）字のときだけ注釈とする
    # （パスの中の「 #」（…\My Research #2\code）で切らないため）
    try:
        src = SOURCES.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:  # 別の文字コードで保存された（説明の行が化けても、パスの行は読める）
        src = SOURCES.read_text(encoding="utf-8", errors="replace")
    for line in src.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            x = line[1:].split()
            if x:  # 「!」だけの行で、すべての会話を除いてしまわないように。後ろの説明は読み飛ばす
                excludes.append(x[0].strip("\"'"))
        else:
            # エクスプローラーの「パスのコピー」が付ける引用符を外し、~ を展開する
            dirs.append(os.path.expanduser(line.strip("\"'")))
    if str(HUB) not in dirs:
        dirs.insert(0, str(HUB))
    return dirs, excludes


def local(ts):
    """記録の時刻（ISO 8601）を、この PC の時間帯の時刻にする。読めなければ None。
    1970 年より前の時刻は、Windows では astimezone が OSError になるので、読めない時刻として扱う（会話の全体を止めない）。"""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError, TypeError, OSError, OverflowError):
        return None


def readable(stamps):
    """時刻の並びのうち、最初に読めるものを、この PC の時間帯の時刻で。どれも読めなければ None。"""
    return next((t for t in map(local, stamps) if t is not None), None)


def mtime(f: Path) -> float:
    try:
        return f.stat().st_mtime
    except OSError:
        return -1.0


def candidate_files(dirs, extra=()):
    """会話のファイル。Claude Code のフォルダにあるものを優先し、消えたもの（既定で 30 日）はハブに写した生の記録を使う。
    extra は、名前によらずに探すフォルダ（フックの標準入力の transcript_path があるフォルダ）。"""
    files = {}
    folders = []
    if PROJECTS.exists():
        encs = [fold_name(encode(d)) for d in dirs]  # Windows ではフォルダの名前の大文字・小文字がそろわないことがある
        try:
            folders = [p for p in PROJECTS.iterdir()
                       if any(fold_name(p.name) == e or fold_name(p.name).startswith(e + "-") for e in encs) and p.is_dir()]
        except OSError:
            folders = []
    seen = {os.path.normcase(str(p.resolve())) for p in folders}
    for p in extra:
        if os.path.normcase(str(p.resolve())) not in seen:
            folders.append(p)
            seen.add(os.path.normcase(str(p.resolve())))
    for p in folders:
        try:
            found = list(p.glob("*.jsonl"))
        except OSError:
            continue
        for f in found:
            old = files.get(f.stem)  # 同じ会話が二つのフォルダにあれば、新しい方を使う
            if old is None or mtime(f) > mtime(old):
                files[f.stem] = f
    if RAW.exists():
        for f in RAW.glob("*.jsonl"):
            files.setdefault(f.stem, f)
    return list(files.values())


def hook_transcript_dir(data: str):
    """フックの標準入力（Claude Code が渡す JSON）の transcript_path から、会話のファイルのあるフォルダを返す。
    読めなければ None（フォルダの名前で探すだけにする）。ハブをジャンクションやシンボリックリンクの先で開いたとき、
    フォルダの名前の付け方が変わったときにも、その会話を拾うため。"""
    try:
        d = json.loads(data.lstrip(chr(0xFEFF)))  # 先頭の BOM を外す
        t = d.get("transcript_path") if isinstance(d, dict) else None
        if isinstance(t, str) and t.strip():
            p = Path(os.path.expanduser(t.strip()))
            if p.is_file():
                return p.parent
    except Exception:  # 形の違う入力は無視する
        pass
    return None


def message_text(content):
    if isinstance(content, list):
        return " ".join(x["text"] for x in content
                        if isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str))
    return content if isinstance(content, str) else ""


def user_text(content):
    """研究者の発言の（本文, 種類）。先に <system-reminder> の囲みを除いてから、機械が足した行かを判定する。
    種類は user（研究者の発言）か command（引数の無いスラッシュコマンド）。
    引数のあるコマンド（「/harness-dispatch 実装を進めたい」）は研究者の発言として user にする。
    引数の無いコマンドは読める形に残すが、研究者が話したかどうか（記録の対象か、要約が要るか）の判定には入れない
    （/exit や /clear で数えないため）。"""
    t = REMINDER_RX.sub("", message_text(content)).strip()
    if not t:
        return "", None
    if "<command-name>" in t:
        name = re.search(r"<command-name>(.*?)</command-name>", t, re.S)
        args = re.search(r"<command-args>(.*?)</command-args>", t, re.S)
        text = " ".join(x.group(1).strip() for x in (name, args) if x and x.group(1).strip())
        return text, ("user" if args and args.group(1).strip() else "command")
    if t.startswith(SKIP_PREFIXES):
        return "", None
    return t, "user"


def one_line(s, n=120):
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_summary(b):
    name, inp = b.get("name", "?"), b.get("input", {}) or {}
    if name == "AskUserQuestion":
        return None  # 質問は別に載せる
    # 伏せてから切る（切ってから伏せると、途中で切れたメールアドレスや鍵が残るため）
    for key in ("description", "file_path", "path", "pattern", "query", "url", "title", "skill"):
        if inp.get(key):
            return f"{name}（{one_line(redact(str(inp[key])))}）"
    if inp.get("command"):
        return f"{name}（{one_line(redact(str(inp['command'])), 80)}）"
    return name


def ask_text(inp):
    out = []
    for q in inp.get("questions", []) or []:
        opts = " / ".join(o.get("label", "") for o in q.get("options", []) or [])
        out.append(f"- 問い：{one_line(redact(str(q.get('question', ''))), 300)}（選択肢：{opts}）")
    return "\n".join(out)


def tool_result_text(c):
    if isinstance(c, list):
        return " ".join(x["text"] for x in c
                        if isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str))
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
            except (json.JSONDecodeError, RecursionError):
                continue
            if not isinstance(d, dict):
                continue  # JSON でも、オブジェクトでない行（[] など）は読み飛ばす
            typ = d.get("type")
            if typ == "custom-title" and isinstance(d.get("customTitle"), str) and d["customTitle"]:
                title_c = d["customTitle"]
            elif typ == "ai-title" and isinstance(d.get("aiTitle"), str) and d["aiTitle"]:
                title_a = d["aiTitle"]
            if isinstance(d.get("cwd"), str) and d["cwd"] and not cwd:
                cwd = d["cwd"]
            if not isinstance(d.get("timestamp"), str):
                d["timestamp"] = None
            if not isinstance(d.get("uuid"), str):
                d["uuid"] = None
            if not isinstance(d.get("sessionId"), str):
                d["sessionId"] = None
            if typ == "attachment" and not d.get("isSidechain"):
                # Claude が作業している間に研究者が送った発言。ほかの会話（peer）や作業の知らせは除く
                a = d.get("attachment")
                a = a if isinstance(a, dict) else {}
                o = a.get("origin")
                origin = o.get("kind") if isinstance(o, dict) else None
                if not isinstance(a.get("timestamp"), str):
                    a["timestamp"] = None
                if (a.get("type") == "queued_command" and a.get("commandMode") == "prompt"
                        and not a.get("isMeta") and origin in (None, "human")):
                    t, kind = user_text(a.get("prompt"))
                    if t:
                        items.append({"uuid": d.get("uuid"), "label": d.get("sessionId") or sid, "kind": kind,
                                      "ts": d.get("timestamp") or a.get("timestamp"), "text": t, "queued": True})
                        cur = None
                continue
            if d.get("isSidechain") or typ not in ("user", "assistant"):
                continue
            ts, uuid, label = d.get("timestamp"), d.get("uuid"), d.get("sessionId") or sid
            msg = d.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
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
                                          "text": ask_ids[x["tool_use_id"]] + "\n- 研究者の回答：" + one_line(redact(ans), 2000)})
                            cur = None
                t, kind = user_text(content)
                if t and not t.startswith("This session is being continued"):
                    items.append({"uuid": uuid, "label": label, "kind": kind, "ts": ts, "text": t})
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
                    if b["type"] == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                        cur["text"].append(b["text"].strip())
                    elif b["type"] == "tool_use":
                        s = tool_summary(b)
                        if s:
                            cur["tools"].append(s)
    for it in items:
        if it["kind"] == "claude":
            it["text"] = "\n\n".join(it["text"])
    items = [it for it in items if it["kind"] != "claude" or it["text"] or it["tools"]]
    # 作業中に送った発言は、あとで普通の発言としても記録されることがある。
    # 同じ会話（label）で、前後 10 分以内に同じ本文の普通の発言があれば、普通の発言の方を残す
    said = {}
    for i in items:
        if i["kind"] in ("user", "command") and not i.get("queued"):
            said.setdefault((i["label"], re.sub(r"\s+", " ", i["text"]).strip()), []).append(local(i["ts"]))

    def dup(i):
        if not i.get("queued"):
            return False
        t0 = local(i["ts"])
        others = said.get((i["label"], re.sub(r"\s+", " ", i["text"]).strip()), [])
        return any(t0 is None or x is None or abs((x - t0).total_seconds()) < 600 for x in others)
    items = [i for i in items if not dup(i)]
    # 題は、伏せてから 40 字で切る（切ってから伏せると、途中で切れたメールアドレスや鍵が、要約の題と一覧に残るため）
    title = redact(title_c or title_a or next((one_line(redact(i["text"]), 40) for i in items if i["kind"] == "user"), sid[:8]))
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
    """会話の ID → その会話のハブのコミット（「短い ID 日付 件名」）。git log は 1 回だけ起動する。
    git が動かなかったときは None（要約の冒頭の囲みを、空の一覧で書き直さないため）。"""
    try:
        # git の出力の文字コードの設定（i18n.logOutputEncoding）によらず UTF-8 で受け取る
        r = subprocess.run(["git", "-c", "i18n.logOutputEncoding=UTF-8", "-C", str(HUB), "log", "--all",
                            "--date=short", "--format=%x1e%h %ad%x1f%s%x1f%B"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if r.returncode != 0:
        return None  # git が無い、所有者の警告、壊れた参照など
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
        L.append(f"- 分岐元：{redact(parent_title)}。分岐元と同じ部分は省いた")
    L += ["", "道具の結果は載せていない（選択肢での質問と回答は載せる）。git の外のファイル。", ""]
    day = None
    for it in items:
        dt = local(it["ts"])
        if dt and f"{dt:%Y-%m-%d}" != day:
            day = f"{dt:%Y-%m-%d}"
            L += [f"## {day}", ""]
        hm = f"{dt:%H:%M:%S}" if dt else "--:--:--"
        who = {"user": "研究者", "command": "研究者のコマンド", "answer": "選択肢での質問と回答", "note": "注記", "claude": "Claude"}[it["kind"]]
        L += [f"### {hm} {who}", f"<!-- uuid: {it['uuid']} -->", ""]
        if it["kind"] in ("user", "command"):
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
AUTO_START_RX = re.compile(r"^<!-- auto:start[^>\n]*-->[ \t]*$", re.M)
AUTO_END = "<!-- auto:end -->"
AUTO_END_RX = re.compile(r"^<!-- auto:end -->[ \t]*$", re.M)
# auto_block() が囲みの中に書く行の形
AUTO_LINE_RX = re.compile(r"^- (?:会話の ID|作業ディレクトリ|期間|分岐元|生の記録|読める形)："
                          rf"|^- この会話でのハブのコミット（{re.escape(TRAILER)}）：[ \t]*$|^  - ")
BROKEN_AUTO = "冒頭の囲みが崩れている"
# git のマージがぶつかったときに入る印の行
CONFLICT_RX = re.compile(r"^(?:<<<<<<< |>>>>>>> |\|\|\|\|\|\|\| ).*$|^=======[ \t]*$")
AUTO_ID_RX = re.compile(r"^- 会話の ID：`([^`\n]+)`", re.M)


def conflict_ok(marks) -> bool:
    """囲みの中にある、git のぶつかりの印の行が、組（<<<<<<< 〜 [||||||| 〜] ======= 〜 >>>>>>>）としてそろっているか。
    そろっていなければ、ぶつかった所が囲みの外まで続いている（人の書いた行を含むかもしれない）。"""
    st = 0  # 0：組の外、1：<<<<<<< の後、2：||||||| の後、3：======= の後
    for m in marks:
        if m.startswith("<<<<<<< ") and st == 0:
            st = 1
        elif m.startswith("||||||| ") and st == 1:
            st = 2
        elif m.rstrip() == "=======" and st in (1, 2):
            st = 3
        elif m.startswith(">>>>>>> ") and st == 3:
            st = 0
        else:
            return False
    return st == 0


def find_auto(text: str):
    """要約の冒頭の囲みを探す。戻り値：(始まり, 終わり) の位置、None（囲みが無い）、"broken"（崩れている）。
    書き直してよいのは、このスクリプトが書いた形のものだけ（それ以外を置き換えると、人の書いた本文を消すため）。
    - 始まりの印の行が、最初の「## 」の見出しより前にある（``` の中の行は数えない）
    - その後、ほかの始まりの印より前に、終わりの印の行がある
    - 間の行（空行を除く）が、どれも auto_block() の書く形。git のぶつかりの印の行（<<<<<<< など）は、
      間で組としてそろっていれば、書く形の行とみなす（マージでぶつかった囲みを書き直せるように）
    「## 」より後ろにだけ始まりの印の行があるときも、崩れているとする（囲みが動かされた）。
    始まりの印の行より前に終わりの印の行があるときと、
    正しい囲みが無く、コードの外に印の行（始まりか終わり）が一つでもあるときは、崩れているとする（終わりの印だけが残った要約に、
    二つ目の囲みを入れて、前にスクリプトが書いた行を本文に残さないため）。None は、印の行が一つも無いときだけ。"""
    off, fence, start, conf = 0, False, None, []
    for line in text.split("\n"):
        if start is None:
            if line.lstrip().startswith(("```", "~~~")):
                fence = not fence
            elif not fence:
                if AUTO_START_RX.match(line):
                    start = off
                elif AUTO_END_RX.match(line):
                    return "broken"  # 始まりの印より前に終わりの印がある（始まりの印が消えた）
                elif line.startswith("## "):
                    # 見出しより後ろの始まりの印は、冒頭の囲みとはみなさない
                    return "broken" if _marker_outside_code(text) else None
        else:
            if AUTO_END_RX.match(line):
                if conf and not conflict_ok(conf):
                    return "broken"  # ぶつかった所が囲みの外まで続いている
                return start, off + len(line)
            if CONFLICT_RX.match(line):
                conf.append(line)
            elif line.strip() and not AUTO_LINE_RX.match(line):
                return "broken"  # 終わりの印が欠けた、またはスクリプトの書かない行がある
        off += len(line) + 1
    return "broken" if start is not None or _marker_outside_code(text) else None


def summary_ids(text: str):
    """要約の冒頭の囲みの「会話の ID」の値の並び。囲みが無ければ空。
    崩れた囲みでは、最初の「## 」の見出しより前から探す。"""
    b = find_auto(text)
    if b is None:
        return []
    if b == "broken":
        m = re.search(r"^## ", text, re.M)
        region = text[: m.start()] if m else text
    else:
        region = text[b[0]:b[1]]
    return AUTO_ID_RX.findall(region)


def insert_auto(text: str, block: str) -> str:
    """囲みの無い要約に、新しい囲みを入れる。最初の行が「# 」で始まればその後ろ、そうでなければいちばん上。
    前後は空行で分け、ほかは変えない。"""
    if text.startswith("# "):
        head, _, rest = text.partition("\n")
        if not rest:
            return f"{head}\n\n{block}\n"
        return f"{head}\n\n{block}\n" + ("" if rest.startswith("\n") else "\n") + rest
    if not text:
        return block + "\n"
    return block + "\n" + ("" if text.startswith("\n") else "\n") + text


def _marker_outside_code(text: str) -> bool:
    """コード（``` の囲み）の外に、冒頭の囲みの印の行（始まりか終わり）があるか。"""
    fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence and (AUTO_START_RX.match(line) or AUTO_END_RX.match(line)):
            return True
    return False


def no_marker(t: str) -> str:
    """件名や題に書かれた「<!--」を、囲みの印と取り違えない形にする（表示は同じ）。"""
    return t.replace("<!--", "&lt;!--")


def table_cell(t: str) -> str:
    """一覧の表の欄に入れる形。「|」は「／」に、「<!--」は印と取り違えない形に、末尾の「\\」は「＼」にする
    （表の区切りを壊したり、印を作ったりしないため）。"""
    t = no_marker(t.replace("|", "／"))
    return re.sub(r"\\+$", lambda m: "＼" * len(m.group(0)), t)


SUMMARY_BODY = """
- 要約した範囲：（要約待ち）

## 何をした会話か

## 流れ

## 決まったこと

## 未決のまま残ったこと

## 作ったもの・変えたもの

## 関連
"""


def read_or_none(path: Path):
    """UTF-8 で読む。読めない（UTF-8 でない、開けない）ときは None。1 つのファイルのせいで全体を止めないため。"""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def summary_body():
    """要約のひな形。研究の層（hub/templates/session-summary.md）があればそちらを使う。"""
    for c in (HUB / "hub" / "templates" / "session-summary.md", HUB / "harness" / "templates" / "session-summary.md"):
        t = read_or_none(c) if c.exists() else None
        if t is not None:
            return "\n" + t.lstrip("\n")
    return SUMMARY_BODY


def auto_block(s, r, parent, commits):
    L = [AUTO_START,
         f"- 会話の ID：`{s['id']}`",
         f"- 作業ディレクトリ：`{s.get('cwd') or '不明'}`"]
    if r["first"] and r["last"]:
        L.append(f"- 期間：{r['first']:%Y-%m-%d %H:%M} 〜 {r['last']:%Y-%m-%d %H:%M}")
    if parent:
        L.append(f"- 分岐元：[{no_marker(one_line(redact(parent['title']), 200))}]({parent['file']})")
    L += [f"- 生の記録：`records/sessions/raw/{s['id']}.jsonl`（git の外。この PC だけ）",
          f"- 読める形：`records/sessions/raw/digest/{s['id']}.md`（git の外）"]
    if commits:
        L.append(f"- この会話でのハブのコミット（{TRAILER}）：")
        L += [f"  - {no_marker(c)}" for c in commits]
    L.append(AUTO_END)
    return "\n".join(L)


NOT_SUMMARIZED_RX = re.compile(r"^- 要約した範囲：\s*要約しない", re.M)


def summarized_until(text):
    """「要約した範囲」の行の、最後の日時（「A 〜 B」なら B）。"""
    m = re.search(r"^- 要約した範囲：(.*)$", text, re.M)
    if not m:
        return None
    found = re.findall(r"(\d{4}-\d{2}-\d{2}) (\d{1,2}):(\d{2})", m.group(1))
    if not found:
        return None
    d, h, mi = found[-1]
    return f"{d} {int(h):02d}:{mi}"


def main_hub_of_worktree():
    """ハブのワークツリーの中で動いているなら、元のハブのフォルダを返す（そうでなければ None）。
    サブモジュールや --separate-git-dir のハブは、ワークツリーとはみなさない。"""
    try:
        r = subprocess.run(["git", "-C", str(HUB), "rev-parse", "--absolute-git-dir", "--git-common-dir"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return main_hub_by_git_file()
    lines = r.stdout.split("\n")
    if r.returncode != 0 or len(lines) < 2 or not lines[1].strip():
        return main_hub_by_git_file()
    git_dir = Path(lines[0].strip()).resolve()
    common = Path(lines[1].strip())
    common = (common if common.is_absolute() else HUB / common).resolve()
    return common.parent if git_dir != common else None


def main_hub_by_git_file():
    """git が動かないとき：ワークツリーでは .git がファイル（gitdir: <元>/.git/worktrees/<名前>）。分からなければ、何も書かない側に倒す。"""
    g = HUB / ".git"
    if not g.is_file():
        return None
    try:
        line = g.read_text(encoding="utf-8").strip()
    except OSError:
        return HUB
    if not line.startswith("gitdir:"):
        return HUB
    gd = Path(line.split(":", 1)[1].strip())
    gd = (gd if gd.is_absolute() else HUB / gd).resolve()
    return gd.parents[2] if gd.parent.name == "worktrees" and len(gd.parents) > 2 else HUB


def make_temp(d: Path):
    """d の中に一時ファイルを作って開く。戻り値：(fd, パス)。
    名前は短く（.t と英数字 2 字と .tmp）、書く先の名前によらない。書く先のパスが長さの上限（Windows）に収まっていれば、
    一時ファイルのパスも収まるように。.gitignore の「.*.tmp」に当たる。"""
    for i in range(80):
        tmp = d / f".t{secrets.token_hex(1 if i < 64 else 4)}.tmp"
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
        except FileExistsError:
            continue  # 同じ名前の一時ファイルがある（同時に動いた実行か、止められた実行の残り）
        return fd, str(tmp)
    raise FileExistsError("一時ファイルを作れない（同じ名前の一時ファイルが多すぎる）")


def same_text(path: Path, text: str) -> bool:
    """いまのファイルの中身が text と同じか（改行の種類と BOM の有無は問わない）。"""
    try:
        return path.read_text(encoding="utf-8-sig") == text
    except (OSError, UnicodeDecodeError):
        return False


def write_target(path: Path) -> Path:
    """一時ファイルから入れ替える先。シンボリックリンクなら、リンクを残して先のファイルを書く（リンクをふつうのファイルに
    置き換えないため）。書き込めないファイル（読み取り専用など）は、どの OS でも PermissionError（入れ替えれば書けてしまう
    OS でも、書かないと決めたものを書き換えないため。Windows では、入れ替えのやり直しで待たないため）。"""
    if os.path.islink(path):
        path = Path(os.path.realpath(path))
    if path.exists() and not os.access(path, os.W_OK):
        raise PermissionError("読み取り専用")
    return path


def keep_mode(tmp, path: Path):
    """入れ替える前に、いまのファイルの許可の設定を一時ファイルに写す。できなければ（まだ無いなど）そのまま。"""
    try:
        os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
    except OSError:
        pass


def clean_temps(dirs):
    """止められた実行が残した一時ファイル（make_temp の名前で、TEMP_STALE_SEC より古いもの）を消す。ほかのファイルは触らない。
    ロックを取った後に呼ぶ（そのときに一時ファイルを書いている実行は無い）。時刻は、書き換えと作成（Windows）・属性の変更
    （ほかの OS）の新しい方で見る（生の記録を写した一時ファイルは、元のファイルの時刻を受け継ぐため）。消せなくても続ける。"""
    now = time.time()
    for d in dirs:
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if not TEMP_RX.match(e.name):
                continue
            try:
                st = e.stat(follow_symlinks=False)
                if stat.S_ISREG(st.st_mode) and now - max(st.st_mtime, st.st_ctime) > TEMP_STALE_SEC:
                    os.unlink(e.path)
            except OSError:
                pass


def write_text(path: Path, text: str):
    """改行は LF にそろえ、その都度ちがう名前の一時ファイルに書いてから入れ替える。
    途中で止まっても壊れたファイルを残さず、同時に動いた別の会話のフックともぶつからない。
    中身が変わらなければ書かない（core.autocrlf=true で、中身の同じファイルが変更として出ないように）。"""
    path = write_target(path)
    if same_text(path, text):
        return
    fd, tmp = make_temp(path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        keep_mode(tmp, path)
        replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def replace(tmp, path):
    """入れ替える。Windows では、ほかのプログラムが一瞬開いていると失敗するので、数回やり直す。"""
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 9:
                raise
            time.sleep(0.2)


def acquire_lock(wait=120):
    """同時に動いた別の会話のフックや、記録どうしのリンクのスクリプトと、順番に動くためのロック。
    止められた実行が残したもの（STALE_SEC より古い）は外す。長く動くあいだは keep_lock で時刻を新しくする。"""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            atexit.register(lambda: LOCK.unlink(missing_ok=True))
            return True
        except FileExistsError:
            try:
                if time.time() - LOCK.stat().st_mtime > STALE_SEC:
                    LOCK.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                return False
            time.sleep(0.2)


_last_touch = [0.0]


def keep_lock():
    """長く動くとき、ロックが古いものと見なされないように、ときどき時刻を新しくする。"""
    now = time.time()
    if now - _last_touch[0] > 20:
        try:
            os.utime(LOCK)
        except OSError:
            pass
        _last_touch[0] = now


def copy_file(src: Path, dst: Path):
    """生の記録を写す。一時ファイルに写してから入れ替える（会話のファイルが消えた後は、これが唯一の写しになるため）。
    写しがシンボリックリンクなら先のファイルを書き、読み取り専用なら書かない（write_target）。"""
    dst = write_target(dst)
    fd, tmp = make_temp(dst.parent)
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        keep_mode(tmp, dst)
        replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_cache():
    try:
        c = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}  # 無い、または壊れている：すべてを読み直す
    if not isinstance(c, dict) or c.get("_v") != CACHE_VERSION:
        return {}
    # 中身の形がおかしい項目は捨てる（その会話は読み直す）
    return {k: v for k, v in c.items() if k == "_v" or (isinstance(v, dict) and isinstance(v.get("key"), list))}


SUMMARY_FILE_RX = re.compile(r"^(\d{4}-\d{2}-\d{2})_([0-9a-f]{8})\.md$")
ROW_RX = re.compile(r"^\| (?P<when>[^|]*) \|.*\| `(?P<id>[0-9a-f]{8})` \| \[(?P<file>[^\]]+)\]\(")
UNREADABLE_STATE = "読めない（UTF-8 で保存し直す）"
NO_OWN = "自分の発言なし"
CLASH = "先頭 8 文字が同じ別の会話の要約がある"
# 要約のファイルの無い行（自分の発言の無い会話、先頭 8 文字が同じ別の会話の要約がある会話）
LINKLESS_RX = re.compile(r"^\| (?P<when>[^|]*) \|.*\| `(?P<id>[0-9a-f]{8})` \| (?P<kind>%s|%s)"
                         % (re.escape(NO_OWN), re.escape(CLASH)))


def row_key(when: str, line: str):
    """一覧の行を並べる鍵。開始の欄の順。開始の無い行は後ろ。同じなら会話の ID の先頭、行の順
    （どの PC でも同じ順になるように。並びが PC で違うと、git で共有したときに毎回変わるため）。"""
    m = re.search(r"\| `([0-9a-f]{8})` \|", line)
    return (when or chr(0xFFFF), m.group(1) if m else "", line)


def other_rows(sessions, excludes, seen8, seen_ids, used, skipped):
    """一覧の、いまの会話以外の行（並べる鍵, 行）。records/sessions/ にある要約のファイルの全部を載せる。
    別の PC で開いた会話など、この PC で対象の会話に無いもの。除いた会話（「! <ID>」）は載せない。
    used は、いまの会話が使っている要約のファイルの名前。いまの会話と先頭 8 文字が同じでも、囲みの「会話の ID」が
    いまの会話のどれでもない要約（別の会話の要約）は載せる。
    前の一覧に行があれば、開始の時刻と題はそれを使い（この PC では分からないため）、無ければファイルから作る（一覧が消えても作り直せるように）。
    要約した範囲の欄は、いつもファイルから決め直す（別の PC で要約したなど、ファイルが変わっていることがあるため）。
    ただし前の行の欄が、決め直した値に印を足したもの（「未要約（**要約が要る**）」など。会話の記録のある PC が付けた）なら、
    前の行の欄をそのまま使う（この PC では要約が要るかを決められず、印を外すと、PC ごとに欄が変わり続けるため）。
    前の一覧に同じファイルの行が二つ以上あれば（git の union のマージ）、欄が決め直した値に合う行を使う。
    要約のファイルの無い行（自分の発言なし、先頭 8 文字が同じ別の会話の要約がある）も、この PC でいまの会話でなければ残す。"""
    cur_ids = {s["id"] for s in sessions}
    cur8 = {s["id"][:8] for s in sessions}
    prev, linkless = {}, {}
    idx = OUT / "README.md"
    for line in ((read_or_none(idx) or "").splitlines() if idx.exists() else []):
        m = ROW_RX.match(line)
        if m and UNREADABLE_STATE not in line:  # 読めなかった行は、ファイルから作り直す（直っているかもしれないため）
            # 最後の欄（要約した範囲）と、それより前。欄の中に「|」は無い（題の「|」は「／」に置き換えてある）
            body = line.rstrip().rstrip("|").rstrip()
            k = body.rindex("|")
            prev.setdefault(m.group("file"), []).append((m.group("when").strip(), body[:k], body[k + 1:].strip()))
            continue
        m = LINKLESS_RX.match(line)
        if m:
            # 同じ会話の行が二つ以上あれば後ろのもの（union のマージでは、取り込んだ側の行が後ろに来る）
            linkless[(m.group("id"), m.group("kind"))] = (m.group("when").strip(), line.rstrip())
    out = []
    on_disk8 = set()
    for p in sorted(OUT.glob("*.md")):
        m = SUMMARY_FILE_RX.match(p.name)
        if not m:
            continue
        day, id8 = m.groups()
        on_disk8.add(id8)
        if p.name in used or any(id8.startswith(x[:8]) for x in excludes):
            continue
        text = read_or_none(p)
        ids = summary_ids(text) if text is not None else []
        if id8 in cur8 and (not ids or set(ids) & cur_ids):
            continue  # いまの会話の要約の、使っていない写し（前と同じく載せない）
        seen = any(i in seen_ids for i in ids) if ids else id8 in seen8
        note = "（この PC では対象の外の会話）" if seen else "（この PC に会話の記録なし）"
        if text is None:
            skipped.append((p.name, "読めない（UTF-8 でない、開けない）"))
            out.append((day, f"| {day} | （読めない）{note} | `{id8}` | [{p.name}]({p.name}) | {UNREADABLE_STATE} |"))
            continue
        state = "要約しない" if NOT_SUMMARIZED_RX.search(text) else (summarized_until(text) or "未要約")
        cands = prev.get(p.name, [])
        if cands:
            fit = [c for c in cands if c[2] == state or c[2].startswith(state + "（")]
            if fit:
                when, head, cell = max(fit, key=lambda c: len(c[2]))  # 印の付いた方（記録のある PC が書いた方）
            else:
                (when, head, _), cell = cands[-1], state
            out.append((when, f"{head}| {cell} |"))
            continue
        head = text.split("\n", 1)[0].strip()
        title = head[len("# 会話の要約："):] if head.startswith("# 会話の要約：") else ""
        title = table_cell(one_line(redact(title.strip() or p.stem), 200))
        out.append((day, f"| {day} | {title}{note} | `{id8}` | [{p.name}]({p.name}) | {state} |"))
    for (id8, kind), (when, line) in linkless.items():
        if id8 in cur8 or any(id8.startswith(x[:8]) for x in excludes):
            continue  # この PC でいまの会話なら、その会話の行を作り直す
        if kind == NO_OWN and id8 in on_disk8:
            continue  # その後、要約ができた
        out.append((when, line))
    return out


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--hook", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--pending", action="store_true")
    a = ap.parse_args()
    extra = []  # 名前によらずに探す、会話のファイルのフォルダ
    if a.hook:
        # Claude Code は、終わった会話の情報（transcript_path など）を JSON で渡す。読めなければ無視する
        try:
            data = sys.stdin.read()
        except Exception:  # 標準入力が無い、閉じている、字が読めないなど
            data = ""
        d = hook_transcript_dir(data or "")
        if d is not None:
            extra.append(d)
    if not (HUB / "hub").exists():
        return  # 導入前のひな形では何もしない
    main_hub = main_hub_of_worktree()
    if main_hub is not None:
        # ハブのワークツリーの中では何も書かない。そこで開いた会話は、元のハブで動くときに取り込まれる
        if not a.hook:
            print(f"ハブのワークツリーの中なので、何もしない。元のハブのスクリプトを動かす：\n"
                  f"  python -X utf8 \"{main_hub / '.claude/skills/harness-session-archive/scripts/session_archive.py'}\" "
                  + " ".join(x for x in sys.argv[1:] if x != "--hook"))
        return
    dirs, excludes = load_sources()
    roots = [norm(d) for d in dirs]
    for d in (OUT, RAW, DIGEST):
        d.mkdir(parents=True, exist_ok=True)
    # フックからは長く待たない（持ち時間が短いため）。毎回すべてを照らし直すので、次の実行で追いつく
    if not acquire_lock(20 if a.hook else 120):
        if not a.hook:
            print(f"ほかの実行が終わらないので、何もしない（{LOCK} が残っていれば、動いている実行が無いことを確かめて消す）")
        return
    clean_temps((OUT, RAW, DIGEST))  # 止められた実行が残した一時ファイル
    cache = load_cache()
    raw_dir = RAW.resolve()
    saving = {"n": 0, "t": time.time(), "err": None}  # 前に控えを書いてから読み直した数と時刻、書けなかった理由

    def save_cache(final=False):
        """控え（records/sessions/raw/.index.json）を、一時ファイルから入れ替えて書く。途中でも書くのは、会話の終わりのフックが
        止められても、次の実行が初めから読み直さないように。書けなくても止めない（次の実行で読み直すだけ）。
        途中で一度書けなかったら、終わりまで書かない（読み取り専用などで、毎回待たないため）。"""
        if saving["err"] and not final:
            return
        cache["_v"] = CACHE_VERSION
        try:
            write_text(CACHE, json.dumps(cache, ensure_ascii=False))
            saving["err"] = None
        except OSError as e:
            saving["err"] = e.strerror or (str(e.args[0]) if e.args else type(e).__name__)
        saving["n"], saving["t"] = 0, time.time()

    def maybe_save_cache():
        """読み直した会話が CACHE_SAVE_EVERY 件になるか、CACHE_SAVE_SEC 秒たったら、途中までを控えに書く。
        一つの会話を済ませてから（生の記録を写してから）呼ぶ。"""
        if saving["n"] >= CACHE_SAVE_EVERY or (saving["n"] and time.time() - saving["t"] > CACHE_SAVE_SEC):
            save_cache()

    def in_scope(cwd):
        if not cwd:
            return False
        n = norm(cwd)
        return any(n == r or n.startswith(r + "\\") or n.startswith(r + "/") for r in roots)

    def take(f, sid):
        """会話のファイルを一つ読む。対象なら会話を返す（対象の外なら None）。"""
        st = f.stat()
        key = [st.st_size, int(st.st_mtime)]
        c = cache.get(sid)
        # 対象かどうかは、記録した作業ディレクトリ（cwd）を、いまの hub/session-sources.txt と毎回照らして決め直す。
        # 作業ディレクトリを足したり外したりしても、会話のファイルを読み直さずに反映されるようにするため
        if c and c.get("key") == key:
            if "labels" in c:
                if in_scope(c.get("cwd")):
                    return {"id": sid, "path": f, "parsed": None, **c}
                cache[sid] = {"key": key, "skip": True, "cwd": c.get("cwd"), "talk": True}
                return None
            if c.get("skip") and "cwd" in c and not (c.get("talk") and in_scope(c["cwd"])):
                return None
        saving["n"] += 1
        p = parse(f)
        talk = any(i["kind"] in ("user", "answer") for i in p["items"])
        if not talk or not in_scope(p["cwd"]):
            cache[sid] = {"key": key, "skip": True, "cwd": p["cwd"], "talk": talk}
            return None
        entry = {"key": key, "title": p["title"], "cwd": p["cwd"],
                 "uuids": [i["uuid"] for i in p["items"]], "labels": [i["label"] for i in p["items"]],
                 "kinds": [i["kind"] for i in p["items"]], "ts": [i["ts"] for i in p["items"]],
                 "te": [i.get("te") or i["ts"] for i in p["items"]], "changed": True}
        cache[sid] = entry
        return {"id": sid, "path": f, "parsed": p, **entry}

    sessions = []
    seen8 = set()  # この PC にある会話の記録（対象の外のものも含む）の、ID の先頭 8 文字
    seen_ids = set()  # 同じく、会話の ID
    failed = []  # 読めなかった・写せなかった会話の記録（ほかの会話は続ける）
    for f in candidate_files(dirs, extra):
        keep_lock()
        if not a.list:  # 一覧だけのときは写さないので、控えも書かない（写さないまま控えに載ると、次に写し直さないため）
            maybe_save_cache()  # 前の会話までを済ませた（写した）ところで
        sid = f.stem
        seen8.add(sid[:8])
        seen_ids.add(sid)
        if any(sid.startswith(x) for x in excludes):
            continue
        try:
            s = take(f, sid)
        except Exception as e:  # 1 つのファイルのせいで全体を止めない
            cache.pop(sid, None)
            failed.append((str(f), f"読めなかった：{type(e).__name__}"))
            continue
        if s is None:
            continue
        sessions.append(s)
        # 生の記録をハブに写す（一覧だけのときと、すでにハブの写しから読んだときは写さない）。
        # 読み直したときのほか、写しが消えていたときも写す（キャッシュがあって読み直さないと、写しが戻らないため）
        if not a.list and (s["parsed"] is not None or not (RAW / f.name).exists()):
            try:
                if f.resolve().parent != raw_dir:
                    copy_file(f, RAW / f.name)
            except Exception as e:
                if sid in cache:
                    cache[sid].pop("key", None)  # 次の実行で読み直して、写し直す
                failed.append((str(f), f"写せなかった：{type(e).__name__}"))

    if not a.hook:
        seen = [norm(v["cwd"]) for k, v in cache.items()
                if k != "_v" and isinstance(v, dict) and isinstance(v.get("cwd"), str) and v["cwd"]]
        hub_n = norm(str(HUB))
        idle = [d for d, r0 in zip(dirs, roots) if r0 != hub_n
                and not any(c == r0 or c.startswith(r0 + "\\") or c.startswith(r0 + "/") for c in seen)]
        if idle:
            print("hub/session-sources.txt の次の行は、どの会話にも当たらなかった（まだ会話が無いか、パスが違う）：")
            for d in idle:
                print(f"  {d}")

    # 各発言の持ち主：発言に記された会話がその発言を持っていればその会話、無ければ最も早い会話
    def first_ts(s):
        return min((x for x in s["ts"] if x), default="")
    sessions.sort(key=lambda s: (first_ts(s), s["id"]))  # 時刻が同じなら会話の ID の順（読み元によって入れ替わらないように）
    holders = {}
    for s in sessions:
        for u, lab in zip(s["uuids"], s["labels"]):
            holders.setdefault(u, []).append((s["id"], lab))
    owner = {}
    for u, hs in holders.items():
        ids = [h[0] for h in hs]
        owner[u] = hs[0][1] if hs[0][1] in ids else ids[0]

    # 要約のファイルの名前は、作るときに決めて変えない。あれば（*_<ID の先頭 8 文字>.md）その名前を使い続ける。
    # 開始日は時間帯や分岐元が分かったことで変わりうるが、名前を変えるとリンクが切れるため
    existing = {}
    for p in OUT.glob("*_*.md"):
        n = p.name
        if len(n) > 12 and n[-12] == "_":
            existing.setdefault(n[-11:-3], []).append(n)
    claimed = set()  # この実行で、どれかの会話が使うと決めた要約のファイル

    def pick_file(s, first):
        """会話の要約のファイルの名前と、使えなかったときの別の会話の要約の名前 (名前, ぶつかった名前)。
        あるファイル（名前の順）のうち、囲みの「会話の ID」がこの会話のもの、または囲みの無いものを使う。
        先頭 8 文字が同じ別の会話の要約は使わない（その要約の囲みを、この会話のもので書き換えないため）。"""
        have = sorted(existing.get(s["id"][:8], []))
        clash = None
        for n in have:
            if n in claimed:
                clash = clash or n
                continue
            t = read_or_none(OUT / n)
            ids = summary_ids(t) if t is not None else []
            if ids and s["id"] not in ids:
                clash = clash or n
                continue
            return n, None
        if have:
            return None, clash
        if not first:
            return None, None
        n = f"{first:%Y-%m-%d}_{s['id'][:8]}.md"
        return (None, n) if n in claimed else (n, None)

    rows = {}
    for s in sessions:
        own = [i for i, u in enumerate(s["uuids"]) if owner.get(u) == s["id"]]
        te, kinds = s.get("te") or s["ts"], s.get("kinds") or [""] * len(s["ts"])
        ts_own = sorted(s["ts"][i] for i in own if s["ts"][i])
        te_own = sorted(te[i] for i in own if te[i])
        talk_own = sorted(s["ts"][i] for i in own if s["ts"][i] and kinds[i] in ("user", "answer"))
        # 読めない時刻（1970 年より前など）は飛ばし、読める時刻の最初と最後を使う
        first = readable(ts_own)
        last = readable(reversed(te_own))
        s["own_idx"] = own
        file, clash = pick_file(s, first)
        if file:
            claimed.add(file)
        rows[s["id"]] = {"id": s["id"], "title": s.get("title", ""), "first": first, "last": last,
                         "last_talk": readable(reversed(talk_own)),
                         "cwd": s.get("cwd"), "n_own": len(own), "file": file, "clash": clash}

    if a.list:
        for s in sessions:
            r = rows[s["id"]]
            when = f"{r['first']:%Y-%m-%d %H:%M}" if r["first"] else "（自分の発言なし）"
            print(f"{when}  {s['id'][:8]}  own={r['n_own']:5d}  {r['title'][:50]}")
        return

    commits_of = hub_commit_map()
    git_ok = commits_of is not None
    commits_of = commits_of or {}
    skipped = []  # 読めなかった・書けなかった要約のファイル
    for s in sessions:
        keep_lock()
        maybe_save_cache()  # 前の会話の読める形と要約を書いたところで
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
            if r.get("clash"):
                skipped.append((r["clash"], f"{CLASH}。会話 {s['id']} の要約は作らない"))
            continue
        dg = DIGEST / f"{s['id']}.md"
        # 読める形は、会話のファイルが変わったときだけでなく、持ち主の決まり方（分岐元）が変わったときにも作り直す
        own_sig = hashlib.sha1(("\n".join(s["uuids"][i] or "" for i in s["own_idx"]) + "|" +
                                (parent["file"] if parent else "")).encode("utf-8")).hexdigest()
        if s.get("changed") or not dg.exists() or cache[s["id"]].get("own_sig") != own_sig:
            saving["n"] += 1
            try:
                p = s["parsed"] or parse(s["path"])
                own_set = {s["uuids"][i] for i in s["own_idx"]}
                items = [i for i in p["items"] if i["uuid"] in own_set]
                write_text(dg, render_digest(p, items, parent["title"] if parent else None))
                cache[s["id"]]["own_sig"] = own_sig
            except Exception as e:  # 読める形が作れなくても、要約のファイルと一覧は続ける
                cache[s["id"]].pop("own_sig", None)
                failed.append((str(s["path"]), f"読める形を作れなかった：{type(e).__name__}"))
        # 要約のファイル。名前は変えない（上の existing）。中身は Claude が書いたものなので消さない
        out = OUT / r["file"]
        block = auto_block(s, r, parent, commits_of.get(s["id"], []))
        text = None
        if out.exists():
            text = read_or_none(out)
            if text is None:
                # 読めない要約（UTF-8 でない、開けない）は書き換えず、ほかの会話を続ける
                skipped.append((r["file"], "読めない（UTF-8 でない、開けない）"))
                r["until"], r["pending"] = "読めない（UTF-8 で保存し直す）", False
                cache[s["id"]].pop("changed", None)
                continue
            b = find_auto(text)
            if b == "broken":
                # 囲みが崩れている（終わりの印が欠けた、囲みの中に書き足した、見出しの後ろに動いた）。
                # 置き換えると本文を消すおそれがあるので、触らずに知らせる。要約が要るかは、いまの本文で決める
                skipped.append((r["file"], BROKEN_AUTO))
                r["broken"] = True
                new = text
            elif not git_ok:
                new = text  # git が動かず、ハブのコミットの一覧が作れない
            elif b:
                new = text[: b[0]] + block + text[b[1]:]
            else:
                new = insert_auto(text, block)  # 囲みが消された。本文はそのままで、新しい囲みを入れる
        else:
            new = f"# 会話の要約：{no_marker(one_line(redact(r['title']), 200))}\n\n{block}\n{summary_body()}"
        if new != text:
            try:
                write_text(out, new)
            except OSError:
                # 書けない（ほかのプログラムが開いているなど）ときも、ほかの会話を続ける。次の実行でやり直す
                skipped.append((r["file"], "書けなかった（次の実行でやり直す）"))
                r["until"], r["pending"] = "書けなかった（次の実行でやり直す）", False
                continue
        if NOT_SUMMARIZED_RX.search(new):
            r["until"], r["pending"] = "要約しない", False
            cache[s["id"]].pop("changed", None)
            continue
        until = summarized_until(new)
        r["until"] = until
        # 要約が要るのは、要約した範囲の後に研究者の発言があるとき（Claude の最後の返答だけでは要らない）。
        # 自分の発言が無い会話（中身はすべて分岐元にある）は要らない
        lt = r["last_talk"]
        r["pending"] = r["n_own"] > 0 and ((until is None) or (lt is not None and f"{lt:%Y-%m-%d %H:%M}" > until))
        cache[s["id"]].pop("changed", None)

    rows_l = [rows[s["id"]] for s in sessions]
    kept = other_rows(sessions, excludes, seen8, seen_ids, {r["file"] for r in rows_l if r["file"]}, skipped)
    idx = ["# 会話の記録", "",
           "研究に関係する Claude Code の会話の要約。**履歴の記録**なので、書かれた決定はその時点のもの（現在の決定は `hub/decisions.md`）。",
           "", "- 対象：`hub/session-sources.txt` の作業ディレクトリで開いた会話",
           "- 要約は Claude が書く（`harness-session-archive`）。「要約が要る」の会話は、統括か、その会話自身が要約する",
           f"- 決定と会話のつながり：`hub/decisions.md` の「会話」の欄、またはコミットの `{TRAILER}:` の行",
           "- 生の記録（JSONL）と読める形：`records/sessions/raw/`（git の外。この PC だけ）", "",
           "| 開始 | 題 | 会話の ID | 要約 | 要約した範囲 |", "|---|---|---|---|---|"]
    lines = []
    for s in sessions:
        r = rows[s["id"]]
        # 並べる鍵は開始の欄（別の PC では、一覧の行の開始の欄で並べるので、同じ順になるように）
        when = f"{r['first']:%Y-%m-%d %H:%M}" if r["first"] else ""
        title = table_cell(one_line(redact(r["title"]), 200)) +("（分岐）" if r.get("parent") else "")
        if r["file"]:
            link = f"[{r['file']}]({r['file']})"
            state = ((r.get("until") or "未要約") + ("（**要約が要る**）" if r.get("pending") else "")
                     + (f"（{BROKEN_AUTO}）" if r.get("broken") else ""))
        elif r.get("clash"):
            link, state = CLASH, "―"
        else:
            par = r.get("parent")
            link = f"{NO_OWN}。中身は [{par['file']}]({par['file']})" if par else NO_OWN
            state = "―"
        lines.append((when, f"| {when} | {title} | `{r['id'][:8]}` | {link} | {state} |"))
    idx += [line for _, line in sorted(lines + kept, key=lambda x: row_key(*x))]
    try:
        write_text(OUT / "README.md", "\n".join(idx) + "\n")
    except OSError:
        skipped.append(("README.md", "書けなかった（次の実行でやり直す）"))
    save_cache(final=True)  # 書けなくても、要約が要る会話は出す
    pend =[r for r in rows_l if r.get("pending")]
    if a.pending:
        for r in pend:
            lt = r["last_talk"] or r["last"]
            print(f"{r['id'][:8]}  要約：records/sessions/{r['file']}  読める形：records/sessions/raw/digest/{r['id']}.md  "
                  f"要約した範囲：{r.get('until') or 'なし'}  最後の発言：{f'{lt:%Y-%m-%d %H:%M}' if lt else 'なし'}")
    elif not a.hook:
        print(f"会話 {len(rows_l)} 件を更新した。要約が要る会話：{len(pend)} 件（--pending で一覧）")
    if skipped and not a.hook:
        print("次の要約のファイルは触っていない：")
        for f, why in skipped:
            print(f"  records/sessions/{f}（{why}）")
        if any(why == BROKEN_AUTO for _, why in skipped):
            print(f"  {BROKEN_AUTO}ときは、冒頭の <!-- auto:start … --> 〜 <!-- auto:end --> を消して"
                  "（書き足した行は本文に移す）、最初の「## 」の見出しより前に印の 2 行だけを置けば、次から書き直される")
    if failed and not a.hook:
        print("次の会話の記録は飛ばした（ほかの会話は続けた）：")
        for f, why in failed:
            print(f"  {f}（{why}）")
    if not git_ok and not a.hook:
        print("git が動かなかったので、要約の冒頭の囲み（ハブのコミットの一覧など）は書き換えなかった")
    if saving["err"] and not a.hook:
        print(f"控え（records/sessions/raw/{CACHE.name}）を書けなかった（{saving['err']}）。"
              "ほかは続けた。次の実行では、会話の記録を読み直す")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # フックでは会話の終了を妨げない
        if "--hook" in sys.argv:
            print(f"session_archive: {e}", file=sys.stderr)
            sys.exit(0)
        raise
