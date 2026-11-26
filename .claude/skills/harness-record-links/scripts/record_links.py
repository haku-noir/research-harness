"""研究ハブの記録どうしのリンクを集め、逆向きのリンクと一覧を作る。

書くのは、各記録の「関連」の欄（その記録から先へのリンク）だけ（その記録を書いた会話の Claude が書く）。このスクリプトがそこから作る：

- 各記録の末尾の「この記録を指しているもの（自動）」の囲み（<!-- links:start --> 〜 <!-- links:end -->）
- records/links.md：記録ごとに「指しているもの」「指されているもの」。切れたリンクも出す
- chats/README.md：文字起こしの一覧と、それぞれを使った相談・会話・決定など

リンクの見つけ方（パスの書き方の揺れに強くするため、既知の記録の名前で探す）：

- 文字起こし：ファイル名（例：レビュー（第2章の草稿）20260115.md）
- 相談の記録：フォルダ名（例：2026-01-15_第2章の草稿）
- 実験の記録：実験の id（例：exp-pilot）
- 会話の要約：ファイル名か、会話の ID の先頭 8 文字（例：1a2b3c4d）
- 決定：hub/decisions.md
- 研究の全体像、論文ごとの論旨、導入前の経緯などの記録、指示書・報告：パス
- 名前の前後は区切られている必要がある（「ゼミ」で「ゼミ_続き」や「第2回ゼミ」を拾わないため）

切れたリンク：

- Markdown のリンク（[…](先) と [ ](<先>)）の先を、その記録のフォルダから（無ければハブから）たどり、無ければ出す
- 印（#d-20260115-143000 の形）が、先の記録に <a id="…"></a> として無ければ出す。見出しへの印は確かめない
- git の外のもの（records/sessions/raw/、export/）、URL、「*」「{{」を含むものは確かめない

使い方:
  python -X utf8 record_links.py          （作り直す）
  python -X utf8 record_links.py --hook   （フックから。失敗しても 0 で終わる）
  python -X utf8 record_links.py --check  （書き込まず、切れたリンクの数だけを出す）
"""
import atexit
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote

HUB = Path(__file__).resolve().parents[4]
LOCK = HUB / "records" / ".links.lock"
# 会話の要約の「決まったこと」などの印（同じ秒が重なったときの -2 なども）
ANCHOR_ID_RX = re.compile(r"^[a-z]{1,3}-\d{8}-\d{6}(?:-\d+)?$")
LINK_RX = re.compile(r"\]\((<[^>\n]+>|[^)\s]+)\)")
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


def rel(p: Path) -> str:
    return p.relative_to(HUB).as_posix()


def strip_generated(text: str) -> str:
    """自動で書いた囲み（このスクリプトの逆向きのリンク）を読み飛ばす。"""
    m = START_RX.search(text)
    if not m:
        return text
    i = m.start()
    j = text.find(END, i)
    head_i = text.rfind(HEAD, 0, i)
    cut_from = head_i if head_i >= 0 and text[head_i:i].strip() == HEAD else i
    return text[:cut_from] + (text[j + len(END):] if j >= 0 else "")


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
        add(dec, "決定", ["hub/decisions.md", "decisions.md"], "hub/decisions.md", True)
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
            before_ok = not (prev.isascii() and prev.isalnum()) if hexid else not (ascii_word(prev) or prev in "-.")
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
    return set(re.findall(r'<a id="([\w-]+)"></a>', read(p) or ""))


def skip_target(t: str) -> bool:
    """確かめないリンク先：URL、同じファイルの中の印、git の外のもの、ひな形の書きかけ。"""
    return (re.match(r"^[a-zA-Z][\w+.-]*:", t) is not None or t.startswith("#") or "*" in t or "{{" in t
            or "records/sessions/raw/" in t or "/export/" in t or t.startswith("export/"))


def ignored_by_git(paths):
    """git に入れない場所（.gitignore。研究で足したものも含む）にあるパスの集まり。別の PC や取り直したハブでは無いので、切れたと数えない。"""
    rels = []
    for p in paths:
        try:
            rels.append(p.resolve().relative_to(HUB.resolve()).as_posix())
        except ValueError:
            pass
    if not rels:
        return set()
    try:
        # NUL で区切った bytes で渡す（文字列で渡すと、Windows では改行が CRLF に変わり、パスの末尾に \r が付くため）
        r = subprocess.run(["git", "-C", str(HUB), "check-ignore", "--no-index", "--stdin", "-z"],
                           input=b"\0".join(x.encode("utf-8") for x in rels) + b"\0", capture_output=True, timeout=30)
        return {x.decode("utf-8", "replace") for x in r.stdout.split(b"\0") if x}
    except (OSError, subprocess.SubprocessError):
        return set()


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
        return None
    lines = r.stdout.split("\n")
    if r.returncode != 0 or len(lines) < 2 or not lines[1].strip():
        return None
    git_dir = Path(lines[0].strip()).resolve()
    common = Path(lines[1].strip())
    common = (common if common.is_absolute() else HUB / common).resolve()
    return common.parent if git_dir != common else None


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
    """同時に動いた別の会話のフックと、順番に動くためのロック。止まった実行が残したもの（10 分より古い）は外す。"""
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
                if time.time() - LOCK.stat().st_mtime > 600:
                    LOCK.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                return False
            time.sleep(0.2)


def link_to(src_dir: Path, dst: Path, anchor=None) -> str:
    r = os.path.relpath(dst, src_dir).replace("\\", "/")
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
    if not check and not acquire_lock():
        if not hook:
            print(f"ほかの実行が終わらないので、何もしない（{LOCK} が残っていれば、動いている実行が無いことを確かめて消す）")
        return
    nodes = collect_nodes()
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
    sources = [n for n, v in nodes.items() if v["kind"] != "文字起こし"]
    for src in sources:
        sp = nodes[src]["path"]
        raw_text = read(sp)
        if raw_text is None:
            continue  # 開けなかった（読めなかったファイルとして知らせる）
        text = strip_generated(raw_text)
        for dst, anchors in find_keys(text, keys).items():
            if dst == src:
                continue
            out_edges[src].setdefault(dst, set()).update(anchors)
            for a in sorted(anchors):
                check_anchor(src, dst, a)
        # Markdown のリンクの先を、その記録のフォルダから（無ければハブから）たどって確かめる。
        # コードの中に書いた例（``` の囲みと `…`）は確かめない
        code_free = INLINE_CODE_RX.sub(" ", FENCE_RX.sub(" ", text))
        for m in LINK_RX.finditer(code_free):
            t = m.group(1).strip("<>").strip()
            if skip_target(t):
                continue
            path_part, _, anchor = unquote(t).partition("#")
            if not path_part:
                continue
            try:
                cands = [(sp.parent / path_part).resolve(), (HUB / path_part).resolve()]
            except (OSError, ValueError):
                continue
            hit = next((c for c in cands if c.exists()), None)
            if hit is None:
                missing.append((src, t, cands))
                continue
            dst = by_path.get(hit) or by_path.get((hit / "README.md").resolve())
            if dst and dst != src:
                out_edges[src].setdefault(dst, set())
                if anchor:
                    out_edges[src][dst].add(anchor)
                    check_anchor(src, dst, anchor)
        # リンクにせずに書いた記録のパスで、実在しないもの（``` の囲みの中の例は除く）
        fence_free = FENCE_RX.sub(" ", text)
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
    ign = ignored_by_git([c for _, _, cs in missing for c in cs])
    for src, t, cs in missing:
        rels = set()
        for c in cs:
            try:
                rels.add(c.resolve().relative_to(HUB.resolve()).as_posix())
            except ValueError:
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
        if NO_MEETING_RX.search(read(v["path"]) or ""):
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
        return

    def write(p: Path, text: str):
        """LF で、その都度ちがう名前の一時ファイルに書いてから入れ替える。"""
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

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
        if not v["inject"] or rel(v["path"]) in GENERATED or rel(v["path"]) in unreadable:
            continue  # 読めなかったファイルには書き込まない（字を置き換えたまま書き戻さないため）
        p = v["path"]
        text = read(p)
        if text is None or rel(p) in unreadable or not p.exists():
            continue  # 開けない、または途中で名前が変わった・消えたファイルには書き込まない（中身を消したり、写しを作ったりしないため）
        items = [(s, a) for s, a in in_edges[n].items()]
        body = grouped(items, p.parent, True) or ["- （まだ無い）"]
        block = "\n".join([START, *body, END])
        m = START_RX.search(text)
        if m and END in text[m.end():]:
            i, j = m.start(), text.index(END, m.end()) + len(END)
            new = text[:i] + block + text[j:]
        else:
            new = text.rstrip("\n") + f"\n\n{HEAD}\n\n{block}\n"
        if new != text:
            write(p, new)

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
    write(links_md, "\n".join(L).rstrip() + "\n")

    # chats/README.md
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
        write(chats_md, "\n".join(C) + "\n")
    if not hook:
        n_edges = sum(len(d) for d in out_edges.values())
        print(f"記録 {len(nodes)} 件、リンク {n_edges} 本。切れたリンク {len(broken)} 件、"
              f"相談の記録につながっていない文字起こし {len(unlinked)} 件、"
              f"読めなかったファイル {len(set(unreadable))} 件（records/links.md）")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if "--hook" in sys.argv:
            print(f"record_links: {e}", file=sys.stderr)
            sys.exit(0)
        raise
