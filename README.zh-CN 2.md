<h1 align="center">code-review-graph</h1>

<p align="center">
  <strong>不再浪费 token，让代码审查更智能。</strong>
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

AI 编码工具在每次任务中都会重新读取整个代码库。`code-review-graph` 解决了这个问题。它使用 [Tree-sitter](https://tree-sitter.github.io/tree-sitter/) 构建代码的结构化映射，增量跟踪变更，并通过 [MCP](https://modelcontextprotocol.io/) 为 AI 助手提供精准的上下文，使其只读取真正需要的内容。

<p align="center">
  <img src="diagrams/diagram1_before_vs_after.png" alt="Token 问题：在 6 个真实仓库中平均减少 8.2 倍的 token 消耗" width="85%" />
</p>

---

## 快速开始

```bash
pip install code-review-graph                     # 或: pipx install code-review-graph
code-review-graph install          # 自动检测并配置所有支持的平台
code-review-graph build            # 解析代码库
```

一条命令完成所有配置。`install` 会检测你安装了哪些 AI 编码工具，为每个工具写入正确的 MCP 配置，并将图感知指令注入平台规则。它会自动判断你是通过 `uvx` 还是 `pip`/`pipx` 安装的，并生成相应的配置。安装后请重启编辑器或工具。

<p align="center">
  <img src="diagrams/diagram8_supported_platforms.png" alt="一次安装，全平台支持：自动检测 Codex、Claude Code、Cursor、Windsurf、Zed、Continue、OpenCode、Antigravity 和 Kiro" width="85%" />
</p>

如需指定特定平台：

```bash
code-review-graph install --platform codex       # 仅配置 Codex
code-review-graph install --platform cursor      # 仅配置 Cursor
code-review-graph install --platform claude-code  # 仅配置 Claude Code
code-review-graph install --platform kiro         # 仅配置 Kiro
```

需要 Python 3.10+。为获得最佳体验，建议安装 [uv](https://docs.astral.sh/uv/)（如果可用，MCP 配置将使用 `uvx`，否则直接使用 `code-review-graph` 命令）。

然后打开项目，向 AI 助手发出指令：

```
Build the code review graph for this project
```

首次构建在 500 个文件的项目上大约需要 10 秒。此后，图会在每次文件编辑和 git 提交时自动更新。

---

## 工作原理

<p align="center">
  <img src="diagrams/diagram7_mcp_integration_flow.png" alt="AI 助手如何使用图：用户请求审查，AI 查询 MCP 工具，图返回影响范围和风险评分，AI 仅读取关键内容" width="80%" />
</p>

代码库通过 Tree-sitter 解析为 AST，以节点（函数、类、导入）和边（调用、继承、测试覆盖）的形式存储为图，然后在审查时查询，计算 AI 助手需要读取的最小文件集。

<p align="center">
  <img src="diagrams/diagram2_architecture_pipeline.png" alt="架构流程：代码库 -> Tree-sitter 解析器 -> SQLite 图 -> 影响半径 -> 最小审查集" width="100%" />
</p>

### 影响半径分析

当文件发生变更时，图会追踪所有可能受影响的调用者、依赖项和测试。这就是变更的"影响半径"。AI 只需读取这些文件，而无需扫描整个项目。

<p align="center">
  <img src="diagrams/diagram3_blast_radius.png" alt="影响半径可视化：展示 login() 的变更如何传播到调用者、依赖项和测试" width="70%" />
</p>

### 增量更新，不到 2 秒

每次 git 提交或文件保存时触发钩子。图对变更文件做差异比较，通过 SHA-256 哈希校验找到相关依赖，仅重新解析变更部分。一个 2,900 文件的项目重新索引不到 2 秒。

<p align="center">
  <img src="diagrams/diagram4_incremental_update.png" alt="增量更新流程：git 提交触发差异比较，找到依赖项，仅重新解析 5 个文件，跳过 2,910 个文件" width="90%" />
</p>

### 解决 monorepo 难题

大型 monorepo 是 token 浪费最严重的场景。图能穿透噪音——排除 27,700+ 个文件，只读取约 15 个文件。

<p align="center">
  <img src="diagrams/diagram6_monorepo_funnel.png" alt="Next.js monorepo：27,732 个文件经过 code-review-graph 过滤后仅剩约 15 个文件——token 减少 49 倍" width="80%" />
</p>

### 支持 23 种语言 + Jupyter 笔记本

<p align="center">
  <img src="diagrams/diagram9_language_coverage.png" alt="按类别组织的 19 种语言：Web、后端、系统、移动端、脚本，外加 Jupyter/Databricks 笔记本支持" width="90%" />
</p>

完整的 Tree-sitter 语法支持，涵盖每种语言的函数、类、导入、调用点、继承和测试检测。包括 Zig、PowerShell、Julia 和 Svelte SFC 支持，以及 Jupyter/Databricks 笔记本解析（`.ipynb`，支持 Python、R、SQL 多语言单元格）和 Perl XS 文件（`.xs`）。

---

## 基准测试

<p align="center">
  <img src="diagrams/diagram5_benchmark_board.png" alt="真实仓库基准测试：token 减少 4.9 倍到 27.3 倍，审查质量更高" width="85%" />
</p>

所有数据来自针对 6 个真实开源仓库（共 13 次提交）的自动化评估。可通过 `code-review-graph eval --all` 复现。完整基准测试数据请参阅[英文 README](README.md)。

---

## 功能一览

| 功能 | 说明 |
|------|------|
| **增量更新** | 仅重新解析变更文件，后续更新不到 2 秒完成 |
| **23 种语言 + 笔记本** | Python, TypeScript/TSX, JavaScript, Vue, Svelte, Go, Rust, Java, Scala, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++, Dart, R, Perl, Lua, Zig, PowerShell, Julia, Jupyter/Databricks (.ipynb) |
| **影响半径分析** | 精确展示任何变更所影响的函数、类和文件 |
| **自动更新钩子** | 每次文件编辑和 git 提交时自动更新图，无需手动干预 |
| **语义搜索** | 可选的向量嵌入，支持 sentence-transformers、Google Gemini、MiniMax，或任何 OpenAI 兼容端点（真实 OpenAI、Azure、new-api、LiteLLM、vLLM、LocalAI） |
| **交互式可视化** | D3.js 力导向图，支持搜索、社区图例切换和按度数缩放的节点 |
| **Hub 与 Bridge 检测** | 查找连接最多的节点和通过介数中心性发现架构瓶颈 |
| **异常评分** | 检测意外耦合：跨社区、跨语言、外围到核心的边 |
| **知识缺口分析** | 识别孤立节点、未测试热点、薄弱社区和结构性弱点 |
| **智能提问** | 基于图分析（桥接点、枢纽、异常）自动生成审查问题 |
| **边置信度** | 三级置信度评分（EXTRACTED/INFERRED/AMBIGUOUS），边上附带浮点分数 |
| **图遍历** | 从任意节点进行自由 BFS/DFS 探索，可配置深度和 token 预算 |
| **导出格式** | GraphML (Gephi/yEd)、Neo4j Cypher、Obsidian 知识库（含 wikilinks）、SVG 静态图 |
| **图差异** | 比较不同时间的图快照：新增/删除的节点、边和社区变化 |
| **Token 基准测试** | 测量朴素全量 token 与图查询 token，附带逐题比率 |
| **记忆循环** | 将问答结果持久化为 Markdown 以供重新摄入，使图从查询中不断成长 |
| **社区自动分割** | 过大的社区（>图的 25%）通过 Leiden 算法递归分割 |
| **执行流** | 从入口点追踪调用链，按加权关键度排序 |
| **社区检测** | 通过 Leiden 算法聚类相关代码，大型图自动调节分辨率 |
| **架构概览** | 自动生成架构图，附带耦合警告 |
| **风险评分审查** | `detect_changes` 将差异映射到受影响的函数、执行流和测试缺口 |
| **重构工具** | 重命名预览、框架感知的死代码检测、基于社区的重构建议 |
| **Wiki 生成** | 从社区结构自动生成 Markdown Wiki |
| **多仓库注册** | 注册多个仓库，跨仓库搜索 |
| **MCP 提示模板** | 5 种工作流模板：审查、架构、调试、入职引导、合并前检查 |
| **全文搜索** | 基于 FTS5 的混合搜索，结合关键词和向量相似度 |
| **本地存储** | SQLite 文件存储在 `.code-review-graph/` 中，无需外部数据库，无云依赖 |
| **监听模式** | 工作时持续更新图 |

---

## 使用方式

<details>
<summary><strong>斜杠命令</strong></summary>
<br>

| 命令 | 说明 |
|------|------|
| `/code-review-graph:build-graph` | 构建或重新构建代码图 |
| `/code-review-graph:review-delta` | 审查自上次提交以来的变更 |
| `/code-review-graph:review-pr` | 完整的 PR 审查，含影响半径分析 |

</details>

<details>
<summary><strong>CLI 参考</strong></summary>
<br>

```bash
code-review-graph install          # 自动检测并配置所有平台
code-review-graph install --platform <name>  # 指定特定平台
code-review-graph build            # 解析整个代码库
code-review-graph update           # 增量更新（仅变更文件）
code-review-graph status           # 图统计信息
code-review-graph watch            # 文件变更时自动更新
code-review-graph visualize        # 生成交互式 HTML 图
code-review-graph visualize --format graphml   # 导出为 GraphML
code-review-graph visualize --format svg       # 导出为 SVG
code-review-graph visualize --format obsidian  # 导出为 Obsidian 知识库
code-review-graph visualize --format cypher    # 导出为 Neo4j Cypher
code-review-graph wiki             # 从社区结构生成 Markdown Wiki
code-review-graph detect-changes   # 风险评分的变更影响分析
code-review-graph register <path>  # 将仓库注册到多仓库注册表
code-review-graph unregister <id>  # 从注册表移除仓库
code-review-graph repos            # 列出已注册的仓库
code-review-graph eval             # 运行评估基准测试
code-review-graph serve            # 启动 MCP 服务器
```

</details>

<details>
<summary><strong>28 个 MCP 工具</strong></summary>
<br>

图构建完成后，AI 助手会自动使用这些工具。

| 工具 | 说明 |
|------|------|
| `build_or_update_graph_tool` | 构建或增量更新图 |
| `get_minimal_context_tool` | 超紧凑上下文（约 100 tokens）——首先调用此工具 |
| `get_impact_radius_tool` | 变更文件的影响半径 |
| `get_review_context_tool` | Token 优化的审查上下文，附带结构摘要 |
| `query_graph_tool` | 查询调用者、被调用者、测试、导入、继承关系 |
| `traverse_graph_tool` | 从任意节点进行 BFS/DFS 遍历，可设置 token 预算 |
| `semantic_search_nodes_tool` | 按名称或语义搜索代码实体 |
| `embed_graph_tool` | 计算向量嵌入以支持语义搜索 |
| `list_graph_stats_tool` | 图的规模和健康状态 |
| `get_docs_section_tool` | 获取文档章节 |
| `find_large_functions_tool` | 查找超过行数阈值的函数/类 |
| `list_flows_tool` | 列出按关键度排序的执行流 |
| `get_flow_tool` | 获取单个执行流的详情 |
| `get_affected_flows_tool` | 查找受变更文件影响的执行流 |
| `list_communities_tool` | 列出检测到的代码社区 |
| `get_community_tool` | 获取单个社区的详情 |
| `get_architecture_overview_tool` | 基于社区结构的架构概览 |
| `detect_changes_tool` | 面向代码审查的风险评分变更影响分析 |
| `get_hub_nodes_tool` | 查找连接最多的节点（架构热点） |
| `get_bridge_nodes_tool` | 通过介数中心性查找架构瓶颈 |
| `get_knowledge_gaps_tool` | 识别结构性弱点和未测试热点 |
| `get_surprising_connections_tool` | 检测意外的跨社区耦合 |
| `get_suggested_questions_tool` | 基于分析自动生成审查问题 |
| `refactor_tool` | 重命名预览、死代码检测、重构建议 |
| `apply_refactor_tool` | 应用先前预览的重构 |
| `generate_wiki_tool` | 从社区结构生成 Markdown Wiki |
| `get_wiki_page_tool` | 获取特定 Wiki 页面 |
| `list_repos_tool` | 列出已注册的仓库 |
| `cross_repo_search_tool` | 跨所有注册仓库搜索 |

**MCP 提示模板**（5 种工作流模板）：
`review_changes`、`architecture_map`、`debug_issue`、`onboard_developer`、`pre_merge_check`

</details>

<details>
<summary><strong>配置</strong></summary>
<br>

要排除特定路径不被索引，请在仓库根目录创建 `.code-review-graphignore` 文件：

```
generated/**
*.generated.ts
vendor/**
node_modules/**
```

注意：在 git 仓库中，仅索引已跟踪的文件（`git ls-files`），因此 gitignore 中的文件会自动跳过。`.code-review-graphignore` 用于排除已跟踪的文件，或在没有 git 的环境中使用。

可选依赖组：

```bash
pip install code-review-graph[embeddings]          # 本地向量嵌入 (sentence-transformers)
pip install code-review-graph[google-embeddings]   # Google Gemini 嵌入
pip install code-review-graph[communities]         # 社区检测 (igraph)
pip install code-review-graph[eval]                # 评估基准测试 (matplotlib)
pip install code-review-graph[wiki]                # 使用 LLM 摘要生成 Wiki (ollama)
pip install code-review-graph[all]                 # 所有可选依赖
```

OpenAI 兼容嵌入（真实 OpenAI、Azure，或自建网关如 new-api / LiteLLM / vLLM / LocalAI / Ollama openai 模式）无需额外安装 —— 只需设置环境变量并在 `embed_graph` 中传入 `provider="openai"`：

```bash
export CRG_OPENAI_BASE_URL=http://127.0.0.1:3000/v1     # 或 https://api.openai.com/v1
export CRG_OPENAI_API_KEY=sk-...
export CRG_OPENAI_MODEL=text-embedding-3-small          # 取决于你网关提供的模型
# 可选：
export CRG_OPENAI_DIMENSION=1536                        # 固定维度（v3 模型支持维度缩减）
export CRG_OPENAI_BATCH_SIZE=100                        # 某些网关有更严格的批次限制时下调
                                                        # （如 Qwen text-embedding-v4 上限为 10）
```

当 base URL 指向 localhost（`127.0.0.1`、`localhost`、`0.0.0.0`、`::1`）时，会自动跳过云出口警告。

> **模型选择提示。** 避免用 `-preview` / `-beta` / `-exp` 结尾的 model ID（例如 `google/gemini-embedding-2-preview`）做长期使用——preview 模型可能更换权重（维度一变就要全量 re-embed）或被无预警下架。建议改用正式 GA 模型：`text-embedding-3-small` / `text-embedding-3-large`（OpenAI）、`Qwen/Qwen3-Embedding-8B`（经 vLLM / LocalAI 自宿主）、或 `gemini-embedding-001`（经原生 Gemini provider，需要 `GOOGLE_API_KEY`）。
>
> 另外请注意：目前 `code-review-graph` 只嵌入**函数签名**（每节点约 10 tokens，例如 `"parse_file function (path: str) returns Tree"`）。那些靠长 context 理解函数 body 来拉开差距的模型（Gemini 2 或 Qwen3-8B 在 MTEB-code 的 SOTA 分数）在这个输入长度下跟小模型的品质差距会小很多。Body / docstring 嵌入已列为后续增强任务。

</details>

---

## 参与贡献

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

<details>
<summary><strong>添加新语言支持</strong></summary>
<br>

编辑 `code_review_graph/parser.py`，将你的文件扩展名添加到 `EXTENSION_TO_LANGUAGE`，并在 `_CLASS_TYPES`、`_FUNCTION_TYPES`、`_IMPORT_TYPES` 和 `_CALL_TYPES` 中添加节点类型映射。附上测试用例文件，然后提交 PR。

</details>

## 许可证

MIT。详见 [LICENSE](LICENSE)。

<p align="center">
<br>
<a href="https://code-review-graph.com">code-review-graph.com</a><br><br>
<code>pip install code-review-graph && code-review-graph install</code><br>
<sub>支持 Codex、Claude Code、Cursor、Windsurf、Zed、Continue、OpenCode、Antigravity 和 Kiro</sub>
</p>
