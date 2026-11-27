# 導入の手順（AI が従う）

この文書は、ユーザーに「このリポジトリを導入したい」と頼まれた AI が従う手順です。人が自分で進めることもできます。

Claude Code 以外の AI で導入するときは、先に `harness/other-ai.md` を読み、この手順を進めながら、自分の機能に合わせた読み替えを作ります。

## 守ること

- **聞いてから決める。** 置き場所、取り込む資料、公開範囲は、ユーザーが決めることです。推測で埋めません。質問は一度に 3 つまでにし、選択肢と推奨を付けます。
- **元の資料を動かさない。** 既存の文書、コードのリポジトリ、文字起こし、文献のフォルダは、ユーザーの許可なく移動・削除・書き換えをしません。取り込みの既定は「写す」か「場所を記録するだけ」です。
- **研究の中身を外に送らない。** 導入中に読んだ内容を、ウェブ検索や外部のサービスに送りません。検索するときは一般の語に言い換えます（`CLAUDE.md` の「ハブを公開しない」）。

## 1. ハブを取得する

1. ユーザーに聞く。
   - ハブを置くフォルダ。既定はホームの下の `research-hub`。研究ごとに分けるなら `research-hub-<短い名前>`。
   - そのフォルダが既にあるか。あれば中身を見せる。空でないフォルダには `git clone` できない。別のフォルダにするか、研究者が自分で中身を空けるかを決めてもらう。既にある中身の上に複製しない（中身を消したり動かしたりもしない）。
2. 取得する。

   ```
   git clone <このリポジトリの URL> <フォルダ>
   cd <フォルダ>
   git remote rename origin template
   git branch --unset-upstream
   ```

   `template` は、ひな形の更新を後で取り込むための登録です。`git branch --unset-upstream` で、`main` がひな形の `main` を追わないようにする（追ったままだと、引数の無い `git push` がひな形に送ろうとして止まる。ひな形の更新は、2 で `template/main` と名前で指定して取り込む）。ユーザーの研究の記録は、自分のコミットとして積みます。リモートに置くなら、導入の途中（harness-install の 3）で、非公開のリモートを `origin` として足します。
3. **push の守りを入れる**（必ず行う）。ハブには研究の中身が入るので、ひな形や公開の場所に誤って push しないようにする。

   ```
   git remote set-url --push template no-push-to-template
   git config core.hooksPath harness/hooks
   ```

   入ったことを確かめる。`git push --dry-run template HEAD` が失敗し、`git config core.hooksPath` が `harness/hooks` を返せばよい。
   - push 先を無効にしたので、ひな形には送れない。
   - `harness/hooks/pre-push` が、許可していないリモートへの push を止める。許可は `git config --add hub.pushAllowed <リモート名>` で足す。研究者が、そのリモートが非公開であることを確かめてから、研究者自身が行う。AI は、頼まれても行わず、コマンドを示すだけにする（`CLAUDE.md` の「ハブを公開しない」）。
   - 許可したリモートでも、履歴を書き換える push（force push）と、ブランチやタグの削除は止まる（`CLAUDE.md` の「履歴を書き換えない」）。
   - **別の PC でハブを複製し直したとき**：git の設定は複製に写らないので、`template` のリモート、`core.hooksPath`、`hub.pushAllowed` が無い。git の外の `.claude/settings.local.json` も無い。
     1. `git remote` に `template` が無ければ、先に `git remote add template <ひな形の URL>` で足してから、上の 2 行を行う。
     2. push の許可（`hub.pushAllowed`）は、研究者が自分で足し直す（上と同じ。AI はコマンドを示すだけ）。
     3. `.claude/settings.local.json` は、`hub/rules.md` の「研究独自のスキル・手引き・ひな形」の記載から作り直す（README の「`base-` のスキルを研究独自のスキルに置き換える」の 6）。
     4. 取り込んだ履歴の `archive/<名前>/…` のブランチは、複製では `origin/archive/<名前>/…` としてだけ見える（`harness-install` の 3）。
     5. PC ごとに違うパスを足す。git で渡ってくるファイルには、前の PC のパスが入っているため。この PC のパスは研究者に確かめ、統括が足す。
        - `hub/profile.md` の「場所」：ハブ、コードのリポジトリ、データなどのパスは、前の PC のもの。前の PC の行は消さず、この PC のパスを足す。PC ごとに名前を付けて並べる（例：「<PC の名前>：<パス>」）。
        - `hub/session-sources.txt`：前の PC の作業ディレクトリが並んでいる。この PC の作業ディレクトリを足す（ハブのフォルダは書かなくても対象になる）。PC ごとに「# <PC の名前>」の行の下に並べる。もう一方の PC の行は、この PC ではどの会話にも当たらない。手で動かすとそう知らされるが、消さない。
        - コードのリポジトリの `CLAUDE.local.md`：git に入らないので、この PC には無い。この PC のリポジトリごとに、この PC のハブのパスで置き直す（`harness-install` の 6）。
        - 指示書（`hub/briefs/`）の「ハブ」と「作業の場所」は、書いた PC のパス。この PC で作業の会話を起動するときは、統括が、この PC のパスを指示書に足してから起動する。
4. 続ける前に、Python を確かめる。`python --version`（無ければ `python3 --version`、Windows では `py -3 --version`）が 3.10 以上を返せばよい。3.10 以上が無ければ、会話の終わりのフックは何もせず、会話の記録とリンクのスクリプトも動かないことを研究者に伝える（入れるかは研究者が決める）。
5. 取得できたら、`<フォルダ>/.claude/skills/harness-install/SKILL.md` を読み、その手順に従って続ける。
   - いまのセッションがハブの外で動いていても、そのファイルを読んで手順どおりに進めてよい。
   - 導入が終わったら、ハブのフォルダで新しいセッションを開くよう、ユーザーに伝える。そのセッションが統括役になる。

## 2. ひな形の更新を取り込む（導入後）

研究者に頼まれたときだけ行います。Claude から更新を探しには行きません。ひな形はめったに変わらず、取り込むとすべての会話の動き方が変わるので、いつ取り込むかは研究者が決めます。

| 頼まれ方 | すること |
|---|---|
| 「ひな形に更新はある？」 | 2-1 だけ。取り込まない |
| 「ひな形の更新を取り込んで」 | 2-1〜2-5。この頼みがマージの指示なので、聞き直さずに進める。止まって研究者に聞くのは、作業セッションが動いているとき（2-2）と、衝突・ひな形の層の書き換え・名前の食い違いが出たとき（2-3、2-4）だけ |

### 2-1. 何が変わるかを示す

```
git fetch template
git log --oneline HEAD..template/main
git diff --name-status -M HEAD...template/main
```

新しいコミットと、変わるファイルを研究者に見せる。名前が変わった（`R`）か消えた（`D`）ファイルは、2-4 で使う。

### 2-2. 動いている作業セッションを伝える

`hub/workstreams.md` に動いている作業があれば、次のことを伝え、取り込むのを待つか、取り込んでから作業を新しい会話に移すかを研究者に決めてもらう。

- 動いている会話は、始めたときに読んだ約束のまま動き続ける。
- 取り込んだ後の約束を使わせたいなら、区切りのよいところで新しい会話にする（`harness-dispatch` の「同じ作業を新しい会話で続ける」）。

### 2-3. マージする

```
git merge --no-ff --no-commit template/main
```

コミットは 2-4 の後にする。衝突したら：

- ひな形の層のファイル（下の一覧）は、ひな形の版を採る。
- 研究がそのファイルに書いていた変更は、研究の層に移す案を研究者に示し、決めてもらう。
- `.gitignore` は、末尾の「この研究で足したもの」の区切りより下を、研究の分として残す。

衝突が無くても、コミットの前に、ひな形の層のファイルがひな形の版と同じかを確かめる。研究がひな形の層のファイルを書き換えていて、ひな形の側がそのファイルを変えていなければ、衝突せずに研究の書き換えが残るため。

```
git diff --stat template/main -- CLAUDE.md AGENTS.md INSTALL.md README.md LICENSE .claude/settings.json .claude/skills/harness-* .claude/skills/base-* harness/ .gitattributes
git diff template/main -- .gitignore
```

- 一つ目に出たファイルは、研究がひな形の層を書き換えたもの。差分を研究者に示し、ひな形の版を採る（`git checkout template/main -- <パス>`。ひな形に無いファイルなら `git rm <パス>`）。研究者がその変更を研究の層に移すと決めたら、移してから採る。
- `.gitignore` は二つ目で見る。区切りより下の差は研究の分なので残す。区切りより上に差があれば、上と同じに研究者に示し、区切りより上をひな形の版に合わせる。

### 2-4. 研究の層を合わせる

2-1 で名前が変わったか消えたスキルやひな形の古い名前で、研究の層を検索する。

- `hub/rules.md`、`hub/roles/`、`hub/templates/`
- 研究独自のスキル（`.claude/skills/` の、`harness-` でも `base-` でも始まらない名前）
- `.claude/settings.local.json` の `skillOverrides`（git の外）

見つけたら、直す案を研究者に示し、直す。

あわせて、研究の層で差し替えているひな形（`hub/templates/<名前>`）と、手引きへの追記（`hub/roles/<役>.md`）について、ひな形の側（`harness/templates/<名前>`、`harness/roles/<役>.md`）が今回変わっていれば、その差分を研究者に示す。研究の層を合わせるかは研究者が決める。仕組みが読む部分（README の「仕組みが読む部分」）が変わっていることがあるため。

### 2-5. コミットして、スクリプトを動かす

コミットの説明は `CLAUDE.md` の「現在の文書と履歴」の形で書く。

- 何を変えたか：取り込んだひな形の範囲（取り込む前の `git merge-base HEAD template/main` から `template/main` まで）
- 2-3 と 2-4 で研究の層に移した・直したもの
- きっかけ：研究者の指示

コミットした後、会話の記録とリンクのスクリプトを動かし、エラーや切れたリンクが出ないかを確かめる。出たら研究者に示す。

```
python -X utf8 .claude/skills/harness-session-archive/scripts/session_archive.py --pending
python -X utf8 .claude/skills/harness-record-links/scripts/record_links.py --check
```

コミットした後に `git status --short` で `.gitignore` か `.gitattributes` が変更（`M`）として出たら、改行が CRLF のまま残っている（ひな形の `.gitattributes` が、この二つも LF にそろえるようになったため）。`git add --renormalize .gitignore .gitattributes` で LF にそろえて別のコミットにし（説明は `CLAUDE.md` の形。何を変えたか：改行を LF にそろえた）、`record_links.py --check` を動かし直す。`.gitignore` が CRLF だと、無いリンクの先が git の外とみなされ、切れたリンクが出ないため。

### ひな形が持つファイル

- `CLAUDE.md`、`AGENTS.md`、`INSTALL.md`、`README.md`、`LICENSE`
- `.claude/settings.json`
- `.claude/skills/harness-*`（仕組みのスキル）と `.claude/skills/base-*`（見本のスキル）
- `harness/`
- `.gitattributes`
- `.gitignore` の、末尾の「この研究で足したもの」の区切りより上

研究の中身（`hub/`、`papers/`、`chats/`、`records/` など）は導入で作るので、ひな形の更新とは衝突しません。

`CLAUDE.md` などひな形の層を自分で書き換えた場合は、衝突することがあります。研究に合わせて直すときは、研究の層（`hub/rules.md`、`hub/roles/`、`hub/templates/`、`harness-` でも `base-` でも始まらない名前のスキル）に書いてください（README の「研究に合わせて直す」）。
