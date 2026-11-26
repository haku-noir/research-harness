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
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

HUB = Path(__file__).resolve().parents[4]
ANCHOR_ID_RX = re.compile(r"^[a-z]{1,3}-\d{8}-\d{6}$")  # 会話の要約の「決まったこと」などの印
LINK_RX = re.compile(r"\]\((<[^>\n]+>|[^)\s]+)\)")
TAIL_RX = re.compile(r"(?:/README\.md)?(?:#([\w-]+))?")
BARE_RX = re.compile(r"(?<![\w/.-])records/(?:sessions|meetings|experiments|history)/[^\s`)>|、。<>\[\]*…{}]+")
unreadable = []
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


def read(p: Path) -> str:
    """UTF-8 で読む。読めない字があれば置き換えて読み、読めなかったファイルとして知らせる（全体を止めない）。"""
    try:
        return p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        unreadable.append(rel(p))
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def find_keys(text, keys):
    """名前の前後が区切られている所だけを拾う。戻り値：{記録: 印の集まり}。str.find で探すので、記録が増えても遅くなりにくい。"""
    found = {}
    for key, dst, hexid in keys:
        i = text.find(key)
        while i != -1:
            j = i + len(key)
            before_ok = i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_-.")
            if before_ok:
                m = TAIL_RX.match(text, j)
                end = m.end()
                nxt = text[end] if end < len(text) else ""
                if hexid:
                    after_ok = not (nxt.isascii() and nxt.isalnum())
                elif key.endswith(".md") and end == j:
                    after_ok = not (nxt.isascii() and (nxt.isalnum() or nxt in "_-"))
                else:
                    after_ok = not (nxt.isalnum() or nxt in "_-")
                if after_ok:
                    found.setdefault(dst, set())
                    if m.group(1):
                        found[dst].add(m.group(1))
            i = text.find(key, i + 1)
    return found


def anchors_of(p: Path):
    return set(re.findall(r'<a id="([\w-]+)"></a>', read(p)))


def skip_target(t: str) -> bool:
    """確かめないリンク先：URL、同じファイルの中の印、git の外のもの、ひな形の書きかけ。"""
    return (re.match(r"^[a-zA-Z][\w+.-]*:", t) is not None or t.startswith("#") or "*" in t or "{{" in t
            or "records/sessions/raw/" in t or "/export/" in t or t.startswith("export/"))


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
    if (HUB / ".git").is_file():
        return  # ハブのワークツリーの中では何もしない（元のハブで作り直す）
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

    sources = [n for n, v in nodes.items() if v["kind"] != "文字起こし"]
    for src in sources:
        sp = nodes[src]["path"]
        text = strip_generated(read(sp))
        for dst, anchors in find_keys(text, keys).items():
            if dst == src:
                continue
            out_edges[src].setdefault(dst, set()).update(anchors)
            for a in sorted(anchors):
                check_anchor(src, dst, a)
        # Markdown のリンクの先を、その記録のフォルダから（無ければハブから）たどって確かめる
        for m in LINK_RX.finditer(text):
            t = m.group(1).strip("<>").strip()
            if skip_target(t):
                continue
            path_part, _, anchor = unquote(t).partition("#")
            if not path_part:
                continue
            cands = [(sp.parent / path_part).resolve(), (HUB / path_part).resolve()]
            hit = next((c for c in cands if c.exists()), None)
            if hit is None:
                broken.append((src, t, "ファイルが無い"))
                continue
            dst = by_path.get(hit) or by_path.get((hit / "README.md").resolve())
            if dst and dst != src:
                out_edges[src].setdefault(dst, set())
                if anchor:
                    out_edges[src][dst].add(anchor)
                    check_anchor(src, dst, anchor)
        # リンクにせずに書いた記録のパスで、実在しないもの
        for m in BARE_RX.finditer(text):
            if m.start() > 0 and text[m.start() - 1] in "(<":
                continue  # リンクの先は、上で確かめた
            path = m.group(0).split("#")[0]
            k = path.find(".md")
            path = (path[:k + 3] if k != -1 else path).rstrip("/")
            if skip_target(path):
                continue
            # 全角の括弧は、フォルダの名前の一部のことも、後ろの説明のこともある。括弧の手前で切った形も確かめる
            cands = [path] + [path[:i].rstrip("/") for i in range(len(path) - 1, 0, -1) if path[i] in "（）"]
            if not any((HUB / c).exists() for c in cands if c):
                broken.append((src, path, "ファイルが無い"))
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
        if NO_MEETING_RX.search(read(v["path"])):
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
            print(f"UTF-8 で読めなかったファイル（字を置き換えて読んだ）：{len(set(unreadable))}")
            for n in sorted(set(unreadable)):
                print("  ", n)
        return

    def write(p: Path, text: str):
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, p)

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
        L += ["## UTF-8 で読めなかったファイル", "",
              "字を置き換えて読んだので、リンクを取りこぼしているかもしれない。UTF-8 で保存し直す。逆向きのリンクは書き込んでいない。", ""]
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
