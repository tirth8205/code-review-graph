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
  <strong>MCP로 AI 코딩 도구에 정확한 리뷰 맥락을 전달하는 로컬 코드 지식 그래프.</strong>
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
  <a href="docs/USAGE.md">사용법</a> ·
  <a href="docs/COMMANDS.md">명령어</a> ·
  <a href="docs/FAQ.md">FAQ</a> ·
  <a href="docs/TROUBLESHOOTING.md">문제 해결</a> ·
  <a href="docs/GITHUB_ACTION.md">GitHub Action</a> ·
  <a href="docs/REPRODUCING.md">벤치마크 재현</a> ·
  <a href="docs/ROADMAP.md">로드맵</a>
</p>

<br>

AI 코딩 도구는 변경 하나를 리뷰하려고 코드베이스의 많은 부분을 다시 읽곤 합니다. `code-review-graph`는 [Tree-sitter](https://tree-sitter.github.io/tree-sitter/)로 코드의 구조 지도를 만들고, 증분으로 최신 상태를 유지하며, [MCP](https://modelcontextprotocol.io/)를 통해 작은 맥락을 제공합니다. 그래서 어시스턴트는 변경이 건드리는 파일만 읽습니다.

<p align="center">
  <img src="diagrams/diagram1_before_vs_after.png" alt="토큰 문제: flask 전체 코퍼스를 읽으면 143,594 토큰, 그래프 응답은 2,196 토큰(65배 적음)" width="85%" />
</p>

---

## 빠른 시작

```bash
pip install code-review-graph          # or: pipx install code-review-graph
code-review-graph install              # detect installed AI coding tools and configure each one
code-review-graph build                # parse the codebase
```

`install`은 설치된 AI 코딩 도구를 찾아 각각에 MCP 서버 항목을 쓰고, 플랫폼이 지원하면 훅과 스킬을 설치하며, 플랫폼의 규칙 파일에 그래프 안내를 추가합니다. MCP 항목은 Poetry나 uv 프로젝트 환경에서는 `poetry run` 또는 `uv run`을, PATH에 `uvx`가 있으면 `uvx code-review-graph serve`를, 그 밖에는 현재 Python 인터프리터를 사용합니다. 실행 후 편집기나 도구를 다시 시작하세요.

<p align="center">
  <img src="diagrams/diagram8_supported_platforms.png" alt="한 번의 설치로 모든 플랫폼 지원: Codex, Claude Code, CodeBuddy Code, Cursor, Windsurf, Zed, Continue, OpenCode, Antigravity, Gemini CLI, Qwen, Qoder, Kiro, GitHub Copilot, GitHub Copilot CLI, Hermes Agent를 감지" width="85%" />
</p>

플랫폼 하나만 설정하려면 `--platform`에 `codex`, `claude-code`, `cursor`, `windsurf`, `zed`, `continue`, `opencode`, `antigravity`, `gemini-cli`, `qwen`, `kiro`, `qoder`, `copilot`, `copilot-cli`, `codebuddy`, `hermes` 중 하나를 넘기세요.

```bash
code-review-graph install --platform cursor
code-review-graph install --platform codebuddy
```

설정 파일 위치는 [docs/USAGE.md](docs/USAGE.md#supported-platforms)에 있습니다. Python 3.10+가 필요합니다.

`uninstall`은 Git 또는 SVN 작업 트리에서 CRG가 만든 파일과 항목을 제거하고, 다른 MCP 서버, 훅, 스킬, JSONC 주석은 건드리지 않습니다. 트리 안 어디에서든 실행할 수 있습니다. 공유 설정 파일은 원자적으로 교체되므로 쓰기가 실패해도 원본은 그대로 남습니다.

```bash
code-review-graph uninstall --dry-run    # preview only
code-review-graph uninstall              # preview, confirm, apply
code-review-graph uninstall --yes        # apply without prompting
code-review-graph uninstall --all-repos  # also clean every registered repository
code-review-graph uninstall --keep-data  # remove integrations, keep graph databases
code-review-graph uninstall --keep-user-configs --repo .  # this project only
```

그런 다음 프로젝트를 열고 어시스턴트에게 이렇게 요청하세요.

```
Build the code review graph for this project
```

빌드 시간은 저장소 크기에 비례합니다. 파일이 약 3,000개인 저장소의 콜드 빌드는 약 40초가 걸렸습니다([측정값](docs/REPRODUCING.md#incremental-update-latency)). 그 뒤로는 훅과 watch 모드가 그래프를 최신으로 유지합니다. 일부 파일의 파싱이 실패하면 결과 상태가 `partial`이 되고 요약에 해당 파일이 나열됩니다. CLI는 stderr에 `Warning:` 줄도 출력하며, 그 파일들은 기존 그래프 행을 유지합니다.


## 동작 방식

<p align="center">
  <img src="diagrams/diagram7_mcp_integration_flow.png" alt="어시스턴트가 그래프를 쓰는 흐름: 사용자가 리뷰를 요청하고, 어시스턴트가 MCP 도구를 호출하고, 그래프가 영향 범위와 위험 점수를 돌려주고, 어시스턴트는 영향받은 파일만 읽는다" width="80%" />
</p>

저장소는 Tree-sitter로 AST로 파싱되어 노드(함수, 클래스, 임포트)와 엣지(호출, 상속, 테스트 커버리지)의 그래프로 저장됩니다. 리뷰 시점에는 어시스턴트가 읽어야 할 최소 파일 집합을 그래프에 질의합니다.

<p align="center">
  <img src="diagrams/diagram2_architecture_pipeline.png" alt="아키텍처 파이프라인: 저장소 → Tree-sitter 파서 → SQLite 그래프 → 영향 범위 → 최소 리뷰 집합" width="100%" />
</p>

### 영향 범위 분석

파일이 바뀌면 그래프는 영향을 받을 수 있는 모든 호출자, 의존 대상, 테스트를 추적합니다. 어시스턴트는 프로젝트 전체를 훑는 대신 그 파일들을 읽습니다.

<p align="center">
  <img src="diagrams/diagram3_blast_radius.png" alt="영향 범위: login() 변경이 호출자, 의존 대상, 테스트로 퍼진다" width="70%" />
</p>

### 증분 업데이트

훅, pre-commit 훅, watch 모드가 증분 업데이트를 실행합니다. 업데이트는 변경된 파일의 diff를 구하고, 그래프의 임포트 엣지와 호출 엣지를 따라 의존 대상을 찾은 뒤, SHA-256 해시가 바뀐 파일만 다시 파싱합니다. 파일이 약 3,000개인 프로젝트(django)에서 파일 두 개를 고치면 훅 경로에서 약 2.5초 만에 재색인되며, 그중 약 1.4초는 프로세스 시작 시간입니다. 바뀐 것이 없으면 그 시작 시간만 듭니다. [증분 업데이트 지연](docs/REPRODUCING.md#incremental-update-latency)을 참고하세요.

<p align="center">
  <img src="diagrams/diagram4_incremental_update.png" alt="증분 업데이트 흐름: 훅이나 watch 업데이트가 git diff를 실행하고, 그래프 엣지로 의존 대상을 찾고, SHA-256 해시가 바뀐 파일만 다시 파싱한다" width="90%" />
</p>

### 코드베이스 전체인가, 겨냥한 답인가?

그래프는 코퍼스 전체를 모델에 넘기는 대신 질문에 맞춰 잘라낸 조각을 돌려줍니다. 이 저장소의 `84bde354`에 대한 2026-08-02 측정에서는 208,821개의 소스 토큰이 질문당 약 3,190 토큰이 되었습니다. 그 스냅샷 이후 저장소가 많이 커져서 오늘은 두 숫자 모두 더 큽니다.

<p align="center">
  <img src="diagrams/diagram6_monorepo_funnel.png" alt="code-review-graph의 84bde354 스냅샷: 208,821개의 소스 토큰이 약 3,190 토큰의 그래프 응답으로 좁혀져, 질문당 토큰이 약 65배 줄어든다" width="80%" />
</p>

### 언어 지원과 노트북

<p align="center">
  <img src="diagrams/diagram9_language_coverage.png" alt="분류별 언어 지원: 웹, 백엔드, 시스템, 모바일, 스크립팅, 셸, 도메인, 기타, 그리고 Jupyter와 Databricks 노트북" width="90%" />
</p>

파서는 함수, 클래스, 임포트, 호출 지점, 상속, 테스트를 추출합니다. 문법이 있는 곳에서는 Tree-sitter를, 그 밖에는 목적에 맞춘 폴백을 씁니다. 지원: Python, JavaScript/TypeScript/TSX, Go, Rust, Java, C/C++, C#, VB.NET, Ruby, Kotlin, Swift, PHP, Scala, Solidity, Dart, R, Perl, Lua/Luau, Objective-C, 셸 스크립트, Elixir, Zig, PowerShell, Julia, ReScript, GDScript, Nix, Verilog/SystemVerilog, SQL, Terraform/OpenTofu(`.tf`. 다른 `.hcl` 파일은 파일 노드만 생성), Ansible YAML(플레이북, 롤, 태스크), Spring Boot 애플리케이션 설정(`application.properties`, `application.yml`, `application.yaml`과 그 `application-<profile>` 변형. 키 이름과 값의 타입만 기록하고 값은 절대 기록하지 않음), Vue/Svelte SFC, Astro 파일(TypeScript 문법으로 파싱), Jupyter와 Databricks 노트북(`.ipynb`), Perl XS 파일(`.xs`). 다른 YAML과 다른 `.properties` 파일은 소스 코드로 취급하지 않습니다.

PHP 프로젝트에는 저장소 범위로 제한된 Composer PSR-4 해석, Blade 템플릿 참조, 그리고 소스에 명시적인 프레임워크 임포트, 모델 상속, 수신자 근거가 드러날 때의 Laravel Route 및 Eloquent 엣지가 추가됩니다.

Java 프로젝트에는 Spring 의존성 주입 호출 해석, 요청 엔드포인트와 WebFlux 라우트, 스케줄 트리거, 애플리케이션 이벤트의 발행자에서 리스너로 가는 엣지, Temporal 워크플로와 액티비티 엣지가 추가됩니다. 각 리졸버는 파싱 이후에 실행되며, 주입된 필드나 발행된 이벤트, 워크플로 스텁이 저장소 안에서 보여야 합니다.

### 직접 언어 추가하기

파서가 다루지 않는 언어를 저장소에서 쓴다면 `.code-review-graph/`에 `languages.toml`을 추가해, 파일 확장자를 `tree_sitter_language_pack`에 들어 있는 아무 문법에 연결하고 함수, 클래스, 임포트, 호출의 노드 타입을 적으세요.

```toml
[languages.erlang]
extensions = [".erl"]
grammar = "erlang"
function_node_types = ["function_clause"]
class_node_types = ["record_decl"]
import_node_types = ["import_attribute"]
call_node_types = ["call"]
```

추출은 범용 tree-sitter 워커가 합니다. 내장 언어는 덮어쓸 수 없습니다. 스키마, 검증 규칙, 실제 예시는 [docs/CUSTOM_LANGUAGES.md](docs/CUSTOM_LANGUAGES.md)를 보세요.

### CI에서의 위험 점수 PR 리뷰(GitHub Action)

같은 분석이 composite GitHub Action으로도 동작합니다. 그래프는 사용자의 CI 러너에서 만들어지고 질의되며, 소스 코드가 외부 서비스로 전송되지 않습니다. 풀 리퀘스트마다 위험 점수가 매겨진 함수, 영향받는 실행 흐름, 테스트 공백을 담은 고정 댓글 하나를 남기고, 푸시할 때마다 그 자리에서 갱신합니다. 선택 입력 `fail-on-risk`를 쓰면 머지 게이트가 됩니다.

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

입력값, 위험 등급, 캐싱은 [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md)를, 이 저장소가 스스로에게 돌리는 워크플로는 [`.github/workflows/pr-review.yml`](.github/workflows/pr-review.yml)을 보세요.

---

## 벤치마크

<p align="center">
  <img src="diagrams/diagram5_benchmark_board.png" alt="저장소 6곳 벤치마크: 질문당 토큰 감소 중앙값 약 63배(최대 358배), 그래프에서 유도한 정답에 대한 영향 분석 평균 F1 0.69" width="85%" />
</p>

저장소 6곳에서 질문당 토큰 감소의 중앙값은 약 **63배**입니다(코퍼스 전체 기준선 대 그래프 질의). **358배**라는 최댓값은 저장소 하나(fastapi, 가장 큰 코퍼스)의 값이지 일반적인 결과가 아닙니다.

모든 숫자는 오픈소스 저장소 6곳(커밋 13개)에 대한 평가 러너에서 나옵니다. 모든 설정이 업스트림 SHA를 고정하고, Leiden은 고정 시드로 돌며, 임베딩은 CPU에서 결정적이므로 다른 기기에서 두 번 실행해도 같은 숫자가 나옵니다. 재현 방법은 [`docs/REPRODUCING.md`](docs/REPRODUCING.md)에 있습니다. 가장 작은 설정 두 개에 대한 주간 보고 전용 실행은 [`.github/workflows/eval.yml`](.github/workflows/eval.yml)에 있습니다.

<details>
<summary><strong>토큰 효율: 질문당 감소 중앙값 약 63배(범위 35배에서 358배. 코퍼스 전체 대 그래프 질의)</strong></summary>
<br>

전형적인 에이전트 질문(`"how does authentication work"`, `"what is the main entry point"` 등)에 대해 그래프는 모든 소스 파일 대신 검색 결과와 이웃 엣지로 이루어진 약 2,200에서 3,900 토큰을 돌려줍니다. 표는 `code_review_graph/token_benchmark.py`에 정의된 샘플 질문 5개를 평균한 값입니다.

| 저장소 | 스냅샷 SHA | naive_corpus_tokens | avg graph_tokens | 감소 배수 |
|------|---|-----------------:|----------------:|----------:|
| fastapi | `22381558` | 948,793 | 2,653 | **357.6x** |
| flask | `a29f88ce` | 143,594 | 2,196 | **65.4x** |
| code-review-graph | `84bde354` | 208,821 | 3,190 | **65.5x** |
| gin | `5c00df8a` | 166,868 | 2,766 | **60.3x** |
| httpx | `b55d4635` | 142,356 | 2,661 | **53.5x** |
| express | `b4ab7d65` | 136,052 | 3,936 | **34.6x** |

> 2026-08-02에 고정된 SHA의 깨끗한 클론에서 측정했습니다(crg 2.3.7, 로컬 `all-MiniLM-L6-v2` 임베딩). 이 숫자들은 이전의 2026-05-25 측정보다 낮습니다. 노드 임베딩 텍스트가 풍부해지면서 모든 저장소에서 `avg graph_tokens`가 올라갔기 때문입니다. fastapi는 폐기된 `0227991a`가 아니라 현재 핀인 `22381558`에서 측정했습니다.
>
> 감소 배수 열은 `naive_corpus_tokens / avg graph_tokens`이므로 옆의 두 열에서 그대로 나눗셈으로 얻을 수 있습니다. 벤치마크 자체의 `average_reduction_ratio`는 질문별 비율을 평균한 값이라 언제나 더 크게 나옵니다. 질문별 숫자는 [`docs/REPRODUCING.md`](docs/REPRODUCING.md#standalone-token-benchmark-code_review_graphtoken_benchmarkpy)에 있습니다.
>
> `code-review-graph` 행은 스냅샷이며 현재 측정값이 아닙니다. `84bde354` 이후 저장소가 커져서 코퍼스도 그래프도 오늘은 훨씬 큽니다.

코퍼스 전체 기준선은 실제 에이전트가 치르지 않는 상한입니다. 에이전트는 식별자를 grep하고 가장 잘 맞는 파일을 읽습니다. `agent_baseline` 평가 벤치마크가 바로 그 경우를 측정합니다(순수 파이썬으로 코퍼스를 grep하고, 일치 수 기준 상위 3개 파일을 골라, 그래프 질의 비용과 토큰을 비교). 결과는 `evaluate/results/<repo>_agent_baseline_<date>.csv`에 기록됩니다. 아직 공식 측정값은 공개하지 않았습니다.

공식 `token_efficiency` 벤치마크는 다른 상황을 측정합니다. 커밋의 변경 파일 내용만을 상대로 `get_review_context()` JSON 전체를 비교하며, 응답에 영향 범위 엣지와 소스 조각이 들어 있어 작은 커밋에서는 비율이 1보다 작게 나옵니다. 두 벤치마크는 서로 다른 질문에 답합니다. [`docs/REPRODUCING.md`](docs/REPRODUCING.md#which-benchmark-measures-what)를 보세요.

리뷰와 영향 분석 도구는 응답에 작은 `context_savings` 추정치를 붙입니다. CLI는 같은 숫자를 `Token Savings` 패널에 보여 주고(아래 사용법 참고), `--verify`는 그 값을 OpenAI의 `cl100k_base` 토크나이저와 비교합니다. 샘플 파일 222개에 대한 보정 결과, 합산 기준 추정값은 실제 토큰과 약 1% 이내로 일치했습니다([데이터](docs/REPRODUCING.md#calibration-table)).

</details>

<details>
<summary><strong>영향 분석 정확도: 그래프에서 유도한 정답에 대한 평균 F1 0.69(재현율 1.0은 순환 논리에 의한 상한)</strong></summary>
<br>

영향 범위 분석은 평가 대상 커밋 13개 모두에서 정답에 있는 파일을 전부 찾아냈습니다. 이를 "재현율 100%"가 아니라 상한으로 읽으세요. 정답(변경된 파일과 그것을 향해 호출 엣지나 임포트 엣지를 가진 파일)은 예측기가 훑는 바로 그 그래프에서 나옵니다. 정밀도가 낮은 것은 의도한 결과입니다. 파일 하나를 더 표시하는 비용이 깨진 의존을 놓치는 비용보다 작기 때문입니다.

| 저장소 | 커밋 수 | 평균 F1 | 평균 정밀도 | 재현율(그래프에서 유도한 상한) |
|------|--------:|-------:|--------------:|-------:|
| httpx | 2 | 0.863 | 0.785 | 1.0 |
| code-review-graph | 2 | 0.734 | 0.584 | 1.0 |
| fastapi | 2 | 0.697 | 0.539 | 1.0 |
| express | 2 | 0.667 | 0.500 | 1.0 |
| flask | 2 | 0.633 | 0.485 | 1.0 |
| gin | 3 | 0.609 | 0.439 | 1.0 |
| **평균** | **13** | **0.693** | **0.546** | **1.000** |

이 벤치마크는 **동시 변경 모드**도 돌립니다. 예측기에 변경 파일 하나를 주고, 같은 커밋에서 작성자가 함께 고친 다른 파일로 채점하는 방식으로, 근거가 그래프가 아니라 git 이력입니다. 두 모드 모두 결과 CSV에 나옵니다(`ground_truth_mode` 열). 2026-08-02 측정에서 동시 변경 모드는 채점한 모든 커밋에서 `predicted_files = 0`을 돌려주었기 때문에 아직 쓸 만한 측정이 아니며, 동시 변경 수치는 인용하지 않습니다.

</details>

<details>
<summary><strong>빌드 통계</strong></summary>
<br>

위에 고정된 SHA에서의 같은 2026-08-02 클린 빌드 결과입니다. File 노드는 임베딩하지 않으므로 임베딩 수가 노드 수보다 적습니다. `code-review-graph` 행은 그 스냅샷이며 지금의 저장소 상태가 아닙니다.

| 저장소 | 노드 | 엣지 | 임베딩 |
|------|------:|------:|-----------:|
| fastapi | 6,287 | 32,036 | 5,159 |
| express | 1,990 | 19,492 | 1,849 |
| gin | 1,589 | 17,237 | 1,491 |
| code-review-graph | 1,446 | 9,094 | 1,354 |
| flask | 1,415 | 8,259 | 1,329 |
| httpx | 1,263 | 8,236 | 1,193 |

</details>

### 한계

- **영향 분석의 "재현율 1.0"은 순환 논리입니다.** 과거 정답이 예측기가 따라가는 바로 그 그래프 엣지에서 나오므로, 구성상 상한일 수밖에 없습니다. 동시 변경 모드는 아직 쓸 만한 측정이 아닙니다.
- **작은 단일 파일 변경.** 사소한 편집에서는 그래프 맥락이 파일을 그냥 읽는 것보다 클 수 있습니다. 그 차이는 여러 파일 분석을 가능하게 하는 구조 메타데이터입니다.
- **검색 순위.** 키워드 검색은 보통 맞는 결과를 위쪽에 두지만 순위 매기기는 더 손봐야 합니다. 모듈 패턴 명명 때문에 Express 질의는 결과가 없을 수 있습니다.
- **흐름 탐지.** 진입점 탐지는 Python과 PHP/Laravel에서 가장 강합니다. JavaScript와 Go의 흐름 탐지는 더 손봐야 합니다.
- **정밀도와 재현율.** 영향 분석은 보수적입니다. 영향을 받을 수 있는 파일을 표시하므로 큰 의존 그래프에서는 거짓 양성이 생깁니다.

---

## 기능

| 기능 | 내용 |
|---------|---------|
| **증분 업데이트** | 해시가 바뀐 파일만 다시 파싱합니다. 파일이 약 3,000개인 저장소에서 파일 두 개 편집은 훅 경로로 약 2.5초입니다([측정값](docs/REPRODUCING.md#incremental-update-latency)). |
| **언어와 노트북 지원** | 위의 [언어 지원](#언어-지원과-노트북)을 보세요. |
| **프레임워크를 아는 PHP 파싱** | 저장소 범위로 제한된 Composer PSR-4 임포트, Blade 템플릿 참조, 근거에 기반한 Laravel Route에서 컨트롤러로 가는 엣지와 Eloquent 관계 엣지 |
| **프레임워크를 아는 Java 파싱** | Spring 의존성 주입 호출 해석, 요청 엔드포인트와 WebFlux 라우트, 스케줄 트리거, 애플리케이션 이벤트의 발행자에서 리스너로 가는 엣지, Temporal 워크플로와 액티비티 엣지, 그리고 값 없이 색인되는 Spring Boot 설정 키 |
| **영향 범위 분석** | 변경으로 영향을 받을 법한 함수, 클래스, 파일 |
| **자동 업데이트 훅** | 편집기 훅, git pre-commit 훅, watch 모드가 작업 중에 그래프를 갱신 |
| **시맨틱 검색** | 선택적 벡터 임베딩. sentence-transformers, Google Gemini, MiniMax, Voyage AI 또는 OpenAI 호환 엔드포인트(OpenAI, Azure, new-api, LiteLLM, vLLM, LocalAI) |
| **대화형 시각화** | D3.js force-directed 그래프. 검색, 커뮤니티 범례 토글, 차수에 따른 노드 크기 |
| **허브와 브리지 탐지** | 가장 많이 연결된 노드와 병목(매개 중심성) |
| **의외성 점수** | 예상 밖 결합: 커뮤니티 간, 언어 간, 주변부에서 허브로 가는 엣지 |
| **지식 공백 분석** | 고립 노드, 테스트 없는 핫스팟, 얇은 커뮤니티 |
| **추천 질문** | 브리지, 허브, 의외의 결합에서 만든 리뷰 질문 |
| **엣지 신뢰도** | 2단계 신뢰도(EXTRACTED/INFERRED)와 엣지의 실수 점수 |
| **그래프 순회** | 아무 노드에서 시작하는 BFS/DFS. 깊이와 토큰 예산 설정 가능 |
| **내보내기 형식** | GraphML(Gephi/yEd), Neo4j Cypher, Obsidian 볼트, JSON, 그리고 SVG(SVG는 `eval` 엑스트라의 matplotlib 필요) |
| **토큰 벤치마크** | `code_review_graph/token_benchmark.py`가 질문마다 코퍼스 전체 토큰과 그래프 질의 토큰을 측정 |
| **맥락 절감 추정치** | 리뷰, 영향 분석, detect-changes, 아키텍처 응답에 붙는 `context_savings` 메타데이터(`estimated`, `saved_tokens`, `saved_percent`) |
| **커뮤니티 자동 분할** | 그래프의 25%를 넘는 커뮤니티를 Leiden으로 재귀 분할 |
| **실행 흐름** | 진입점에서 이어지는 호출 사슬을 가중 중요도로 정렬 |
| **커뮤니티 탐지** | 그래프 크기에 맞춰 해상도를 조절하는 Leiden 클러스터링 |
| **아키텍처 개요** | 커뮤니티 구조 기반 아키텍처 지도와 결합 경고 |
| **위험 점수 리뷰** | `detect_changes`가 diff를 영향받는 함수, 흐름, 테스트 공백에 대응 |
| **사용자 정의 언어** | `.code-review-graph/languages.toml`로 새 언어 추가. 포크 불필요 |
| **GitHub Action** | CI에서 위험 점수가 매겨진 고정 PR 리뷰 댓글. 선택적 `fail-on-risk` 머지 게이트 |
| **리팩터링 도구** | 이름 변경 미리보기, 프레임워크를 아는 데드 코드 탐지, 커뮤니티 기반 제안 |
| **위키 생성** | 커뮤니티 구조에서 Markdown 위키 생성 |
| **다중 저장소 레지스트리** | 여러 저장소를 등록하고 가로질러 검색 |
| **다중 저장소 데몬** | `crg-daemon`이 여러 저장소를 자식 프로세스로 감시. 헬스 체크와 재시작 포함 |
| **MCP 프롬프트** | 워크플로 템플릿 5개: 리뷰, 아키텍처, 디버그, 온보딩, 머지 전 점검 |
| **전문 검색** | 키워드와 벡터 유사도를 결합한 FTS5 하이브리드 검색 |
| **로컬 저장** | `.code-review-graph/` 안의 SQLite 파일 하나. 외부 데이터베이스나 클라우드 서비스 없음 |

---

## 사용법

<details>
<summary><strong>스킬</strong></summary>
<br>

`install`은 스킬을 지원하는 플랫폼(Claude Code, Gemini CLI, CodeBuddy Code, Hermes Agent, Qoder)에 다음 네 가지 스킬을 씁니다. 이름으로 불러 쓰세요.

| 스킬 | 내용 |
|-------|-------------|
| `explore-codebase` | 지식 그래프로 코드베이스 구조를 탐색하고 이해 |
| `review-changes` | 변경 탐지와 영향 분석으로 구조화된 코드 리뷰 수행 |
| `debug-issue` | 그래프 기반 코드 탐색으로 문제를 체계적으로 디버깅 |
| `refactor-safely` | 의존성 분석으로 안전한 리팩터링을 계획하고 실행 |

Qoder에는 이 저장소의 `skills/` 디렉터리에서 `build-graph`, `review-delta`, `review-pr`도 들어갑니다.

</details>

<details>
<summary><strong>CLI 레퍼런스</strong></summary>
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

이것은 일부만 고른 목록입니다. `code-review-graph --help`가 모든 명령을 보여 주고, [docs/COMMANDS.md](docs/COMMANDS.md)가 각 플래그를 설명합니다.

`detect-changes --base`에 브랜치 이름을 주면 diff는 그 브랜치와 HEAD의 머지 베이스를 기준으로 실행됩니다. 커밋 해시와 그 밖의 리비전은 준 그대로 쓰입니다.

`visualize --format svg`에는 matplotlib이 필요하며, `eval` 엑스트라에 들어 있습니다(`pip install "code-review-graph[eval]"`). 다른 내보내기 형식에는 추가 설치가 필요 없습니다.

JSON 내보내기는 로컬 그래프 데이터 디렉터리에 기록되며, 이 디렉터리는 기본으로 Git이 무시합니다. 절대 경로와 코드 구조 메타데이터가 들어갈 수 있으니 공개하기 전에 내용을 확인하세요.

</details>

<details>
<summary><strong>Token Savings 패널: <code>detect-changes --brief</code>와 <code>update --brief</code></strong></summary>
<br>

두 명령 모두, 변경 파일을 에이전트에게 그대로 넘길 때와 비교해 그래프가 토큰을 얼마나 아꼈는지 보여 주는 같은 패널을 출력합니다. 차이는 한 가지, 그래프를 먼저 갱신하는지 여부입니다.

```text
┌─────────────────────── Token Savings ────────────────────────┐
│ Full context would be:     12,921 tokens                     │
│ Graph context used:           762 tokens                     │
│ Saved:                     12,159 tokens (~94%)              │
│ Breakdown: Functions 244 · Tests 191 · Risk 244 · Other 83   │
└──────────────────────────────────────────────────────────────┘
```

| 명령 | 하는 일 | 쓸 때 |
|---|---|---|
| `detect-changes --brief` | 읽기 전용. 현재 변경에 대해 기존 그래프에 질의하고 패널을 출력합니다. | 대부분의 경우. 훅이나 `crg-daemon`이 그래프를 최신으로 유지합니다. |
| `update --brief` | 변경 파일을 먼저 그래프에 다시 파싱한 다음 같은 패널을 출력합니다. | 리베이스 뒤, 변경 폭이 클 때, 그래프가 오래됐을 수 있을 때. |

두 명령 중 아무 쪽에나 `--verify`를 붙이면 숫자를 OpenAI의 `cl100k_base` 토크나이저와 비교합니다(`pip install tiktoken` 필요). 합산 기준 추정값은 실제 토큰과 약 1% 이내로 일치합니다. [`docs/REPRODUCING.md`](docs/REPRODUCING.md#calibration-table)를 보세요.

같은 `context_savings` 메타데이터가 `get_impact_radius`, `get_review_context`, `detect_changes`, `get_architecture_overview` MCP 도구의 JSON 응답에도 붙습니다.

</details>

<details>
<summary><strong>다중 저장소 데몬</strong></summary>
<br>

편집기가 훅을 지원하지 않거나(예: Cursor, OpenCode) 편집기 연동 없이 그래프를 최신으로 두고 싶다면, 데몬이 저장소를 감시하며 그래프를 갱신합니다. `code-review-graph`에 함께 들어 있어 따로 설치할 필요가 없습니다.

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

`code-review-graph daemon start|stop|status|...`로도 쓸 수 있습니다.

`crg-daemon add`는 `~/.code-review-graph/watch.toml`에 기록하며, 이 파일은 직접 편집해도 됩니다.

```toml
[[repos]]
path = "/home/user/project-a"
alias = "proj-a"

[[repos]]
path = "/home/user/project-b"
alias = "project-b"
```

데몬은 이 파일을 감시하며 저장소가 추가되거나 빠질 때 감시 프로세스를 켜고 끕니다. 30초마다 도는 헬스 체크가 죽은 감시 프로세스를 다시 띄웁니다.

전체 설정 레퍼런스는 [docs/COMMANDS.md](docs/COMMANDS.md#standalone-daemon-cli-crg-daemon)를 보세요.

</details>

<details>
<summary><strong>MCP 도구 30개</strong></summary>
<br>

그래프를 만들고 나면 어시스턴트가 이 도구들을 씁니다.

| 도구 | 내용 |
|------|-------------|
| `build_or_update_graph_tool` | 그래프 생성 또는 증분 갱신 |
| `run_postprocess_tool` | 흐름 탐지, 커뮤니티 탐지, FTS 색인 재실행 |
| `get_minimal_context_tool` | 작은 맥락(약 100 토큰). 이것을 먼저 호출 |
| `get_impact_radius_tool` | 변경 파일의 영향 범위 |
| `get_review_context_tool` | 구조 요약이 붙은 리뷰 맥락 |
| `query_graph_tool` | 호출자, 피호출자, 테스트, 임포트, 상속 질의 |
| `traverse_graph_tool` | 아무 노드에서 시작하는 BFS/DFS 순회. 토큰 예산 포함 |
| `semantic_search_nodes_tool` | 이름이나 의미로 코드 개체 검색 |
| `embed_graph_tool` | 시맨틱 검색용 벡터 임베딩 계산 |
| `list_graph_stats_tool` | 그래프 크기와 상태 |
| `get_docs_section_tool` | 문서 절 가져오기 |
| `find_large_functions_tool` | 줄 수 기준을 넘는 함수, 클래스, 파일 |
| `list_flows_tool` | 중요도순으로 정렬한 실행 흐름 |
| `get_flow_tool` | 실행 흐름 하나 |
| `get_affected_flows_tool` | 변경 파일이 영향을 주는 흐름 |
| `list_communities_tool` | 탐지된 코드 커뮤니티 |
| `get_community_tool` | 커뮤니티 하나 |
| `get_architecture_overview_tool` | 커뮤니티 구조 기반 아키텍처 개요 |
| `detect_changes_tool` | 위험 점수가 매겨진 변경 영향 분석 |
| `get_hub_nodes_tool` | 가장 많이 연결된 노드 |
| `get_bridge_nodes_tool` | 매개 중심성 기준 병목 |
| `get_knowledge_gaps_tool` | 구조적 약점과 테스트 없는 핫스팟 |
| `get_surprising_connections_tool` | 예상 밖의 커뮤니티 간 결합 |
| `get_suggested_questions_tool` | 분석에서 만든 리뷰 질문 |
| `refactor_tool` | 이름 변경 미리보기, 데드 코드 탐지, 제안 |
| `apply_refactor_tool` | 미리 본 리팩터링 적용 |
| `generate_wiki_tool` | 커뮤니티에서 Markdown 위키 생성 |
| `get_wiki_page_tool` | 위키 페이지 하나 |
| `list_repos_tool` | 등록된 저장소 |
| `cross_repo_search_tool` | 등록된 저장소 검색. `repos`로 대상을 좁힐 수 있음 |

**MCP 프롬프트**(워크플로 템플릿 5개):
`review_changes`, `architecture_map`, `debug_issue`, `onboard_developer`, `pre_merge_check`

</details>

<details>
<summary><strong>설정</strong></summary>
<br>

색인에서 빼고 싶은 경로가 있으면 저장소 루트에 `.code-review-graphignore` 파일을 만드세요.

```
generated/**
*.generated.ts
vendor/**
node_modules/**
```

git 저장소에서는 추적되는 파일만 색인하므로(`git ls-files`) gitignore된 파일은 건너뜁니다. 추적되는 파일을 빼거나 git을 쓸 수 없을 때 `.code-review-graphignore`를 쓰세요. 기본 무시 목록은 [docs/USAGE.md](docs/USAGE.md#ignore-patterns)에 있습니다.

선택적 의존성 그룹:

```bash
pip install "code-review-graph[embeddings]"          # Local vector embeddings (sentence-transformers)
pip install "code-review-graph[google-embeddings]"   # Google Gemini embeddings
pip install "code-review-graph[communities]"         # Community detection (igraph)
pip install "code-review-graph[enrichment]"          # Python call-resolution enrichment (Jedi)
pip install "code-review-graph[eval]"                # Evaluation benchmarks and SVG export (matplotlib)
pip install "code-review-graph[wiki]"                # ollama client (not used by the current wiki generator)
pip install "code-review-graph[all]"                 # All optional dependencies
```

### 환경 변수

| 변수 | 내용 | 기본값 |
|----------|-------------|---------|
| `CRG_GIT_TIMEOUT` | Git 작업의 제한 시간(초, build / update / watch) | `30` |
| `CRG_DISCOVERY_TIMEOUT` | 파일 목록이 주어지지 않았을 때 변경 내용을 찾는 각 Git 명령의 제한 시간(초). 초과하면 오류를 반환하며 '변경 없음'으로 보고하지 않음 | `5`(`CRG_GIT_TIMEOUT`을 직접 설정하면 그 값) |
| `CRG_DATA_DIR` | 그래프 데이터베이스와 생성물이 놓이는 디렉터리 | - |
| `CRG_HOOK_WORKTREES` | `1`로 두면 pre-commit 훅이 연결된 git worktree에서도 실행 | - |
| `CRG_EMBEDDING_MODEL` | 로컬 벡터 임베딩의 기본 모델 | `all-MiniLM-L6-v2` |
| `CRG_ACCEPT_CLOUD_EMBEDDINGS` | `1`로 두면 클라우드 임베딩 전송 경고를 숨김 | - |
| `CRG_ALLOW_REMOTE_CODE` | `trust_remote_code=True`가 필요한 HuggingFace 모델 허용 | `0` |
| `CRG_MAX_IMPACT_NODES` | 영향 분석에 넣는 최대 노드 수 | `500` |
| `CRG_MAX_IMPACT_DEPTH` | 영향 범위 분석의 탐색 깊이 | `2` |
| `CRG_MAX_BFS_DEPTH` | 그래프 순회의 최대 깊이 | `15` |
| `CRG_MAX_CHANGED_FUNCS` | 변경 보고 하나에서 분석하는 변경 함수의 최대 수 | `500` |
| `CRG_MAX_TRANSITIVE_FRONTIER` | 전이적 호출자/피호출자 확장의 최대 프런티어 크기 | `50` |
| `CRG_TOOL_TIMEOUT` | 읽기 전용 MCP 도구의 제한 시간(초. `0`이면 해제). 쓰기 작업(build / postprocess / embed / wiki / apply-refactor)에는 적용되지 않음 | `0` |
| `CRG_CHURN_WINDOW_DAYS` | `detect-changes --churn`의 커밋 수를 세는 기간 | `90` |
| `CRG_LEIDEN_SEED` | Leiden 커뮤니티 탐지의 시드 | `42` |
| `CRG_RECURSE_SUBMODULES` | `1`, `true`, `yes`일 때 git 서브모듈 포함 | - |
| `CRG_TOOLS` | 서버로 띄울 때 노출할 MCP 도구의 쉼표 구분 허용 목록 | - |
| `GOOGLE_API_KEY` | Google Gemini 임베딩의 API 키 | - |
| `MINIMAX_API_KEY` | MiniMax 임베딩의 API 키 | - |
| `VOYAGE_API_KEY` | Voyage 임베딩의 API 키 | - |
| `CRG_VOYAGE_MODEL` | Voyage 임베딩의 모델 | `voyage-code-3` |
| `CRG_VOYAGE_OUTPUT_DIMENSION` | Voyage 임베딩의 출력 차원 | `1024` |
| `CRG_VOYAGE_OUTPUT_DTYPE` | Voyage 임베딩의 출력 dtype | `float` |
| `CRG_VOYAGE_BASE_URL` | Voyage 임베딩 엔드포인트 | `https://api.voyageai.com/v1` |
| `CRG_VOYAGE_BATCH_SIZE` | Voyage 요청의 배치 크기 | `100` |
| `CRG_VOYAGE_MIN_INTERVAL_SEC` | Voyage 요청 사이의 최소 간격 | `0` |
| `CRG_OPENAI_BASE_URL` | OpenAI 호환 임베딩 엔드포인트 | - |
| `CRG_OPENAI_API_KEY` | OpenAI 호환 임베딩의 API 키 | - |
| `CRG_OPENAI_MODEL` | OpenAI 호환 임베딩의 모델 | - |
| `CRG_OPENAI_DIMENSION` | 임베딩 차원 고정(v3 모델은 축소 지원) | - |
| `CRG_OPENAI_BATCH_SIZE` | OpenAI 호환 요청의 배치 크기 | `100` |
| `NO_COLOR` | 터미널의 ANSI 색 끄기 | - |
| `CRG_SERIAL_PARSE` | `1`로 두면 병렬 파싱 끄기(디버깅용) | - |

OpenAI 호환 임베딩(OpenAI, Azure 또는 new-api, LiteLLM, vLLM, LocalAI, OpenAI 모드의 Ollama 같은 자체 게이트웨이)에는 추가 설치가 필요 없습니다. 변수를 설정하고 `embed_graph`에 `provider="openai"`를 넘기세요.

```bash
export CRG_OPENAI_BASE_URL=http://127.0.0.1:3000/v1     # or https://api.openai.com/v1
export CRG_OPENAI_API_KEY=sk-...
export CRG_OPENAI_MODEL=text-embedding-3-small          # whatever your gateway serves
# optional:
export CRG_OPENAI_DIMENSION=1536                        # pin dim (v3 models support reduction)
export CRG_OPENAI_BATCH_SIZE=100                        # lower for gateways with tight limits
                                                        # (e.g. Qwen text-embedding-v4 caps at 10)
```

베이스 URL이 localhost(`127.0.0.1`, `localhost`, `0.0.0.0`, `::1`)를 가리키면 클라우드 전송 경고를 건너뜁니다.

Voyage 임베딩에도 추가 설치가 필요 없습니다. `VOYAGE_API_KEY`를 설정하고 `embed_graph`에 `provider="voyage"`를 넘기세요. 기본 모델은 `voyage-code-3`입니다.

```bash
export VOYAGE_API_KEY=pa-...
export CRG_ACCEPT_CLOUD_EMBEDDINGS=1
code-review-graph embed --provider voyage --model voyage-code-3
```

> **모델 선택.** 오래 유지할 색인이라면 `-preview`, `-beta`, `-exp` 모델 ID는 피하세요. 프리뷰 모델은 가중치가 바뀌거나(차원이 달라지면 전체를 다시 임베딩해야 합니다) 내려갈 수 있습니다. `text-embedding-3-small` / `text-embedding-3-large`(OpenAI), `Qwen/Qwen3-Embedding-8B`(자체 호스팅 vLLM 또는 LocalAI), `gemini-embedding-001`(`GOOGLE_API_KEY`가 필요한 네이티브 Gemini 제공자) 같은 정식 출시 모델을 쓰세요.
>
> 임베딩 텍스트는 식별자, 시그니처, 구조 맥락, 그리고 길이를 제한한 첫 문단의 docstring이나 문서 주석 요약입니다. 함수 본문은 보내지 않습니다. 문서 추출 기능이 들어오기 전에 만든 그래프는 다시 임베딩하기 전에 `code-review-graph build`를 한 번 완전히 돌려야 합니다. 일반 빌드는 임베딩을 갱신하지 않습니다. 빌드 뒤에 갱신하려면 `--embedding-provider`와 `--embedding-model`을 모두 넘기세요. 클라우드 제공자는 이 소스 기반 텍스트를 받고 그에 대해 요금을 매길 수 있습니다.

#### 도구 필터링

CRG는 기본으로 MCP 도구 30개를 노출합니다. 서버를 일부로 제한하려면 `--tools`나 `CRG_TOOLS` 환경 변수를 쓰세요.

```bash
# CLI flag
code-review-graph serve --tools query_graph_tool,semantic_search_nodes_tool,detect_changes_tool

# Environment variable
CRG_TOOLS=query_graph_tool,semantic_search_nodes_tool code-review-graph serve
```

플래그가 환경 변수보다 우선합니다. 둘 다 설정하지 않으면 모든 도구를 쓸 수 있습니다. MCP 클라이언트 설정에서는 이렇게 씁니다.

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

## FAQ와 비교

답은 [docs/FAQ.md](docs/FAQ.md)에 있습니다.

- [LSP / 언어 서버와의 차이](docs/FAQ.md#how-is-this-different-from-lsp-and-language-servers): 언어별 데몬 대신 언어를 가로지르는 영구 그래프 하나. 심볼 단위 정확도는 LSP가 더 낫습니다.
- [RAG / 임베딩과의 차이](docs/FAQ.md#isnt-this-just-rag): 유사도 청크가 아니라 AST에서 파싱한 구조 엣지. 임베딩은 선택이고 검색을 돕는 역할만 합니다.
- [grep / 에이전트 검색과의 차이](docs/FAQ.md#why-not-just-grep): 한 홉 조회는 grep이 낫고, 여러 홉 질문(영향 범위, 호출자의 호출자, tests-for, 영향받는 흐름)은 그래프가 낫습니다.
- [Serena, codegraph, claude-context, repomix와의 비교](docs/FAQ.md#how-does-it-compare-to-serena-codegraph-claude-context-and-repomix): 비교 표.
- [쓰지 않는 게 나을 때](docs/FAQ.md#when-should-i-not-use-it): 작은 저장소, 사소한 단일 파일 diff, 일회성 질문.
- [외부로 데이터를 보내나요?](docs/FAQ.md#does-it-phone-home): 텔레메트리 없음. 클라우드 임베딩은 직접 켜야 합니다.
- [동작하는지 어떻게 확인하나요?](docs/FAQ.md#how-do-i-verify-it-is-working): `status`, `detect-changes --brief`, `/mcp`.

## 문제 해결

Windows/WSL을 포함한 더 많은 사례는 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)에 있습니다.

### `pip` / `pipx`가 `hatchling`을 내려받지 못함(또는 PyPI에 대한 `Errno 9` / `Bad file descriptor`)

소스 트리에서 설치할 때(예: `pipx install .`)는 PyPI에서 빌드 의존성을 받아야 합니다. 연결 경고 뒤에 `Could not find a version that satisfies the requirement hatchling`이 보인다면, 그 터미널의 Python이 `pypi.org`로 HTTPS 연결을 열지 못하는 것일 수 있습니다. 편집기의 통합 터미널에서 가장 자주 보이며, VPN, 방화벽, 프록시가 원인일 때도 있습니다.

1. 편집기 터미널 대신 Terminal.app이나 iTerm에서 같은 명령을 실행하세요.
2. 내려받는 방식이 다른 [uv](https://docs.astral.sh/uv/)로 체크아웃에서 설치하세요.

   ```bash
   cd /path/to/code-review-graph
   uv tool install . --force
   ```

3. 클론에서 개발할 때는 `uv sync`와 `uv run code-review-graph ...`를 쓰세요.

원인을 가리려면 `python3 scripts/diagnose_pypi_connectivity.py`를 실행하세요. `FAILED`가 나오면 문제는 패키지 이름이 아니라 네트워크 환경입니다.

### Windows: `Invalid JSON: EOF while parsing` 또는 `MCP error -32000: Connection closed`

Claude Code 설정에서 `cmd /c` 래퍼를 쓰지 마세요. `~/.claude.json`이 `.exe`를 바로 가리키게 하고 UTF-8은 설정으로 넘기세요.

```json
"code-review-graph": {
  "command": "C:\\path\\to\\your\\venv\\Scripts\\code-review-graph.exe",
  "args": ["serve", "--repo", "C:\\path\\to\\your\\project"],
  "env": { "PYTHONUTF8": "1" }
}
```

## 기여하기

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

풀 리퀘스트는 `staging`(기본 브랜치)으로 보내 주세요. 변경은
`staging` → `testing` → `main` 순으로 올라가고, 릴리스는 `main`에서 태그합니다. 전체 흐름은
[CONTRIBUTING.md](CONTRIBUTING.md#branching-and-promotion)를 보세요.

내장 언어를 추가하려면 `code_review_graph/parser.py`를 고치세요. 확장자를 `EXTENSION_TO_LANGUAGE`에, 노드 타입 매핑을 `_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES`, `_CALL_TYPES`에 추가하면 됩니다. 테스트 픽스처를 함께 넣어 PR을 보내 주세요. 저장소 하나에서만 필요한 언어라면 대신 [`languages.toml`](docs/CUSTOM_LANGUAGES.md)을 쓰세요.

## 라이선스

MIT. [LICENSE](LICENSE)를 보세요.

<p align="center">
<br>
<a href="https://code-review-graph.com">code-review-graph.com</a><br><br>
<code>pip install code-review-graph && code-review-graph install</code>
</p>
