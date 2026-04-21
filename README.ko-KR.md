<h1 align="center">code-review-graph</h1>

<p align="center">
  <strong>토큰 낭비를 멈추세요. 더 스마트하게 리뷰하세요.</strong>
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

AI 코딩 도구는 매 작업마다 전체 코드베이스를 다시 읽습니다. `code-review-graph`는 이 문제를 해결합니다. [Tree-sitter](https://tree-sitter.github.io/tree-sitter/)로 코드의 구조적 맵을 구축하고, 변경 사항을 점진적으로 추적하며, [MCP](https://modelcontextprotocol.io/)를 통해 AI 어시스턴트에게 정확한 컨텍스트를 제공하여 필요한 부분만 읽도록 합니다.

<p align="center">
  <img src="diagrams/diagram1_before_vs_after.png" alt="토큰 문제: 6개 실제 저장소에서 평균 8.2배 토큰 절감" width="85%" />
</p>

---

## 빠른 시작

```bash
pip install code-review-graph                     # 또는: pipx install code-review-graph
code-review-graph install          # 지원되는 모든 플랫폼을 자동 감지하고 설정
code-review-graph build            # 코드베이스 파싱
```

하나의 명령으로 모든 설정이 완료됩니다. `install`은 사용 중인 AI 코딩 도구를 감지하고, 각 도구에 맞는 MCP 설정을 작성하며, 플랫폼 규칙에 그래프 인식 지침을 주입합니다. `uvx` 또는 `pip`/`pipx` 중 어떤 방식으로 설치했는지 자동 감지하여 올바른 설정을 생성합니다. 설치 후 에디터/도구를 재시작하세요.

<p align="center">
  <img src="diagrams/diagram8_supported_platforms.png" alt="한 번의 설치로 모든 플랫폼 지원: Codex, Claude Code, Cursor, Windsurf, Zed, Continue, OpenCode, Antigravity, Kiro 자동 감지" width="85%" />
</p>

특정 플랫폼만 설정하려면:

```bash
code-review-graph install --platform codex       # Codex만 설정
code-review-graph install --platform cursor      # Cursor만 설정
code-review-graph install --platform claude-code  # Claude Code만 설정
code-review-graph install --platform kiro         # Kiro만 설정
```

Python 3.10 이상이 필요합니다. 최상의 경험을 위해 [uv](https://docs.astral.sh/uv/)를 설치하세요 (MCP 설정은 `uvx`가 있으면 이를 사용하고, 없으면 `code-review-graph` 명령을 직접 사용합니다).

프로젝트를 열고 AI 어시스턴트에게 다음과 같이 요청하세요:

```
Build the code review graph for this project
```

초기 빌드는 500개 파일 프로젝트 기준 약 10초가 소요됩니다. 이후에는 파일 편집 및 git 커밋마다 그래프가 자동으로 업데이트됩니다.

---

## 작동 원리

<p align="center">
  <img src="diagrams/diagram7_mcp_integration_flow.png" alt="AI 어시스턴트의 그래프 활용: 사용자가 리뷰 요청, AI가 MCP 도구 확인, 그래프가 영향 범위와 위험 점수 반환, AI가 필요한 부분만 읽음" width="80%" />
</p>

저장소를 Tree-sitter로 AST로 파싱하고, 노드(함수, 클래스, import)와 엣지(호출, 상속, 테스트 커버리지)의 그래프로 SQLite에 저장한 후, 리뷰 시점에 쿼리하여 AI 어시스턴트가 읽어야 할 최소한의 파일 집합을 계산합니다.

<p align="center">
  <img src="diagrams/diagram2_architecture_pipeline.png" alt="아키텍처 파이프라인: 저장소에서 Tree-sitter 파서, SQLite 그래프, 영향 범위, 최소 리뷰 세트까지" width="100%" />
</p>

### 영향 범위 분석

파일이 변경되면, 그래프는 영향을 받을 수 있는 모든 호출자, 의존 대상, 테스트를 추적합니다. 이것이 변경의 "영향 범위"입니다. AI는 전체 프로젝트를 스캔하는 대신 이 파일들만 읽습니다.

<p align="center">
  <img src="diagrams/diagram3_blast_radius.png" alt="login() 변경이 호출자, 의존 대상, 테스트로 전파되는 영향 범위 시각화" width="70%" />
</p>

### 2초 미만의 점진적 업데이트

git 커밋이나 파일 저장마다 훅이 실행됩니다. 그래프는 변경된 파일을 비교하고, SHA-256 해시 검사를 통해 의존 대상을 찾으며, 변경된 부분만 다시 파싱합니다. 2,900개 파일 프로젝트의 재인덱싱이 2초 이내에 완료됩니다.

<p align="center">
  <img src="diagrams/diagram4_incremental_update.png" alt="점진적 업데이트 흐름: git 커밋이 diff를 트리거하고, 의존 대상을 찾고, 5개 파일만 다시 파싱하며 2,910개는 건너뜀" width="90%" />
</p>

### 모노레포 문제 해결

대규모 모노레포에서 토큰 낭비가 가장 심합니다. 그래프는 불필요한 파일을 제거합니다 -- 27,700개 이상의 파일이 리뷰 컨텍스트에서 제외되고, 실제로 읽는 파일은 약 15개뿐입니다.

<p align="center">
  <img src="diagrams/diagram6_monorepo_funnel.png" alt="Next.js 모노레포: 27,732개 파일이 code-review-graph를 거쳐 약 15개 파일로 -- 49배 적은 토큰" width="80%" />
</p>

### 23개 언어 + Jupyter 노트북

<p align="center">
  <img src="diagrams/diagram9_language_coverage.png" alt="카테고리별 19개 언어: 웹, 백엔드, 시스템, 모바일, 스크립팅, 그리고 Jupyter/Databricks 노트북 지원" width="90%" />
</p>

모든 언어에서 함수, 클래스, import, 호출 위치, 상속, 테스트 감지를 위한 완전한 Tree-sitter 문법을 지원합니다. Zig, PowerShell, Julia, Svelte SFC도 포함됩니다. 다중 언어 셀 지원(Python, R, SQL)이 가능한 Jupyter/Databricks 노트북(`.ipynb`) 파싱과 Perl XS 파일(`.xs`)도 지원합니다.

---

## 벤치마크

<p align="center">
  <img src="diagrams/diagram5_benchmark_board.png" alt="실제 저장소 벤치마크: 4.9배에서 27.3배 적은 토큰, 더 높은 리뷰 품질" width="85%" />
</p>

모든 수치는 6개 실제 오픈소스 저장소(총 13개 커밋)에 대한 자동화된 평가 실행 결과입니다. `code-review-graph eval --all`로 재현할 수 있습니다. 원본 데이터는 [`evaluate/reports/summary.md`](evaluate/reports/summary.md)에 있습니다.

전체 벤치마크 결과는 [영문 README](README.md#benchmarks)를 참조하세요.

---

## 기능

| 기능 | 세부 사항 |
|------|-----------|
| **점진적 업데이트** | 변경된 파일만 다시 파싱합니다. 이후 업데이트는 2초 이내에 완료됩니다. |
| **23개 언어 + 노트북** | Python, TypeScript/TSX, JavaScript, Vue, Svelte, Go, Rust, Java, Scala, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++, Dart, R, Perl, Lua, Zig, PowerShell, Julia, Jupyter/Databricks (.ipynb) |
| **영향 범위 분석** | 변경에 의해 영향 받는 함수, 클래스, 파일을 정확히 보여줍니다 |
| **자동 업데이트 훅** | 수동 개입 없이 파일 편집 및 git 커밋마다 그래프가 업데이트됩니다 |
| **시맨틱 검색** | sentence-transformers, Google Gemini, MiniMax, 또는 OpenAI 호환 엔드포인트(실제 OpenAI, Azure, new-api, LiteLLM, vLLM, LocalAI)를 통한 선택적 벡터 임베딩 |
| **인터랙티브 시각화** | 검색, 커뮤니티 범례 토글, 차수 기반 노드 크기 조정이 가능한 D3.js 포스 다이렉티드 그래프 |
| **허브 및 브릿지 감지** | 가장 많이 연결된 노드와 매개 중심성을 통한 아키텍처 병목 지점 탐색 |
| **서프라이즈 스코어링** | 예상치 못한 결합 감지: 커뮤니티 간, 언어 간, 주변부-허브 엣지 |
| **지식 격차 분석** | 고립된 노드, 테스트되지 않은 핫스팟, 얇은 커뮤니티, 구조적 약점 식별 |
| **질문 제안** | 그래프 분석(브릿지, 허브, 서프라이즈)을 기반으로 자동 생성되는 리뷰 질문 |
| **엣지 신뢰도** | 엣지에 실수 점수를 포함한 3단계 신뢰도 평가 (EXTRACTED/INFERRED/AMBIGUOUS) |
| **그래프 탐색** | 임의 노드에서 설정 가능한 깊이와 토큰 예산으로 자유 형식 BFS/DFS 탐색 |
| **내보내기 형식** | GraphML (Gephi/yEd), Neo4j Cypher, Obsidian vault (위키링크), SVG 정적 그래프 |
| **그래프 비교** | 시간에 따른 그래프 스냅샷 비교: 새로운/삭제된 노드, 엣지, 커뮤니티 변경 |
| **토큰 벤치마킹** | 전체 코퍼스 토큰 대비 그래프 쿼리 토큰을 질문별 비율로 측정 |
| **메모리 루프** | Q&A 결과를 마크다운으로 저장하여 재수집, 쿼리로 그래프가 성장 |
| **커뮤니티 자동 분할** | 과대 커뮤니티(그래프의 25% 초과)를 Leiden 알고리즘으로 재귀적 분할 |
| **실행 흐름** | 가중 중요도 순으로 정렬된 진입점에서의 호출 체인 추적 |
| **커뮤니티 감지** | 대규모 그래프를 위한 해상도 스케일링이 포함된 Leiden 알고리즘으로 관련 코드 클러스터링 |
| **아키텍처 개요** | 결합 경고가 포함된 자동 생성 아키텍처 맵 |
| **위험 점수 리뷰** | `detect_changes`가 diff를 영향 받는 함수, 흐름, 테스트 격차에 매핑 |
| **리팩토링 도구** | 이름 변경 미리보기, 프레임워크 인식 데드 코드 감지, 커뮤니티 기반 제안 |
| **위키 생성** | 커뮤니티 구조에서 마크다운 위키 자동 생성 |
| **멀티 레포 레지스트리** | 여러 저장소를 등록하고 모든 저장소에서 검색 |
| **MCP 프롬프트** | 5개 워크플로 템플릿: 리뷰, 아키텍처, 디버그, 온보딩, 사전 머지 검사 |
| **전문 검색** | FTS5 기반 키워드와 벡터 유사도를 결합한 하이브리드 검색 |
| **로컬 스토리지** | `.code-review-graph/`에 SQLite 파일 저장. 외부 데이터베이스나 클라우드 의존성 없음. |
| **감시 모드** | 작업 중 지속적인 그래프 업데이트 |

---

## 사용법

<details>
<summary><strong>슬래시 명령</strong></summary>
<br>

| 명령 | 설명 |
|------|------|
| `/code-review-graph:build-graph` | 코드 그래프 빌드 또는 재빌드 |
| `/code-review-graph:review-delta` | 마지막 커밋 이후 변경 사항 리뷰 |
| `/code-review-graph:review-pr` | 영향 범위 분석을 포함한 전체 PR 리뷰 |

</details>

<details>
<summary><strong>CLI 레퍼런스</strong></summary>
<br>

```bash
code-review-graph install          # 모든 플랫폼 자동 감지 및 설정
code-review-graph install --platform <name>  # 특정 플랫폼 지정
code-review-graph build            # 전체 코드베이스 파싱
code-review-graph update           # 점진적 업데이트 (변경 파일만)
code-review-graph status           # 그래프 통계
code-review-graph watch            # 파일 변경 시 자동 업데이트
code-review-graph visualize        # 인터랙티브 HTML 그래프 생성
code-review-graph visualize --format graphml   # GraphML로 내보내기
code-review-graph visualize --format svg       # SVG로 내보내기
code-review-graph visualize --format obsidian  # Obsidian vault로 내보내기
code-review-graph visualize --format cypher    # Neo4j Cypher로 내보내기
code-review-graph wiki             # 커뮤니티에서 마크다운 위키 생성
code-review-graph detect-changes   # 위험 점수 기반 변경 영향 분석
code-review-graph register <path>  # 멀티 레포 레지스트리에 저장소 등록
code-review-graph unregister <id>  # 레지스트리에서 저장소 제거
code-review-graph repos            # 등록된 저장소 목록
code-review-graph eval             # 평가 벤치마크 실행
code-review-graph serve            # MCP 서버 시작
```

</details>

<details>
<summary><strong>28개 MCP 도구</strong></summary>
<br>

그래프가 빌드되면 AI 어시스턴트가 이 도구들을 자동으로 사용합니다.

| 도구 | 설명 |
|------|------|
| `build_or_update_graph_tool` | 그래프 빌드 또는 점진적 업데이트 |
| `get_minimal_context_tool` | 초소형 컨텍스트 (~100 토큰) -- 이것을 먼저 호출 |
| `get_impact_radius_tool` | 변경된 파일의 영향 범위 |
| `get_review_context_tool` | 구조적 요약이 포함된 토큰 최적화 리뷰 컨텍스트 |
| `query_graph_tool` | 호출자, 피호출자, 테스트, import, 상속 쿼리 |
| `traverse_graph_tool` | 토큰 예산으로 임의 노드에서 BFS/DFS 탐색 |
| `semantic_search_nodes_tool` | 이름이나 의미로 코드 엔티티 검색 |
| `embed_graph_tool` | 시맨틱 검색을 위한 벡터 임베딩 계산 |
| `list_graph_stats_tool` | 그래프 크기 및 상태 |
| `get_docs_section_tool` | 문서 섹션 조회 |
| `find_large_functions_tool` | 줄 수 임계값을 초과하는 함수/클래스 찾기 |
| `list_flows_tool` | 중요도 순으로 정렬된 실행 흐름 목록 |
| `get_flow_tool` | 단일 실행 흐름의 세부 정보 |
| `get_affected_flows_tool` | 변경된 파일에 영향 받는 흐름 찾기 |
| `list_communities_tool` | 감지된 코드 커뮤니티 목록 |
| `get_community_tool` | 단일 커뮤니티의 세부 정보 |
| `get_architecture_overview_tool` | 커뮤니티 구조의 아키텍처 개요 |
| `detect_changes_tool` | 코드 리뷰를 위한 위험 점수 기반 변경 영향 분석 |
| `get_hub_nodes_tool` | 가장 많이 연결된 노드 (아키텍처 핫스팟) 찾기 |
| `get_bridge_nodes_tool` | 매개 중심성을 통한 병목 지점 찾기 |
| `get_knowledge_gaps_tool` | 구조적 약점 및 테스트되지 않은 핫스팟 식별 |
| `get_surprising_connections_tool` | 예상치 못한 커뮤니티 간 결합 감지 |
| `get_suggested_questions_tool` | 분석에서 자동 생성된 리뷰 질문 |
| `refactor_tool` | 이름 변경 미리보기, 데드 코드 감지, 제안 |
| `apply_refactor_tool` | 미리보기한 리팩토링 적용 |
| `generate_wiki_tool` | 커뮤니티에서 마크다운 위키 생성 |
| `get_wiki_page_tool` | 특정 위키 페이지 조회 |
| `list_repos_tool` | 등록된 저장소 목록 |
| `cross_repo_search_tool` | 등록된 모든 저장소에서 검색 |

**MCP 프롬프트** (5개 워크플로 템플릿):
`review_changes`, `architecture_map`, `debug_issue`, `onboard_developer`, `pre_merge_check`

</details>

<details>
<summary><strong>설정</strong></summary>
<br>

인덱싱에서 경로를 제외하려면 저장소 루트에 `.code-review-graphignore` 파일을 생성하세요:

```
generated/**
*.generated.ts
vendor/**
node_modules/**
```

참고: git 저장소에서는 추적되는 파일만 인덱싱됩니다 (`git ls-files`). gitignore된 파일은 자동으로 건너뜁니다. `.code-review-graphignore`는 추적되는 파일을 제외하거나 git을 사용할 수 없을 때 사용합니다.

선택적 의존성 그룹:

```bash
pip install code-review-graph[embeddings]          # 로컬 벡터 임베딩 (sentence-transformers)
pip install code-review-graph[google-embeddings]   # Google Gemini 임베딩
pip install code-review-graph[communities]         # 커뮤니티 감지 (igraph)
pip install code-review-graph[eval]                # 평가 벤치마크 (matplotlib)
pip install code-review-graph[wiki]                # LLM 요약 위키 생성 (ollama)
pip install code-review-graph[all]                 # 모든 선택적 의존성
```

OpenAI 호환 임베딩(실제 OpenAI, Azure, 또는 자체 호스팅 게이트웨이 new-api / LiteLLM / vLLM / LocalAI / Ollama openai 모드)은 추가 설치가 필요하지 않습니다. 환경 변수만 설정하고 `embed_graph`에 `provider="openai"`를 전달하면 됩니다:

```bash
export CRG_OPENAI_BASE_URL=http://127.0.0.1:3000/v1     # 또는 https://api.openai.com/v1
export CRG_OPENAI_API_KEY=sk-...
export CRG_OPENAI_MODEL=text-embedding-3-small          # 게이트웨이에서 제공하는 모델
# 선택:
export CRG_OPENAI_DIMENSION=1536                        # 차원 고정 (v3 모델은 차원 축소 지원)
export CRG_OPENAI_BATCH_SIZE=100                        # 배치 제한이 엄격한 게이트웨이에서 낮추기
                                                        # (예: Qwen text-embedding-v4 는 최대 10)
```

base URL이 localhost(`127.0.0.1`, `localhost`, `0.0.0.0`, `::1`)를 가리킬 때는 클라우드 egress 경고가 자동으로 생략됩니다.

> **모델 선택 팁.** `-preview` / `-beta` / `-exp` 접미사가 붙은 model ID(예: `google/gemini-embedding-2-preview`)는 장기 운영용으로 피하세요. preview 모델은 가중치가 바뀌거나(차원 변경 시 전체 re-embed 필수) 예고 없이 deprecate될 수 있습니다. 안정 GA 모델 권장: `text-embedding-3-small` / `text-embedding-3-large`(OpenAI), `Qwen/Qwen3-Embedding-8B`(vLLM / LocalAI 자체 호스팅 경유), 또는 `gemini-embedding-001`(네이티브 Gemini provider 경유, `GOOGLE_API_KEY` 필요).
>
> 참고로 현재 `code-review-graph`는 **함수 시그니처만** 임베딩합니다(노드당 약 10 토큰, 예: `"parse_file function (path: str) returns Tree"`). 긴 context로 함수 body를 이해하는 능력으로 차별화되는 모델(Gemini 2 또는 Qwen3-8B의 MTEB-code SOTA 점수)은 이 입력 길이에서 소형 모델과의 품질 차이가 훨씬 좁아집니다. Body / docstring 임베딩은 후속 개선 과제로 추적 중입니다.

</details>

---

## 기여

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

<details>
<summary><strong>새 언어 추가</strong></summary>
<br>

`code_review_graph/parser.py`를 편집하여 `EXTENSION_TO_LANGUAGE`에 확장자를 추가하고, `_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES`, `_CALL_TYPES`에 노드 타입 매핑을 추가하세요. 테스트 픽스처를 포함하여 PR을 제출하세요.

</details>

## 라이선스

MIT. [LICENSE](LICENSE)를 참조하세요.

<p align="center">
<br>
<a href="https://code-review-graph.com">code-review-graph.com</a><br><br>
<code>pip install code-review-graph && code-review-graph install</code><br>
<sub>Codex, Claude Code, Cursor, Windsurf, Zed, Continue, OpenCode, Antigravity, Kiro에서 사용 가능</sub>
</p>
