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
- 「パス#印」と書かれていれば、印まで記録し、印が無ければ切れたリンクとして出す

使い方:
  python -X utf8 record_links.py          （作り直す）
  python -X utf8 record_links.py --hook   （フックから。失敗しても 0 で終わる）
  python -X utf8 record_links.py --check  （書き込まず、切れたリンクの数だけを出す）
"""
import os
import re
import sys
from pathlib import Path

HUB = Path(__file__).resolve().parents[4]
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
    for p in sorted((HUB / "papers").glob("*/*.md")):
        r = rel(p)
        add(p, "論旨", [r], r, False)
    return nodes


def key_regex(key: str):
    if re.fullmatch(r"[0-9a-f]{8}", key):
        return re.compile(rf"(?<![0-9a-zA-Z]){key}(?![0-9a-zA-Z])")
    if re.fullmatch(r"exp-[a-z0-9-]+", key):
        return re.compile(rf"(?<![\w-]){re.escape(key)}(?![\w-])")
    return re.compile(re.escape(key) + r"(?:/README\.md)?(?:#([\w-]+))?")


def anchors_of(p: Path):
    try:
        return set(re.findall(r'<a id="([\w-]+)"></a>', p.read_text(encoding="utf-8")))
    except OSError:
        return set()


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
    nodes = collect_nodes()
    regs = {n: [key_regex(k) for k in v["keys"]] for n, v in nodes.items()}
    out_edges = {n: {} for n in nodes}   # src -> {dst: set(anchors)}
    broken = []

    sources = [n for n, v in nodes.items() if v["kind"] != "文字起こし"]
    for src in sources:
        text = strip_generated(nodes[src]["path"].read_text(encoding="utf-8"))
        for dst, rxs in regs.items():
            if dst == src:
                continue
            found, anchors = False, set()
            for rx in rxs:
                for m in rx.finditer(text):
                    found = True
                    if m.groups() and m.group(1):
                        anchors.add(m.group(1))
            if found:
                out_edges[src][dst] = anchors
                if anchors:
                    have = anchors_of(nodes[dst]["path"])
                    for a in sorted(anchors - have):
                        broken.append((src, f"{dst}#{a}", "印が無い"))
        # 記録らしいパスで、実在しないもの
        for m in re.finditer(r"records/(?:sessions|meetings|experiments|history)/[^\s`)>|（）、。]+", text):
            path = m.group(0).split("#")[0].rstrip("/")
            cand = HUB / path
            if not cand.exists() and not (HUB / (path + "/README.md")).exists() and "<" not in path and "{" not in path:
                broken.append((src, path, "ファイルが無い"))

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
        if NO_MEETING_RX.search(v["path"].read_text(encoding="utf-8")):
            continue
        unlinked.append(n)

    if check:
        print(f"切れたリンク：{len(broken)}")
        for b in broken:
            print("  ", *b)
        print(f"相談の記録につながっていない文字起こし：{len(unlinked)}")
        for n in unlinked:
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
        if not v["inject"] or rel(v["path"]) in GENERATED:
            continue
        p = v["path"]
        text = p.read_text(encoding="utf-8")
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
            p.write_text(new, encoding="utf-8")

    # records/links.md
    links_md = HUB / "records" / "links.md"
    L = ["# 記録どうしのリンク", "",
         "`harness-record-links` が、各記録の「関連」の欄などから作る一覧。人も Claude も直接書き換えない。横断して調べるときは、まずここを読む。", "",
         "- 指しているもの：その記録が書いているリンク",
         "- 指されているもの：その記録を指している記録（各記録の末尾にも同じものがある）", ""]
    if broken:
        L += ["## 切れたリンク", ""] + [f"- {s} → `{d}`（{why}）" for s, d, why in broken] + [""]
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
    links_md.write_text("\n".join(L).rstrip() + "\n", encoding="utf-8")

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
        chats_md.write_text("\n".join(C) + "\n", encoding="utf-8")
    if not hook:
        n_edges = sum(len(d) for d in out_edges.values())
        print(f"記録 {len(nodes)} 件、リンク {n_edges} 本。切れたリンク {len(broken)} 件、"
              f"相談の記録につながっていない文字起こし {len(unlinked)} 件（records/links.md）")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if "--hook" in sys.argv:
            print(f"record_links: {e}", file=sys.stderr)
            sys.exit(0)
        raise
