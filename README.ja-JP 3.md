<h1 align="center">code-review-graph</h1>

<p align="center">
  <strong>トークンの無駄遣いをやめて、スマートなレビューを。</strong>
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
  <a href="https://github.com/tirth8205/code-review-graph/actions/workflows/ci.yml"><img src="https://github.com/tirth8205/code-review-graph/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg?style=flat-square" alt="Python 3.10+"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-compatible-green.svg?style=flat-square" alt="MCP"></a>
  <a href="https://code-review-graph.com"><img src="https://img.shields.io/badge/website-code--review--graph.com-blue?style=flat-square" alt="Website"></a>
  <a href="https://discord.gg/3p58KXqGFN"><img src="https://img.shields.io/badge/discord-join-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord"></a>
</p>

<br>

AIコーディングツールはタスクのたびにコードベース全体を再読み込みします。`code-review-graph` はその問題を解決します。[Tree-sitter](https://tree-sitter.github.io/tree-sitter/) でコードの構造マップを構築し、変更を差分で追跡し、[MCP](https://modelcontextprotocol.io/) を通じてAIアシスタントに必要最小限のコンテキストだけを提供します。

<p align="center">
  <img src="diagrams/diagram1_before_vs_after.png" alt="トークン問題：6つの実リポジトリで平均8.2倍のトークン削減" width="85%" />
</p>

---

## クイックスタート

```bash
pip install code-review-graph                     # または: pipx install code-review-graph
code-review-graph install          # 対応プラットフォームを自動検出して設定
code-review-graph build            # コードベースを解析
```

1つのコマンドですべてが完了します。`install` は使用中のAIコーディングツールを検出し、各ツールに適切なMCP設定を書き込み、プラットフォームルールにグラフ対応の指示を注入します。`uvx` と `pip`/`pipx` のどちらでインストールしたかを自動判別し、適切な設定を生成します。インストール後はエディタ/ツールを再起動してください。

<p align="center">
  <img src="diagrams/diagram8_supported_platforms.png" alt="ワンインストールで全プラットフォーム対応：Codex、Claude Code、Cursor、Windsurf、Zed、Continue、OpenCode、Antigravity、Kiroを自動検出" width="85%" />
</p>

特定のプラットフォームのみを設定する場合：

```bash
code-review-graph install --platform codex       # Codexのみ設定
code-review-graph install --platform cursor      # Cursorのみ設定
code-review-graph install --platform claude-code  # Claude Codeのみ設定
code-review-graph install --platform kiro         # Kiroのみ設定
```

Python 3.10以上が必要です。最良の体験のためには [uv](https://docs.astral.sh/uv/) のインストールを推奨します（MCP設定は利用可能な場合 `uvx` を使用し、そうでない場合は `code-review-graph` コマンドに直接フォールバックします）。

セットアップ後、プロジェクトを開いてAIアシスタントに聞いてみましょう：

```
Build the code review graph for this project
```

初回ビルドは500ファイルのプロジェクトで約10秒です。以降はファイル編集やgitコミットのたびにグラフが自動更新されます。

---

## 仕組み

<p align="center">
  <img src="diagrams/diagram7_mcp_integration_flow.png" alt="AIアシスタントがグラフを活用する流れ：ユーザーがレビューを依頼、AIがMCPツールを確認、グラフが影響範囲とリスクスコアを返却、AIは必要なものだけを読む" width="80%" />
</p>

リポジトリはTree-sitterでASTに解析され、ノード（関数、クラス、インポート）とエッジ（呼び出し、継承、テストカバレッジ）のグラフとしてSQLiteに保存されます。レビュー時にはこのグラフを参照して、AIアシスタントが読むべきファイルの最小セットを算出します。

<p align="center">
  <img src="diagrams/diagram2_architecture_pipeline.png" alt="アーキテクチャパイプライン：リポジトリ → Tree-sitterパーサー → SQLiteグラフ → 影響範囲 → 最小レビューセット" width="100%" />
</p>

### 影響範囲分析（ブラストラディウス）

ファイルが変更されると、グラフは影響を受ける可能性のあるすべての呼び出し元、依存先、テストをトレースします。これが変更の「影響範囲（ブラストラディウス）」です。AIはプロジェクト全体をスキャンする代わりに、これらのファイルだけを読みます。

<p align="center">
  <img src="diagrams/diagram3_blast_radius.png" alt="影響範囲の可視化：login()への変更が呼び出し元、依存先、テストにどう伝播するか" width="70%" />
</p>

### 2秒以内のインクリメンタル更新

gitコミットやファイル保存のたびにフックが起動します。グラフは変更ファイルの差分を取り、SHA-256ハッシュで依存先を特定し、変更されたものだけを再解析します。2,900ファイルのプロジェクトでも2秒以内で再インデックスが完了します。

<p align="center">
  <img src="diagrams/diagram4_incremental_update.png" alt="インクリメンタル更新フロー：gitコミットが差分をトリガー、依存先を検出、5ファイルのみ再解析、2,910ファイルはスキップ" width="90%" />
</p>

### モノレポ問題の解決

大規模モノレポこそトークンの無駄が最も深刻な場所です。グラフがノイズを除去し、27,700以上のファイルをレビューコンテキストから除外、実際に読むのは約15ファイルだけです。

<p align="center">
  <img src="diagrams/diagram6_monorepo_funnel.png" alt="Next.jsモノレポ：27,732ファイルをcode-review-graphで絞り込み、約15ファイルに - トークン49分の1" width="80%" />
</p>

### 23言語 + Jupyterノートブック対応

<p align="center">
  <img src="diagrams/diagram9_language_coverage.png" alt="カテゴリ別19言語：Web、バックエンド、システム、モバイル、スクリプト、さらにJupyter/Databricksノートブック対応" width="90%" />
</p>

すべての言語で関数、クラス、インポート、呼び出し箇所、継承、テスト検出の完全なTree-sitter文法をサポート。Zig、PowerShell、Julia、Svelte SFCにも対応。さらにJupyter/Databricksノートブック（`.ipynb`）の多言語セル対応（Python、R、SQL）やPerl XSファイル（`.xs`）も解析可能です。

---

## ベンチマーク

<p align="center">
  <img src="diagrams/diagram5_benchmark_board.png" alt="実リポジトリでのベンチマーク：トークン4.9倍から27.3倍削減、レビュー品質向上" width="85%" />
</p>

すべての数値は6つの実際のオープンソースリポジトリ（合計13コミット）に対する自動評価ランナーの結果です。`code-review-graph eval --all` で再現可能です。生データは [`evaluate/reports/summary.md`](evaluate/reports/summary.md) をご覧ください。

> 詳細なベンチマーク結果（トークン効率、影響精度、ビルド性能、既知の制限事項）については [英語版README](README.md) を参照してください。

---

## 機能一覧

| 機能 | 詳細 |
|------|------|
| **インクリメンタル更新** | 変更されたファイルのみを再解析。更新は2秒以内に完了。 |
| **23言語 + ノートブック** | Python, TypeScript/TSX, JavaScript, Vue, Svelte, Go, Rust, Java, Scala, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++, Dart, R, Perl, Lua, Zig, PowerShell, Julia, Jupyter/Databricks (.ipynb) |
| **影響範囲分析** | 変更によって影響を受ける関数、クラス、ファイルを正確に表示 |
| **自動更新フック** | ファイル編集やgitコミットのたびに手動操作なしでグラフを更新 |
| **セマンティック検索** | sentence-transformers、Google Gemini、MiniMax、またはOpenAI互換エンドポイント（本家OpenAI、Azure、new-api、LiteLLM、vLLM、LocalAI）によるオプションのベクトル埋め込み |
| **インタラクティブ可視化** | D3.js力学レイアウトグラフ。検索、コミュニティ凡例切替、次数スケーリングノード対応 |
| **ハブ・ブリッジ検出** | 最も接続の多いノードと媒介中心性によるアーキテクチャのボトルネックを発見 |
| **サプライズスコアリング** | 予期しない結合を検出：コミュニティ間、言語間、周辺からハブへのエッジ |
| **ナレッジギャップ分析** | 孤立ノード、テストされていないホットスポット、薄いコミュニティ、構造的弱点を特定 |
| **レビュー質問の自動生成** | グラフ分析（ブリッジ、ハブ、サプライズ）からレビュー質問を自動生成 |
| **エッジ信頼度** | エッジに3段階の信頼度スコアリング（EXTRACTED/INFERRED/AMBIGUOUS）とfloatスコア |
| **グラフ走査** | 任意のノードからBFS/DFSで自由に探索。深さとトークン予算を設定可能 |
| **エクスポート形式** | GraphML（Gephi/yEd）、Neo4j Cypher、Obsidianボールト（ウィキリンク付き）、SVG静的グラフ |
| **グラフ差分** | グラフのスナップショットを時系列で比較：ノード・エッジの追加/削除、コミュニティの変更 |
| **トークンベンチマーク** | ナイーブな全ファイル読み込みとグラフクエリのトークン数を質問ごとに比較 |
| **メモリループ** | Q&A結果をMarkdownとして保存し再取り込み。クエリからグラフが成長 |
| **コミュニティ自動分割** | グラフの25%を超えるコミュニティはLeidenアルゴリズムで再帰的に分割 |
| **実行フロー** | エントリーポイントからの呼び出しチェーンを重み付き重要度でソートしてトレース |
| **コミュニティ検出** | Leidenアルゴリズムで関連コードをクラスタリング。大規模グラフ向け解像度スケーリング対応 |
| **アーキテクチャ概要** | コミュニティ構造から自動生成されるアーキテクチャマップ（結合度警告付き） |
| **リスクスコア付きレビュー** | `detect_changes` が差分を影響する関数、フロー、テストギャップにマッピング |
| **リファクタリングツール** | リネームプレビュー、フレームワーク対応のデッドコード検出、コミュニティ駆動の提案 |
| **Wiki生成** | コミュニティ構造からMarkdown Wikiを自動生成 |
| **マルチリポジトリ管理** | 複数リポジトリを登録し、横断検索が可能 |
| **MCPプロンプト** | 5つのワークフローテンプレート：レビュー、アーキテクチャ、デバッグ、オンボーディング、マージ前チェック |
| **全文検索** | FTS5によるハイブリッド検索（キーワードとベクトル類似度の組み合わせ） |
| **ローカルストレージ** | `.code-review-graph/` 内のSQLiteファイル。外部DB不要、クラウド依存なし。 |
| **ウォッチモード** | 作業中にグラフを継続的に更新 |

---

## 使い方

<details>
<summary><strong>スラッシュコマンド</strong></summary>
<br>

| コマンド | 説明 |
|---------|------|
| `/code-review-graph:build-graph` | コードグラフのビルドまたは再ビルド |
| `/code-review-graph:review-delta` | 最後のコミット以降の変更をレビュー |
| `/code-review-graph:review-pr` | 影響範囲分析付きのフルPRレビュー |

</details>

<details>
<summary><strong>CLIリファレンス</strong></summary>
<br>

```bash
code-review-graph install          # 全プラットフォームを自動検出して設定
code-review-graph install --platform <name>  # 特定のプラットフォームのみ設定
code-review-graph build            # コードベース全体を解析
code-review-graph update           # インクリメンタル更新（変更ファイルのみ）
code-review-graph status           # グラフの統計情報
code-review-graph watch            # ファイル変更時に自動更新
code-review-graph visualize        # インタラクティブHTMLグラフを生成
code-review-graph visualize --format graphml   # GraphML形式でエクスポート
code-review-graph visualize --format svg       # SVG形式でエクスポート
code-review-graph visualize --format obsidian  # Obsidianボールトとしてエクスポート
code-review-graph visualize --format cypher    # Neo4j Cypher形式でエクスポート
code-review-graph wiki             # コミュニティからMarkdown Wikiを生成
code-review-graph detect-changes   # リスクスコア付き変更影響分析
code-review-graph register <path>  # マルチリポジトリレジストリにリポジトリを登録
code-review-graph unregister <id>  # レジストリからリポジトリを削除
code-review-graph repos            # 登録済みリポジトリの一覧表示
code-review-graph eval             # 評価ベンチマークの実行
code-review-graph serve            # MCPサーバーの起動
```

</details>

<details>
<summary><strong>28のMCPツール</strong></summary>
<br>

グラフのビルド後、AIアシスタントがこれらのツールを自動的に使用します。

| ツール | 説明 |
|--------|------|
| `build_or_update_graph_tool` | グラフのビルドまたはインクリメンタル更新 |
| `get_minimal_context_tool` | 超コンパクトなコンテキスト（約100トークン） -- 最初にこれを呼び出す |
| `get_impact_radius_tool` | 変更ファイルの影響範囲 |
| `get_review_context_tool` | 構造サマリー付きトークン最適化レビューコンテキスト |
| `query_graph_tool` | 呼び出し元、呼び出し先、テスト、インポート、継承のクエリ |
| `traverse_graph_tool` | 任意のノードからトークン予算付きBFS/DFS走査 |
| `semantic_search_nodes_tool` | 名前や意味でコードエンティティを検索 |
| `embed_graph_tool` | セマンティック検索用のベクトル埋め込みを計算 |
| `list_graph_stats_tool` | グラフのサイズと健全性 |
| `get_docs_section_tool` | ドキュメントセクションの取得 |
| `find_large_functions_tool` | 行数閾値を超える関数/クラスの検出 |
| `list_flows_tool` | 重要度順の実行フロー一覧 |
| `get_flow_tool` | 単一の実行フローの詳細取得 |
| `get_affected_flows_tool` | 変更ファイルに影響するフローの検出 |
| `list_communities_tool` | 検出されたコードコミュニティの一覧 |
| `get_community_tool` | 単一コミュニティの詳細取得 |
| `get_architecture_overview_tool` | コミュニティ構造からのアーキテクチャ概要 |
| `detect_changes_tool` | コードレビュー用のリスクスコア付き変更影響分析 |
| `get_hub_nodes_tool` | 最も接続の多いノード（アーキテクチャのホットスポット）の検出 |
| `get_bridge_nodes_tool` | 媒介中心性によるボトルネックの検出 |
| `get_knowledge_gaps_tool` | 構造的弱点とテストされていないホットスポットの特定 |
| `get_surprising_connections_tool` | 予期しないコミュニティ間結合の検出 |
| `get_suggested_questions_tool` | 分析から自動生成されたレビュー質問 |
| `refactor_tool` | リネームプレビュー、デッドコード検出、提案 |
| `apply_refactor_tool` | プレビュー済みリファクタリングの適用 |
| `generate_wiki_tool` | コミュニティからMarkdown Wikiを生成 |
| `get_wiki_page_tool` | 特定のWikiページの取得 |
| `list_repos_tool` | 登録済みリポジトリの一覧 |
| `cross_repo_search_tool` | 全登録リポジトリを横断検索 |

**MCPプロンプト**（5つのワークフローテンプレート）：
`review_changes`, `architecture_map`, `debug_issue`, `onboard_developer`, `pre_merge_check`

</details>

<details>
<summary><strong>設定</strong></summary>
<br>

インデックス対象から除外するパスを指定するには、リポジトリルートに `.code-review-graphignore` ファイルを作成します：

```
generated/**
*.generated.ts
vendor/**
node_modules/**
```

注意：gitリポジトリでは追跡対象ファイルのみがインデックスされます（`git ls-files`）。そのため、gitignoreされたファイルは自動的にスキップされます。`.code-review-graphignore` は追跡対象ファイルの除外や、gitが利用できない環境で使用してください。

オプションの依存グループ：

```bash
pip install code-review-graph[embeddings]          # ローカルベクトル埋め込み (sentence-transformers)
pip install code-review-graph[google-embeddings]   # Google Gemini埋め込み
pip install code-review-graph[communities]         # コミュニティ検出 (igraph)
pip install code-review-graph[eval]                # 評価ベンチマーク (matplotlib)
pip install code-review-graph[wiki]                # LLMサマリー付きWiki生成 (ollama)
pip install code-review-graph[all]                 # 全オプション依存
```

OpenAI互換の埋め込み（本家OpenAI、Azure、または自前のゲートウェイ: new-api / LiteLLM / vLLM / LocalAI / Ollama openaiモード）は追加インストール不要です。環境変数を設定し、`embed_graph` に `provider="openai"` を渡すだけで動作します：

```bash
export CRG_OPENAI_BASE_URL=http://127.0.0.1:3000/v1     # または https://api.openai.com/v1
export CRG_OPENAI_API_KEY=sk-...
export CRG_OPENAI_MODEL=text-embedding-3-small          # ゲートウェイが提供するモデル名
# 任意:
export CRG_OPENAI_DIMENSION=1536                        # 次元を固定（v3モデルは次元削減対応）
export CRG_OPENAI_BATCH_SIZE=100                        # バッチ上限が厳しいゲートウェイで下げる
                                                        # （例: Qwen text-embedding-v4 は上限10）
```

base URLがlocalhost（`127.0.0.1`、`localhost`、`0.0.0.0`、`::1`）を指している場合、クラウドegress警告は自動的にスキップされます。

> **モデル選択のヒント。** `-preview` / `-beta` / `-exp` 付きのmodel ID（例：`google/gemini-embedding-2-preview`）は長期運用には避けてください。preview モデルは重みが変更される（次元が変わると全ノード re-embed 必須）か、予告なく deprecate される可能性があります。安定版 GA モデル推奨：`text-embedding-3-small` / `text-embedding-3-large`（OpenAI）、`Qwen/Qwen3-Embedding-8B`（vLLM / LocalAI 自前ホスト経由）、または `gemini-embedding-001`（ネイティブ Gemini provider 経由、`GOOGLE_API_KEY` が必要）。
>
> また注意：現状 `code-review-graph` は**関数シグネチャのみ**を埋め込みます（ノードあたり約10トークン、例：`"parse_file function (path: str) returns Tree"`）。長 context で関数 body を理解する能力で差をつけるモデル（Gemini 2 や Qwen3-8B の MTEB-code SOTA スコア）は、この入力長では小型モデルとの差がかなり縮まります。Body / docstring 埋め込みはフォローアップ拡張として追跡中です。

</details>

---

## コントリビュート

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

<details>
<summary><strong>新しい言語の追加</strong></summary>
<br>

`code_review_graph/parser.py` を編集し、`EXTENSION_TO_LANGUAGE` に拡張子を追加します。合わせて `_CLASS_TYPES`、`_FUNCTION_TYPES`、`_IMPORT_TYPES`、`_CALL_TYPES` にノードタイプのマッピングを追加してください。テストフィクスチャを含めてPRを作成してください。

</details>

## ライセンス

MIT。詳細は [LICENSE](LICENSE) を参照してください。

<p align="center">
<br>
<a href="https://code-review-graph.com">code-review-graph.com</a><br><br>
<code>pip install code-review-graph && code-review-graph install</code><br>
<sub>Codex、Claude Code、Cursor、Windsurf、Zed、Continue、OpenCode、Antigravity、Kiroに対応</sub>
</p>
