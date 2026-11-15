<h1 align="center">code-review-graph</h1>

<p align="center">
  <a href="https://trendshift.io/repositories/23329?utm_source=repository-badge&amp;utm_medium=badge&amp;utm_campaign=badge-repository-23329"
     target="_blank"
     rel="noopener noreferrer">
    <img src="https://trendshift.io/api/badge/repositories/23329"
         alt="tirth8205%2Fcode-review-graph | Trendshift"
         width="250"
         height="55" />
  </a>
</p>

<p align="center">
  <strong>MCP 経由で AI コーディングツールに正確なレビュー文脈を渡す、ローカルのコード知識グラフ。</strong>
</p>
<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a> |
  <a href="README.ja-JP.md">日本語</a> |
  <a href="README.ko-KR.md">한국어</a> |
  <a href="README.hi-IN.md">हिन्दी</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/code-review-graph/"><img src="https://img.shields.io/pypi/v/code-review-graph?style=flat-square&color=blue" alt="PyPI"></a>
  <a href="https://pepy.tech/project/code-review-graph"><img src="https://img.shields.io/pepy/dt/code-review-graph?style=flat-square" alt="Downloads"></a>
  <a href="https://github.com/tirth8205/code-review-graph/stargazers"><img src="https://img.shields.io/github/stars/tirth8205/code-review-graph?style=flat-square" alt="Stars"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square" alt="MIT Licence"></a>
  <a href="https://github.com/tirth8205/code-review-graph/actions/workflows/ci.yml"><img src="https://github.com/tirth8205/code-review-graph/actions/workflows/ci.yml/badge.svg?branch=staging" alt="CI"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg?style=flat-square" alt="Python 3.10+"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-compatible-green.svg?style=flat-square" alt="MCP"></a>
  <a href="https://code-review-graph.com"><img src="https://img.shields.io/badge/website-code--review--graph.com-blue?style=flat-square" alt="Website"></a>
  <a href="https://discord.gg/3p58KXqGFN"><img src="https://img.shields.io/badge/discord-join-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <a href="docs/USAGE.md">使い方</a> ·
  <a href="docs/COMMANDS.md">コマンド</a> ·
  <a href="docs/FAQ.md">FAQ</a> ·
  <a href="docs/TROUBLESHOOTING.md">トラブルシューティング</a> ·
  <a href="docs/FEATURES.md">新機能</a> ·
  <a href="CHANGELOG.md">変更履歴</a> ·
  <a href="docs/GITHUB_ACTION.md">GitHub Action</a> ·
  <a href="docs/REPRODUCING.md">ベンチマークの再現</a> ·
  <a href="docs/ROADMAP.md">ロードマップ</a> ·
  <a href="docs/INDEX.md">すべてのドキュメント</a>
</p>

<br>

AI コーディングツールは、変更をレビューするためにコードベースの大部分を読み直しがちです。`code-review-graph` は [Tree-sitter](https://tree-sitter.github.io/tree-sitter/) でコードの構造マップを作り、増分で更新し続け、[MCP](https://modelcontextprotocol.io/) 経由で小さな文脈を渡します。アシスタントは変更が触れるファイルだけを読みます。

<p align="center">
  <img src="diagrams/diagram1_before_vs_after.png" alt="トークンの問題: flask のコーパス全体を読むと 143,594 トークン、グラフの回答は 2,196 トークン（65 分の 1）" width="85%" />
</p>

**できること**

- **コードを地図にします。** 関数、クラス、インポート、呼び出し、テストが 1 つのグラフになります。
- **変更で壊れうるものを示します。** 変更が届きうる呼び出し元、依存先、テストを、リスクスコアつきで示します。
- **常に最新に保ちます。** フックと watch モードが、変わったファイルだけを再解析します。
- **AI ツールと連携します。** `install` を 1 回実行すると、見つかったツールをそれぞれ設定します。
- **手元のマシンから出ません。** SQLite ファイル 1 つで、クラウドサービスは使いません。クラウド埋め込みは明示的に有効にした場合だけです。

---

## クイックスタート

Python 3.10+ が必要です。

```bash
uv tool install code-review-graph      # or: pipx install / pip install code-review-graph
code-review-graph install              # set up the AI coding tools found on this machine
code-review-graph build                # parse the codebase
code-review-graph detect-changes --brief  # see what your last change touches
```

エディタやツールを再起動し、プロジェクトを開いて、アシスタントにこう頼みます。

```
Review my changes
```

`code-review-graph uninstall` ですべて元に戻せます（`--dry-run` で先に確認できます）。うまく動かないときは `code-review-graph doctor` を実行してください。設定を調べるだけで、何も変えません。

<details>
<summary><strong><code>install</code> が変えるものと、先に確かめる方法</strong></summary>
<br>

`install` はこのマシンにある AI コーディングツールを探し、見つかったものだけを設定します。それぞれに MCP サーバーのエントリを書き込み、ツールが対応していればフックとスキルを入れ、ルールファイルにグラフの説明を追加します。最後に、見つからなかったツールを示します。それでも追加したいツールは `install --platform NAME` で設定できます。

`install` は、変更するファイルを一覧にし、指示ファイルについての質問にあなたが答えるまで、何も書き込みません。AI エージェントが実行する場合のように端末がないときは、既定の答え（はい）を選び、そのことを表示します。`--no-instructions` を付けるとそれらのファイルを書きません。`install --dry-run` は各変更を差分で示し、何も書き込みません。実行後はエディタやツールを再起動してください。

MCP エントリとすべてのフックは、`install` を実行した code-review-graph を動かします。MCP エントリは、Poetry や uv のプロジェクトでは `poetry run` または `uv run` を、`install` 自体が `uvx` か `uv tool install` から動いたときは `uvx` を、それ以外では `install` を実行した Python を使います。フックはその Python（または `uvx`）を名前で指定し、それがなくなっていれば PATH 上の `code-review-graph` を実行します。アップグレードしたら `install` をもう一度実行してください。

`install` が書くファイル（`.claude/` や `.mcp.json` など）は、チームでコミットする場合に備えて git から見えるままにしてあります。git が追跡するまでは `build` とレビューがこれらを除外するので、最初のレビューには自分の変更だけが出ます。`install` はこれらを `.code-review-graph/install-manifest.json` に記録します。`install --gitignore` を付けると、その実行で作ったものを `.gitignore` の印付きブロックに追加します。`uninstall` はそのブロックを取り除きます。

Codex の場合、インストーラはさらにグローバルな `code-review-graph` スキルを `$CODEX_HOME/skills/code-review-graph/`（既定: `~/.codex/skills/code-review-graph/`）に置きます。このスキルは CRG の MCP ツールを優先し、MCP が使えないときは同梱の読み取り専用 CLI に切り替えます。グラフがない、空、または古い場合でも、暗黙にビルドや更新を始めることはありません。WSL と Windows の Codex ランタイムは `CODEX_HOME` ディレクトリとツール一覧がそれぞれ別なので、タスクを実行するランタイムの側でインストールと更新を行ってください。

</details>

<details>
<summary><strong>対応するすべてのプラットフォームと、1 つだけを設定する方法</strong></summary>
<br>

<p align="center">
  <img src="diagrams/diagram8_supported_platforms.png" alt="一度のインストールで全プラットフォーム対応: Codex、Claude Code、CodeBuddy Code、Cursor、Windsurf、Zed、Continue、OpenCode、MiMo Code、Antigravity、Gemini CLI、Qwen Code、Qoder、Kiro、GitHub Copilot、GitHub Copilot CLI、Hermes Agent、DeepSeek Harness を検出" width="85%" />
</p>

1 つのプラットフォームだけを設定するには、`--platform` に `codex`、`claude-code`、`cursor`、`windsurf`、`zed`、`continue`、`opencode`、`mimo`、`antigravity`、`gemini-cli`、`qwen`、`kiro`、`qoder`、`copilot`、`copilot-cli`、`codebuddy`、`hermes`、`dsh` のいずれかを渡します。

```bash
code-review-graph install --platform cursor
code-review-graph install --platform codebuddy
```

この方法で指定したプラットフォームは、`install` が見つけられなくても設定されます。設定ファイルの場所は [docs/USAGE.md](docs/USAGE.md#supported-platforms) にあります。

</details>

<details>
<summary><strong><code>uninstall</code> のその他の使い方</strong></summary>
<br>

`uninstall` は Git または SVN の作業ツリーから CRG が書いたファイルとエントリを取り除き、ほかの MCP サーバー、フック、スキル、JSONC のコメントはそのまま残します。ツリー内のどこからでも実行できます。共有設定ファイルはアトミックに置き換えるので、書き込みに失敗しても元のファイルは壊れません。`install` が作った設定ファイルで、git が追跡しておらず中身がほかにないものは、`install` が作った空のフォルダーと一緒に削除されます。端末がないときは既定の答え（いいえ）を選び、`--yes` を付けるよう案内します。

```bash
code-review-graph uninstall --dry-run    # preview only
code-review-graph uninstall              # preview, confirm, apply
code-review-graph uninstall --yes        # apply without prompting
code-review-graph uninstall --all-repos  # also clean every registered repository
code-review-graph uninstall --keep-data  # remove integrations, keep graph databases
code-review-graph uninstall --keep-user-configs --repo .  # this project only
```

</details>

<details>
<summary><strong>ビルドにかかる時間と、一部だけのビルドの意味</strong></summary>
<br>

ビルド時間はリポジトリの規模に比例します。約 3,000 ファイルのリポジトリのコールドビルドで約 40 秒でした（[計測値](docs/REPRODUCING.md#incremental-update-latency)）。その後はフックと watch モードがグラフを更新し続けます。一部のファイルの解析に失敗した場合、結果のステータスは `partial` になり、要約にそのファイル名が入ります。CLI は stderr に `Warning:` の行も出力し、それらのファイルは以前のグラフの行をそのまま保持します。ソースファイルが 1 つも見つからないビルドは stderr に警告を出し、プロジェクトのルートにいるかどうかを尋ねます。

</details>

<details>
<summary><strong>2.3.x からのアップグレード</strong></summary>
<br>

```bash
pip install -U code-review-graph     # or: pipx upgrade / uv tool upgrade code-review-graph
code-review-graph doctor             # every copy, MCP entry and hook, and the version each runs
code-review-graph install --dry-run  # see what changes, then run install
code-review-graph build              # once: new file types and fixes reach only re-parsed files
```

手元のすべてのコピーをアップグレードしてください。MCP エントリ、git とエディタのフック、`crg-daemon` は、それぞれ別のコピーを動かすことがあります。2.4.0 以降が一度開いたグラフは、2.3.9 のコピーでは読めず、それより古い 2.3.x のコピーでは壊れることがあります。`install` をもう一度実行してエントリとフックが新しいコピーを動かすようにし、エディタを再起動してください（デーモンを使っている場合は `crg-daemon restart` も実行します）。

2.4.0 以降で最初に実行したコマンドが、グラフをスキーマ 15 に移します。VS Code 拡張機能を使っている場合、このグラフを読むにはバージョン 0.4.0 が必要です。全体の一覧: [アップグレードの注意](CHANGELOG.md#upgrade-notes)。

</details>

## 仕組み

リポジトリは Tree-sitter で AST に解析され、ノード（関数、クラス、インポート）とエッジ（呼び出し、継承、テストカバレッジ）のグラフとして保存されます。レビュー時には、アシスタントが読むべき最小のファイル集合をグラフに問い合わせます。

<details>
<summary><strong>2 つの図: アシスタントがグラフを使う流れと、ビルドのパイプライン</strong></summary>
<br>

<p align="center">
  <img src="diagrams/diagram7_mcp_integration_flow.png" alt="アシスタントがグラフを使う流れ: ユーザーがレビューを依頼し、アシスタントが MCP ツールを呼び、グラフが影響範囲とリスクスコアを返し、アシスタントは影響のあるファイルだけを読む" width="80%" />
</p>

<p align="center">
  <img src="diagrams/diagram2_architecture_pipeline.png" alt="アーキテクチャのパイプライン: リポジトリ → Tree-sitter パーサー → SQLite グラフ → 影響範囲 → 最小レビュー集合" width="100%" />
</p>

</details>

### 影響範囲の分析

<details>
<summary><strong>変更が影響しうる呼び出し元、依存先、テストのすべて</strong></summary>
<br>

ファイルが変更されると、グラフは影響を受けうる呼び出し元、依存先、テストをすべて辿ります。アシスタントはプロジェクト全体を走査せず、それらのファイルを読みます。

<p align="center">
  <img src="diagrams/diagram3_blast_radius.png" alt="影響範囲: login() の変更が呼び出し元、依存先、テストへ波及する" width="70%" />
</p>

</details>

### 増分更新

<details>
<summary><strong>変更したファイルだけを再解析（2 ファイルの編集で約 2.5 秒）</strong></summary>
<br>

エディタのフック、git のフック（pre-commit、post-checkout、post-merge、post-rewrite）、watch モードが増分更新を起動します。フックは静かに更新します。コミットのたびに pre-commit フックが出すのは `detect-changes --brief` の短いレビューだけです。更新では変更ファイルの差分を取り、グラフのインポートエッジと呼び出しエッジから依存先を見つけ、SHA-256 ハッシュが変わったファイルだけを再解析します。約 3,000 ファイルのプロジェクト（django）では、2 ファイルの編集がフックの経路で約 2.5 秒で再インデックスされ、そのうち約 1.4 秒はプロセスの起動時間です。変更がない場合はその起動時間だけで済みます。[増分更新のレイテンシ](docs/REPRODUCING.md#incremental-update-latency)を参照してください。

<p align="center">
  <img src="diagrams/diagram4_incremental_update.png" alt="増分更新の流れ: フックまたは watch の更新が git diff を起動し、グラフのエッジから依存先を見つけ、SHA-256 ハッシュが変わったファイルだけを再解析する" width="90%" />
</p>

</details>

### コードベース全体か、的を絞った回答か

<details>
<summary><strong>コードベース全体ではなく、質問に合わせて切り出したグラフの一部</strong></summary>
<br>

グラフはコーパス全体をモデルに渡す代わりに、質問に合わせて切り出した一部を返します。このリポジトリの `84bde354` における 2026-08-02 の計測では、208,821 のソーストークンが 1 質問あたり約 3,190 トークンになりました。そのスナップショット以降リポジトリは大きく育っているので、今日ではどちらの数字も大きくなっています。

<p align="center">
  <img src="diagrams/diagram6_monorepo_funnel.png" alt="code-review-graph の 84bde354 スナップショット: 208,821 のソーストークンが約 3,190 トークンのグラフ応答に絞り込まれ、1 質問あたりのトークンが約 65 分の 1 になる" width="80%" />
</p>

</details>

### 対応言語とノートブック

<details>
<summary><strong>Python、JavaScript、TypeScript、Go、Rust、Java、C/C++、C# など多数とノートブック: 全一覧</strong></summary>
<br>

<p align="center">
  <img src="diagrams/diagram9_language_coverage.png" alt="カテゴリ別の言語カバレッジ: Web、バックエンド、システム、モバイル、スクリプト、シェル、ドメイン、設定、その他、加えて Jupyter と Databricks のノートブック" width="90%" />
</p>

パーサーは関数、クラス、インポート、呼び出し箇所、継承、テストを抽出します。文法がある言語では Tree-sitter を、それ以外では的を絞ったフォールバックを使います。対応: Python、JavaScript/TypeScript/TSX（`.mjs`、`.cjs`、`.mts`、`.cts` を含む）、Go、Rust、Java、C/C++、C#、VB.NET、Ruby、Kotlin（`.kt`、`.kts`）、Swift、PHP、Scala、Solidity、Dart、R、Perl、Lua/Luau、Objective-C、シェルスクリプト、Elixir、Zig、PowerShell、Julia、ReScript、GDScript、Nix、Verilog/SystemVerilog、SQL/PL/SQL（Oracle）と dbt モデル、Terraform/OpenTofu（`.tf` と、`.hcl` ファイル内の Terraform ブロック。Terraform ブロックのない `.hcl` ファイルはファイルノードのみ）、Ansible YAML（playbook、role、task）、Spring Boot のアプリケーション設定（`application.properties`、`application.yml`、`application.yaml` とその `application-<profile>` 版。キー名と値の型のみを記録し、値は決して記録しません）、値を含まない一般的な YAML の構造（`locales/` などのフォルダにある翻訳ファイルはファイルノードのみ）、XSLT スタイルシート（`.xsl`、`.xslt`）、値を含まない JSON の構造（`.json`。翻訳、テスト、ドキュメント、サンプル、データのファイルはファイルノードのみで、ロックファイルは飛ばします）、Vue/Svelte の SFC、HTML ページ（`.html`、`.htm`。インラインスクリプトと、id を持つ `div`、`section`、`article` のブロックを `DocSection` ノードとして）、Astro ファイル（TypeScript の文法で解析）、Jupyter と Databricks のノートブック（`.ipynb`。加えて Databricks の `.py` エクスポート）、Perl XS ファイル（`.xs`）、Robot Framework のファイル（`.robot`、`.resource`）、Liquid テンプレート（`.liquid`）。ほかの `.properties` ファイルはソースコードとして扱いません。

PHP プロジェクトではさらに、リポジトリ内に閉じた Composer PSR-4 の解決、Blade テンプレートの参照、そしてソースに明示的なフレームワークのインポート、モデルの継承、レシーバの根拠が現れている場合の Laravel Route と Eloquent のエッジが得られます。`routes/*.php` 内の静的な Laravel ルートは、Endpoint ノードとコントローラへの HANDLES エッジを生成し、リテラルのグループプレフィックスも反映します。

Java プロジェクトでは、Spring の依存性注入による呼び出し解決、リクエストエンドポイントと WebFlux ルート、スケジュールトリガー、アプリケーションイベントの発行側からリスナーへのエッジ、Spring AOP のアドバイスから対象へのエッジ、Temporal のワークフローとアクティビティのエッジが得られます。各リゾルバは解析のあとに動き、注入されたフィールド、発行されるイベント、ワークフローのスタブがリポジトリ内で見えている必要があります。

</details>

### 独自の言語を追加する

<details>
<summary><strong><code>languages.toml</code> でファイル拡張子を同梱の Tree-sitter 文法に対応づける</strong></summary>
<br>

パーサーが未対応の言語をリポジトリで使っている場合は、`.code-review-graph/` に `languages.toml` を置き、ファイル拡張子を `tree_sitter_language_pack` に同梱された任意の文法に対応づけ、関数、クラス、インポート、呼び出しのノード型を指定します。

```toml
[languages.erlang]
extensions = [".erl"]
grammar = "erlang"
function_node_types = ["function_clause"]
class_node_types = ["record_decl"]
import_node_types = ["import_attribute"]
call_node_types = ["call"]
```

抽出は汎用の tree-sitter ウォーカーが行います。組み込み言語は上書きできません。スキーマ、検証ルール、具体例は [docs/CUSTOM_LANGUAGES.md](docs/CUSTOM_LANGUAGES.md) を参照してください。

</details>

### CI でのリスクスコア付き PR レビュー（GitHub Action）

<details>
<summary><strong>リスクスコア、影響を受けるフロー、テストの穴をまとめたプルリクエストのコメント 1 件。ワークフローの例を参照</strong></summary>
<br>

同じ分析が composite の GitHub Action としても動きます。グラフはあなたの CI ランナー上で構築・照会され、ソースコードが外部サービスに送られることはありません。プルリクエストごとに、リスクスコア付きの関数、影響を受ける実行フロー、テストの穴をまとめた固定コメントを 1 件投稿し、プッシュのたびにその場で更新します。任意の `fail-on-risk` 入力を使うと、マージの関門にできます。任意の `details` 入力は、折りたたんでサイズに上限を設けた形でグラフの文脈を追加します。上位の各シンボルがそのスコアになった理由、変更が触れるコミュニティ、ハブ、境界をまたぐつながり、影響範囲、テストのないホットスポット、そしてグラフの出どころです。

```yaml
# .github/workflows/code-review-graph.yml
on:
  pull_request:

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: tirth8205/code-review-graph@v2.4.0
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

入力、リスクレベル、キャッシュについては [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md) を、このリポジトリ自身に対して動かしているワークフローは [`.github/workflows/pr-review.yml`](.github/workflows/pr-review.yml) を参照してください。

</details>

---

## ベンチマーク

6 リポジトリにおける 1 質問あたりのトークン削減の中央値は約 **63 倍**です（コーパス全体のベースライン対グラフ照会）。**358 倍**という最大値は 1 つのリポジトリ（fastapi、最大のコーパス）のもので、典型的な結果ではありません。

<details>
<summary><strong>ベンチマークの一覧図と、数字の出し方</strong></summary>
<br>

<p align="center">
  <img src="diagrams/diagram5_benchmark_board.png" alt="6 リポジトリのベンチマーク: 1 質問あたりのトークン削減の中央値は約 63 倍（最大 358 倍）、グラフ由来の正解に対する影響分析の平均 F1 は 0.69" width="85%" />
</p>

数字はすべて、6 つのオープンソースリポジトリ（13 コミット）に対する評価ランナーの出力です。各設定は上流の SHA を固定し、Leiden は固定シードで動き、埋め込みは CPU 上で決定的なので、別のマシンで 2 回実行しても同じ数字になります。再現手順は [`docs/REPRODUCING.md`](docs/REPRODUCING.md) にあります。最小の 2 設定に対する週次のレポート専用実行は [`.github/workflows/eval.yml`](.github/workflows/eval.yml) にあります。

</details>

<details>
<summary><strong>トークン効率: 1 質問あたりの削減は中央値で約 63 倍（範囲は 35 倍から 358 倍。コーパス全体対グラフ照会）</strong></summary>
<br>

典型的なエージェントの質問（`"how does authentication work"`、`"what is the main entry point"` など）に対して、グラフはすべてのソースファイルではなく、検索ヒットと近傍エッジからなる約 2,200 から 3,900 トークンを返します。表は `code_review_graph/token_benchmark.py` で定義された 5 つのサンプル質問を平均したものです。

| リポジトリ | スナップショット SHA | naive_corpus_tokens | avg graph_tokens | 削減率 |
|------|---|-----------------:|----------------:|----------:|
| fastapi | `22381558` | 948,793 | 2,653 | **357.6x** |
| flask | `a29f88ce` | 143,594 | 2,196 | **65.4x** |
| code-review-graph | `84bde354` | 208,821 | 3,190 | **65.5x** |
| gin | `5c00df8a` | 166,868 | 2,766 | **60.3x** |
| httpx | `b55d4635` | 142,356 | 2,661 | **53.5x** |
| express | `b4ab7d65` | 136,052 | 3,936 | **34.6x** |

> 2026-08-02 に、固定した SHA のクリーンなクローンから取得しました（crg 2.3.7、ローカルの `all-MiniLM-L6-v2` 埋め込み）。これらの数字は、置き換えた 2026-05-25 の計測より低くなっています。ノードの埋め込みテキストが充実したため、どのリポジトリでも `avg graph_tokens` が増えました。fastapi は引退した `0227991a` ではなく、現在のピン `22381558` で計測しています。
>
> 削減率の列は `naive_corpus_tokens / avg graph_tokens` なので、隣の 2 列から割り算で求まります。ベンチマーク自身の `average_reduction_ratio` は質問ごとの比率を平均したもので、必ずこれより大きな値になります。質問ごとの数字は [`docs/REPRODUCING.md`](docs/REPRODUCING.md#standalone-token-benchmark-code_review_graphtoken_benchmarkpy) にあります。
>
> `code-review-graph` の行はスナップショットであり、現在の計測値ではありません。`84bde354` 以降リポジトリは大きくなっており、コーパスもグラフも今日ではかなり大きくなっています。

コーパス全体のベースラインは、実際のエージェントが払うことのない上限です。エージェントは識別子を grep して、いちばん合致するファイルを読みます。`agent_baseline` の評価ベンチマークはその場合を測ります（純 Python でコーパスを grep し、一致数の多い上位 3 ファイルを取り、グラフ照会のコストとトークン数を比較）。この現実的なベースラインに対する中央値は、6 リポジトリ 18 問で **6.65x** です（平均 14.8x、範囲 4.2x〜72.6x）。2026-09-22 の計測は、検索とこのベースラインがテストファイルの順位を下げるようになる前のものです。計測し直すまで、`code-review-graph eval --benchmark agent_baseline --embed` の結果はこの数字と一致しません。完全な表は `docs/REPRODUCING.md` の「Canonical capture, 2026-09-22」にあります。

この比はリポジトリの規模に追随します。グラフの応答は一定のまま（今回計測したすべてのリポジトリで 2,432〜4,021 トークン）である一方、grep のベースラインはコーパスとともに増えるためです。express では 4.5x、fastapi では 52x でした。どちらの数字も**検索**のコスト、つまり答えを見つけるのに必要なトークンを測っています。グラフが示したファイルをエージェントがそのうえで開くなら、グラフの分は置き換えではなく上乗せになります。`semantic_search_nodes_tool(include_source=True)` は位置とともにコードを返すのでその必要がなくなりますが、この経路を端から端まで計測することは今後の課題です。

正式な `token_efficiency` ベンチマークは別の状況を測ります。コミットの変更ファイルの中身だけに対して `get_review_context()` の JSON 全体を比較するもので、応答には影響範囲のエッジとソース断片が入るため、小さなコミットでは比率が 1 を下回ります。2 つのベンチマークは別の問いに答えています。[`docs/REPRODUCING.md`](docs/REPRODUCING.md#which-benchmark-measures-what) を参照してください。

レビューと影響分析のツールは、応答に小さな `context_savings` の推定値を付けます。CLI は同じ数字を `Token Savings` パネルに表示し（後述の使い方を参照）、`--verify` は OpenAI の `cl100k_base` トークナイザと突き合わせます。222 のサンプルファイルでの較正では、集計値で見た推定値は実トークンとの差が約 1% 以内でした（[データ](docs/REPRODUCING.md#calibration-table)）。

</details>

<details>
<summary><strong>影響分析の精度: グラフ由来の正解に対する平均 F1 は 0.69（再現率 1.0 は循環した上限）</strong></summary>
<br>

影響範囲の分析は、評価対象の 13 コミットすべてで正解のファイルをすべて拾いました。これは「再現率 100%」ではなく上限として読んでください。正解（変更ファイルと、そこへ呼び出しエッジやインポートエッジを持つファイル）は、予測器が辿るのと同じグラフから作られています。適合率が低いのは意図的です。余分に 1 ファイル挙げるコストは、壊れた依存を見落とすコストより小さいからです。

| リポジトリ | コミット数 | 平均 F1 | 平均適合率 | 再現率（グラフ由来の上限） |
|------|--------:|-------:|--------------:|-------:|
| httpx | 2 | 0.863 | 0.785 | 1.0 |
| code-review-graph | 2 | 0.734 | 0.584 | 1.0 |
| fastapi | 2 | 0.697 | 0.539 | 1.0 |
| express | 2 | 0.667 | 0.500 | 1.0 |
| flask | 2 | 0.633 | 0.485 | 1.0 |
| gin | 3 | 0.609 | 0.439 | 1.0 |
| **平均** | **13** | **0.693** | **0.546** | **1.000** |

この表は 2026-08-02 の計測で、後述するパスの修正より前に取ったものです。そのバグはグラフ由来の正解からも本来の近傍ファイルを落としていたため、ベンチマークを再実行するとこれらの数字は変わります。

このベンチマークは**共変更モード**でも動きます。予測器に変更ファイルを 1 つ与え、同じコミットで作者が触れたほかのファイルで採点するもので、根拠はグラフではなく git の履歴です。どちらのモードも結果 CSV に出ます（`ground_truth_mode` 列）。2026-08-02 の計測では、共変更モードは採点対象のどのコミットでも `predicted_files = 0` を返しました。コードのシード（たとえば `code_review_graph/cli.py` や gin の `context.go`）でも同じです。原因はベンチマークのパスのバグでした。`git diff` はリポジトリからの相対パスを返し、グラフは絶対パスを保存するため、シードがどのノードとも一致しなかったのです。このバグは修正済みですが、ベンチマークはまだ再実行していないので、共変更の数字は引用していません。

</details>

<details>
<summary><strong>ビルド統計</strong></summary>
<br>

上記の固定 SHA における、同じ 2026-08-02 のクリーンビルドから取りました。File ノードは埋め込まないため、埋め込み数はノード数より少なくなります。`code-review-graph` の行はそのスナップショットであり、今のリポジトリの姿ではありません。

| リポジトリ | ノード | エッジ | 埋め込み |
|------|------:|------:|-----------:|
| fastapi | 6,287 | 32,036 | 5,159 |
| express | 1,990 | 19,492 | 1,849 |
| gin | 1,589 | 17,237 | 1,491 |
| code-review-graph | 1,446 | 9,094 | 1,354 |
| flask | 1,415 | 8,259 | 1,329 |
| httpx | 1,263 | 8,236 | 1,193 |

</details>

### 制限

<details>
<summary><strong>既知の 5 つの制限: 循環した再現率、小さな編集、検索の順位付け、フロー検出、偽陽性</strong></summary>
<br>

- **影響分析の「再現率 1.0」は循環している。** 過去の正解は予測器が辿るのと同じグラフのエッジから作られているので、構造上それは上限です。共変更の数字は、パスの修正を入れてベンチマークを再実行するまで引用しません。
- **小さな単一ファイルの変更。** ささいな編集では、グラフの文脈のほうが素のファイル読み込みより大きくなることがあります。その差は、複数ファイルの分析を可能にしている構造メタデータです。
- **検索の順位付け。** キーワード検索はたいてい正しい結果を上位に出しますが、順位付けには改善が必要です。モジュールパターンの命名のため、Express への問い合わせはヒットなしになることがあります。
- **フロー検出。** エントリポイントの検出は Python と PHP/Laravel で最も強力です。JavaScript と Go のフロー検出には改善が必要です。
- **適合率と再現率。** 影響分析は保守的です。影響を受けうるファイルを挙げるため、大きな依存グラフでは偽陽性が出ます。

</details>

---

## 機能

更新、影響分析、検索、可視化、CI でのレビュー、リファクタリング、マルチリポジトリ対応。すべて手元のマシンで完結します。

<details>
<summary><strong>機能の一覧表</strong></summary>
<br>

| 機能 | 内容 |
|---------|---------|
| **増分更新** | ハッシュが変わったファイルだけを再解析します。約 3,000 ファイルのリポジトリでは、2 ファイルの編集がフックの経路で約 2.5 秒です（[計測値](docs/REPRODUCING.md#incremental-update-latency)）。 |
| **言語とノートブックの対応** | 上記の[対応言語](#対応言語とノートブック)を参照。 |
| **フレームワークを理解する PHP 解析** | リポジトリ内に閉じた Composer PSR-4 のインポート、Blade テンプレートの参照、根拠に基づく Laravel Route からコントローラへのエッジと Eloquent のリレーションのエッジ |
| **フレームワークを理解する Java 解析** | Spring の依存性注入による呼び出し解決、リクエストエンドポイントと WebFlux ルート、スケジュールトリガー、アプリケーションイベントの発行側からリスナーへのエッジ、Spring AOP のアドバイスのエッジ、Temporal のワークフローとアクティビティのエッジ、そして値を含めずにインデックスする Spring Boot の設定キー |
| **影響範囲の分析** | 変更によって影響を受けそうな関数、クラス、ファイル |
| **自動更新フック** | エディタのフック、git のフック（コミットの前と、チェックアウト、マージ、リベースの後）、watch モードが作業中にグラフを更新 |
| **セマンティック検索** | 任意のベクトル埋め込み。sentence-transformers、Google Gemini、MiniMax、Voyage AI、あるいは任意の OpenAI 互換エンドポイント |
| **対話的な可視化** | D3.js の力学モデルグラフ。検索、コミュニティ凡例の切り替え、次数に応じたノードサイズ。加えて、シンボル、ファイル、フロー、変更を起点にした近傍ビュー |
| **ハブとブリッジの検出** | 最も多く接続されたノードとボトルネック（媒介中心性） |
| **意外度スコア** | 想定外の結合: コミュニティをまたぐ、言語をまたぐ、周辺からハブへのエッジ |
| **知識のギャップ分析** | 孤立ノード、テストのないホットスポット、薄いコミュニティ、インポートの循環 |
| **推奨質問** | ブリッジ、ハブ、意外な結合から生成したレビュー用の質問 |
| **エッジの信頼度** | 4 段階（EXTRACTED、INFERRED、AMBIGUOUS、UNRESOLVED）とエッジ上の浮動小数点スコア。グラフが結び付けられなかった呼び出しはそうと分かる |
| **グラフ探索** | 任意のノードからの BFS/DFS。深さとトークン予算を設定可能。`path_to` はあるシンボルが別のシンボルにどう到達するかを示し、最も弱いホップを挙げる |
| **エクスポート形式** | GraphML、Neo4j Cypher、Obsidian vault、JSON、そして SVG（SVG には `eval` エクストラの matplotlib が必要） |
| **トークンのベンチマーク** | `code_review_graph/token_benchmark.py` が、質問ごとにコーパス全体のトークンとグラフ照会のトークンを計測 |
| **文脈削減量の推定** | レビュー、影響分析、detect-changes、アーキテクチャの応答に付く `context_savings` メタデータ（`estimated`、`saved_tokens`、`saved_percent`） |
| **コミュニティの自動分割** | グラフの 25% を超えるコミュニティを Leiden で再帰的に分割 |
| **実行フロー** | エントリポイントからの呼び出し連鎖を、重み付けした重要度で並べ替え |
| **コミュニティ検出** | グラフ規模に応じて解像度を調整する Leiden クラスタリング |
| **アーキテクチャ概要** | コミュニティ構造に基づくアーキテクチャ図と結合の警告 |
| **リスクスコア付きレビュー** | `detect_changes` が差分を、影響を受ける関数、YAML と JSON のパス、フロー、テストの穴に対応づけ |
| **カスタム言語** | `.code-review-graph/languages.toml` で新しい言語を追加。フォーク不要 |
| **GitHub Action** | CI でのリスクスコア付き PR レビューの固定コメント。任意の `fail-on-risk` マージ関門つき |
| **リファクタリング支援** | リネームのプレビュー、フレームワークを理解するデッドコード検出、コミュニティに基づく提案 |
| **Wiki 生成** | コミュニティ構造から Markdown の Wiki を生成 |
| **マルチリポジトリのレジストリ** | 複数のリポジトリを登録し、横断して検索 |
| **マルチリポジトリのデーモン** | `crg-daemon` が複数のリポジトリを子プロセスで監視。ヘルスチェックと再起動つき |
| **MCP プロンプト** | 5 つのワークフローテンプレート: レビュー、アーキテクチャ、デバッグ、オンボーディング、マージ前チェック |
| **全文検索** | キーワードとベクトル類似度を組み合わせた FTS5 のハイブリッド検索 |
| **ローカル保存** | `.code-review-graph/` の SQLite ファイル 1 つ。外部データベースもクラウドサービスも不要 |

</details>

---

## 使い方

グラフを構築すれば、アシスタントは自分でそれを使います。スキル、コマンド、MCP ツール、設定については、下の各項目を開いてください。

<details>
<summary><strong>スキル</strong></summary>
<br>

`install` は、設定するプラットフォームのうちスキルに対応したもの（Claude Code、Gemini CLI、CodeBuddy Code、Hermes Agent、Qoder）向けに次の 4 つのスキルを書き込みます。名前で呼び出してください。

| スキル | 内容 |
|-------|-------------|
| `explore-codebase` | 知識グラフを使ってコードベースの構造を辿り、理解する |
| `review-changes` | 変更検出と影響分析で、構造化したコードレビューを行う |
| `debug-issue` | グラフによるコード探索で、体系的に不具合を追う |
| `refactor-safely` | 依存関係の分析で、安全なリファクタリングを計画して実行する |

Qoder にはさらに、このリポジトリの `skills/` ディレクトリから `build-graph`、`review-delta`、`review-pr` が入ります。

</details>

<details>
<summary><strong>CLI リファレンス</strong></summary>
<br>

```bash
code-review-graph install          # Set up the AI coding tools found on this machine
code-review-graph install --platform <name>  # One platform
code-review-graph install --steering  # Claude Code: graph context beside each search and read
code-review-graph uninstall --dry-run  # Preview removal of installed artifacts
code-review-graph doctor           # Check copies, MCP entries, graph and hooks; changes nothing
code-review-graph build            # Parse the whole codebase
code-review-graph build --follow-symlinks  # Also index symlinked folders
code-review-graph update           # Incremental update (changed files only)
code-review-graph status           # Graph statistics
code-review-graph watch            # Update on file changes
code-review-graph forget <path>    # Drop already-parsed files from the graph
code-review-graph dead-code        # Functions and classes with no callers or tests
code-review-graph query path_to <from> --to <to>  # How one symbol reaches another
code-review-graph impact --detail-level compact  # Blast radius as ranked file paths
code-review-graph visualize        # Interactive HTML graph
code-review-graph visualize --seed-symbol <name>  # Neighbourhood of one symbol
code-review-graph visualize --format json      # Export graph data as JSON
code-review-graph visualize --format graphml   # Export as GraphML
code-review-graph visualize --format svg       # Export as SVG (needs matplotlib)
code-review-graph visualize --format obsidian  # Export as Obsidian vault
code-review-graph visualize --format cypher    # Export as Neo4j Cypher
code-review-graph wiki             # Markdown wiki from communities
code-review-graph detect-changes --brief         # Top 3 to review, risk + token savings (read-only)
code-review-graph detect-changes --brief --base main  # Against the merge base of main and HEAD
code-review-graph update --brief                 # Refresh graph + same panel
code-review-graph detect-changes --brief --verify  # Cross-check against tiktoken
code-review-graph detect-changes --details all  # Add graph context to the JSON
code-review-graph context-history  # Trend of the opt-in savings log (CRG_CONTEXT_HISTORY=1)
code-review-graph register <path>  # Register repo in the multi-repo registry
code-review-graph unregister <path|alias>  # Remove repo from the registry
code-review-graph repos            # List registered repositories
code-review-graph daemon start     # Start the multi-repo watch daemon
code-review-graph daemon stop      # Stop the daemon
code-review-graph daemon status    # Daemon status and repos
code-review-graph eval --all       # Run every evaluation benchmark
code-review-graph serve            # Start the MCP server (stdio)
code-review-graph serve --http     # MCP over Streamable HTTP on localhost:5555
code-review-graph serve --http --multi-repo  # One HTTP server for every registered repo
code-review-graph serve --multi-worktree  # One child server per linked Git worktree
```

これは抜粋です。コマンドを付けずに `code-review-graph` を実行すると、すべてのコマンドがグループごとに並び、[docs/COMMANDS.md](docs/COMMANDS.md) がそれぞれのフラグを説明します。コマンドを打ち間違えると終了コード 2 で終わり、いちばん近いコマンドを示します。

`detect-changes --base` にブランチ名を渡すと、差分はそのブランチと HEAD のマージベースに対して取られます。コミットハッシュやその他のリビジョンはそのまま使われます。git が知らないベースを渡すと、終了コード 1 で止まり、確かめ方を示します。コミットが 1 つしかない、または 1 つもないリポジトリでは、既定のベースの代わりに、コミットしていないファイルと新しいファイルをレビューし、そのことを表示します。

`visualize --format svg` には matplotlib が必要で、これは `eval` エクストラに入っています（`pip install "code-review-graph[eval]"`）。ほかのエクスポート形式に追加インストールは要りません。

JSON のエクスポートはローカルのグラフデータディレクトリに書かれ、そのディレクトリは既定で Git の対象外です。絶対パスやコード構造のメタデータを含みうるので、公開する前に中身を確認してください。

</details>

<details>
<summary><strong>Token Savings パネル: <code>detect-changes --brief</code> と <code>update --brief</code></strong></summary>
<br>

どちらのコマンドも、変更ファイルをそのままエージェントに渡す場合と比べてグラフがどれだけトークンを節約したかを示す同じパネルを表示します。違いは 1 点だけ、先にグラフを更新するかどうかです。パネルの上には、`detect-changes --brief` が先にレビューすべきものを最大 3 つ並べます。それぞれにファイルと行、1.00 を満点とするリスク、呼び出し元、テストが付きます。

```text
┌─────────────────────── Token Savings ────────────────────────┐
│ Full context would be:     12,921 tokens                     │
│ Graph context used:           762 tokens                     │
│ Saved:                     12,159 tokens (~94%)              │
│ Breakdown: Functions 244 · Tests 191 · Risk 244 · Other 83   │
└──────────────────────────────────────────────────────────────┘
```

| コマンド | 動作 | 使いどころ |
|---|---|---|
| `detect-changes --brief` | 読み取り専用。現在の変更について既存のグラフに問い合わせ、パネルを表示します。 | ふだんはこちら。フックや `crg-daemon` がグラフを新しく保ちます。 |
| `update --brief` | 先に変更ファイルをグラフへ再解析してから、同じパネルを表示します。 | リベース後、変更が大きいとき、グラフが古いかもしれないとき。 |

どちらのコマンドにも `--verify` を付けると、数字を OpenAI の `cl100k_base` トークナイザと比べられます（`pip install tiktoken` が必要）。集計値で見た推定値は実トークンとの差が約 1% 以内です。[`docs/REPRODUCING.md`](docs/REPRODUCING.md#calibration-table) を参照してください。

同じ `context_savings` メタデータは、`get_impact_radius`、`get_review_context`、`detect_changes`、`get_architecture_overview` の各 MCP ツールの JSON 応答にも付きます。

推定値の推移を見るには `CRG_CONTEXT_HISTORY=1` を設定します（既定ではオフ）。推定値ごとに `graph.db` の隣のローカルログへ 1 行追加されます。内容はトークン数、ツール名、タイムスタンプ、詳細レベル、ファイル数と深さで、ファイル名、パス、ソースコードは含みません。`code-review-graph context-history` で日別またはツール別に表示できます。[docs/USAGE.md](docs/USAGE.md#context-history) を参照してください。

</details>

<details>
<summary><strong>マルチリポジトリのデーモン</strong></summary>
<br>

`install` がエディタにフックを入れない場合（Windsurf や Zed など）や、エディタ連携なしでグラフを新しく保ちたい場合は、デーモンがリポジトリを監視してグラフを更新します。`code-review-graph` に同梱されているので、別途インストールは要りません。

```bash
# 1. Register the repos to watch
crg-daemon add ~/project-a --alias proj-a
crg-daemon add ~/project-b

# 2. Start the daemon (runs in the background)
crg-daemon start

# 3. Check on it
crg-daemon status                 # daemon and per-repo watcher status
crg-daemon logs --repo proj-a -f  # tail logs for one repo
crg-daemon stop                   # stop the daemon and all watchers
```

`code-review-graph daemon start|stop|status|...` としても使えます。

`crg-daemon add` は `~/.code-review-graph/watch.toml` に書き込みます。このファイルは直接編集してもかまいません。

```toml
[[repos]]
path = "/home/user/project-a"
alias = "proj-a"

[[repos]]
path = "/home/user/project-b"
alias = "project-b"
```

デーモンはこのファイルを監視し、リポジトリの追加や削除に応じて watcher のプロセスを起動・停止します。30 秒ごとのヘルスチェックが、落ちた watcher を再起動します。

設定の完全なリファレンスは [docs/COMMANDS.md](docs/COMMANDS.md#standalone-daemon-cli-crg-daemon) を参照してください。

</details>

<details>
<summary><strong>30 個の MCP ツール</strong></summary>
<br>

グラフを構築すると、アシスタントはこれらを使います。読むだけの 25 個のツールには `readOnlyHint: true` が付いているので、クライアントは確認なしで実行できます。書き込みを行う 5 個（`build_or_update_graph_tool`、`run_postprocess_tool`、`embed_graph_tool`、`apply_refactor_tool`、`generate_wiki_tool`）には付いていません。

| ツール | 内容 |
|------|-------------|
| `build_or_update_graph_tool` | グラフの構築または増分更新 |
| `run_postprocess_tool` | フロー検出、コミュニティ検出、FTS インデックスの再実行 |
| `get_minimal_context_tool` | 小さな文脈（約 100 トークン）。最初にこれを呼ぶ |
| `get_impact_radius_tool` | 変更ファイルの影響範囲。`detail_level="compact"` で順位付きのファイルパスを返す |
| `get_review_context_tool` | 構造の要約つきレビュー文脈 |
| `query_graph_tool` | 呼び出し元、呼び出し先、テスト、インポート、継承、そして 2 つのシンボル間の `path_to` の照会 |
| `traverse_graph_tool` | 任意のノードからの BFS/DFS 探索。トークン予算つき |
| `semantic_search_nodes_tool` | 名前や意味でコードの実体を検索 |
| `embed_graph_tool` | セマンティック検索用のベクトル埋め込みを計算 |
| `list_graph_stats_tool` | グラフの規模と健全性 |
| `get_docs_section_tool` | ドキュメントの節を取得 |
| `find_large_functions_tool` | 行数のしきい値を超える関数、クラス、ファイル |
| `list_flows_tool` | 重要度順に並べた実行フロー |
| `get_flow_tool` | 1 つの実行フロー |
| `get_affected_flows_tool` | 変更ファイルが影響するフロー |
| `list_communities_tool` | 検出されたコードのコミュニティ |
| `get_community_tool` | 1 つのコミュニティ |
| `get_architecture_overview_tool` | コミュニティ構造によるアーキテクチャ概要 |
| `detect_changes_tool` | リスクスコア付きの変更影響分析 |
| `get_hub_nodes_tool` | 最も多く接続されたノード |
| `get_bridge_nodes_tool` | 媒介中心性によるボトルネック |
| `get_knowledge_gaps_tool` | 構造上の弱点、テストのないホットスポット、インポートの循環 |
| `get_surprising_connections_tool` | 想定外のコミュニティ間の結合 |
| `get_suggested_questions_tool` | 分析から生成したレビュー用の質問 |
| `refactor_tool` | リネームのプレビュー、デッドコード検出、提案 |
| `apply_refactor_tool` | プレビュー済みのリファクタリングを適用 |
| `generate_wiki_tool` | コミュニティから Markdown の Wiki を生成 |
| `get_wiki_page_tool` | 1 つの Wiki ページ |
| `list_repos_tool` | 登録済みのリポジトリ |
| `cross_repo_search_tool` | 登録済みのリポジトリを検索。`repos` で対象を絞り込める |

**MCP プロンプト**（5 つのワークフローテンプレート）:
`review_changes`、`architecture_map`、`debug_issue`、`onboard_developer`、`pre_merge_check`

</details>

<details>
<summary><strong>設定</strong></summary>
<br>

インデックスから除外したいパスがある場合は、リポジトリのルートに `.code-review-graphignore` を作ります。

```
generated/**
*.generated.ts
vendor/**
node_modules/**
```

git リポジトリでは、ビルドは追跡されているファイルと、git が無視しない未追跡のファイルを読むので（`git ls-files` と `git ls-files --others --exclude-standard`）、gitignore されたファイルは飛ばされます。ほかのファイルを除外したいとき、あるいは git が使えないときは `.code-review-graphignore` を使ってください。既定の除外リストは [docs/USAGE.md](docs/USAGE.md#ignore-patterns) にあります。

任意の依存グループ:

```bash
pip install "code-review-graph[embeddings]"          # Local vector embeddings (sentence-transformers)
pip install "code-review-graph[google-embeddings]"   # Google Gemini embeddings
pip install "code-review-graph[communities]"         # Community detection (igraph)
pip install "code-review-graph[enrichment]"          # Python call resolution with Jedi (then build --enrich)
pip install "code-review-graph[eval]"                # Evaluation benchmarks and SVG export (matplotlib)
pip install "code-review-graph[wiki]"                # ollama client (not used by the current wiki generator)
pip install "code-review-graph[all]"                 # All optional dependencies
```

### 共有レジストリの整合性

並行プロセスは同じロックを保持してレジストリを読み直し、不可分に置き換えます。不正なレジストリはエラーになり、各作業ツリーは独自のグラフを持ちます。

### 環境変数

| 変数 | 内容 | 既定値 |
|----------|-------------|---------|
| `CRG_GIT_TIMEOUT` | Git 操作のタイムアウト（秒。build / update / watch） | `30` |
| `CRG_DISCOVERY_TIMEOUT` | レビューのツールやコマンドにファイル一覧が明示されなかったとき、変更内容を特定する各 Git コマンドのタイムアウト（秒）。超過時はエラーを返し、「変更なし」とは報告しない | `5`（`CRG_GIT_TIMEOUT` を明示設定した場合はその値） |
| `CRG_DATA_DIR` | グラフのデータベースと生成物を置くディレクトリ。`install` が書いたファイルの記録は、リポジトリの `.code-review-graph/` に残ります | - |
| `CRG_HOME` | ユーザーごとの状態を置くフォルダ: マルチリポジトリの登録簿（`registry.json`）、デーモンの `watch.toml`、その pid と状態のファイル、`logs/` | `~/.code-review-graph` |
| `CRG_HOOK_WORKTREES` | `1` にすると、生成された git フックをリンク済みの git worktree でも動かす | - |
| `CRG_EMBEDDING_MODEL` | ローカルのベクトル埋め込みの既定モデル | `all-MiniLM-L6-v2` |
| `CRG_ACCEPT_CLOUD_EMBEDDINGS` | `1` にするとクラウド埋め込みの送信警告を抑制 | - |
| `CRG_ALLOW_REMOTE_CODE` | `trust_remote_code=True` を要する HuggingFace モデルを許可 | `0` |
| `CRG_VECTOR_CACHE` | `1` にすると、デコード済みの埋め込みベクトルをセマンティック検索の合間もメモリに保持し、クエリごとに SQLite からすべて読み直さない。代わりに RAM（キャッシュするインデックスごとに ベクトル数 × 次元数 × 4 バイト）と、キャッシュ対象のデータベースごとにプロセス終了まで開いたままの読み取り接続 1 本を使う。Windows では開いている間 `graph.db` を削除・置換できない | - |
| `CRG_MAX_IMPACT_NODES` | 影響分析に含める最大ノード数 | `500` |
| `CRG_MAX_IMPACT_DEPTH` | 影響範囲分析の探索の深さ | `2` |
| `CRG_MAX_BFS_DEPTH` | グラフ探索の最大の深さ | `15` |
| `CRG_MAX_CHANGED_FUNCS` | 1 回の変更レポートで分析する変更関数の最大数 | - |
| `CRG_MAX_DELETION_FILES` | 差分で削除された関数とクラスを挙げるために、1 回の変更レポートで旧版を読むファイルの最大数 | `100` |
| `CRG_MAX_CHANGED_YAML_PATHS` | 1 回の変更レポートで分析する変更 YAML パスの最大数。JSON パスにも別に同じ上限がかかる | - |
| `CRG_MAX_TRANSITIVE_FRONTIER` | `tests_for` と `detect-changes` がテストを探すとき、1 ホップでたどる呼び出し先の最大数。名前順で先頭のものを残すので、同じグラフからは常に同じ答えが出る | `50` |
| `CRG_CALLER_TEST_ROUTE_DEPTH` | テストのない変更シンボルについて、テスト済みの呼び出し元を探して `CALLS` を遡るホップ数。カバレッジではなく注記として報告されます | `2` |
| `CRG_CALLER_TEST_ROUTE_MAX_CALLERS` | そのテスト済み呼び出し元を探す際に調べる呼び出し元の最大数 | `500` |
| `CRG_TOOL_TIMEOUT` | 読み取り専用 MCP ツールのタイムアウト（秒。`0` で無効）。書き込みを行うツール（build / postprocess / embed / wiki / apply-refactor）には適用されない | `0` |
| `CRG_CHURN_WINDOW_DAYS` | `detect-changes --churn` のコミット数を数える期間 | `90` |
| `CRG_CONTEXT_HISTORY` | `1` にすると、`context_savings` の推定値を毎回ローカルログ `.code-review-graph/context-history.jsonl` に記録します。`code-review-graph context-history` で表示します。記録するのはトークン数、ツール名、タイムスタンプ、詳細レベル、ファイル数と深さで、ファイル名、パス、ソースコードは記録せず、外部にも送信しません | - |
| `CRG_LEIDEN_SEED` | Leiden のコミュニティ検出のシード | `42` |
| `CRG_RECURSE_SUBMODULES` | `1`、`true`、`yes` のとき git サブモジュールを含める | - |
| `CRG_FOLLOW_SYMLINKS` | `1`、`true`、`yes`、`on` のとき、シンボリックリンクのフォルダ（リポジトリの内外）をたどる。`--follow-symlinks` と同じ。[シンボリックリンクのフォルダ](docs/USAGE.md#symlinked-folders) を参照 | - |
| `CRG_MAX_FOLLOWED_FILES` | シンボリックリンクのフォルダから取り込むファイルの上限 | `10000` |
| `CRG_TOOLS` | サーバー起動時に公開する MCP ツールのカンマ区切り許可リスト | - |
| `CRG_READ_ONLY` | `1`（または `true`、`yes`、`on`）にすると、グラフを読み取り専用で開きます。データフォルダでは何も作らず、移行も記録もせず、書き込むコマンドは失敗します。Codex スキルの読み取り専用 CLI がこれを設定します | - |
| `CRG_BIN` | Codex スキルの読み取り専用 CLI が実行する `code-review-graph` のプログラム。名前が違う場合や PATH にない場合に使います | PATH 上のもの |
| `GOOGLE_API_KEY` | Google Gemini 埋め込みの API キー | - |
| `MINIMAX_API_KEY` | MiniMax 埋め込みの API キー | - |
| `VOYAGE_API_KEY` | Voyage 埋め込みの API キー | - |
| `CRG_VOYAGE_MODEL` | Voyage 埋め込みのモデル | `voyage-code-3` |
| `CRG_VOYAGE_OUTPUT_DIMENSION` | Voyage 埋め込みの出力次元 | `1024` |
| `CRG_VOYAGE_OUTPUT_DTYPE` | Voyage 埋め込みの出力 dtype | `float` |
| `CRG_VOYAGE_BASE_URL` | Voyage 埋め込みのエンドポイント | `https://api.voyageai.com/v1` |
| `CRG_VOYAGE_BATCH_SIZE` | Voyage へのリクエストのバッチサイズ | `100` |
| `CRG_VOYAGE_MIN_INTERVAL_SEC` | Voyage へのリクエストの最小間隔 | `0` |
| `CRG_OPENAI_BASE_URL` | OpenAI 互換の埋め込みエンドポイント | - |
| `CRG_OPENAI_API_KEY` | OpenAI 互換の埋め込みの API キー | - |
| `CRG_OPENAI_MODEL` | OpenAI 互換の埋め込みのモデル | - |
| `CRG_OPENAI_DIMENSION` | 埋め込みの次元を固定（v3 系モデルは次元削減に対応） | - |
| `CRG_OPENAI_BATCH_SIZE` | OpenAI 互換のリクエストのバッチサイズ | `100` |
| `NO_COLOR` | 端末の ANSI 色を無効化 | - |
| `CRG_SERIAL_PARSE` | `1` にすると並列解析を無効化（デバッグ用） | - |
| `CRG_ENABLE_ENRICHMENT` | `1` にすると毎回の build と update で Jedi により Python のメソッド呼び出しを解決（`enrichment` エクストラが必要、Python ファイル 1 つにつき約 1 秒増）。`build --enrich` と同じ | オフ |
| `CRG_DISABLE_ENRICHMENT` | `1` にすると要求があっても Jedi を実行しない | - |

OpenAI 互換の埋め込みに追加インストールは要りません。変数を設定し、`embed_graph` に `provider="openai"` を渡します。

```bash
export CRG_OPENAI_BASE_URL=http://127.0.0.1:3000/v1     # or https://api.openai.com/v1
export CRG_OPENAI_API_KEY=sk-...
export CRG_OPENAI_MODEL=text-embedding-3-small          # whatever your gateway serves
# optional:
export CRG_OPENAI_DIMENSION=1536                        # pin dim (v3 models support reduction)
export CRG_OPENAI_BATCH_SIZE=100                        # lower for gateways with tight limits
```

ベース URL が localhost（`127.0.0.1`、`localhost`、`0.0.0.0`、`::1`）を指している場合、クラウド送信の警告は出ません。

Voyage の埋め込みに追加インストールは要りません。`VOYAGE_API_KEY` を設定し、`embed_graph` に `provider="voyage"` を渡します。既定のモデルは `voyage-code-3` です。

```bash
export VOYAGE_API_KEY=pa-...
export CRG_ACCEPT_CLOUD_EMBEDDINGS=1
code-review-graph embed --provider voyage --model voyage-code-3
```

> **モデルの選び方。** 長く使うインデックスには `-preview`、`-beta`、`-exp` のモデル ID を避けてください。プレビュー版は重みが変わったり（次元が変わると全件の再埋め込みが必要）、取り下げられたりします。`text-embedding-3-small` / `text-embedding-3-large`（OpenAI）や `gemini-embedding-001`（`GOOGLE_API_KEY` が必要なネイティブの Gemini プロバイダ）といった正式リリースを選んでください。
>
> 埋め込むテキストは、識別子、シグネチャ、構造上の文脈、そして長さを制限した最初の段落の docstring かドキュメントコメントの要約です。関数の本体は送りません。ドキュメント抽出の追加前に作られたグラフは、再埋め込みの前に `code-review-graph build` を一度通す必要があります。通常のビルドでは埋め込みは更新されません。ビルド後に更新するには `--embedding-provider` と `--embedding-model` の両方を渡してください。クラウドのプロバイダはこのソース由来のテキストを受け取り、課金する場合があります。

#### ツールの絞り込み

CRG は既定で 30 個の MCP ツールを公開します。サーバーを一部に限定するには、`--tools` か `CRG_TOOLS` 環境変数を使います。

```bash
# CLI flag
code-review-graph serve --tools query_graph_tool,semantic_search_nodes_tool,detect_changes_tool

# Environment variable
CRG_TOOLS=query_graph_tool,semantic_search_nodes_tool code-review-graph serve
```

フラグは環境変数より優先されます。どちらも設定しなければ、すべてのツールが使えます。MCP クライアントの設定では次のようにします。

```json
{
  "mcpServers": {
    "code-review-graph": {
      "command": "code-review-graph",
      "args": ["serve", "--tools", "query_graph_tool,semantic_search_nodes_tool,detect_changes_tool,get_review_context_tool"]
    }
  }
}
```

</details>

### Docker または Podman で実行する

<details>
<summary><strong>リリースからイメージをビルドし、自分のリポジトリで実行する</strong></summary>
<br>

公開されたイメージはありません。リリースのタグからビルドしてください。普通にクローンすると `staging` ブランチになり、まだリリースされていない変更を含むことがあります。

```bash
git clone --branch v2.4.0 https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
docker build -t code-review-graph:local .
```

解析するリポジトリのディレクトリで、次を実行します。

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --volume "$PWD:/workspace" \
  code-review-graph:local build
```

マウントするリポジトリには書き込み権限が必要です。
上のコマンドはホストのユーザー ID とグループ ID を使用するため、
生成される `.code-review-graph/` ディレクトリの所有者は自分のユーザーになります。
これらのコマンドは、ホスト上の root 以外のユーザーで実行してください。

Podman では、同じクローンで同じ Dockerfile をビルドし、ユーザーマッピングを維持します。

```bash
podman build -t code-review-graph:local .
podman run --rm \
  --userns=keep-id \
  --env HOME=/tmp \
  --volume "$PWD:/workspace" \
  code-review-graph:local build
```

グラフはマウントしたリポジトリの `.code-review-graph/` に保存され、
`/workspace/app.py` などのコンテナ内の絶対パスを記録します。
`update` と `detect-changes` は、同じ `/workspace` マウントを使って Docker または Podman で実行してください。
このグラフに対してホストで `update` を実行すると、"built with a different repository root"
というエラーになる場合があります。ホストのコマンドに切り替えるには、ホストで
`code-review-graph build` を実行し、ホストのパスでグラフを再構築してください。

保存されたグラフを確認するには、`build` を `status` に置き換えます。
イメージには Git が含まれ、デフォルトでは root 以外のユーザーで実行されます。
イメージのビルド中にグラフは生成されません。

これらの例は macOS/Linux のシェル構文を使用しています。
macOS では、実行前に Docker 互換エンジンまたは Podman 仮想マシンを起動してください。
Fedora など SELinux が有効なホストでは、マウントに `:z` を追加してください（`"$PWD:/workspace:z"`）。追加しないと、コンテナはリポジトリにアクセスできません。

</details>

---

## FAQ と比較

答えは [docs/FAQ.md](docs/FAQ.md) にあります。

<details>
<summary><strong>LSP、RAG、grep との比較、使わないほうがよい場合など</strong></summary>
<br>

- [LSP / 言語サーバーとの違い](docs/FAQ.md#how-is-this-different-from-lsp-and-language-servers): 言語ごとのデーモンではなく、言語をまたぐ永続的なグラフが 1 つ。シンボル単位の正確さでは LSP が上。
- [RAG / 埋め込みとの違い](docs/FAQ.md#isnt-this-just-rag): 類似度でのチャンクではなく、AST から解析した構造的なエッジ。埋め込みは任意で、検索の補助にすぎません。
- [grep / エージェント検索との違い](docs/FAQ.md#why-not-just-grep): 1 ホップの探索なら grep。多ホップの問い（影響範囲、呼び出し元の呼び出し元、tests-for、影響を受けるフロー）ならグラフ。
- [使わないほうがよい場合](docs/FAQ.md#when-should-i-not-use-it): 小さなリポジトリ、ささいな単一ファイルの差分、1 回きりの質問。
- [外部に通信しますか？](docs/FAQ.md#does-it-phone-home): テレメトリはありません。クラウド埋め込みは明示的に有効にした場合だけ。
- [動いているかどう確かめますか？](docs/FAQ.md#how-do-i-verify-it-is-working): `status`、`detect-changes --brief`、`/mcp`。

</details>

## トラブルシューティング

まず `code-review-graph doctor` を実行してください。AI ツールがグラフについて "written by a newer code-review-graph" と言うときは特にです。PATH 上のすべてのコピー、各 MCP エントリと各フックが動かすバージョン、グラフの状態を示します。何も変えず、失敗したチェックがあれば終了コード 1 で終わり、`--json` も使えます。

Windows/WSL を含むより多くの事例は [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) にあります。

### `pip` / `pipx` が `hatchling` を取得できない（あるいは PyPI への `Errno 9` / `Bad file descriptor`）

<details>
<summary><strong>直し方</strong></summary>
<br>

ソースツリーからのインストール（たとえば `pipx install .`）には、PyPI からビルド依存を取る必要があります。接続の警告のあとに `Could not find a version that satisfies the requirement hatchling` が出る場合、その端末の Python が `pypi.org` への HTTPS 接続を開けていない可能性があります。エディタの統合ターミナルで最も多く、VPN、ファイアウォール、プロキシが原因のこともあります。

1. エディタの外のターミナル（Terminal.app など）から同じコマンドを実行する。
2. ダウンロードの仕組みが異なる [uv](https://docs.astral.sh/uv/) を使って、チェックアウトからインストールする。

   ```bash
   cd /path/to/code-review-graph
   uv tool install . --force
   ```

3. クローンでの開発には `uv sync` と `uv run code-review-graph ...` を使う。

切り分けには `python3 scripts/diagnose_pypi_connectivity.py` を実行します。`FAILED` と表示されたら、原因はパッケージ名ではなくネットワーク環境です。

</details>

### Windows: `Invalid JSON: EOF while parsing` または `MCP error -32000: Connection closed`

<details>
<summary><strong>直し方</strong></summary>
<br>

Claude Code の設定で `cmd /c` によるラッパーを使わないでください。`~/.claude.json` から `.exe` を直接指し、UTF-8 は設定で渡します。

```json
"code-review-graph": {
  "command": "C:\\path\\to\\your\\venv\\Scripts\\code-review-graph.exe",
  "args": ["serve", "--repo", "C:\\path\\to\\your\\project"],
  "env": { "PYTHONUTF8": "1" }
}
```

</details>

## コントリビュート

プルリクエストは `staging`（既定のブランチ）に出してください。変更は
`staging` → `testing` → `main` の順に進み、リリースは `main` からタグを打ちます。昇格は自動では行われず、毎回人がレビューしてマージします。全体の流れは
[CONTRIBUTING.md](CONTRIBUTING.md#branching-and-promotion) を参照してください。

<details>
<summary><strong>開発用のコピーを用意してテストを実行する</strong></summary>
<br>

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

</details>

<details>
<summary><strong>組み込みの言語を追加する</strong></summary>
<br>

組み込みの言語を追加するには `code_review_graph/parser.py` を編集します。拡張子を `EXTENSION_TO_LANGUAGE` に、ノード型の対応を `_CLASS_TYPES`、`_FUNCTION_TYPES`、`_IMPORT_TYPES`、`_CALL_TYPES` に追加してください。テスト用のフィクスチャを添えて PR を出してください。1 つのリポジトリだけで必要な言語なら、代わりに [`languages.toml`](docs/CUSTOM_LANGUAGES.md) を使ってください。

</details>

## ライセンス

MIT。[LICENSE](LICENSE) を参照してください。

<p align="center">
<br>
<a href="https://code-review-graph.com">code-review-graph.com</a><br><br>
<code>pip install code-review-graph && code-review-graph install</code>
</p>
