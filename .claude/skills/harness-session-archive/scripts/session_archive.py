"""Claude Code の会話を、研究ハブに残す。

- 生の記録（JSONL）を records/sessions/raw/<会話の ID>.jsonl に写す（git の外。.gitignore）。
- 要約するための読める形を records/sessions/raw/digest/<会話の ID>.md に書く（git の外）。
  研究者の発言・Claude の返答の本文・道具の呼び出しの 1 行・選択肢での質問と回答を、時刻と uuid つきで載せる。
  ほかの会話・アプリが送った発言（turnOrigin で見分ける）は、そうと分かる見出しで載せ、研究者の発言には数えない。
  分岐・続きの会話は、各行を、それを最初に書いた会話に持たせ、同じ行を二重に載せない。
  各ファイルの頭の、ほかの会話から写した部分（copied_block）を、ファイルの形（続きでは、Claude Code のフォルダで
  初めて読んだときに控えた、ファイルの作られた時刻も）から決め、行の持ち主は、その行を
  写した部分の外に持つ会話とする。分岐元は、写した部分の最後の行の持ち主。持ち主がいま無い行は、持つ会話のうち
  元に最も近いものが載せる（ownership）。
- 要約のファイル records/sessions/<開始日>_<ID の先頭 8 文字>.md（git で管理）を用意する。
  - 無ければ、ひな形（中身は「要約待ち」）を作る。名前は作るときに決め、後から変えない（リンクを切らないため）。
  - あれば、冒頭の <!-- auto:start --> 〜 <!-- auto:end --> の間（会話の ID、期間、生の記録への参照、ハブのコミット）だけを書き直す。
    書き直すのは、囲みが最初の「## 」の見出しより前にあり、中身がこのスクリプトの書く形の行だけのときだけ
    （組になった git のぶつかりの印の行は、スクリプトの行とみなす）。崩れていれば触らずに知らせる。
    囲みの外（最初の「## 」より前）に、ほかの印の行か git のぶつかりの印の行があるときも、崩れているとする。
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
  python -X utf8 session_archive.py --list     （対象の会話を一覧するだけ。何も書かず、ロックも取らない）
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
from datetime import datetime, timedelta, timezone
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
CACHE_VERSION = 8  # 読み方を変えたら上げる。上がると、すべての会話を一度読み直す
CACHE_SAVE_EVERY, CACHE_SAVE_SEC = 25, 10  # 読み直した会話がこの数になるか、この秒数がたつごとに、途中までを控えに書く
TEMP_RX = re.compile(r"^\.t(?:[0-9a-f]{2}|[0-9a-f]{8})\.tmp$")  # make_temp の名前
TEMP_STALE_SEC = 600  # これより古い一時ファイルは、止められた実行が残したもの
SOURCES = HUB / "hub" / "session-sources.txt"
PROJECTS = Path.home() / ".claude" / "projects"
TRAILER = "Hub-Session"

SKIP_PREFIXES = (
    "<system-reminder", "<local-command", "<side-sessions", "<task-notification",
    "[Request interrupted", "<ci-monitor-event", "<user-prompt-submit-hook",
)
# Claude Code がスラッシュコマンドの発言に書く印。いまの版は <command-name> から始め、<command-message>、<command-args> と続く
COMMAND_TAGS = ("<command-name>", "<command-message>", "<command-args>")
# 指示書から始めた作業の会話の、起動の文（「<hub>/hub/briefs/<id>.md を読んで…」）の作業の id
BRIEF_RX = re.compile(r"hub[/\\]briefs[/\\]([^/\\\s`'\"<>|]+?)\.md")
CHORE_RX = re.compile(r"^chore\((sessions|links)\)!?:")
SECRET_MARK = "<秘密の値>"
# 鍵の前後は英数字で区切る（\b は日本語も字に数えるので、「キーはghp_…」の鍵を見落とす）。中身も英数字と記号だけにする
# （日本語や、ふつうの英語の文を飲み込まないように）
_NB = r"(?<![A-Za-z0-9])"
# PEM の秘密鍵（redact_pem）
PEM_BEGIN_RX = re.compile(r"-----BEGIN (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----")
PEM_END_RX = re.compile(r"-----END (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----")
# 鍵の字の行の並び。行の間の空白は、改行の前と後ろで分けて読む（空白の続きを二通りに分けて読み直さないため。
# 長い空白の後ろに鍵の字が無いとき、遅くならないように）
PEM_BODY_RX = re.compile(r"(?:[ \t]*(?:(?:\r\n?|\n)[ \t]*)?[A-Za-z0-9+/=]{16,})*")
PEM_MAX = 16384  # 始まりの行から、終わりの行までの長さの上限
SECRET = [
    # 英数字だけで、前半は 64 字まで（日本語は \w に入るので、長い日本語の並びを読みに行かないように）。
    # 最後の区切り（.com など）に英字が要る（「com.example.pkg@69.0.0」のような、名前と版の組を伏せないように）。
    # 後ろに「:」とパスの字が続くもの（「git@github.com:lab/x.git」）は、git の URL なので伏せない。
    # 後ろに区切りの字が続くとき、短く切って当てないように、後ろが英数字・「.英数字」でないことも確かめる
    (re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
                r"\.(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]+(?![A-Za-z0-9-]|\.[A-Za-z0-9-]|:[A-Za-z0-9_./~-])"), "<メールアドレス>"),
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


def user_text(content):
    """研究者の発言の（本文, 種類）。先に <system-reminder> の囲みを除いてから、機械が足した行かを判定する。
    種類は user（研究者の発言）か command（引数の無いスラッシュコマンド）。
    引数のあるコマンド（「/harness-dispatch 実装を進めたい」）は研究者の発言として user にする。
    引数の無いコマンドは読める形に残すが、研究者が話したかどうか（記録の対象か、要約が要るか）の判定には入れない
    （/exit や /clear で数えないため）。
    スラッシュコマンドとみなすのは、Claude Code が書く印（COMMAND_TAGS）で始まる発言だけ（印を本文に貼った発言を、
    コマンドとして読んで本文を失わないため）。"""
    t = strip_reminders(message_text(content)).strip()
    if not t:
        return "", None
    if t.startswith(COMMAND_TAGS):
        name, args = tag_text(t, "command-name"), tag_text(t, "command-args")
        text = " ".join(x.strip() for x in (name, args) if x and x.strip())
        return text, ("user" if args and args.strip() else "command")
    if t.startswith(SKIP_PREFIXES):
        return "", None
    return t, "user"


def not_typed(d) -> bool:
    """user の行が、研究者が打ったものではなく、ほかの会話かアプリが送ったものか。確かめられた形だけを数える：
    - turnOrigin が host_synthetic（ほかの会話が start_session で始めた会話の、最初の発言）
    - turnOrigin が sdk で、Agent SDK から始めた会話（entrypoint が sdk-cli）か、その実行の最初の発言（promptIndex が 0）
    ほかの値（auto_continuation など、確かめられなかったもの）と、印の無い行は、研究者の発言のままにする。"""
    to = d.get("turnOrigin")
    if to == "host_synthetic":
        return True
    if to == "sdk":
        tp = d.get("turnPosition")
        return d.get("entrypoint") == "sdk-cli" or (isinstance(tp, dict) and tp.get("promptIndex") == 0)
    return False


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


def drop_queued_twins(items):
    """作業中に送った発言（queued）は、あとで普通に打った発言としても記録されることがある。
    同じ会話（label）で、同じ本文（空白をそろえる）の普通の発言が、その時刻から 10 分以内に後に続けば、作業中に送った方を除く。
    一つの普通の発言で除けるのは、作業中に送った発言の一つだけ（続けて送った「はい」を、まとめて消さないため）。
    前にある普通の発言では除かない（同じ本文を、普通に打ってから作業中にもう一度送ったときに、二つ目を消さないため）。
    時刻の読めない発言は除かない。"""
    typed = {}
    for i in items:
        if i["kind"] in ("user", "command") and not i.get("queued"):
            t = local(i["ts"])
            if t is not None:
                typed.setdefault((i["label"], re.sub(r"\s+", " ", i["text"]).strip()), []).append(t)
    if not typed:
        return items
    for v in typed.values():
        v.sort()
    queued = [(local(i["ts"]), k) for k, i in enumerate(items) if i.get("queued")]
    used, drop = set(), set()
    # 時刻の順に、まだ使っていない、いちばん早い普通の発言と組にする（同じ長さの窓なので、これで組がいちばん多くなる）
    for t0, k in sorted((x for x in queued if x[0] is not None), key=lambda x: (x[0], x[1])):
        i = items[k]
        key = (i["label"], re.sub(r"\s+", " ", i["text"]).strip())
        for n, x in enumerate(typed.get(key, [])):
            d = (x - t0).total_seconds()
            if d >= 600:
                break
            if d >= 0 and (key, n) not in used:
                used.add((key, n))
                drop.add(k)
                break
    return [i for k, i in enumerate(items) if k not in drop]


def fallback_title(text: str) -> str:
    """題（customTitle も aiTitle も）が無いときの題。指示書から始めた作業の会話（起動の文に hub/briefs/<id>.md がある）は、
    作業の id から作る（どの作業の会話も、起動の文の同じパスの頭になるため）。そうでなければ、最初の発言の頭の 40 字。"""
    m = BRIEF_RX.search(text)
    if m:
        return one_line(redact(f"作業 {m.group(1)}" + ("（続き）" if "続きから" in text else "")), 80)
    return one_line(redact(text), 40)


def parse(path: Path, birth=None):
    """会話を、発言の並び（uuid、種類、時刻、本文）に直す。birth は、会話のファイルの作られた時刻（copied_block）。"""
    sid, cwd, title_c, title_a = path.stem, None, None, None
    items, ask_ids = [], {}
    raw, nou = [], None  # uuid のある行の (uuid, 行の会話の ID, 時刻, 印)、uuid の無い行の最初の時刻
    cur = None  # 続いている Claude の返答
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n_line, line in enumerate(fh):
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
            ri = None  # この行の、uuid のある行の並び（raw）での位置
            if not d.get("isSidechain"):
                if d["uuid"]:
                    a = d.get("attachment")
                    mark = ("fb" if typ == "attachment" and isinstance(a, dict) and a.get("type") == "fork_briefing" else
                            "cb" if typ == "system" and d.get("subtype") == "compact_boundary" else None)
                    ri = len(raw)
                    raw.append((d["uuid"], d["sessionId"] or sid, d["timestamp"], mark))
                elif d["timestamp"] and nou is None:
                    nou = d["timestamp"]  # uuid の無い、時刻のある最初の行（続きでは、続きを始めた時刻）
            if typ == "attachment" and not d.get("isSidechain"):
                # Claude が作業している間に研究者が送った発言。ほかの会話（peer）や作業の知らせは除く
                a = d.get("attachment")
                a = a if isinstance(a, dict) else {}
                o = a.get("origin")
                origin = o.get("kind") if isinstance(o, dict) else None
                if not isinstance(a.get("timestamp"), str):
                    a["timestamp"] = None
                if a.get("type") == "fork_briefing":
                    # 分岐して作った会話に、Claude Code が写した部分の後ろに置く行（写した部分は copied_block で決める）
                    cur = None
                    continue
                if (a.get("type") == "queued_command" and a.get("commandMode") == "prompt"
                        and not a.get("isMeta") and origin in (None, "human")):
                    t, kind = user_text(a.get("prompt"))
                    if t:
                        items.append({"uuid": d.get("uuid"), "label": d.get("sessionId") or sid, "kind": kind,
                                      "ts": d.get("timestamp") or a.get("timestamp"), "text": t, "queued": True, "ri": [ri]})
                        cur = None
                continue
            if d.get("isSidechain") or typ not in ("user", "assistant"):
                continue
            ts, uuid, label = d.get("timestamp"), d.get("uuid"), d.get("sessionId") or sid
            msg = d.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if typ == "user":
                if d.get("isCompactSummary"):
                    items.append({"uuid": uuid, "label": label, "kind": "note", "ts": ts, "ri": [ri],
                                  "text": "（ここで会話が要約され、続きの会話になった）"})
                    cur = None
                    continue
                if d.get("isMeta"):
                    continue
                if isinstance(content, list):
                    for x in content:
                        if isinstance(x, dict) and x.get("type") == "tool_result" and x.get("tool_use_id") in ask_ids:
                            if x.get("is_error") is True:
                                # 質問が取り消された・断られた（研究者の回答ではない）
                                items.append({"uuid": f"{uuid}-a", "label": label, "kind": "cancelled", "ts": ts, "ri": [ri],
                                              "text": ask_ids[x["tool_use_id"]] + "\n- 質問は取り消された"})
                            else:
                                ans = tool_result_text(x.get("content"))
                                items.append({"uuid": f"{uuid}-a", "label": label, "kind": "answer", "ts": ts, "ri": [ri],
                                              "text": ask_ids[x["tool_use_id"]] + "\n- 研究者の回答：" + one_line(redact(ans), 2000)})
                            cur = None
                t, kind = user_text(content)
                if t and not t.startswith("This session is being continued"):
                    if kind == "user" and not_typed(d):
                        kind = "sent"  # ほかの会話・アプリが送った発言（研究者の発言には数えない）
                    items.append({"uuid": uuid, "label": label, "kind": kind, "ts": ts, "text": t, "ri": [ri]})
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
                    # 続いている返答と行の会話の ID が違う（分岐で写した返答の後ろに、分岐の自分の返答が続いた）なら、別の返答にする
                    if cur is not None and cur["label"] != label:
                        cur = None
                    if cur is None:
                        cur = {"uuid": uuid, "label": label, "kind": "claude", "ts": ts, "parts": []}
                        items.append(cur)
                    cur["te"] = ts or cur.get("te")  # 返答が続いた最後の時刻（期間と、要約が要るかの判定に使う）
                    text = b["text"].strip() if b["type"] == "text" and isinstance(b.get("text"), str) else ""
                    tool = tool_summary(b) if b["type"] == "tool_use" else None
                    if text or tool:
                        # まとめた返答の、行ごとの部分（載せるものがある行だけ）。行ごとに持ち主を決めるため
                        if not cur["parts"] or cur["parts"][-1]["n"] != n_line:
                            cur["parts"].append({"n": n_line, "i": ri, "u": uuid, "ts": ts, "text": [], "tools": []})
                        cur["parts"][-1]["text" if text else "tools"].append(text or tool)
    items = [it for it in items if it["kind"] != "claude" or it["parts"]]
    for it in items:
        if it["kind"] == "claude":
            it["ri"] = [p["i"] for p in it["parts"]]
            it["text"] = "\n\n".join(x for p in it["parts"] for x in p["text"])
            it["tools"] = [x for p in it["parts"] for x in p["tools"]]
    items = drop_queued_twins(items)
    # 題は、伏せてから 40 字で切る（切ってから伏せると、途中で切れたメールアドレスや鍵が、要約の題と一覧に残るため）
    first_user = next((i["text"] for i in items if i["kind"] in ("user", "sent")), None)
    title = redact(title_c or title_a or (fallback_title(first_user) if first_user else sid[:8]))
    times = [local(i.get("te") or i["ts"]) for i in items]
    first = next((local(i["ts"]) for i in items if local(i["ts"])), None)
    last = max((t for t in times if t), default=None)
    blk = copied_block(raw, sid, nou, birth)
    return {"id": sid, "cwd": cwd, "title": title, "items": items, "first": first, "last": last,
            "raw": [r[0] for r in raw], "blk": blk,
            # 写した部分の後ろで、行の会話の ID がほかの会話のもの（持ち主が決まらないときに、後回しにする）
            "fl": [k for k in range(blk, len(raw)) if raw[k][1] != sid]}


def _instant(ts):
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return t if t.tzinfo else t.astimezone()
    except (ValueError, AttributeError, TypeError, OSError, OverflowError):
        return None


BIRTH_SLACK = 2  # 秒。続きの自分の最初の行は、ファイルが作られた時刻より少し前の時刻を持つことがある


def birth_time(st):
    """ファイルの作られた時刻（エポック秒）。Windows と macOS のほかでは分からないので None。"""
    b = getattr(st, "st_birthtime", None)
    if b is None and os.name == "nt":
        b = st.st_ctime  # Python 3.12 より前の Windows では、st_ctime が作られた時刻
    return float(b) if isinstance(b, (int, float)) and not isinstance(b, bool) and b > 0 else None


def _birth_instant(birth):
    """作られた時刻の BIRTH_SLACK 秒前（時間帯つき）。分からなければ None。"""
    if birth is None:
        return None
    try:
        return datetime.fromtimestamp(birth - BIRTH_SLACK, timezone.utc)
    except (ValueError, OSError, OverflowError, TypeError):
        return None


def copied_block(raw, sid, nou, birth=None):
    """ファイルの頭の、ほかの会話から写した行の数（raw の位置で。これより後ろが、この会話の書いた行）。
    - 分岐（写した行が元の会話の ID のまま）：頭の、行の会話の ID がほかの会話のものの並び
    - 分岐（写した行に新しい ID を付け、写した部分の後ろに fork_briefing を置く）：最後の fork_briefing より前
    - 続き（元の会話の最後の要約の境目（compact_boundary）の写しで始まる）：頭の、続きを始めた時刻
      （uuid の無い、時刻のある最初の行。無ければ境目の時刻）より前の行（境目、要約、要約の後に残した部分、
      境目の後の元の会話の行）。ファイルの作られた時刻 birth（エポック秒。分かるときだけ）の BIRTH_SLACK 秒前が、
      その時刻より前なら、その時刻より前の行（CLI では、uuid の無い行が、続きの自分の最初の発言の後にしか無いことが
      ある）。作られた時刻は写した部分を短くするだけで、ほかの PC から写したファイルのように後ろなら使わない。
      ただし uuid の無い時刻つきの行が一つも無く、作られた時刻が最後の行の時刻より後ろでなければ、境目の時刻の
      代わりに、作られた時刻の BIRTH_SLACK 秒前を使う。最後の行が fork_briefing なら、それは自分の行
    当てはまるもののうち、いちばん長いもの。"""
    k = 0
    while k < len(raw) and raw[k][1] != sid:
        k += 1
    fb = [j for j in range(k, len(raw)) if raw[j][3] == "fb"]
    n = max(k, fb[-1] if fb else 0)
    if k == 0 and raw and raw[0][3] == "cb":
        lim = _instant(nou) if nou else None
        b = _birth_instant(birth)
        if lim is None and b is not None:
            # uuid の無い時刻つきの行が無い：境目の時刻は当てにならないので、作られた時刻が続きを始めた時刻として
            # ありうる（最後の行の時刻より後ろでない。後ろなら、ほかから写したファイル）なら、それを使う
            last = max((x for x in map(_instant, (r[2] for r in raw)) if x is not None), default=None)
            if last is not None and b + timedelta(seconds=BIRTH_SLACK) <= last:
                lim = b
        lim = lim or _instant(raw[0][2])
        if lim is not None and b is not None and b < lim:
            lim = b
        t = 1
        while lim is not None and t < len(raw) and (_instant(raw[t][2]) or lim) < lim:
            t += 1
        if raw[t - 1][3] == "fb":
            t -= 1
        n = max(n, t)
    return n


def ownership(sessions):
    """各行の持ち主と、各会話の読める形に載せる部分を、ファイルの形（copied_block）だけから決める。
    - 行の持ち主（書いた会話）は、その行を写した部分の外に持つ会話。二つ以上あれば（形の崩れた記録）、
      行の会話の ID が自分のもの、早く始まった、会話の ID の順
    - 持ち主がいま無い行（除いた、対象の外、この PC に無い）は、持つ会話のうち元に最も近いもの（ほかの持つ会話の
      祖先でないもの。写した部分の短い、写した後の自分の発言が早い、会話の ID の順）が載せる
    戻り値：(会話の ID -> [(項目の番号, 部分の始まり, 終わり)]、会話の ID -> 写した部分の行の持ち主（後ろの行から、重ねない）)。
    項目は、行の持ち主の変わり目で部分に分ける（行ごとに一つの会話だけが載せる。落とさず、二重にしない）。"""
    blk = {s["id"]: s.get("blk") or 0 for s in sessions}
    fl = {s["id"]: set(s.get("fl") or ()) for s in sessions}
    start = {s["id"]: min((x for x in s["ts"] if x), default="") for s in sessions}
    # 写した部分の後ろの、自分の最初の発言の時刻（写しを作った順。自分の発言の無い写しは後ろ）
    own0 = {s["id"]: min((t for t, ri in zip(s["ts"], s.get("ri") or ()) if t and any(i is not None and i >= blk[s["id"]] for i in ri)),
                         default=chr(0xFFFF)) for s in sessions}
    pos = {}  # 行の uuid -> {持つ会話: その会話での位置}
    for s in sessions:
        sid = s["id"]
        for k, u in enumerate(s.get("raw") or ()):
            d = pos.get(u)
            if d is None:
                pos[u] = {sid: k}
            elif sid not in d:
                d[sid] = k
    memo = {}

    def writer(u):
        hs = pos[u]
        if len(hs) == 1:
            (h, k), = hs.items()
            return h if k >= blk[h] else None
        if u not in memo:
            outs = [h for h, k in hs.items() if k >= blk[h]]
            memo[u] = min(outs, key=lambda h: (hs[h] in fl[h], start[h], h)) if outs else None
        return memo[u]

    chain = {}
    for s in sessions:
        sid, raw, c = s["id"], s.get("raw") or (), []
        for k in range(blk[sid] - 1, -1, -1):
            o = writer(raw[k])
            if o is not None and o != sid and o not in c:
                c.append(o)
        chain[sid] = c

    def ancestors(h):
        out, x = set(), h
        while chain[x] and chain[x][0] not in out and chain[x][0] != h:
            x = chain[x][0]
            out.add(x)
        return out
    anc, near = {}, {}

    def holder(u):
        o = writer(u)
        if o is not None:
            return o
        if u not in near:
            hs = pos[u]
            for h in hs:
                if h not in anc:
                    anc[h] = ancestors(h)
            cands = [h for h in hs if not (anc[h] & hs.keys())] or list(hs)
            near[u] = min(cands, key=lambda h: (blk[h], own0[h], h))
        return near[u]

    runs = {}
    for s in sessions:
        sid, raw, out = s["id"], s.get("raw") or (), []
        for k, ri in enumerate(s.get("ri") or ()):
            who = [sid if i is None else holder(raw[i]) for i in ri] or [sid]
            a = 0
            for j in range(1, len(who) + 1):
                if j == len(who) or who[j] != who[a]:
                    if who[a] == sid:
                        out.append((k, a, j))
                    a = j
        runs[sid] = out
    return runs, chain


def run_info(s, k, a, b):
    """会話 s の項目 k の部分 a〜b の (uuid, 時刻, 終わりの時刻, 種類)。項目の全体なら項目の値。"""
    ri, te = s["ri"][k], (s.get("te") or s["ts"])[k]
    if a == 0 and b == len(ri):
        return s["uuids"][k], s["ts"][k], te, s["kinds"][k]
    rt = s["rt"][str(k)]
    uuid = s["raw"][ri[a]] if a and ri[a] is not None else s["uuids"][k]
    ts = s["ts"][k] if a == 0 else rt[a]
    return uuid, ts, next((x for x in reversed(rt[a:b]) if x), ts), s["kinds"][k]


def part_item(it, a, b):
    """項目の部分 a〜b（Claude の返答の、まとめた行の一部）を、読める形に載せる項目にする。全体ならそのまま。"""
    if it["kind"] != "claude" or (a == 0 and b == len(it["parts"])):
        return it
    ps = it["parts"][a:b]
    return dict(it, uuid=it["uuid"] if a == 0 else ps[0]["u"] or it["uuid"],
                ts=it["ts"] if a == 0 else ps[0]["ts"], text="\n\n".join(x for p in ps for x in p["text"]),
                tools=[x for p in ps for x in p["tools"]])


def zone_text(stamps) -> str:
    """時刻の並びを、この PC の時間帯で書いたときの UTC からの差（「+09:00」。夏時間などで二つ以上なら「 / 」でつなぐ）。"""
    seen = []
    for ts in stamps:
        t = local(ts) if ts else None
        z = t.strftime("%z") if t else ""
        z = f"{z[:3]}:{z[3:5]}" if z else ""
        if z and z not in seen:
            seen.append(z)
    return " / ".join(seen)


def cut_cycles(parent_of, start_of):
    """分岐元の輪を切る（輪の中で最も早く始まった会話の分岐元を外す）。外した会話の ID の並びを返す。"""
    cut = []
    for sid in sorted(parent_of):
        seen, x = [], sid
        while x is not None and x not in seen:
            seen.append(x)
            x = parent_of.get(x)
        if x is not None:
            cyc = seen[seen.index(x):]
            root = min(cyc, key=lambda c: (start_of.get(c, ""), c))
            parent_of[root] = None
            cut.append(root)
    return cut


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


def render_digest(s, items, parent_title, zone=""):
    L = [f"# 会話（要約するための読める形）：{redact(s['title'])}", "",
         f"- 会話の ID：`{s['id']}`",
         f"- 作業ディレクトリ：`{s['cwd'] or '不明'}`",
         f"- 生の記録：`records/sessions/raw/{s['id']}.jsonl`（`<!-- uuid: … -->` の値で行を探せる）"]
    if zone:
        L.append(f"- 時刻の時差（UTC から）：{zone}（この読める形を作ったときの、この PC の時間帯）")
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
        who = {"user": "研究者", "command": "研究者のコマンド", "sent": "ほかの会話・アプリからの入力",
               "answer": "選択肢での質問と回答", "cancelled": "選択肢での質問（取り消された）", "note": "注記",
               "claude": "Claude"}[it["kind"]]
        L += [f"### {hm} {who}", f"<!-- uuid: {it['uuid']} -->", ""]
        if it["kind"] in ("user", "command", "sent"):
            L += [quote(redact(it["text"])), ""]
        elif it["kind"] in ("answer", "cancelled", "note"):
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
    二つ目の囲みを入れて、前にスクリプトが書いた行を本文に残さないため）。None は、印の行が一つも無いときだけ。
    正しい囲みがあっても、最初の「## 」の見出しより前のコードの外に、ほかの印の行か、囲みの外の git のぶつかりの印の行が
    あれば、崩れているとする（ぶつかった所が囲みの前から始まり、囲みが二つ写ったときに、一つ目だけを書き直して、
    ぶつかりの印と二つ目を残さないため）。"""
    off, fence, start, conf, pre_conflict = 0, False, None, [], False
    lines = text.split("\n")
    for k, line in enumerate(lines):
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
                elif CONFLICT_RX.match(line):
                    pre_conflict = True
        else:
            if AUTO_END_RX.match(line):
                if conf and not conflict_ok(conf):
                    return "broken"  # ぶつかった所が囲みの外まで続いている
                if pre_conflict or _marker_before_heading(lines[k + 1:]):
                    return "broken"  # 囲みの外の、ぶつかりの印か、二つ目の囲みの印がある
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


def _marker_before_heading(lines) -> bool:
    """行の並びの、最初の「## 」の見出しより前のコード（``` の囲み）の外に、冒頭の囲みの印の行（始まりか終わり）か、
    git のぶつかりの印の行があるか。"""
    fence = False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence:
            if line.startswith("## "):
                return False
            if AUTO_START_RX.match(line) or AUTO_END_RX.match(line) or CONFLICT_RX.match(line):
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
        # 時差を付ける（この PC のいまの時間帯の時刻。始まりと終わりの時差が同じなら、終わりにだけ付ける）
        a, b = with_offset(r["first"]), with_offset(r["last"])
        if a[16:] == b[16:]:
            a = a[:16]
        L.append(f"- 期間：{a} 〜 {b}")
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


# 「要約した範囲」の日時。秒があってもよい。後ろに時差があってもよい：空白の後ろに「+09:00」「+0900」「+09」「-05:00」
# 「Z」（符号は全角の「＋」「−」「－」も）、または括弧の中（「（+09:00）」）。「+」と「Z」は、空白が無くてもよい（前と同じ）。
# 空白も括弧も無い「-」（「10:00-11:30」）は範囲の印として読み、時差にしない。
# 時差として読むのは、実際にある時差だけ：−12 から +14 までのちょうどの時と、使われている 30 分・45 分のずれ
# （「10:00 -11:30」の「-11:30」は時差にしない）
_OFF_POS = r"(?:0\d|1[0-4])(?::?00)?|(?:0[3-6]|09|10):?30|(?:05|08|12|13):?45"
_OFF_NEG = r"(?:0\d|1[0-2])(?::?00)?|(?:0[23]|09):?30"
_OFF_END = r"(?![0-9:A-Za-z])"
_TIME = r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?![0-9])"
_OFF_BODY = (rf"(?:([+＋](?=(?:{_OFF_POS}){_OFF_END})|[\-−－](?=(?:{_OFF_NEG}){_OFF_END}))(\d{{2}})(?::?(\d{{2}}))?|(Z))"
             + _OFF_END)
_OFF = rf"(?:(?:[ \t　]+|(?=[+＋Z])){_OFF_BODY}|[ \t　]*[(（][ \t　]*{_OFF_BODY}[ \t　]*[)）])?"
UNTIL_RX = re.compile(rf"(\d{{4}}-\d{{2}}-\d{{2}}) {_TIME}{_OFF}")
# 日時の後ろの「〜 11:30」（同じ日の時刻で終わる範囲）。「〜」「～」か、前後に空白のある「-」で区切ったもの
UNTIL_TAIL_RX = re.compile(rf"(?:[ \t　]*[〜～][ \t　]*|[ \t　]+-[ \t　]+){_TIME}{_OFF}")


def _until_match(text):
    """「要約した範囲」の行の最後の日時の (日付, 時, 分, 秒か None, 時差の分か None)。日時の後ろに「〜 11:30」が続けば、
    終わりはその日付のその時刻（時差が無ければ、前の日時の時差）。その時刻が前の日時の時刻より前なら、次の日の時刻
    （「2026-01-10 23:00 〜 01:30」）。読めなければ None。"""
    m = re.search(r"^- 要約した範囲：(.*)$", text, re.M)
    if not m:
        return None
    line = m.group(1)
    found = list(UNTIL_RX.finditer(line))
    if not found:
        return None
    f = found[-1]
    d, g = f.group(1), f.groups()[1:]
    off = _offset(g[3:])
    t = UNTIL_TAIL_RX.match(line, f.end())
    if t:
        start = (int(g[0]), int(g[1]), int(g[2] or 0))
        g = t.groups()
        o = _offset(g[3:])
        off = off if o is None else o
        if (int(g[0]), int(g[1]), int(g[2] or 0)) < start:
            try:
                d = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            except (ValueError, OverflowError):
                pass  # 読めない日付は、前と同じに読む（until_instant が読めない時刻とする）
    return d, int(g[0]), int(g[1]), (int(g[2]) if g[2] else None), off


def _offset(g):
    """時差の組（空白の後ろの形と、括弧の中の形の、符号・時・分・Z）を分にする。無ければ None。"""
    for sg, oh, om, z in (g[0:4], g[4:8]):
        if z:
            return 0
        if sg:
            v = int(oh) * 60 + int(om or 0)
            return -v if sg in "-−－" else v
    return None


def summarized_until(text):
    """「要約した範囲」の行の、最後の日時（「A 〜 B」なら B）。時差が書いてあれば、後ろに「+09:00」の形で付ける。"""
    m = _until_match(text)
    if not m:
        return None
    d, h, mi, sec, off = m
    s = f"{d} {h:02d}:{mi:02d}" + (f":{sec:02d}" if sec is not None else "")
    if off is not None:
        s += f" {'-' if off < 0 else '+'}{abs(off) // 60:02d}:{abs(off) % 60:02d}"
    return s


def until_instant(text):
    """「要約した範囲」の最後の日時を、時刻（時間帯つき）にする。時差が書いてあればそれで、無ければこの PC の時間帯で読む
    （前と同じ）。読めなければ None。"""
    m = _until_match(text)
    if not m:
        return None
    d, h, mi, sec, off = m
    try:
        t = datetime.strptime(f"{d} {h:02d}:{mi:02d}:{sec or 0:02d}", "%Y-%m-%d %H:%M:%S")
        if off is not None:
            return t.replace(tzinfo=timezone(timedelta(minutes=off)))
        return t.astimezone()  # 時差の無い時刻は、この PC の時間帯の時刻として読む
    except (ValueError, OSError, OverflowError):
        return None


def with_offset(t) -> str:
    """時刻を「YYYY-MM-DD HH:MM +09:00」の形にする（要約した範囲に、そのまま写せるように）。"""
    z = t.strftime("%z")
    return f"{t:%Y-%m-%d %H:%M}" + (f" {z[:3]}:{z[3:5]}" if z else "")


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


def same_text(path: Path, text: str, bom_ok=True) -> bool:
    """いまのファイルの中身が text と同じか（改行の種類は問わない）。BOM の有無は、bom_ok のときだけ問わない
    （すべてをスクリプトが作る一覧では、BOM が付いたら書き直す。人の書いた要約は、BOM だけでは書き直さない）。"""
    try:
        return path.read_text(encoding="utf-8-sig" if bom_ok else "utf-8") == text
    except (OSError, UnicodeDecodeError):
        return False


def encodable(text: str) -> str:
    """UTF-8 で書けない字（対になっていないサロゲート。JSON の「\\ud83d」だけの字など）を「?」に置き換える
    （一つの字のせいで、書けずに全体が止まらないように）。"""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


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


def write_text(path: Path, text: str, bom_ok=True):
    """改行は LF にそろえ、その都度ちがう名前の一時ファイルに書いてから入れ替える。
    途中で止まっても壊れたファイルを残さず、同時に動いた別の会話のフックともぶつからない。
    中身が変わらなければ書かない（core.autocrlf=true で、中身の同じファイルが変更として出ないように）。
    bom_ok が偽なら（すべてをスクリプトが作るファイル）、BOM が付いていれば、中身が同じでも BOM を外して書き直す。"""
    text = encodable(text)
    path = write_target(path)
    if same_text(path, text, bom_ok):
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
            _lock_held[0] = True
            atexit.register(release_lock)
            return True
        except FileExistsError:
            try:
                if time.time() - LOCK.stat().st_mtime > STALE_SEC:
                    owner = lock_owner()
                    # 外す直前に、もう一度古さと中身を確かめる（同時に待っていた別の実行が、古いロックを外して
                    # 新しく取ったロックを、こちらが外さないように）
                    if time.time() - LOCK.stat().st_mtime > STALE_SEC and lock_owner() == owner:
                        LOCK.unlink(missing_ok=True)
                        continue
            except OSError:
                pass
            if time.time() > deadline:
                return False
            time.sleep(0.2)


_lock_held = [False]


def lock_owner():
    """ロックに書かれた、取った実行のプロセスの番号（字のまま）。読めなければ None。"""
    try:
        return LOCK.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def release_lock():
    """ロックを外す。中身がこの実行のプロセスの番号のときだけ（古いとして外された後に、別の実行が取ったロックを消さないため）。"""
    try:
        if lock_owner() == str(os.getpid()):
            LOCK.unlink(missing_ok=True)
    except OSError:
        pass
    _lock_held[0] = False


_last_touch = [0.0]


def keep_lock():
    """長く動くとき、ロックが古いものと見なされないように、ときどき時刻を新しくする。ロックを取っていなければ何もしない。"""
    if not _lock_held[0]:
        return
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
    if not isinstance(c, dict):
        return {}
    if c.get("_v") != CACHE_VERSION:
        # 版が変わっても、控えた会話のファイルの作られた時刻（take）は残す（Claude Code のファイルが消えた後は、
        # もう分からないため）。その会話は読み直す
        return {k: {"key": [], "birth": v["birth"]} for k, v in c.items()
                if isinstance(v, dict) and isinstance(v.get("birth"), (int, float)) and not isinstance(v["birth"], bool)}
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
    この PC に記録があり、対象の外になった会話の行は、印を付けない決め直した値にする（印の要るかを決めるのは対象の会話だけ。
    印を残すと、--pending に出ない会話に印が残り続けるため）。
    前の一覧に同じファイルの行が二つ以上あれば（git の union のマージ）、欄が決め直した値に合う行を使う。
    要約のファイルの無い行（自分の発言なし、先頭 8 文字が同じ別の会話の要約がある）も、この PC でいまの会話でなければ残す。
    除いた会話かは、要約の囲みに「会話の ID」があれば、その ID の全体で確かめる（「! <ID の全体>」で、先頭 8 文字が同じ
    別の会話の行まで除かないため）。ID が無ければ、先頭 8 文字で確かめる。"""
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
    file_ids = {}  # ID の先頭 8 文字 -> 要約のファイルの囲みにある会話の ID
    for p in sorted(OUT.glob("*.md")):
        m = SUMMARY_FILE_RX.match(p.name)
        if not m:
            continue
        day, id8 = m.groups()
        on_disk8.add(id8)
        if p.name in used:
            continue  # （いまの会話の先頭 8 文字なので、下の要約のファイルの無い行でも、file_ids は使わない）
        text = read_or_none(p)
        ids = summary_ids(text) if text is not None else []
        file_ids.setdefault(id8, set()).update(ids)
        if ids:
            if any(i.startswith(x) for i in ids for x in excludes):
                continue
        elif any(id8.startswith(x[:8]) for x in excludes):
            continue
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
                if seen:
                    cell = state  # この PC に記録のある、対象の外の会話（印は付けない）
            else:
                (when, head, _), cell = cands[-1], state
            out.append((when, f"{head}| {cell} |"))
            continue
        head = text.split("\n", 1)[0].strip()
        title = head[len("# 会話の要約："):] if head.startswith("# 会話の要約：") else ""
        title = table_cell(one_line(redact(title.strip() or p.stem), 200))
        out.append((day, f"| {day} | {title}{note} | `{id8}` | [{p.name}]({p.name}) | {state} |"))
    for (id8, kind), (when, line) in linkless.items():
        # 行には ID の先頭 8 文字しか無い。除く値が、同じ先頭 8 文字の要約のファイルの会話（ID の全体）を指していれば、
        # その値はこの行の会話（要約のファイルの無い会話）を指していない
        if id8 in cur8 or any(id8.startswith(x[:8]) and not (len(x) > 8 and any(i.startswith(x) for i in file_ids.get(id8, ())))
                              for x in excludes):
            continue  # この PC でいまの会話なら、その会話の行を作り直す
        if kind == NO_OWN and id8 in on_disk8:
            continue  # その後、要約ができた
        out.append((when, line))
    return out


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 書けない字（対になっていないサロゲート）は置き換える
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
    # 一覧だけのとき（--list）は何も書かない：フォルダを作らず、ロックを取らず、一時ファイルを消さず、控えも写しも書かない
    # （控えはあれば読む）
    if not a.list:
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
        # 会話のファイルの作られた時刻（続きの、写した部分を決める。copied_block）。Claude Code のフォルダで初めて読んだ
        # ときのものを控えに残し、その後は変えない（ハブの写しの作られた時刻は、写した時刻なので使わない）
        b = c.get("birth") if c else None
        known = isinstance(b, (int, float)) and not isinstance(b, bool)
        birth = b if known else (birth_time(st) if f.resolve().parent != raw_dir else None)

        def with_birth(e):
            if birth is not None:
                e["birth"] = birth
            return e
        # 対象かどうかは、記録した作業ディレクトリ（cwd）を、いまの hub/session-sources.txt と毎回照らして決め直す。
        # 作業ディレクトリを足したり外したりしても、会話のファイルを読み直さずに反映されるようにするため。
        # 作られた時刻が初めて分かった対象の会話は、写した部分を決め直すために読み直す
        if c and c.get("key") == key and not ("raw" in c and birth is not None and not known):
            if "raw" in c:
                if in_scope(c.get("cwd")):
                    return {"id": sid, "path": f, "parsed": None, **c}
                cache[sid] = with_birth({"key": key, "skip": True, "cwd": c.get("cwd"), "talk": True})
                return None
            if c.get("skip") and "cwd" in c and not (c.get("talk") and in_scope(c["cwd"])):
                with_birth(c)
                return None
        saving["n"] += 1
        p = parse(f, birth)
        # ほかの会話が送った起動の文（sent）だけの会話も対象にする（切り出した作業の会話を残すため）
        talk = any(i["kind"] in ("user", "answer", "sent") for i in p["items"])
        if not talk or not in_scope(p["cwd"]):
            cache[sid] = with_birth({"key": key, "skip": True, "cwd": p["cwd"], "talk": talk})
            return None
        # 持ち主を決めるのに要るのは、ファイルの形だけ：uuid のある行の並び（raw）、写した部分の行の数（blk）、
        # 各項目の行の raw での位置（ri。Claude の返答は、まとめた行ごと）と、まとめた行の時刻（rt）
        entry = {"key": key, "title": p["title"], "cwd": p["cwd"], "raw": p["raw"], "blk": p["blk"], "fl": p["fl"],
                 "uuids": [i["uuid"] for i in p["items"]], "ri": [i["ri"] for i in p["items"]],
                 "rt": {str(k): [x["ts"] for x in i["parts"]] for k, i in enumerate(p["items"]) if len(i.get("parts") or ()) > 1},
                 "kinds": [i["kind"] for i in p["items"]], "ts": [i["ts"] for i in p["items"]],
                 "te": [i.get("te") or i["ts"] for i in p["items"]], "changed": True}
        cache[sid] = with_birth(entry)
        return {"id": sid, "path": f, "parsed": p, **entry}

    sessions = []
    seen8 = set()  # この PC にある会話の記録（対象の外のものも含む）の、ID の先頭 8 文字
    seen_ids = set()  # 同じく、会話の ID
    failed = []  # 読めなかった・写せなかった会話の記録（ほかの会話は続ける）
    cand = candidate_files(dirs, extra)
    for f in cand:
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
        stems = [f.stem for f in cand]
        lone = [x for x in dict.fromkeys(excludes) if not any(s.startswith(x) for s in stems)]
        if lone:
            print("hub/session-sources.txt の次の「!」の行は、どの会話にも当たらなかった（ID が違うか、この PC に会話の記録が無い）：")
            for x in lone:
                print(f"  ! {x}")

    # 要約のファイルの名前は、作るときに決めて変えない。あれば（*_<ID の先頭 8 文字>.md）その名前を使い続ける。
    # 開始日は時間帯や分岐元が分かったことで変わりうるが、名前を変えるとリンクが切れるため
    existing = {}
    for p in OUT.glob("*_*.md"):
        n = p.name
        if len(n) > 12 and n[-12] == "_":
            existing.setdefault(n[-11:-3], []).append(n)

    def first_ts(s):
        return min((x for x in s["ts"] if x), default="")
    sessions.sort(key=lambda s: (first_ts(s), s["id"]))  # 時刻が同じなら会話の ID の順（読み元によって入れ替わらないように）
    runs_of, chain_of = ownership(sessions)
    claimed = set()  # この実行で、どれかの会話が使うと決めた要約のファイル
    claimed8 = {}  # ID の先頭 8 文字 -> この実行で、その先頭 8 文字の会話に最初に決めた要約のファイル

    def pick_file(s, first):
        """会話の要約のファイルの名前と、使えなかったときの別の会話の要約の名前 (名前, ぶつかった名前)。
        あるファイル（名前の順）のうち、囲みの「会話の ID」がこの会話のもの、または囲みの無いものを使う。
        先頭 8 文字が同じ別の会話の要約は使わない（その要約の囲みを、この会話のもので書き換えないため）。
        新しく作るのは、この実行で、先頭 8 文字が同じ会話にまだファイルを決めていないときだけ（開始日が違っても。
        別の実行で初めて見たときと同じに、二つ目の会話の要約は作らず、ぶつかったとして知らせる）。"""
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
        if s["id"][:8] in claimed8:
            return None, claimed8[s["id"][:8]]
        n = f"{first:%Y-%m-%d}_{s['id'][:8]}.md"
        return (None, n) if n in claimed else (n, None)

    rows = {}
    for s in sessions:
        own = [run_info(s, *x) for x in runs_of[s["id"]]]  # (uuid, 時刻, 終わりの時刻, 種類)
        ts_own = sorted(x[1] for x in own if x[1])
        te_own = sorted(x[2] for x in own if x[2])
        talk_own = sorted(x[1] for x in own if x[1] and x[3] in ("user", "answer"))
        # ほかの会話・アプリが送った発言（切り出した作業の起動の文など）。要約のファイルを作るかには数え、
        # 要約した範囲の後に発言が増えたかには数えない（研究者の発言ではないため）
        sent_own = any(x[1] for x in own if x[3] == "sent")
        # 読めない時刻（1970 年より前など）は飛ばし、読める時刻の最初と最後を使う
        first = readable(ts_own)
        last = readable(reversed(te_own))
        s["own"] = own
        # 新しく要約のファイルを作るのは、自分の発言（研究者の発言か選択肢の回答、ほかの会話からの起動の文）があるときだけ
        # （引数の無いコマンドと Claude の返答だけの分岐は、記録の対象の判定と同じに、自分の発言なしとする）。
        # もうあるファイルは使い続ける
        file, clash = pick_file(s, first if (talk_own or sent_own) else None)
        if file:
            claimed.add(file)
            claimed8.setdefault(s["id"][:8], file)
        rows[s["id"]] = {"id": s["id"], "title": s.get("title", ""), "first": first, "last": last,
                         "last_talk": readable(reversed(talk_own)), "talk": bool(talk_own) or sent_own,
                         "cwd": s.get("cwd"), "n_own": len(own), "file": file, "clash": clash}

    if a.list:
        for s in sessions:
            r = rows[s["id"]]
            when = f"{r['first']:%Y-%m-%d %H:%M}" if r["first"] else "（自分の発言なし）"
            print(f"{when}  {s['id'][:8]}  own={r['n_own']:5d}  {r['title'][:50]}")
        return

    # 分岐元は、写した部分の最後の行の持ち主（その会話を作った元の会話）。要約のファイルの無い会話（この PC に無い、
    # 除いた、対象の外、自分の発言の無い会話）なら、写した部分のより前の行の持ち主をたどる
    parent_of = {s["id"]: next((o for o in chain_of[s["id"]] if rows[o]["file"]), None) for s in sessions}
    start_of = {s["id"]: first_ts(s) for s in sessions}
    cut = cut_cycles(parent_of, start_of)  # この決め方では輪にならない。なったときの安全のため（外した分岐元を知らせる）

    commits_of = hub_commit_map()
    git_ok = commits_of is not None
    commits_of = commits_of or {}
    skipped = []  # 読めなかった・書けなかった要約のファイル
    for s in sessions:
        keep_lock()
        maybe_save_cache()  # 前の会話の読める形と要約を書いたところで
        r = rows[s["id"]]
        po = parent_of.get(s["id"])
        parent = {"title": rows[po]["title"], "file": rows[po]["file"]} if po else None
        r["parent"] = parent
        if not r["file"]:
            cache[s["id"]].pop("changed", None)
            if r.get("clash"):
                skipped.append((r["clash"], f"{CLASH}。会話 {s['id']} の要約は作らない"))
            continue
        dg = DIGEST / f"{s['id']}.md"
        # 読める形は、会話のファイルが変わったときだけでなく、持ち主の決まり方（分岐元）か、時刻を書く時間帯が
        # 変わったときにも作り直す
        zone = zone_text(x[1] for x in s["own"])
        own_sig = hashlib.sha1(("\n".join(f"{k} {a_} {b_}" for k, a_, b_ in runs_of[s["id"]]) + "|" +
                                (parent["file"] if parent else "") + "|" + zone).encode("utf-8", "replace")).hexdigest()
        if s.get("changed") or not dg.exists() or cache[s["id"]].get("own_sig") != own_sig:
            saving["n"] += 1
            try:
                p = s["parsed"] or parse(s["path"])
                items = [part_item(p["items"][k], a_, b_) for k, a_, b_ in runs_of[s["id"]]]
                write_text(dg, render_digest(p, items, parent["title"] if parent else None, zone))
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
        # 自分の発言（研究者の発言か選択肢の回答）が無い会話は要らない（中身はすべて分岐元にある、引数の無いコマンドだけ）
        # 比べるのは分の単位まで（要約した範囲は分までなので）。時間帯が書いてあれば、その時刻と比べる（時間帯が変わっても
        # 取り違えないため）。無ければ、この PC の時間帯の時刻として比べる（前と同じ）
        lt = r["last_talk"]
        ui = until_instant(new) if until is not None else None
        if until is None:
            r["pending"] = r["talk"]
        elif lt is None:
            r["pending"] = False
        elif ui is not None:
            # 秒まで書いてあれば秒まで比べる
            sec = _until_match(new)[3] is not None
            r["pending"] = r["talk"] and lt.replace(microsecond=0, **({} if sec else {"second": 0})) > ui
        else:
            r["pending"] = r["talk"] and f"{lt:%Y-%m-%d %H:%M}" > until
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
        write_text(OUT / "README.md", "\n".join(idx) + "\n", bom_ok=False)  # すべてをスクリプトが作る一覧
    except OSError:
        skipped.append(("README.md", "書けなかった（次の実行でやり直す）"))
    save_cache(final=True)  # 書けなくても、要約が要る会話は出す
    pend =[r for r in rows_l if r.get("pending")]
    if a.pending:
        for r in pend:
            lt = r["last_talk"] or r["last"]
            print(f"{r['id'][:8]}  要約：records/sessions/{r['file']}  読める形：records/sessions/raw/digest/{r['id']}.md  "
                  f"要約した範囲：{r.get('until') or 'なし'}  最後の発言：{with_offset(lt) if lt else 'なし'}")
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
    if cut and not a.hook:
        print("分岐元が輪になったので、次の会話の分岐元を外した（会話の記録の形が崩れている）：")
        for x in cut:
            print(f"  {x}")
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
