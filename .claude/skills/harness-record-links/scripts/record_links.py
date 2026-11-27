"""研究ハブの記録どうしのリンクを集め、逆向きのリンクと一覧を作る。

書くのは、各記録の「関連」の欄（その記録から先へのリンク）だけ（その記録を書いた会話の Claude が書く）。このスクリプトがそこから作る：

- 各記録の末尾の「この記録を指しているもの（自動）」の囲み（<!-- links:start --> 〜 <!-- links:end -->）。
  囲みとみなすのは、その見出しの行のすぐ後に、印の行で始まり印の行で終わり、中がこのスクリプトの書く形の行だけのもの。
  本文やコードの中で印に触れても、囲みとはみなさない。
  見出しと印の組が崩れている（印の片方が欠けた、見出しを書き換えた、囲みの中に書き足した、囲みが二つある）なら、
  書き込まずに知らせる。どの場合も、スクリプトの書いた行は、その記録のリンクとして数えない
- records/links.md：記録ごとに「指しているもの」「指されているもの」。切れたリンクも出す
- chats/README.md：文字起こしの一覧と、それぞれを使った相談・会話・決定など

リンクの見つけ方（パスの書き方の揺れに強くするため、既知の記録の名前で探す）：

- 文字起こし：ファイル名（例：レビュー（第2章の草稿）20260115.md）
- 相談の記録：フォルダ名（例：2026-01-15_第2章の草稿）
- 実験の記録：実験の id（例：exp-pilot）
- 会話の要約：ファイル名か、会話の ID の先頭 8 文字（例：1a2b3c4d）
- 決定：hub/decisions.md（パスで書く。名前だけの「decisions.md」は、ひな形などと取り違えるので数えない）
- 研究の全体像、論文ごとの論旨、導入前の経緯などの記録、指示書・報告：パス
- 名前の前後は区切られている必要がある（「ゼミ」で「ゼミ_続き」や「第2回ゼミ」を拾わないため）

切れたリンク：

- Markdown のリンク（[…](先) と [ ](<先>)）の先を、その記録のフォルダから（無ければハブから）たどり、無ければ出す
- 印（#d-20260115-143000 の形）が、先の記録に <a id="…"></a> として無ければ出す。見出しへの印は確かめない
- git の外のもの（records/sessions/raw/、export/）、URL、「*」「{{」を含むものは確かめない
- 会話の要約の冒頭の囲み（harness-session-archive が書く。コミットの件名や分岐元の題）と、題の行（「# 会話の要約：」。
  40 字で切った題は、パスの途中で終わることがある）の中は、先のファイルや印が無くても出さない（だれも直せない所のため）。
  そこにあるリンクも、先があればリンクとして数える
- リンクの先は、最初の「#」で印と分けてから、それぞれの %xx を戻す（名前の「#」は %23 と書く）。
  このスクリプトが書くリンクでは、名前の「%」と「#」を %25、%23 にする
- ひな形の書きかけ（1 行の {{…}}）の中は、記録の名前もリンクも探さない

書けなかったファイル（読み取り専用、パスが長すぎるなど）は飛ばしてほかを続け、手で動かしたときと records/links.md に出す。
中身が変わらないファイルは書き直さない。

使い方:
  python -X utf8 record_links.py          （作り直す）
  python -X utf8 record_links.py --hook   （フックから。失敗しても 0 で終わる）
  python -X utf8 record_links.py --check  （書き込まず、切れたリンクの数だけを出す）
"""
import sys

# 古い Python では、この先の書き方を読めずに分かりにくい失敗をするので、先に知らせて終わる（フックでは何も言わずに終わる）
if sys.version_info < (3, 10):
    if "--hook" in sys.argv:
        sys.exit(0)
    sys.exit("record_links: Python 3.10 以降が要る（いまは %d.%d）" % sys.version_info[:2])

import atexit
import os
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote

HUB = Path(__file__).resolve().parents[4]
# 会話の記録のスクリプトと共有するロック。git の中（.git/）に置く（git に入らず、目にも触れない）
LOCK = (HUB / ".git" / "hub-scripts.lock") if (HUB / ".git").is_dir() else (HUB / "records" / ".hub-scripts.lock")
# これより古いロックは、止められた実行が残したものとして外す。会話の終わりのフックは 60 秒ほどで止められ、
# 手で動かした長い実行は（どちらのスクリプトも）20 秒ごとにロックの時刻を新しくする（keep_lock）ので、これより古いものは確かに止まっている
STALE_SEC = 90
# 会話の要約の「決まったこと」などの印（同じ秒が重なったときの -2 なども）
ANCHOR_ID_RX = re.compile(r"^[a-z]{1,3}-\d{8}-\d{6}(?:-\d+)?$")
# リンクの先。<…> で囲まないときは、一段までの対になった括弧を含めてよい（../2026-01-18_D(1)/README.md）
LINK_RX = re.compile(r"\]\((<[^>\n]+>|(?:[^()\s]|\([^()\s]*\))+)\)")
TAIL_RX = re.compile(r"(?:/README\.md)?(?:#([A-Za-z0-9_-]+))?")  # 印は英数字だけ（後ろに続く日本語を印に含めない）
BARE_RX = re.compile(r"(?<![\w/.-])records/(?:sessions|meetings|experiments|history)/[^\s`)>|、。<>\[\]*…{}]+")
FENCE_RX = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.S | re.M)
INLINE_CODE_RX = re.compile(r"`[^`\n]*`")
unreadable = []  # UTF-8 で読めなかった、または開けなかったファイル
START = "<!-- links:start（harness-record-links が作る。人も Claude も直接書き換えない） -->"
START_RX = re.compile(r"<!-- links:start[^>]*-->")
END = "<!-- links:end -->"
HEAD = "## この記録を指しているもの（自動）"
GENERATED = {"records/links.md", "chats/README.md", "records/sessions/README.md"}
NO_MEETING_RX = re.compile(r"^- 相談の記録：\s*なし", re.M)
KIND_ORDER = ["相談", "実験", "会話", "決定", "論旨", "文字起こし", "経緯", "指示書・報告", "記録"]
# git のマージがぶつかったときに入る印の行
CONFLICT_RX = re.compile(r"^(?:<<<<<<< |>>>>>>> |\|\|\|\|\|\|\| ).*$|^=======[ \t]*$")
# ひな形の書きかけ（1 行の {{…}}）。中に「{」「}」を含めない（閉じていない「{{」が長い行に並んでも、行の終わりまで読み直さないため）
PLACEHOLDER_RX = re.compile(r"\{\{[^{}\n]*\}\}")
# このスクリプトと会話の記録のスクリプトが作る一時ファイルの名前（make_temp）
TEMP_RX = re.compile(r"^\.t(?:[0-9a-f]{2}|[0-9a-f]{8})\.tmp$")
TEMP_STALE_SEC = 600  # これより古い一時ファイルは、止められた実行が残したもの
# 会話の要約の冒頭の囲み（harness-session-archive が書く）の印
AUTO_START_RX = re.compile(r"^<!-- auto:start[^>\n]*-->[ \t]*$")
AUTO_END_RX = re.compile(r"^<!-- auto:end -->[ \t]*$")
unwritable = []  # 書けなかったファイル（相対パス, 理由）


def rel_to(p: Path, base: Path) -> str:
    """p.relative_to(base).as_posix() と同じ（base の外なら ValueError）。p の字が base の字で始まるときは、字の上で切る
    （記録が多いと relative_to が遅いため）。"""
    s, b = str(p), str(base)
    if s.startswith(b) and s[len(b):len(b) + 1] in ("\\", "/") and ".." not in s[len(b):].replace("\\", "/").split("/"):
        return s[len(b) + 1:].replace("\\", "/") if os.name == "nt" else s[len(b) + 1:]
    return p.relative_to(base).as_posix()


def rel(p: Path) -> str:
    return rel_to(p, HUB)


def mask_code(text: str) -> str:
    """コードの中（``` の囲みと `…`）を、同じ長さの空白に置き換える（位置を保ったまま、コードの中を探さないため）。"""
    t = FENCE_RX.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    return INLINE_CODE_RX.sub(lambda m: " " * len(m.group(0)), t)


# このスクリプトが囲みの中に書く行の形（「- 相談：…」など）
GENERATED_LINE_RX = re.compile(r"^- (?:%s)：|^- （まだ無い）[ \t]*$" % "|".join(map(re.escape, KIND_ORDER)))


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


_analyzed = {}  # 本文 -> analyze の結果（一回の実行の中で、同じ本文を何度も調べないため）


def analyze(text: str):
    r = _analyzed.get(text)
    if r is None:
        r = _analyzed[text] = _analyze(text)
    return r


def _analyze(text: str):
    """自動の囲みを、行ごとに見て探す。戻り値：(囲みの行の組 [(始まりの行, 終わりの行)], 崩れているか, 読み飛ばす行, 行)。
    印と見出しとみなすのは、コード（``` の囲みと `…`）の外で、その行がそれだけのものだけ。文の中で触れた印は印ではない。
    始まりの印の行ごとに、その後の最初の終わりの印の行まで（無ければ、次の始まりの印の行か終わりまで）を見る。
    - 終わりがあり、中が空行とスクリプトの書く形の行だけで、前の行（空行を除く）が見出し → 囲み
    - 終わりがあり、中がスクリプトの書く形の行だけで、見出しが前に無い（見出しを書き換えた）→ 崩れている
    - 終わりがあり、スクリプトの書く形の行と、ほかの行が混ざる（囲みの中に書き足した）→ 崩れている
    - 終わりがあり、中にスクリプトの書く形の行が無い（本文に書いた例）、または空で見出しも無い → 本文
    - 終わりが無く、始まりの後の最初の行（空行を除く）がスクリプトの書く形 → 崩れている。そうでなければ本文
    見出しの次の行（空行を除く）が始まりの印でなく、スクリプトの書く形の行か終わりの印なら、始まりの印が消えた → 崩れている。
    始まりと終わりの間にある git のぶつかりの印の行（<<<<<<< など）は、組としてそろっていれば、スクリプトの書く形の行とみなす
    （マージでぶつかった囲みを作り直せるように）。そろっていなければ人の書いた行とみなす。囲みの外の印の行は、ふつうの本文。
    読み飛ばす行は、囲みと崩れた囲みの、見出し・印・スクリプトの書いた行（人の書いた行は読む）。"""
    lines = text.split("\n")
    masked = mask_code(text).split("\n")
    kinds = []
    for o, m in zip(lines, masked):
        s = m.strip()
        if START_RX.fullmatch(s):
            kinds.append("start")
        elif s == END:
            kinds.append("end")
        elif s == HEAD:
            kinds.append("head")
        elif CONFLICT_RX.match(m):
            kinds.append("conflict")  # 囲みの中でだけ、スクリプトの行とみなす（下）
        elif GENERATED_LINE_RX.match(m):
            kinds.append("gen")
        elif not o.strip():
            kinds.append("blank")
        else:
            kinds.append("other")  # コードの中の行も、人の書いた行
    n = len(kinds)
    blocks, broken, skip = [], False, set()

    def prev_nonblank(i):
        i -= 1
        while i >= 0 and kinds[i] == "blank":
            i -= 1
        return i

    def next_nonblank(i):
        i += 1
        while i < n and kinds[i] == "blank":
            i += 1
        return i

    for s in range(n):
        if kinds[s] != "start":
            continue
        e = s + 1
        while e < n and kinds[e] not in ("start", "end"):
            e += 1
        inner = range(s + 1, e)
        gens = [i for i in inner if kinds[i] == "gen"]
        others = [i for i in inner if kinds[i] in ("other", "head")]
        conf = [i for i in inner if kinds[i] == "conflict"]
        if conf and not (e < n and kinds[e] == "end" and conflict_ok([lines[i] for i in conf])):
            others += conf  # 組になっていない印の行は、人の書いた行とみなす
            conf = []
        p = prev_nonblank(s)
        headed = p >= 0 and kinds[p] == "head"
        if e < n and kinds[e] == "end":
            if not others and headed:
                blocks.append((s, e))
            elif not gens:
                continue  # 本文に書いた例、または見出しの無い空の組
            else:
                broken = True  # 見出しを書き換えた、または囲みの中に書き足した
            skip.update([s, e, *gens, *conf])
            if headed:
                skip.add(p)
        else:
            j = next_nonblank(s)
            while j < e and kinds[j] == "conflict":
                j = next_nonblank(j)
            if j < e and kinds[j] == "gen":
                broken = True  # 終わりの印が消えた
                skip.add(s)
                if headed:
                    skip.add(p)
                while j < e and kinds[j] in ("blank", "gen"):
                    if kinds[j] == "gen":
                        skip.add(j)
                    j += 1
    for h in range(n):
        if kinds[h] != "head":
            continue
        j = next_nonblank(h)
        if j < n and kinds[j] in ("gen", "end"):
            broken = True  # 始まりの印が消えた
            skip.add(h)
            while j < n and kinds[j] in ("blank", "gen"):
                if kinds[j] == "gen":
                    skip.add(j)
                j += 1
            if j < n and kinds[j] == "end":
                skip.add(j)
    return blocks, broken, skip, lines


def find_block(text: str):
    """自動の囲みを探す。戻り値：(囲みの始まり, 囲みの終わり) の位置（元の text の中）、None（まだ無い）、"broken"（崩れている）。
    崩れている、または囲みが二つ以上あるときは "broken"（どれを置き換えてよいか分からないので、書き込まずに知らせる）。"""
    blocks, broken, _, lines = analyze(text)
    if broken or len(blocks) > 1:
        return "broken"
    if not blocks:
        return None
    s, e = blocks[0]
    start = sum(len(x) + 1 for x in lines[:s])
    end = sum(len(x) + 1 for x in lines[:e]) + len(lines[e])
    return start, end


def strip_generated(text: str) -> str:
    """このスクリプトが書いた逆向きのリンク（囲みと崩れた囲みの、見出し・印・スクリプトの書いた行）を読み飛ばす。
    囲みの外の、同じ形の行（「関連」の欄の「- 相談：…」）は本文として読む。"""
    _, _, skip, lines = analyze(text)
    if not skip:
        return text
    return "\n".join("" if i in skip else x for i, x in enumerate(lines))


def collect_nodes():
    nodes = {}  # 相対パス -> {kind, keys(探す文字列), label, inject(逆向きを書き込むか)}

    def add(p: Path, kind, keys, label, inject):
        nodes[rel(p)] = {"kind": kind, "keys": keys, "label": label, "inject": inject, "path": p}

    for p in sorted((HUB / "chats").glob("*.md")):
        if p.name != "README.md":
            add(p, "文字起こし", [p.name], p.stem, False)
    for p in sorted((HUB / "records" / "meetings").glob("*/README.md")):
        add(p, "相談", [p.parent.name], p.parent.name, True)
    for p in sorted((HUB / "records" / "experiments").glob("*/README.md")):
        add(p, "実験", [p.parent.name], p.parent.name, True)
    for p in sorted((HUB / "records" / "sessions").glob("2*_*.md")):
        sid8 = p.stem.split("_")[-1]
        add(p, "会話", [p.name, sid8], p.stem, True)
    for p in sorted((HUB / "records" / "history").glob("*.md")):
        add(p, "経緯", [p.name], p.stem, True)
    dec = HUB / "hub" / "decisions.md"
    if dec.exists():
        add(dec, "決定", ["hub/decisions.md"], "hub/decisions.md", True)
    for sub in ("briefs", "reports"):
        for p in sorted((HUB / "hub" / sub).glob("*.md")):
            add(p, "指示書・報告", [f"hub/{sub}/{p.name}"], f"{sub}/{p.stem}", False)
    research = HUB / "hub" / "research.md"
    if research.exists():
        add(research, "論旨", ["hub/research.md"], "hub/research.md", False)
    papers = HUB / "papers"
    for p in (sorted(papers.rglob("*.md")) if papers.exists() else []):
        # 章ごとのフォルダ（papers/<id>/chapters/…）も拾う。確認用の資料の出力（export/。git の外）は除く
        if "export" in p.relative_to(papers).parts[1:-1]:
            continue
        r = rel(p)
        add(p, "論旨", [r], r, False)
    return nodes


def read(p: Path):
    """UTF-8 で読む。読めない字があれば置き換えて読む。開けなければ None。
    どちらのときも読めなかったファイルとして知らせ、そのファイルには書き込まない（中身を消さないため）。"""
    try:
        return p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        unreadable.append(rel(p))
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    except OSError:
        unreadable.append(rel(p))
        return None


_texts = {}  # パス -> read の結果（リンクを集める間、同じファイルを何度も読まないため）


def read_once(p: Path):
    """read と同じ。一回の実行の中で、同じファイルは一度だけ読む（書き込む前には read で読み直す）。"""
    k = str(p)
    if k not in _texts:
        _texts[k] = read(p)
    return _texts[k]


_resolved = {}  # たたんだパスの字 -> たどった先（一回の実行の中で覚えておく）
_exists = {}  # たどった先の字 -> あるか


def resolve_key(s: str) -> str:
    """覚えておくときの鍵。Windows では、たどる前に「..」を字の上でたたむ（os.path.realpath と同じ）ので、たたんだ形にする
    （別の記録から同じ先を指すリンクをまとめる）。ほかの OS では、シンボリックリンクの後ろの「..」の意味が変わるので、そのまま。"""
    return os.path.normpath(s) if os.name == "nt" else s


def resolved(p) -> Path:
    """Path(p).resolve() と同じ先（シンボリックリンクやジャンクションをたどる）。同じパスは一度だけたどる。
    たどれなければ OSError か ValueError（呼ぶ側で、そのリンクを飛ばす）。"""
    k = resolve_key(str(p))
    r = _resolved.get(k)
    if r is None:
        r = Path(os.path.realpath(k))
        _resolved[k] = r
        _resolved.setdefault(resolve_key(str(r)), r)  # たどった先をもう一度たどっても同じ
    return r


def exists(p: Path) -> bool:
    k = str(p)
    e = _exists.get(k)
    if e is None:
        e = _exists[k] = os.path.exists(k)
    return e


def ascii_word(c: str) -> bool:
    return c.isascii() and (c.isalnum() or c == "_")


def find_keys(text, keys):
    """記録の名前が書かれた所を拾う。戻り値：{記録: 印の集まり}。
    - 名前は長いものから当て、長い名前に当たった所は、その中の短い名前では数えない（「ゼミ」と「ゼミ_続き」）
    - 前後の区切りは英数字で判定する。日本語の文の中に書いた名前（「会話1a2b3c4dで」「ゼミの相談で」）も拾う
    - str.find で探すので、記録が増えても遅くなりにくい"""
    found = {}
    taken = bytearray(len(text))
    for key, dst, hexid in keys:  # 長い順
        i = text.find(key)
        while i != -1:
            j = i + len(key)
            prev = text[i - 1] if i > 0 else ""
            # 文の頭（prev が空）は区切り。「"" in "-."」は真になるので、空かを先に見る
            before_ok = not (prev.isascii() and prev.isalnum()) if hexid else not (ascii_word(prev) or (prev != "" and prev in "-."))
            if before_ok and not any(taken[i:j]):
                m = TAIL_RX.match(text, j)
                end = m.end()
                nxt = text[end] if end < len(text) else ""
                after_ok = not (nxt.isascii() and nxt.isalnum()) if hexid else not (ascii_word(nxt) or nxt == "-")
                if after_ok:
                    taken[i:end] = b"\x01" * (end - i)
                    found.setdefault(dst, set())
                    if m.group(1):
                        found[dst].add(m.group(1))
            i = text.find(key, i + 1)
    return found


def anchors_of(p: Path):
    return set(re.findall(r'<a id="([\w-]+)"></a>', read_once(p) or ""))


def skip_target(t: str) -> bool:
    """確かめないリンク先：URL、同じファイルの中の印、git の外のもの、ひな形の書きかけ。"""
    return (re.match(r"^[a-zA-Z][\w+.-]*:", t) is not None or t.startswith("#") or "*" in t or "{{" in t
            or "records/sessions/raw/" in t or "/export/" in t or t.startswith("export/"))


def ignored_by_git(paths):
    """git に入れない場所（.gitignore。研究で足したものも含む）にあるパスの集まり。別の PC や取り直したハブでは無いので、切れたと数えない。"""
    rels = []
    hub_r = resolved(HUB)
    for p in paths:
        try:
            rels.append(rel_to(resolved(p), hub_r))
        except (OSError, ValueError):
            pass
    rels = list(dict.fromkeys(rels))  # 同じ先を指すリンクが多くても、一度だけ聞く
    if not rels:
        return set()
    # フォルダだけに当たる形（data/ など）は、無いパスには「/」を付けないと当たらない。両方で聞き、どちらかで当たれば外とする
    query = [y for x in rels for y in (x, x + "/")]
    try:
        # NUL で区切った bytes で渡す（文字列で渡すと、Windows では改行が CRLF に変わり、パスの末尾に \r が付くため）
        r = subprocess.run(["git", "-C", str(HUB), "check-ignore", "--no-index", "--stdin", "-z"],
                           input=b"\0".join(x.encode("utf-8") for x in query) + b"\0", capture_output=True, timeout=30)
        return {x.decode("utf-8", "replace").rstrip("/") for x in r.stdout.split(b"\0") if x}
    except (OSError, subprocess.SubprocessError):
        return set()


def auto_region(lines):
    """会話の要約の冒頭の囲み（harness-session-archive が書く）の行の範囲 (始まりの行, 終わりの行)。無ければ None。
    最初の「## 」の見出しより前の、コード（``` の囲み）の外の始まりの印の行から、その後の最初の終わりの印の行まで。"""
    fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        if line.startswith("## "):
            return None
        if AUTO_START_RX.match(line):
            for j in range(i + 1, len(lines)):
                if AUTO_END_RX.match(lines[j]):
                    return i, j
                if lines[j].startswith("## "):
                    return None
            return None
    return None


def mask_placeholders(text: str) -> str:
    """ひな形の書きかけ（1 行の {{…}}）を、同じ長さの空白に置き換える（ひな形の説明に書いた名前やリンクを拾わないため）。"""
    return PLACEHOLDER_RX.sub(lambda m: " " * len(m.group(0)), text)


def same_text(p: Path, text: str) -> bool:
    """いまのファイルの中身が text と同じか（改行の種類と BOM の有無は問わない）。"""
    try:
        return p.read_text(encoding="utf-8-sig") == text
    except (OSError, UnicodeDecodeError):
        return False


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


def write_target(p: Path) -> Path:
    """一時ファイルから入れ替える先。シンボリックリンクなら、リンクを残して先のファイルを書く（リンクをふつうのファイルに
    置き換えないため）。書き込めないファイル（読み取り専用など）は、どの OS でも PermissionError（入れ替えれば書けてしまう
    OS でも、書かないと決めたものを書き換えないため。Windows では、入れ替えのやり直しで待たないため）。"""
    if os.path.islink(p):
        p = Path(os.path.realpath(p))
    if p.exists() and not os.access(p, os.W_OK):
        raise PermissionError("読み取り専用")
    return p


def keep_mode(tmp, p: Path):
    """入れ替える前に、いまのファイルの許可の設定（実行の可否など）を一時ファイルに写す。できなければそのまま。"""
    try:
        os.chmod(tmp, stat.S_IMODE(os.stat(p).st_mode))
    except OSError:
        pass


def clean_temps(dirs):
    """止められた実行が残した一時ファイル（make_temp の名前で、TEMP_STALE_SEC より古いもの）を消す。ほかのファイルは触らない。
    ロックを取った後に呼ぶ（そのときに一時ファイルを書いている実行は無い）。時刻は、書き換えと作成（Windows）・属性の変更
    （ほかの OS）の新しい方で見る（写した一時ファイルは、元のファイルの時刻を受け継ぐため）。消せなくても続ける。"""
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


def write_file(p: Path, text: str):
    """LF で、一時ファイルに書いてから入れ替える。中身が変わらなければ書かない（core.autocrlf=true で、
    中身の同じファイルが変更として出ないように）。書けなければ OSError（呼ぶ側で、ほかのファイルを続ける）。"""
    p = write_target(p)
    if same_text(p, text):
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = make_temp(p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        keep_mode(tmp, p)
        replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def try_write(p: Path, text: str) -> bool:
    """書く。書けなければ知らせるために残して False（一つのファイルのせいで全体を止めない）。"""
    try:
        write_file(p, text)
        return True
    except OSError as e:
        # 理由にはパスを入れない（records/links.md は git に入るので、この PC のパスを書かないため）
        unwritable.append((rel(p), e.strerror or (str(e.args[0]) if e.args else type(e).__name__)))
        return False


def bare_path_exists(path: str) -> bool:
    """リンクにせずに書いた記録のパスが実在するか。後ろに続く説明（「（続きの回）」「の結果」）は名前に含めない。"""
    cur = HUB
    for part in [x for x in path.split("/") if x]:
        if (cur / part).exists():
            cur = cur / part
            continue
        if not part[0].isascii():
            return True  # 実在するフォルダの後ろに、日本語や全角の括弧の説明が続いている
        try:
            heads = [c.name for c in cur.iterdir() if part.startswith(c.name)]
        except OSError:
            return False
        rest = [part[len(h):] for h in heads]
        return any(r and not (ascii_word(r[0]) or r[0] in "-.") for r in rest)
    return True


def main_hub_of_worktree():
    """ハブのワークツリーの中で動いているなら、元のハブのフォルダを返す（そうでなければ None）。"""
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


def replace(tmp, path):
    for i in range(10):  # Windows では、ほかのプログラムが一瞬開いていると失敗するので、数回やり直す
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 9:
                raise
            time.sleep(0.2)


def acquire_lock(wait=120):
    """同時に動いた別の会話のフックや、会話の記録のスクリプトと、順番に動くためのロック。
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
    """長く動くとき（記録の多いハブを手で作り直すとき）、ロックが古いものと見なされないように、ときどき時刻を新しくする。"""
    now = time.time()
    if now - _last_touch[0] > 20:
        try:
            os.utime(LOCK)
        except OSError:
            pass
        _last_touch[0] = now


_relpaths = {}  # (元のフォルダ, 先) -> 相対パス（一回の実行の中で覚えておく）


def relpath(dst: Path, src_dir: Path) -> str:
    """os.path.relpath(dst, src_dir) を「/」で区切ったもの。どちらもハブの中なら、ハブからの相対の字の上で求める
    （記録が多いと os.path.relpath が遅いため）。名前は、os.path.relpath と同じく os.path.normcase で比べる。"""
    try:
        a = [x for x in rel_to(src_dir, HUB).split("/") if x not in ("", ".")]
        b = [x for x in rel_to(dst, HUB).split("/") if x not in ("", ".")]
    except ValueError:
        a = b = [".."]
    if ".." in a or ".." in b:
        return os.path.relpath(dst, src_dir).replace("\\", "/")
    i = 0
    while i < len(a) and i < len(b) and os.path.normcase(a[i]) == os.path.normcase(b[i]):
        i += 1
    return "/".join([".."] * (len(a) - i) + b[i:]) or "."


def link_to(src_dir: Path, dst: Path, anchor=None) -> str:
    """src_dir から dst へのリンクの先。名前の「%」と「#」は %25、%23 にする（「#」は印の区切り、「%」は %xx と読まれるため）。"""
    k = (str(src_dir), str(dst))
    r = _relpaths.get(k)
    if r is None:
        r = relpath(dst, src_dir).replace("%", "%25").replace("#", "%23")
        _relpaths[k] = r
    if anchor:
        r += f"#{anchor}"
    return f"<{r}>" if (" " in r or "(" in r or ")" in r) else r


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    hook, check = "--hook" in sys.argv, "--check" in sys.argv
    if not (HUB / "hub").exists():
        return
    main_hub = main_hub_of_worktree()
    if main_hub is not None:
        # ハブのワークツリーの中では何もしない（元のハブで作り直す）
        if not hook:
            print(f"ハブのワークツリーの中なので、何もしない。元のハブのスクリプトを動かす：\n"
                  f"  python -X utf8 \"{main_hub / '.claude/skills/harness-record-links/scripts/record_links.py'}\" "
                  + " ".join(x for x in sys.argv[1:] if x != "--hook"))
        return
    # フックからは長く待たない（持ち時間が短いため）。どちらのスクリプトも毎回すべてを作り直すので、次の実行で追いつく
    if not check and not acquire_lock(20 if hook else 120):
        if not hook:
            print(f"ほかの実行が終わらないので、何もしない（{LOCK} が残っていれば、動いている実行が無いことを確かめて消す）")
        return
    nodes = collect_nodes()
    if not check:
        # 止められた実行が残した一時ファイルを消す（書き込むかもしれない記録のフォルダ、records/、chats/）
        clean_temps(dict.fromkeys([str(v["path"].parent) for v in nodes.values() if v["inject"]]
                                  + [str(HUB / "records"), str(HUB / "chats")]))
    keys = sorted(((k, n, bool(re.fullmatch(r"[0-9a-f]{8}", k))) for n, v in nodes.items() for k in v["keys"]),
                  key=lambda x: -len(x[0]))
    by_path = {v["path"].resolve(): n for n, v in nodes.items()}
    out_edges = {n: {} for n in nodes}   # src -> {dst: set(anchors)}
    broken = []
    anchor_cache = {}

    def check_anchor(src, dst, a):
        if not ANCHOR_ID_RX.match(a):
            return  # 見出しへの印などは確かめない
        if dst not in anchor_cache:
            anchor_cache[dst] = anchors_of(nodes[dst]["path"])
        if a not in anchor_cache[dst]:
            broken.append((src, f"{dst}#{a}", "印が無い"))

    missing = []  # 先が無いリンク（あとで、git に入れない場所のものを除く）
    broken_blocks = []  # 末尾の囲みが崩れている記録（書き込まない）
    sources = [n for n, v in nodes.items() if v["kind"] != "文字起こし"]
    for src in sources:
        if not check:  # --check ではロックを取らない
            keep_lock()
        sp = nodes[src]["path"]
        raw_text = read_once(sp)
        if raw_text is None:
            continue  # 開けなかった（読めなかったファイルとして知らせる）
        if nodes[src]["inject"] and find_block(raw_text) == "broken":
            broken_blocks.append(src)
        # ひな形の書きかけ（{{…}}）の中は、名前もリンクも探さない
        text = mask_placeholders(strip_generated(raw_text))
        sp_dir = str(sp.parent)

        def names(t_all, report):
            """記録の名前が書かれた所を拾う。report が偽なら、印が無くても出さない（リンクとしては数える）。"""
            for dst, anchors in find_keys(t_all, keys).items():
                if dst == src:
                    continue
                out_edges[src].setdefault(dst, set()).update(anchors)
                if report:
                    for a in sorted(anchors):
                        check_anchor(src, dst, a)

        def scan(t_all, report):
            """Markdown のリンクの先を、その記録のフォルダから（無ければハブから）たどって確かめる。
            コードの中に書いた例（``` の囲みと `…`）は確かめない。report が偽なら、先のファイルや印が無くても出さない
            （先があれば、リンクとしては数える）。"""
            code_free = INLINE_CODE_RX.sub(" ", FENCE_RX.sub(" ", t_all))
            for m in LINK_RX.finditer(code_free):
                t = m.group(1).strip("<>").strip()
                if skip_target(t):
                    continue
                # 最初の「#」で印と分けてから %xx を戻す（名前の「#」は %23 と書かれる）
                path_part, _, anchor = t.partition("#")
                path_part, anchor = unquote(path_part), unquote(anchor)
                if not path_part:
                    continue
                # その記録のフォルダからたどる。無いときだけ、ハブからもたどる
                try:
                    c1 = resolved(os.path.join(sp_dir, path_part))
                    hit = c1 if exists(c1) else None
                    if hit is None:
                        c2 = resolved(os.path.join(str(HUB), path_part))
                        hit = c2 if exists(c2) else None
                except (OSError, ValueError):
                    continue
                if hit is None:
                    if report:
                        missing.append((src, t, [c1, c2]))
                    continue
                try:
                    dst = by_path.get(hit) or by_path.get(resolved(hit / "README.md"))
                except (OSError, ValueError):
                    continue
                if dst and dst != src:
                    out_edges[src].setdefault(dst, set())
                    if anchor:
                        out_edges[src][dst].add(anchor)
                        if report:
                            check_anchor(src, dst, anchor)
            if not report:
                return
            # リンクにせずに書いた記録のパスで、実在しないもの（``` の囲みの中の例は除く）
            fence_free = FENCE_RX.sub(" ", t_all)
            for m in BARE_RX.finditer(fence_free):
                if m.start() > 0 and fence_free[m.start() - 1] in "(<":
                    continue  # リンクの先は、上で確かめた
                path = m.group(0).split("#")[0]
                k = path.find(".md")
                path = (path[:k + 3] if k != -1 else path).rstrip("/")
                if skip_target(path):
                    continue
                if not bare_path_exists(path):
                    missing.append((src, path, [HUB / path]))

        # 会話の要約の冒頭の囲み（コミットの件名、分岐元の題）と題の行（40 字で切った題は、パスの途中で終わることがある）は、
        # だれも直せないので、先のファイルや印が無くても出さない
        lines_t = text.split("\n")
        quiet = set()
        if nodes[src]["kind"] == "会話":
            region = auto_region(lines_t)
            if region:
                quiet.update(range(region[0], region[1] + 1))
            first = next((i for i, x in enumerate(lines_t) if x.strip()), None)
            if first is not None and lines_t[first].startswith("# 会話の要約："):
                quiet.add(first)
        if quiet:
            loud_t = "\n".join("" if i in quiet else x for i, x in enumerate(lines_t))
            quiet_t = "\n".join(x if i in quiet else "" for i, x in enumerate(lines_t))
            names(loud_t, True)
            names(quiet_t, False)
            scan(loud_t, True)
            scan(quiet_t, False)
        else:
            names(text, True)
            scan(text, True)
    ign = ignored_by_git([c for _, _, cs in missing for c in cs])
    hub_r = resolved(HUB)
    for src, t, cs in missing:
        rels = set()
        for c in cs:
            try:
                rels.add(rel_to(resolved(c), hub_r))
            except (OSError, ValueError):
                pass
        if not (rels & ign):
            broken.append((src, t, "ファイルが無い"))
    broken = sorted(set(broken))

    in_edges = {n: {} for n in nodes}
    for src, dsts in out_edges.items():
        for dst, anchors in dsts.items():
            in_edges[dst][src] = anchors

    # どの相談の記録にもつながっていない文字起こし（冒頭の「相談の記録：なし」は除く）
    unlinked = []
    for n, v in nodes.items():
        if v["kind"] != "文字起こし":
            continue
        if any(nodes[s]["kind"] == "相談" for s in in_edges[n]):
            continue
        if NO_MEETING_RX.search(read_once(v["path"]) or ""):
            continue
        unlinked.append(n)

    if check:
        print(f"切れたリンク：{len(broken)}")
        for b in broken:
            print("  ", *b)
        print(f"相談の記録につながっていない文字起こし：{len(unlinked)}")
        for n in unlinked:
            print("  ", n)
        if unreadable:
            print(f"読めなかったファイル（UTF-8 でない、または開けない）：{len(set(unreadable))}")
            for n in sorted(set(unreadable)):
                print("  ", n)
        if broken_blocks:
            print(f"末尾の囲みが崩れている記録（書き込まない）：{len(broken_blocks)}")
            for n in broken_blocks:
                print("  ", n)
        return

    def grouped(items, base_dir, with_anchor):
        by = {}
        for n, anchors in items:
            by.setdefault(nodes[n]["kind"], []).append((n, anchors))
        lines = []
        for kind in KIND_ORDER:
            if kind not in by:
                continue
            parts = []
            for n, anchors in sorted(by[kind]):
                s = f"[{nodes[n]['label']}]({link_to(base_dir, nodes[n]['path'])})"
                if with_anchor and anchors:
                    s += "（" + "、".join(f"#{a}" for a in sorted(anchors)) + "）"
                parts.append(s)
            lines.append(f"- {kind}：" + "、".join(parts))
        return lines

    # 各記録の末尾に、指しているもの
    for n, v in nodes.items():
        keep_lock()
        if not v["inject"] or rel(v["path"]) in GENERATED or rel(v["path"]) in unreadable or n in broken_blocks:
            continue  # 読めなかったファイルには書き込まない（字を置き換えたまま書き戻さないため）
        p = v["path"]
        text = read(p)
        if text is None or rel(p) in unreadable or not p.exists():
            continue  # 開けない、または途中で名前が変わった・消えたファイルには書き込まない（中身を消したり、写しを作ったりしないため）
        items = [(s, a) for s, a in in_edges[n].items()]
        body = grouped(items, p.parent, True) or ["- （まだ無い）"]
        block = "\n".join([START, *body, END])
        b = find_block(text)
        if b == "broken":
            if n not in broken_blocks:
                broken_blocks.append(n)  # 読んだ後に崩れた
            continue
        if b:
            new = text[:b[0]] + block + text[b[1]:]
        else:
            new = text.rstrip("\n") + f"\n\n{HEAD}\n\n{block}\n"
        if new != text:
            try_write(p, new)  # 書けなければ知らせて、ほかの記録を続ける

    # chats/README.md（records/links.md より先に書く。書けなかったことを records/links.md に出せるように）
    chats_md = HUB / "chats" / "README.md"
    C = ["# 文字起こしと指導教員の記録の一覧", "",
         "`harness-record-links` が作る一覧。人も Claude も直接書き換えない。各文字起こしを使った相談・会話・決定などを並べる（文字起こしの本文には手を加えない）。", "",
         "| 文字起こし | 使っている記録 |", "|---|---|"]
    for n, v in nodes.items():
        if v["kind"] != "文字起こし":
            continue
        users = grouped(list(in_edges[n].items()), chats_md.parent, False)
        cell = "<br>".join(u[2:] for u in users) if users else "（まだ無い）"
        C.append(f"| [{v['label']}]({link_to(chats_md.parent, v['path'])}) | {cell} |")
    if chats_md.parent.exists():
        try_write(chats_md, "\n".join(C) + "\n")

    # records/links.md
    links_md = HUB / "records" / "links.md"
    L = ["# 記録どうしのリンク", "",
         "`harness-record-links` が、各記録の「関連」の欄などから作る一覧。人も Claude も直接書き換えない。横断して調べるときは、まずここを読む。", "",
         "- 指しているもの：その記録が書いているリンク",
         "- 指されているもの：その記録を指している記録（各記録の末尾にも同じものがある）", ""]
    if broken:
        L += ["## 切れたリンク", ""] + [f"- {s} → `{d}`（{why}）" for s, d, why in broken] + [""]
    if unreadable:
        L += ["## 読めなかったファイル", "",
              "UTF-8 でないファイルは字を置き換えて読んだので、リンクを取りこぼしているかもしれない（UTF-8 で保存し直す）。"
              "開けなかったファイルは読んでいない。どちらにも、逆向きのリンクは書き込んでいない。", ""]
        L += [f"- `{n}`" for n in sorted(set(unreadable))] + [""]
    if broken_blocks:
        L += ["## 末尾の囲みが崩れている記録", "",
              "末尾の「この記録を指しているもの（自動）」の見出しと囲みの印（始まりと終わり）の組が崩れているので、書き込んでいない"
              "（印の片方が欠けた、見出しを書き換えた、囲みの中に書き足した、囲みが二つある）。"
              "見出しから囲みの終わりまでを消せば（書き足した行は本文に移す）、次に作り直したときに新しく足される。", ""]
        L += [f"- `{n}`" for n in broken_blocks] + [""]
    if unwritable:
        L += ["## 書けなかったファイル", "",
              "読み取り専用、パスが長すぎるなどで、書き込めなかったファイル。ほかのファイルは書いた。直せば、次に作り直したときに書かれる。", ""]
        L += [f"- `{n}`（{why}）" for n, why in unwritable] + [""]
    if unlinked:
        L += ["## 相談の記録につながっていない文字起こし", "",
              "どの相談の記録にも書かれていない文字起こし。`harness-meeting-record` でつなぐ。相談ではないものは、文字起こしの冒頭の「相談の記録」の行を「なし」にする。", ""]
        L += [f"- [{nodes[n]['label']}]({link_to(links_md.parent, nodes[n]['path'])})" for n in unlinked] + [""]
    for kind in KIND_ORDER:
        ns = [n for n, v in nodes.items() if v["kind"] == kind and (out_edges[n] or in_edges[n])]
        if not ns:
            continue
        L += [f"## {kind}", ""]
        for n in ns:
            L.append(f"### [{nodes[n]['label']}]({link_to(links_md.parent, nodes[n]['path'])})")
            L.append("")
            outs = grouped(list(out_edges[n].items()), links_md.parent, True)
            ins = grouped(list(in_edges[n].items()), links_md.parent, True)
            if outs:
                L += ["指しているもの：", ""] + outs + [""]
            if ins:
                L += ["指されているもの：", ""] + ins + [""]
    try_write(links_md, "\n".join(L).rstrip() + "\n")
    if not hook:
        n_edges = sum(len(d) for d in out_edges.values())
        print(f"記録 {len(nodes)} 件、リンク {n_edges} 本。切れたリンク {len(broken)} 件、"
              f"相談の記録につながっていない文字起こし {len(unlinked)} 件、"
              f"読めなかったファイル {len(set(unreadable))} 件、囲みが崩れている記録 {len(broken_blocks)} 件、"
              f"書けなかったファイル {len(unwritable)} 件（records/links.md）")
        if unwritable:
            print("次のファイルは書けなかった（ほかのファイルは続けた。直せば、次に動かしたときに書かれる）：")
            for n, why in unwritable:
                print(f"  {n}（{why}）")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if "--hook" in sys.argv:
            print(f"record_links: {e}", file=sys.stderr)
            sys.exit(0)
        raise
