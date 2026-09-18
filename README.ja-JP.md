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
  <a href="docs/GITHUB_ACTION.md">GitHub Action</a> ·
  <a href="docs/REPRODUCING.md">ベンチマークの再現</a> ·
  <a href="docs/ROADMAP.md">ロードマップ</a>
</p>

<br>

AI コーディングツールは、変更をレビューするためにコードベースの大部分を読み直しがちです。`code-review-graph` は [Tree-sitter](https://tree-sitter.github.io/tree-sitter/) でコードの構造マップを作り、増分で更新し続け、[MCP](https://modelcontextprotocol.io/) 経由で小さな文脈を渡します。アシスタントは変更が触れるファイルだけを読みます。

<p align="center">
  <img src="diagrams/diagram1_before_vs_after.png" alt="トークンの問題: flask のコーパス全体を読むと 143,594 トークン、グラフの回答は 2,196 トークン（65 分の 1）" width="85%" />
</p>

---

## クイックスタート

```bash
pip install code-review-graph          # or: pipx install code-review-graph
code-review-graph install              # detect installed AI coding tools and configure each one
code-review-graph build                # parse the codebase
```

`install` は導入済みの AI コーディングツールを検出し、それぞれに MCP サーバーのエントリを書き込み、プラットフォームが対応していればフックとスキルを入れ、プラットフォームのルールファイルにグラフの説明を追加します。MCP エントリは、Poetry や uv のプロジェクト環境では `poetry run` または `uv run` を、PATH に `uvx` があれば `uvx code-review-graph serve` を、それ以外では現在の Python インタプリタを使います。実行後はエディタやツールを再起動してください。

<p align="center">
  <img src="diagrams/diagram8_supported_platforms.png" alt="一度のインストールで全プラットフォーム対応: Codex、Claude Code、CodeBuddy Code、Cursor、Windsurf、Zed、Continue、OpenCode、Antigravity、Gemini CLI、Qwen、Qoder、Kiro、GitHub Copilot、GitHub Copilot CLI、Hermes Agent を検出" width="85%" />
</p>

1 つのプラットフォームだけを設定するには、`--platform` に `codex`、`claude-code`、`cursor`、`windsurf`、`zed`、`continue`、`opencode`、`antigravity`、`gemini-cli`、`qwen`、`kiro`、`qoder`、`copilot`、`copilot-cli`、`codebuddy`、`hermes` のいずれかを渡します。

```bash
code-review-graph install --platform cursor
code-review-graph install --platform codebuddy
```

設定ファイルの場所は [docs/USAGE.md](docs/USAGE.md#supported-platforms) にあります。Python 3.10+ が必要です。

`uninstall` は Git または SVN の作業ツリーから CRG が書いたファイルとエントリを取り除き、ほかの MCP サーバー、フック、スキル、JSONC のコメントはそのまま残します。ツリー内のどこからでも実行できます。共有設定ファイルはアトミックに置き換えるので、書き込みに失敗しても元のファイルは壊れません。

```bash
code-review-graph uninstall --dry-run    # preview only
code-review-graph uninstall              # preview, confirm, apply
code-review-graph uninstall --yes        # apply without prompting
code-review-graph uninstall --all-repos  # also clean every registered repository
code-review-graph uninstall --keep-data  # remove integrations, keep graph databases
code-review-graph uninstall --keep-user-configs --repo .  # this project only
```

そのあとプロジェクトを開き、アシスタントにこう頼みます。

```
Build the code review graph for this project
```

ビルド時間はリポジトリの規模に比例します。約 3,000 ファイルのリポジトリのコールドビルドで約 40 秒でした（[計測値](docs/REPRODUCING.md#incremental-update-latency)）。その後はフックと watch モードがグラフを更新し続けます。一部のファイルの解析に失敗した場合、結果のステータスは `partial` になり、要約にそのファイル名が入ります。CLI は stderr に `Warning:` の行も出力し、それらのファイルは以前のグラフの行をそのまま保持します。


## 仕組み

<p align="center">
  <img src="diagrams/diagram7_mcp_integration_flow.png" alt="アシスタントがグラフを使う流れ: ユーザーがレビューを依頼し、アシスタントが MCP ツールを呼び、グラフが影響範囲とリスクスコアを返し、アシスタントは影響のあるファイルだけを読む" width="80%" />
</p>

リポジトリは Tree-sitter で AST に解析され、ノード（関数、クラス、インポート）とエッジ（呼び出し、継承、テストカバレッジ）のグラフとして保存されます。レビュー時には、アシスタントが読むべき最小のファイル集合をグラフに問い合わせます。

<p align="center">
  <img src="diagrams/diagram2_architecture_pipeline.png" alt="アーキテクチャのパイプライン: リポジトリ → Tree-sitter パーサー → SQLite グラフ → 影響範囲 → 最小レビュー集合" width="100%" />
</p>

### 影響範囲の分析

ファイルが変更されると、グラフは影響を受けうる呼び出し元、依存先、テストをすべて辿ります。アシスタントはプロジェクト全体を走査せず、それらのファイルを読みます。

<p align="center">
  <img src="diagrams/diagram3_blast_radius.png" alt="影響範囲: login() の変更が呼び出し元、依存先、テストへ波及する" width="70%" />
</p>

### 増分更新

フック、pre-commit フック、watch モードが増分更新を起動します。更新では変更ファイルの差分を取り、グラフのインポートエッジと呼び出しエッジから依存先を見つけ、SHA-256 ハッシュが変わったファイルだけを再解析します。約 3,000 ファイルのプロジェクト（django）では、2 ファイルの編集がフックの経路で約 2.5 秒で再インデックスされ、そのうち約 1.4 秒はプロセスの起動時間です。変更がない場合はその起動時間だけで済みます。[増分更新のレイテンシ](docs/REPRODUCING.md#incremental-update-latency)を参照してください。

<p align="center">
  <img src="diagrams/diagram4_incremental_update.png" alt="増分更新の流れ: フックまたは watch の更新が git diff を起動し、グラフのエッジから依存先を見つけ、SHA-256 ハッシュが変わったファイルだけを再解析する" width="90%" />
</p>

### コードベース全体か、的を絞った回答か

グラフはコーパス全体をモデルに渡す代わりに、質問に合わせて切り出した一部を返します。このリポジトリの `84bde354` における 2026-08-02 の計測では、208,821 のソーストークンが 1 質問あたり約 3,190 トークンになりました。そのスナップショット以降リポジトリは大きく育っているので、今日ではどちらの数字も大きくなっています。

<p align="center">
  <img src="diagrams/diagram6_monorepo_funnel.png" alt="code-review-graph の 84bde354 スナップショット: 208,821 のソーストークンが約 3,190 トークンのグラフ応答に絞り込まれ、1 質問あたりのトークンが約 65 分の 1 になる" width="80%" />
</p>

### 対応言語とノートブック

<p align="center">
  <img src="diagrams/diagram9_language_coverage.png" alt="カテゴリ別の言語カバレッジ: Web、バックエンド、システム、モバイル、スクリプト、シェル、ドメイン、その他、加えて Jupyter と Databricks のノートブック" width="90%" />
</p>

パーサーは関数、クラス、インポート、呼び出し箇所、継承、テストを抽出します。文法がある言語では Tree-sitter を、それ以外では的を絞ったフォールバックを使います。対応: Python、JavaScript/TypeScript/TSX、Go、Rust、Java、C/C++、C#、VB.NET、Ruby、Kotlin、Swift、PHP、Scala、Solidity、Dart、R、Perl、Lua/Luau、Objective-C、シェルスクリプト、Elixir、Zig、PowerShell、Julia、ReScript、GDScript、Nix、Verilog/SystemVerilog、SQL、Terraform/OpenTofu（`.tf`。ほかの `.hcl` ファイルはファイルノードのみ）、Ansible YAML（playbook、role、task）、Spring Boot のアプリケーション設定（`application.properties`、`application.yml`、`application.yaml` とその `application-<profile>` 版。キー名と値の型のみを記録し、値は決して記録しません）、Vue/Svelte の SFC、Astro ファイル（TypeScript の文法で解析）、Jupyter と Databricks のノートブック（`.ipynb`）、Perl XS ファイル（`.xs`）。ほかの YAML とほかの `.properties` ファイルはソースコードとして扱いません。

PHP プロジェクトではさらに、リポジトリ内に閉じた Composer PSR-4 の解決、Blade テンプレートの参照、そしてソースに明示的なフレームワークのインポート、モデルの継承、レシーバの根拠が現れている場合の Laravel Route と Eloquent のエッジが得られます。

Java プロジェクトでは、Spring の依存性注入による呼び出し解決、リクエストエンドポイントと WebFlux ルート、スケジュールトリガー、アプリケーションイベントの発行側からリスナーへのエッジ、Temporal のワークフローとアクティビティのエッジが得られます。各リゾルバは解析のあとに動き、注入されたフィールド、発行されるイベント、ワークフローのスタブがリポジトリ内で見えている必要があります。

### 独自の言語を追加する

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

### CI でのリスクスコア付き PR レビュー（GitHub Action）

同じ分析が composite の GitHub Action としても動きます。グラフはあなたの CI ランナー上で構築・照会され、ソースコードが外部サービスに送られることはありません。プルリクエストごとに、リスクスコア付きの関数、影響を受ける実行フロー、テストの穴をまとめた固定コメントを 1 件投稿し、プッシュのたびにその場で更新します。任意の `fail-on-risk` 入力を使うと、マージの関門にできます。

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
      - uses: tirth8205/code-review-graph@v2.3.8
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

入力、リスクレベル、キャッシュについては [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md) を、このリポジトリ自身に対して動かしているワークフローは [`.github/workflows/pr-review.yml`](.github/workflows/pr-review.yml) を参照してください。

---

## ベンチマーク

<p align="center">
  <img src="diagrams/diagram5_benchmark_board.png" alt="6 リポジトリのベンチマーク: 1 質問あたりのトークン削減の中央値は約 63 倍（最大 358 倍）、グラフ由来の正解に対する影響分析の平均 F1 は 0.69" width="85%" />
</p>

6 リポジトリにおける 1 質問あたりのトークン削減の中央値は約 **63 倍**です（コーパス全体のベースライン対グラフ照会）。**358 倍**という最大値は 1 つのリポジトリ（fastapi、最大のコーパス）のもので、典型的な結果ではありません。

数字はすべて、6 つのオープンソースリポジトリ（13 コミット）に対する評価ランナーの出力です。各設定は上流の SHA を固定し、Leiden は固定シードで動き、埋め込みは CPU 上で決定的なので、別のマシンで 2 回実行しても同じ数字になります。再現手順は [`docs/REPRODUCING.md`](docs/REPRODUCING.md) にあります。最小の 2 設定に対する週次のレポート専用実行は [`.github/workflows/eval.yml`](.github/workflows/eval.yml) にあります。

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

コーパス全体のベースラインは、実際のエージェントが払うことのない上限です。エージェントは識別子を grep して、いちばん合致するファイルを読みます。`agent_baseline` の評価ベンチマークはその場合を測ります（純 Python でコーパスを grep し、一致数の多い上位 3 ファイルを取り、グラフ照会のコストとトークン数を比較）。結果は `evaluate/results/<repo>_agent_baseline_<date>.csv` に書かれます。正式な計測結果はまだ公開していません。

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

このベンチマークは**共変更モード**でも動きます。予測器に変更ファイルを 1 つ与え、同じコミットで作者が触れたほかのファイルで採点するもので、根拠はグラフではなく git の履歴です。どちらのモードも結果 CSV に出ます（`ground_truth_mode` 列）。2026-08-02 の計測では、共変更モードは採点対象のどのコミットでも `predicted_files = 0` を返したため、まだ使える計測にはなっておらず、共変更の数字は引用していません。

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

- **影響分析の「再現率 1.0」は循環している。** 過去の正解は予測器が辿るのと同じグラフのエッジから作られているので、構造上それは上限です。共変更モードはまだ使える計測になっていません。
- **小さな単一ファイルの変更。** ささいな編集では、グラフの文脈のほうが素のファイル読み込みより大きくなることがあります。その差は、複数ファイルの分析を可能にしている構造メタデータです。
- **検索の順位付け。** キーワード検索はたいてい正しい結果を上位に出しますが、順位付けには改善が必要です。モジュールパターンの命名のため、Express への問い合わせはヒットなしになることがあります。
- **フロー検出。** エントリポイントの検出は Python と PHP/Laravel で最も強力です。JavaScript と Go のフロー検出には改善が必要です。
- **適合率と再現率。** 影響分析は保守的です。影響を受けうるファイルを挙げるため、大きな依存グラフでは偽陽性が出ます。

---

## 機能

| 機能 | 内容 |
|---------|---------|
| **増分更新** | ハッシュが変わったファイルだけを再解析します。約 3,000 ファイルのリポジトリでは、2 ファイルの編集がフックの経路で約 2.5 秒です（[計測値](docs/REPRODUCING.md#incremental-update-latency)）。 |
| **言語とノートブックの対応** | 上記の[対応言語](#対応言語とノートブック)を参照。 |
| **フレームワークを理解する PHP 解析** | リポジトリ内に閉じた Composer PSR-4 のインポート、Blade テンプレートの参照、根拠に基づく Laravel Route からコントローラへのエッジと Eloquent のリレーションのエッジ |
| **フレームワークを理解する Java 解析** | Spring の依存性注入による呼び出し解決、リクエストエンドポイントと WebFlux ルート、スケジュールトリガー、アプリケーションイベントの発行側からリスナーへのエッジ、Temporal のワークフローとアクティビティのエッジ、そして値を含めずにインデックスする Spring Boot の設定キー |
| **影響範囲の分析** | 変更によって影響を受けそうな関数、クラス、ファイル |
| **自動更新フック** | エディタのフック、git の pre-commit フック、watch モードが作業中にグラフを更新 |
| **セマンティック検索** | 任意のベクトル埋め込み。sentence-transformers、Google Gemini、MiniMax、Voyage AI、あるいは任意の OpenAI 互換エンドポイント（OpenAI、Azure、new-api、LiteLLM、vLLM、LocalAI） |
| **対話的な可視化** | D3.js の力学モデルグラフ。検索、コミュニティ凡例の切り替え、次数に応じたノードサイズ |
| **ハブとブリッジの検出** | 最も多く接続されたノードとボトルネック（媒介中心性） |
| **意外度スコア** | 想定外の結合: コミュニティをまたぐ、言語をまたぐ、周辺からハブへのエッジ |
| **知識のギャップ分析** | 孤立ノード、テストのないホットスポット、薄いコミュニティ |
| **推奨質問** | ブリッジ、ハブ、意外な結合から生成したレビュー用の質問 |
| **エッジの信頼度** | 2 段階の信頼度（EXTRACTED/INFERRED）と、エッジ上の浮動小数点スコア |
| **グラフ探索** | 任意のノードからの BFS/DFS。深さとトークン予算を設定可能 |
| **エクスポート形式** | GraphML（Gephi/yEd）、Neo4j Cypher、Obsidian vault、JSON、そして SVG（SVG には `eval` エクストラの matplotlib が必要） |
| **トークンのベンチマーク** | `code_review_graph/token_benchmark.py` が、質問ごとにコーパス全体のトークンとグラフ照会のトークンを計測 |
| **文脈削減量の推定** | レビュー、影響分析、detect-changes、アーキテクチャの応答に付く `context_savings` メタデータ（`estimated`、`saved_tokens`、`saved_percent`） |
| **コミュニティの自動分割** | グラフの 25% を超えるコミュニティを Leiden で再帰的に分割 |
| **実行フロー** | エントリポイントからの呼び出し連鎖を、重み付けした重要度で並べ替え |
| **コミュニティ検出** | グラフ規模に応じて解像度を調整する Leiden クラスタリング |
| **アーキテクチャ概要** | コミュニティ構造に基づくアーキテクチャ図と結合の警告 |
| **リスクスコア付きレビュー** | `detect_changes` が差分を、影響を受ける関数、フロー、テストの穴に対応づけ |
| **カスタム言語** | `.code-review-graph/languages.toml` で新しい言語を追加。フォーク不要 |
| **GitHub Action** | CI でのリスクスコア付き PR レビューの固定コメント。任意の `fail-on-risk` マージ関門つき |
| **リファクタリング支援** | リネームのプレビュー、フレームワークを理解するデッドコード検出、コミュニティに基づく提案 |
| **Wiki 生成** | コミュニティ構造から Markdown の Wiki を生成 |
| **マルチリポジトリのレジストリ** | 複数のリポジトリを登録し、横断して検索 |
| **マルチリポジトリのデーモン** | `crg-daemon` が複数のリポジトリを子プロセスで監視。ヘルスチェックと再起動つき |
| **MCP プロンプト** | 5 つのワークフローテンプレート: レビュー、アーキテクチャ、デバッグ、オンボーディング、マージ前チェック |
| **全文検索** | キーワードとベクトル類似度を組み合わせた FTS5 のハイブリッド検索 |
| **ローカル保存** | `.code-review-graph/` の SQLite ファイル 1 つ。外部データベースもクラウドサービスも不要 |

---

## 使い方

<details>
<summary><strong>スキル</strong></summary>
<br>

`install` は、スキルに対応したプラットフォーム（Claude Code、Gemini CLI、CodeBuddy Code、Hermes Agent、Qoder）向けに次の 4 つのスキルを書き込みます。名前で呼び出してください。

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
code-review-graph install          # Detect and configure all platforms
code-review-graph install --platform <name>  # One platform
code-review-graph uninstall --dry-run  # Preview removal of installed artifacts
code-review-graph build            # Parse the whole codebase
code-review-graph update           # Incremental update (changed files only)
code-review-graph status           # Graph statistics
code-review-graph watch            # Update on file changes
code-review-graph forget <path>    # Drop already-parsed files from the graph
code-review-graph dead-code        # Functions and classes with no callers or tests
code-review-graph visualize        # Interactive HTML graph
code-review-graph visualize --format json      # Export graph data as JSON
code-review-graph visualize --format graphml   # Export as GraphML
code-review-graph visualize --format svg       # Export as SVG (needs matplotlib)
code-review-graph visualize --format obsidian  # Export as Obsidian vault
code-review-graph visualize --format cypher    # Export as Neo4j Cypher
code-review-graph wiki             # Markdown wiki from communities
code-review-graph detect-changes --brief         # Risk panel + token savings (read-only)
code-review-graph detect-changes --brief --base main  # Against the merge base of main and HEAD
code-review-graph update --brief                 # Refresh graph + same panel
code-review-graph detect-changes --brief --verify  # Cross-check against tiktoken
code-review-graph register <path>  # Register repo in the multi-repo registry
code-review-graph unregister <path|alias>  # Remove repo from the registry
code-review-graph repos            # List registered repositories
code-review-graph daemon start     # Start the multi-repo watch daemon
code-review-graph daemon stop      # Stop the daemon
code-review-graph daemon status    # Daemon status and repos
code-review-graph eval             # Run evaluation benchmarks
code-review-graph serve            # Start the MCP server (stdio)
code-review-graph serve --http     # MCP over Streamable HTTP on localhost:5555
```

これは抜粋です。`code-review-graph --help` がすべてのコマンドを並べ、[docs/COMMANDS.md](docs/COMMANDS.md) がそれぞれのフラグを説明します。

`detect-changes --base` にブランチ名を渡すと、差分はそのブランチと HEAD のマージベースに対して取られます。コミットハッシュやその他のリビジョンはそのまま使われます。

`visualize --format svg` には matplotlib が必要で、これは `eval` エクストラに入っています（`pip install "code-review-graph[eval]"`）。ほかのエクスポート形式に追加インストールは要りません。

JSON のエクスポートはローカルのグラフデータディレクトリに書かれ、そのディレクトリは既定で Git の対象外です。絶対パスやコード構造のメタデータを含みうるので、公開する前に中身を確認してください。

</details>

<details>
<summary><strong>Token Savings パネル: <code>detect-changes --brief</code> と <code>update --brief</code></strong></summary>
<br>

どちらのコマンドも、変更ファイルをそのままエージェントに渡す場合と比べてグラフがどれだけトークンを節約したかを示す同じパネルを表示します。違いは 1 点だけ、先にグラフを更新するかどうかです。

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

</details>

<details>
<summary><strong>マルチリポジトリのデーモン</strong></summary>
<br>

エディタがフックに対応していない場合（Cursor や OpenCode など）や、エディタ連携なしでグラフを新しく保ちたい場合は、デーモンがリポジトリを監視してグラフを更新します。`code-review-graph` に同梱されているので、別途インストールは要りません。

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

グラフを構築すると、アシスタントはこれらを使います。

| ツール | 内容 |
|------|-------------|
| `build_or_update_graph_tool` | グラフの構築または増分更新 |
| `run_postprocess_tool` | フロー検出、コミュニティ検出、FTS インデックスの再実行 |
| `get_minimal_context_tool` | 小さな文脈（約 100 トークン）。最初にこれを呼ぶ |
| `get_impact_radius_tool` | 変更ファイルの影響範囲 |
| `get_review_context_tool` | 構造の要約つきレビュー文脈 |
| `query_graph_tool` | 呼び出し元、呼び出し先、テスト、インポート、継承の照会 |
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
| `get_knowledge_gaps_tool` | 構造上の弱点とテストのないホットスポット |
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

git リポジトリでは追跡されているファイルだけがインデックスされるので（`git ls-files`）、gitignore されたファイルは飛ばされます。追跡されているファイルを除外したいとき、あるいは git が使えないときに `.code-review-graphignore` を使ってください。既定の除外リストは [docs/USAGE.md](docs/USAGE.md#ignore-patterns) にあります。

任意の依存グループ:

```bash
pip install "code-review-graph[embeddings]"          # Local vector embeddings (sentence-transformers)
pip install "code-review-graph[google-embeddings]"   # Google Gemini embeddings
pip install "code-review-graph[communities]"         # Community detection (igraph)
pip install "code-review-graph[enrichment]"          # Python call-resolution enrichment (Jedi)
pip install "code-review-graph[eval]"                # Evaluation benchmarks and SVG export (matplotlib)
pip install "code-review-graph[wiki]"                # ollama client (not used by the current wiki generator)
pip install "code-review-graph[all]"                 # All optional dependencies
```

### 環境変数

| 変数 | 内容 | 既定値 |
|----------|-------------|---------|
| `CRG_GIT_TIMEOUT` | Git 操作のタイムアウト（秒。build / update / watch） | `30` |
| `CRG_DISCOVERY_TIMEOUT` | ファイル一覧が明示されなかったとき、変更内容を特定する各 Git コマンドのタイムアウト（秒）。超過時はエラーを返し、「変更なし」とは報告しない | `5`（`CRG_GIT_TIMEOUT` を明示設定した場合はその値） |
| `CRG_DATA_DIR` | グラフのデータベースと生成物を置くディレクトリ | - |
| `CRG_HOOK_WORKTREES` | `1` にすると pre-commit フックをリンク済みの git worktree でも動かす | - |
| `CRG_EMBEDDING_MODEL` | ローカルのベクトル埋め込みの既定モデル | `all-MiniLM-L6-v2` |
| `CRG_ACCEPT_CLOUD_EMBEDDINGS` | `1` にするとクラウド埋め込みの送信警告を抑制 | - |
| `CRG_ALLOW_REMOTE_CODE` | `trust_remote_code=True` を要する HuggingFace モデルを許可 | `0` |
| `CRG_MAX_IMPACT_NODES` | 影響分析に含める最大ノード数 | `500` |
| `CRG_MAX_IMPACT_DEPTH` | 影響範囲分析の探索の深さ | `2` |
| `CRG_MAX_BFS_DEPTH` | グラフ探索の最大の深さ | `15` |
| `CRG_MAX_CHANGED_FUNCS` | 1 回の変更レポートで分析する変更関数の最大数 | `500` |
| `CRG_MAX_TRANSITIVE_FRONTIER` | 推移的な呼び出し元・呼び出し先の展開における最大フロンティア数 | `50` |
| `CRG_TOOL_TIMEOUT` | 読み取り専用 MCP ツールのタイムアウト（秒。`0` で無効）。書き込みを行うツール（build / postprocess / embed / wiki / apply-refactor）には適用されない | `0` |
| `CRG_CHURN_WINDOW_DAYS` | `detect-changes --churn` のコミット数を数える期間 | `90` |
| `CRG_LEIDEN_SEED` | Leiden のコミュニティ検出のシード | `42` |
| `CRG_RECURSE_SUBMODULES` | `1`、`true`、`yes` のとき git サブモジュールを含める | - |
| `CRG_TOOLS` | サーバー起動時に公開する MCP ツールのカンマ区切り許可リスト | - |
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

OpenAI 互換の埋め込み（OpenAI、Azure、あるいは new-api、LiteLLM、vLLM、LocalAI、OpenAI モードの Ollama といった自前のゲートウェイ）に追加インストールは要りません。変数を設定し、`embed_graph` に `provider="openai"` を渡します。

```bash
export CRG_OPENAI_BASE_URL=http://127.0.0.1:3000/v1     # or https://api.openai.com/v1
export CRG_OPENAI_API_KEY=sk-...
export CRG_OPENAI_MODEL=text-embedding-3-small          # whatever your gateway serves
# optional:
export CRG_OPENAI_DIMENSION=1536                        # pin dim (v3 models support reduction)
export CRG_OPENAI_BATCH_SIZE=100                        # lower for gateways with tight limits
                                                        # (e.g. Qwen text-embedding-v4 caps at 10)
```

ベース URL が localhost（`127.0.0.1`、`localhost`、`0.0.0.0`、`::1`）を指している場合、クラウド送信の警告は出ません。

Voyage の埋め込みに追加インストールは要りません。`VOYAGE_API_KEY` を設定し、`embed_graph` に `provider="voyage"` を渡します。既定のモデルは `voyage-code-3` です。

```bash
export VOYAGE_API_KEY=pa-...
export CRG_ACCEPT_CLOUD_EMBEDDINGS=1
code-review-graph embed --provider voyage --model voyage-code-3
```

> **モデルの選び方。** 長く使うインデックスには `-preview`、`-beta`、`-exp` のモデル ID を避けてください。プレビュー版は重みが変わったり（次元が変わると全件の再埋め込みが必要）、取り下げられたりします。`text-embedding-3-small` / `text-embedding-3-large`（OpenAI）、`Qwen/Qwen3-Embedding-8B`（自前の vLLM や LocalAI）、`gemini-embedding-001`（`GOOGLE_API_KEY` が必要なネイティブの Gemini プロバイダ）といった正式リリースを選んでください。
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

---

## FAQ と比較

答えは [docs/FAQ.md](docs/FAQ.md) にあります。

- [LSP / 言語サーバーとの違い](docs/FAQ.md#how-is-this-different-from-lsp-and-language-servers): 言語ごとのデーモンではなく、言語をまたぐ永続的なグラフが 1 つ。シンボル単位の正確さでは LSP が上。
- [RAG / 埋め込みとの違い](docs/FAQ.md#isnt-this-just-rag): 類似度でのチャンクではなく、AST から解析した構造的なエッジ。埋め込みは任意で、検索の補助にすぎません。
- [grep / エージェント検索との違い](docs/FAQ.md#why-not-just-grep): 1 ホップの探索なら grep。多ホップの問い（影響範囲、呼び出し元の呼び出し元、tests-for、影響を受けるフロー）ならグラフ。
- [Serena、codegraph、claude-context、repomix との比較](docs/FAQ.md#how-does-it-compare-to-serena-codegraph-claude-context-and-repomix): 比較表。
- [使わないほうがよい場合](docs/FAQ.md#when-should-i-not-use-it): 小さなリポジトリ、ささいな単一ファイルの差分、1 回きりの質問。
- [外部に通信しますか？](docs/FAQ.md#does-it-phone-home): テレメトリはありません。クラウド埋め込みは明示的に有効にした場合だけ。
- [動いているかどう確かめますか？](docs/FAQ.md#how-do-i-verify-it-is-working): `status`、`detect-changes --brief`、`/mcp`。

## トラブルシューティング

Windows/WSL を含むより多くの事例は [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) にあります。

### `pip` / `pipx` が `hatchling` を取得できない（あるいは PyPI への `Errno 9` / `Bad file descriptor`）

ソースツリーからのインストール（たとえば `pipx install .`）には、PyPI からビルド依存を取る必要があります。接続の警告のあとに `Could not find a version that satisfies the requirement hatchling` が出る場合、その端末の Python が `pypi.org` への HTTPS 接続を開けていない可能性があります。エディタの統合ターミナルで最も多く、VPN、ファイアウォール、プロキシが原因のこともあります。

1. エディタのターミナルではなく、Terminal.app や iTerm から同じコマンドを実行する。
2. ダウンロードの仕組みが異なる [uv](https://docs.astral.sh/uv/) を使って、チェックアウトからインストールする。

   ```bash
   cd /path/to/code-review-graph
   uv tool install . --force
   ```

3. クローンでの開発には `uv sync` と `uv run code-review-graph ...` を使う。

切り分けには `python3 scripts/diagnose_pypi_connectivity.py` を実行します。`FAILED` と表示されたら、原因はパッケージ名ではなくネットワーク環境です。

### Windows: `Invalid JSON: EOF while parsing` または `MCP error -32000: Connection closed`

Claude Code の設定で `cmd /c` によるラッパーを使わないでください。`~/.claude.json` から `.exe` を直接指し、UTF-8 は設定で渡します。

```json
"code-review-graph": {
  "command": "C:\\path\\to\\your\\venv\\Scripts\\code-review-graph.exe",
  "args": ["serve", "--repo", "C:\\path\\to\\your\\project"],
  "env": { "PYTHONUTF8": "1" }
}
```

## コントリビュート

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

プルリクエストは `staging`（既定のブランチ）に出してください。変更は
`staging` → `testing` → `main` の順に進み、リリースは `main` からタグを打ちます。全体の流れは
[CONTRIBUTING.md](CONTRIBUTING.md#branching-and-promotion) を参照してください。

組み込みの言語を追加するには `code_review_graph/parser.py` を編集します。拡張子を `EXTENSION_TO_LANGUAGE` に、ノード型の対応を `_CLASS_TYPES`、`_FUNCTION_TYPES`、`_IMPORT_TYPES`、`_CALL_TYPES` に追加してください。テスト用のフィクスチャを添えて PR を出してください。1 つのリポジトリだけで必要な言語なら、代わりに [`languages.toml`](docs/CUSTOM_LANGUAGES.md) を使ってください。

## ライセンス

MIT。[LICENSE](LICENSE) を参照してください。

<p align="center">
<br>
<a href="https://code-review-graph.com">code-review-graph.com</a><br><br>
<code>pip install code-review-graph && code-review-graph install</code>
</p>
