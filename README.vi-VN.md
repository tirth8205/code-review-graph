<h1 align="center">code-review-graph</h1>

> **Lưu ý:** Bản dịch tiếng Việt này được duy trì song song với tài liệu gốc. Mọi thắc mắc hoặc thông tin mới nhất có thể tham khảo thêm tại [README tiếng Anh](README.md).

<a href="https://trendshift.io/repositories/23329?utm_source=repository-badge&amp;utm_medium=badge&amp;utm_campaign=badge-repository-23329" target="_blank" rel="noopener noreferrer"><img src="https://trendshift.io/api/badge/repositories/23329" alt="tirth8205%2Fcode-review-graph | Trendshift" width="250" height="55"/></a>

<p align="center">
  <strong>Ngừng lãng phí token. Bắt đầu review code thông minh hơn.</strong>
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a> |
  <a href="README.ja-JP.md">日本語</a> |
  <a href="README.ko-KR.md">한국어</a> |
  <a href="README.hi-IN.md">हिन्दी</a> |
  <a href="README.vi-VN.md">Tiếng Việt</a>
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

<p align="center">
  <a href="docs/USAGE.md">Hướng dẫn sử dụng</a> ·
  <a href="docs/COMMANDS.md">Lệnh CLI</a> ·
  <a href="docs/FAQ.md">FAQ</a> ·
  <a href="docs/TROUBLESHOOTING.md">Xử lý lỗi</a> ·
  <a href="docs/GITHUB_ACTION.md">GitHub Action</a> ·
  <a href="docs/REPRODUCING.md">Tái hiện Benchmark</a> ·
  <a href="docs/ROADMAP.md">Lộ trình phát triển</a>
</p>

<br>

Các công cụ lập trình AI có thể phải đọc lại phần lớn mã nguồn của bạn mỗi khi review code. `code-review-graph` khắc phục điều đó bằng cách xây dựng một bản đồ cấu trúc code với [Tree-sitter](https://tree-sitter.github.io/tree-sitter/), theo dõi các thay đổi một cách lũy tiến và cung cấp ngữ cảnh chính xác cho trợ lý AI của bạn thông qua giao thức [MCP](https://modelcontextprotocol.io/) — giúp AI chỉ đọc những phần mã nguồn thực sự cần thiết.

<p align="center">
  <img src="diagrams/diagram1_before_vs_after.png" alt="Vấn đề token: Tiết kiệm token từ 38x đến 528x trên 6 repository thực tế" width="85%" />
</p>

---

## Cài đặt nhanh

```bash
pip install code-review-graph                     # hoặc: pipx install code-review-graph
code-review-graph install          # Tự động phát hiện và cấu hình mọi nền tảng được hỗ trợ
code-review-graph build            # Phân tích toàn bộ mã nguồn của bạn
```

Chỉ cần một câu lệnh để thiết lập tất cả. `install` sẽ tự động phát hiện các công cụ lập trình AI mà bạn đang sử dụng, ghi cấu hình MCP chính xác cho từng công cụ, cài đặt các hook/skill mặc định của nền tảng (nếu hỗ trợ) và chèn các chỉ dẫn nhận biết graph vào rule của công cụ. Nó tự động nhận diện bạn cài đặt qua `uvx` hay `pip`/`pipx` để tạo cấu hình tương ứng. Hãy khởi động lại trình soạn thảo / công cụ sau khi cài đặt.

<p align="center">
  <img src="diagrams/diagram8_supported_platforms.png" alt="Một lệnh cài đặt cho mọi nền tảng: tự động nhận diện Codex, Claude Code, CodeBuddy Code, Cursor, Windsurf, Zed, Continue, OpenCode, Antigravity, Gemini CLI, Qwen, Qoder, Kiro, và GitHub Copilot" width="85%" />
</p>

Để chỉ định một nền tảng cụ thể:

```bash
code-review-graph install --platform codex       # Chỉ cấu hình Codex
code-review-graph install --platform cursor      # Chỉ cấu hình Cursor
code-review-graph install --platform claude-code  # Chỉ cấu hình Claude Code
code-review-graph install --platform gemini-cli   # Chỉ cấu hình Gemini CLI
code-review-graph install --platform kiro         # Chỉ cấu hình Kiro
code-review-graph install --platform copilot      # Chỉ cấu hình GitHub Copilot (VS Code)
code-review-graph install --platform copilot-cli  # Chỉ cấu hình GitHub Copilot CLI
code-review-graph install --platform codebuddy    # Chỉ cấu hình CodeBuddy Code
```

Yêu cầu Python 3.10 trở lên. Để có trải nghiệm tốt nhất, hãy cài đặt [uv](https://docs.astral.sh/uv/) (cấu hình MCP sẽ ưu tiên sử dụng `uvx` nếu có sẵn, nếu không sẽ dùng lệnh `code-review-graph` trực tiếp).

Để gỡ bỏ CRG khỏi dự án Git hoặc SVN, sử dụng lệnh gỡ cài đặt đối ứng từ bất kỳ vị trí nào bên trong cây thư mục làm việc. Thư mục mục tiêu được chuẩn hóa về gốc của thư mục làm việc, và các thư mục không phải repository sẽ bị từ chối. Lệnh chỉ xóa các file và mục nhập thuộc sở hữu của CRG; các máy chủ MCP, hook, skill không liên quan và các comment JSONC khác sẽ được giữ nguyên. Các thay đổi đối với cấu hình dùng chung sử dụng cơ chế thay thế nguyên tử (atomic replacement) để khi thao tác ghi thất bại, file gốc vẫn nguyên vẹn.

```bash
code-review-graph uninstall --dry-run    # Xem trước các thao tác, không thay đổi file
code-review-graph uninstall              # Xem trước, xác nhận trước khi thực hiện
code-review-graph uninstall --yes        # Gỡ cài đặt không cần xác nhận
code-review-graph uninstall --all-repos  # Dọn dẹp tất cả các repository đã đăng ký
code-review-graph uninstall --keep-data  # Gỡ tích hợp nhưng giữ lại cơ sở dữ liệu graph
code-review-graph uninstall --keep-user-configs --repo .  # Chỉ dọn dẹp dự án hiện tại
```

Sau đó, mở dự án của bạn và yêu cầu trợ lý AI:

```
Build the code review graph for this project
```

Lần build đầu tiên mất khoảng 10 giây cho một dự án 500 file. Sau đó, chế độ watch và các git hook sẽ giữ cho graph luôn được cập nhật tự động.


## Cách thức hoạt động

<p align="center">
  <img src="diagrams/diagram7_mcp_integration_flow.png" alt="Cách trợ lý AI sử dụng graph: Người dùng yêu cầu review, AI kiểm tra MCP tools, graph trả về bán kính ảnh hưởng và điểm rủi ro, AI chỉ đọc những file cần thiết" width="80%" />
</p>

Repository của bạn được phân tích thành AST bằng Tree-sitter, lưu trữ dưới dạng đồ thị gồm các node (hàm, class, import) và edge (cuộc gọi hàm, kế thừa, test coverage), sau đó được truy vấn khi review để tính toán tập hợp file tối thiểu mà trợ lý AI cần đọc.

<p align="center">
  <img src="diagrams/diagram2_architecture_pipeline.png" alt="Luồng kiến trúc: Repository -> Tree-sitter Parser -> SQLite Graph -> Blast Radius -> Tập file review tối thiểu" width="100%" />
</p>

### Phân tích bán kính ảnh hưởng (Blast-radius)

Khi một file thay đổi, graph sẽ truy vết mọi caller, dependent và test có thể bị ảnh hưởng. Đây chính là "bán kính ảnh hưởng" của thay đổi. Trợ lý AI của bạn chỉ đọc các file này thay vì quét toàn bộ dự án.

<p align="center">
  <img src="diagrams/diagram3_blast_radius.png" alt="Minh họa bán kính ảnh hưởng khi hàm login() thay đổi lan truyền đến caller, dependent và test" width="70%" />
</p>

### Cập nhật lũy tiến < 2 giây

Khi bật hook hoặc watch mode, việc lưu file hoặc commit sẽ kích hoạt cập nhật lũy tiến. Graph tìm sự khác biệt giữa các file thay đổi, tìm các phần phụ thuộc qua kiểm tra mã hash SHA-256 và chỉ phân tích lại những gì đã thay đổi. Một dự án 2.900 file chỉ mất dưới 2 giây để đánh chỉ mục lại.

<p align="center">
  <img src="diagrams/diagram4_incremental_update.png" alt="Luồng cập nhật lũy tiến: hook hoặc watch mode kích hoạt diff, tìm dependent, chỉ re-parse 5 file và bỏ qua 2.910 file" width="90%" />
</p>

### Giải quyết bài toán Monorepo

Các monorepo lớn là nơi lãng phí token nghiêm trọng nhất. Graph giúp lọc bỏ nhiễu — loại bỏ hơn 27.700+ file khỏi ngữ cảnh review và chỉ đọc khoảng 15 file thực sự liên quan.

<p align="center">
  <img src="diagrams/diagram6_monorepo_funnel.png" alt="Dự án code-review-graph: 208.821 token mã nguồn được lọc xuống còn ~2.495 token kết quả graph — tiết kiệm 93 lần token cho mỗi câu hỏi" width="80%" />
</p>

### Hỗ trợ đa dạng ngôn ngữ & Jupyter Notebooks

<p align="center">
  <img src="diagrams/diagram9_language_coverage.png" alt="Danh sách ngôn ngữ được hỗ trợ phân loại theo: Web, Backend, Systems, Mobile, Scripting, Config, kèm Jupyter và Databricks notebook" width="90%" />
</p>

Trình phân tích hỗ trợ hàm, class, import, call site, kế thừa và phát hiện test trên nhiều ngôn ngữ bằng Tree-sitter. Hỗ trợ hiện tại bao gồm: Python, JavaScript/TypeScript/TSX, Go, Rust, Java, C/C++, C#, VB.NET, Ruby, Kotlin, Swift, PHP, Scala, Solidity, Dart, R, Perl, Lua/Luau, Objective-C, shell scripts, Elixir, Zig, PowerShell, Julia, ReScript, GDScript, Nix, Verilog/SystemVerilog, SQL, Terraform/OpenTofu cấu trúc (`.tf`; các file `.hcl` chung được coi là node file), Ansible playbooks/roles/tasks, Vue/Svelte SFCs, Astro files phân tích qua trình phân tích TypeScript, Jupyter/Databricks notebooks (`.ipynb`), và Perl XS (`.xs`). YAML chung không được coi là mã nguồn.

Dự án PHP bổ sung thêm khả năng phân tích Composer PSR-4 trong phạm vi repo, tham chiếu Blade template và các liên kết ngữ nghĩa Laravel Route-to-controller và quan hệ Eloquent khi mã nguồn chứa các import framework rõ ràng, kế thừa model và bằng chứng đối tượng nhận.

### Tự thêm ngôn ngữ mới (Không cần fork repo)

Nếu dự án của bạn dùng ngôn ngữ chưa được hỗ trợ sẵn, bạn chỉ cần tạo file `languages.toml` trong thư mục `.code-review-graph/` để ánh xạ đuôi file với grammar có sẵn trong `tree_sitter_language_pack`, kèm theo các loại node tree-sitter cho hàm, class, import và call:

```toml
[languages.erlang]
extensions = [".erl"]
grammar = "erlang"
function_node_types = ["function_clause"]
class_node_types = ["record_decl"]
import_node_types = ["import_attribute"]
call_node_types = ["call"]
```

Trình duyệt tree-sitter chung sẽ tự xử lý phần trích xuất còn lại — không cần sửa code và các ngôn ngữ tích hợp sẵn không bao giờ bị ghi đè. Xem [docs/CUSTOM_LANGUAGES.md](docs/CUSTOM_LANGUAGES.md) để biết chi tiết hướng dẫn schema, quy tắc kiểm tra và ví dụ hoàn chỉnh.

### Review PR đánh giá rủi ro trong CI (GitHub Action)

Phân tích tương tự cũng chạy dưới dạng một GitHub Action dạng composite — và giữ nguyên tính chất local-first: toàn bộ đồ thị tri thức được dựng và truy vấn trực tiếp trên runner CI của bạn mà không gửi bất kỳ mã nguồn nào ra bên ngoài. Trên mỗi pull request, action sẽ đăng một comment duy nhất gắn cố định chứa các hàm được chấm điểm rủi ro, các luồng thực thi bị ảnh hưởng và lỗ hổng bài test, cập nhật trực tiếp tại chỗ theo từng lượt push. Tùy chọn `fail-on-risk` giúp biến buổi review thành cổng kiểm duyệt merge.

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
      - uses: tirth8205/code-review-graph@v2.3.6
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

Xem chi tiết tại [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md) để biết thêm về các đầu vào, cấp độ rủi ro và bộ nhớ tạm (caching), hoặc xem workflow thực tế của chính repo này tại [`.github/workflows/pr-review.yml`](.github/workflows/pr-review.yml).

---

## Benchmarks

<p align="center">
  <img src="diagrams/diagram5_benchmark_board.png" alt="Kết quả benchmark trên 6 repository thực tế: giảm trung bình ~82x token mỗi câu hỏi (tối đa 528x), F1 ảnh hưởng trung bình 0.71" width="85%" />
</p>

**Con số tiêu điểm: Số lượng token giảm trung vị cho mỗi câu hỏi trên 6 repo là ~82x** (so sánh giữa toàn bộ mã nguồn naive và truy vấn graph). Con số **528x thường được trích dẫn là mức tối đa** — đạt được trên dự án lớn nhất (fastapi) — không phải kết quả trung bình.

Tất cả số liệu đến từ bộ chạy đánh giá tự động trên 6 repository mở thực tế (tổng cộng 13 commit). Mỗi cấu hình được ghim một mã SHA upstream, bộ phát hiện cộng đồng Leiden chạy với seed cố định và vector embeddings là định hình trên CPU — giúp hai lượt chạy trên các máy khác nhau cho ra số liệu hoàn toàn giống nhau. Hướng dẫn tái hiện đầy đủ với đầu ra dự kiến có tại [`docs/REPRODUCING.md`](docs/REPRODUCING.md). Một lượt chạy chỉ báo cáo hàng tuần trên hai cấu hình nhỏ nhất nằm tại [`.github/workflows/eval.yml`](.github/workflows/eval.yml).

<details>
<summary><strong>Hiệu quả Token: Giảm trung vị ~82x cho mỗi câu hỏi (khoảng từ 38x – 528x; toàn bộ mã nguồn vs truy vấn graph)</strong></summary>
<br>

Đối với một câu hỏi điển hình của AI agent (`"how does authentication work"`, `"what is the main entry point"`, v.v.), graph chỉ trả về ~2.000–3.500 token kết quả tìm kiếm chính xác + các cạnh lân cận thay vì ép agent đọc toàn bộ các file nguồn. Bảng dưới đây tính trung bình qua 5 câu hỏi mẫu định nghĩa trong `code_review_graph/token_benchmark.py`.

| Repo | Snapshot SHA | naive_corpus_tokens | avg graph_tokens | Giảm (Reduction) |
|------|---|-----------------:|----------------:|----------:|
| fastapi | `0227991a` | 951,071 | 2,169 | **528.4x** |
| code-review-graph | `84bde354` | 208,821 | 2,495 | **93.0x** |
| gin | `5c00df8a` | 166,868 | 1,990 | **91.8x** |
| flask | `a29f88ce` | 125,022 | 1,986 | **71.4x** |
| express | `b4ab7d65` | 135,955 | 3,465 | **40.6x** |
| httpx | `b55d4635` | 89,492 | 2,438 | **38.0x** |

Mức giảm trung vị mỗi câu hỏi qua 6 repo: **~82x**. Khoảng biến thiên là 38x – 528x, trong đó **528x là trường hợp tốt nhất** (fastapi, tập mã nguồn lớn nhất), không phải con số trung bình.

Mức cơ sở toàn bộ mã nguồn ở trên là giới hạn trên mà không agent thực tế nào phải trả: một agent giỏi sẽ dùng grep tìm định danh và chỉ đọc các file khớp nhất. Benchmark đánh giá `agent_baseline` đo lường mức cơ sở thực tế đó — một phép grep thuần Python trên mã nguồn, lấy 3 file có số lượng khớp cao nhất, đếm token và so sánh với chi phí truy vấn graph (`evaluate/results/<repo>_agent_baseline_*.csv`).

Benchmark chính thức `eval/benchmarks/token_efficiency.py` đo lường một kịch bản khác — toàn bộ JSON `get_review_context()` so với chỉ nội dung file thay đổi của một commit — và báo cáo tỷ lệ dưới 1 cho các commit nhỏ, vì phản hồi ngữ cảnh review mang theo các cạnh bán kính ảnh hưởng cộng với đoạn mã nguồn vượt quá một diff file đơn lẻ nhỏ. Đó không phải lỗi; hai benchmark trả lời cho hai câu hỏi khác nhau. Xem [`docs/REPRODUCING.md`](docs/REPRODUCING.md) để biết phương pháp đầy đủ.

Từ v2.3.4, các công cụ review và impact gắn kèm ước tính `context_savings` gọn nhẹ để MCP client xem được ngữ cảnh ước tính tiết kiệm được cho mỗi lệnh gọi. Trong v2.3.5, CLI hiển thị điều này dưới dạng bảng `Token Savings` (xem phần Hướng dẫn sử dụng) và thêm cờ `--verify` để đối chiếu với tokenizer `cl100k_base` của OpenAI. Dữ liệu hiệu chỉnh trong [`docs/REPRODUCING.md`](docs/REPRODUCING.md) cho thấy ước tính nằm trong khoảng ~1% so với token GPT-4 thực tế trên tổng số 222 file mẫu.

</details>

<details>
<summary><strong>Độ chính xác ảnh hưởng: F1 trung bình 0.71 so với ground-truth từ graph (Recall 1.0 là giới hạn trên tuần hoàn, không phải "100% recall")</strong></summary>
<br>

Phân tích bán kính ảnh hưởng tìm lại mọi file trong ground-truth trên tất cả 13 commit đánh giá — **nhưng hãy hiểu đó là giới hạn trên, không phải "100% recall"**: ở chế độ này, ground-truth (các file thay đổi + các file có cạnh gọi/import trỏ vào) được suy ra từ chính đồ thị mà bộ dự đoán duyệt qua, nên nó có tính chất tuần hoàn. Việc dự đoán thừa ở cột precision là sự đánh đổi cố ý: thà cảnh báo dư file còn hơn bỏ sót một phụ thuộc bị hỏng.

| Repo | Commits | F1 trung bình | Precision trung bình | Recall (giới hạn trên từ graph) |
|------|--------:|-------:|--------------:|-------:|
| httpx | 2 | 0.864 | 0.786 | 1.0 |
| fastapi | 2 | 0.834 | 0.750 | 1.0 |
| code-review-graph | 2 | 0.734 | 0.584 | 1.0 |
| express | 2 | 0.667 | 0.500 | 1.0 |
| flask | 2 | 0.628 | 0.481 | 1.0 |
| gin | 3 | 0.609 | 0.439 | 1.0 |
| **Trung bình** | **13** | **0.714** | **0.578** | **1.000** |

Benchmark cũng chạy một chế độ **co-change (đồng thay đổi)** trung thực: bộ dự đoán được khởi tạo với một file duy nhất bị thay đổi và được chấm điểm dựa trên *các file khác* mà tác giả thực sự chạm vào trong cùng commit — bằng chứng độc lập từ lịch sử git, không phải từ graph. Cả hai chế độ xuất hiện song song trong các file kết quả CSV (`ground_truth_mode`). Các số liệu co-change sẽ được bổ sung vào số liệu thống kê chính thức sau khi được bộ chạy eval ghi lại; chúng tôi không trích dẫn khi chưa đo lường.

</details>

<details>
<summary><strong>Hiệu năng Build</strong></summary>
<br>

| Repo | Files | Nodes | Edges | Phát hiện Flow | Độ trễ tìm kiếm |
|------|------:|------:|------:|---------------:|---------------:|
| express | 141 | 1,910 | 17,553 | 106ms | 0.7ms |
| fastapi | 1,122 | 6,285 | 27,117 | 128ms | 1.5ms |
| flask | 83 | 1,446 | 7,974 | 95ms | 0.7ms |
| gin | 99 | 1,286 | 16,762 | 111ms | 0.5ms |
| httpx | 60 | 1,253 | 7,896 | 96ms | 0.4ms |

</details>

### Hạn chế và điểm yếu đã biết

- **Impact "recall 1.0" mang tính tuần hoàn từ graph:** ground-truth lịch sử đến từ chính các cạnh đồ thị mà bộ dự đoán đi qua, do đó nó là một giới hạn trên. Chế độ co-change trung thực (chấm điểm dựa trên các file thực sự đồng thay đổi trong cùng commit) được đo lường song song; các con số đó sẽ thấp hơn đáng kể.
- **Thay đổi nhỏ trên 1 file:** Ngữ cảnh graph có thể vượt quá việc đọc file đơn thuần đối với các chỉnh sửa quá nhỏ (xem kết quả express ở trên). Chi phí tăng thêm chính là các định danh cấu trúc giúp phân tích đa file.
- **Chất lượng tìm kiếm (MRR 0.35):** Tìm kiếm từ khóa thấy kết quả đúng trong top-4 cho hầu hết truy vấn, nhưng thứ tự xếp hạng cần cải thiện. Truy vấn Express trả về 0 kết quả do quy cách đặt tên module-pattern.
- **Phát hiện Flow (Recall 33%):** Mô hình entry point hoạt động mạnh nhất với Python và PHP/Laravel. Việc phát hiện flow cho JavaScript và Go cần cải thiện thêm.
- **Đánh đổi giữa Precision và Recall:** Phân tích ảnh hưởng được thiết kế mang tính bảo thủ. Nó đánh dấu các file *có thể* bị ảnh hưởng, đồng nghĩa với việc có một số kết quả dương tính giả (false positive) trong các đồ thị phụ thuộc lớn.

---

## Tính năng chính

| Tính năng | Chi tiết |
|---|---|
| **Cập nhật lũy tiến** | Chỉ re-parse các file thay đổi, cập nhật hoàn tất trong < 2 giây. |
| **Hỗ trợ đa ngôn ngữ & Notebook** | Python, JavaScript/TypeScript/TSX, Go, Rust, Java, C/C++, C#, VB.NET, Ruby, Kotlin, Swift, PHP, Scala, Solidity, Dart, R, Perl, Lua/Luau, Objective-C, shell scripts, Elixir, Zig, PowerShell, Julia, ReScript, GDScript, Nix, Verilog/SystemVerilog, SQL, Terraform/OpenTofu (`.tf`; file `.hcl` chung chỉ dạng file), Ansible playbooks/roles/tasks, Vue/Svelte SFCs, Astro files phân tích qua trình phân tích TypeScript, Jupyter/Databricks (`.ipynb`), và Perl XS (`.xs`) |
| **Phân tích PHP nhận biết framework** | Nhận diện Composer PSR-4 trong phạm vi repo, tham chiếu Blade template, và các liên kết Laravel Route-to-controller & quan hệ Eloquent dựa trên bằng chứng |
| **Phân tích Blast-radius** | Chỉ ra các hàm, class và file có khả năng bị ảnh hưởng bởi thay đổi |
| **Hook tự động cập nhật** | Hook và watch mode có thể cập nhật graph khi lưu file và qua các commit hook hỗ trợ |
| **Tìm kiếm ngữ nghĩa (Semantic search)** | Hỗ trợ vector embeddings tùy chọn qua sentence-transformers, Google Gemini, MiniMax hoặc bất kỳ endpoint tương thích OpenAI nào (OpenAI thực tế, Azure, new-api, LiteLLM, vLLM, LocalAI) |
| **Trực quan hóa tương tác** | Đồ thị D3.js tương tác với tìm kiếm, bật/tắt chú thích cộng đồng, và các node co giãn theo bậc |
| **Phát hiện Hub & Bridge** | Tìm các node kết nối nhiều nhất và các điểm thắt cổ chai kiến trúc qua giữa trung tâm (betweenness centrality) |
| **Điểm số bất ngờ (Surprise scoring)** | Phát hiện liên kết không mong đợi: giữa các cộng đồng, giữa các ngôn ngữ, cạnh từ ngoại vi tới hub |
| **Phân tích lỗ hổng tri thức** | Nhận diện node cô lập, điểm nóng chưa được test, cộng đồng thưa thớt và điểm yếu cấu trúc |
| **Gợi ý câu hỏi** | Tự động tạo câu hỏi review từ phân tích đồ thị (bridges, hubs, surprises) |
| **Độ tin cậy của cạnh (Edge confidence)** | Đánh giá độ tin cậy 3 cấp (EXTRACTED/INFERRED/AMBIGUOUS) kèm điểm số thực trên các cạnh |
| **Duyệt đồ thị (Traversal)** | Khám phá BFS/DFS tự do từ bất kỳ node nào với độ sâu và ngân sách token tùy chỉnh |
| **Định dạng xuất (Export)** | GraphML (Gephi/yEd), Neo4j Cypher, Obsidian vault với wikilinks, đồ thị tĩnh SVG |
| **So sánh graph (Diff)** | So sánh các ảnh chụp graph theo thời gian: node mới/đã xóa, cạnh, thay đổi cộng đồng |
| **Benchmark Token** | Đo lường token toàn bộ mã nguồn vs token truy vấn graph với tỷ lệ theo từng câu hỏi |
| **Ước tính tiết kiệm context** | Metadata `context_savings` gọn nhẹ trên các đầu ra review MCP/CLI phù hợp, được dán nhãn ước tính và giữ ở 3 trường nhỏ |
| **Vòng lặp bộ nhớ (Memory loop)** | Lưu kết quả Hỏi & Đáp dưới dạng markdown để nạp lại, giúp graph phát triển từ các truy vấn |
| **Tự động tách cộng đồng** | Các cộng đồng quá lớn (>25% graph) được tách đệ quy qua giải thuật Leiden |
| **Luồng thực thi (Execution flows)** | Truy vết chuỗi cuộc gọi từ các entry point, sắp xếp theo mức độ quan trọng có trọng số |
| **Phát hiện cộng đồng** | Gom nhóm code liên quan qua thuật toán Leiden với khả năng co giãn độ phân giải cho graph lớn |
| **Tổng quan kiến trúc** | Bản đồ kiến trúc tự động tạo kèm cảnh báo liên kết phụ thuộc |
| **Review chấm điểm rủi ro** | `detect_changes` ánh xạ diff tới các hàm bị ảnh hưởng, luồng thực thi và thiếu sót trong bài test |
| **Tùy chỉnh ngôn ngữ** | Thêm ngôn ngữ mới qua `.code-review-graph/languages.toml` — không cần fork hay sửa code |
| **GitHub Action** | Đăng comment review PR chấm điểm rủi ro cố định trong CI, tùy chọn cổng gác merge `fail-on-risk` |
| **Công cụ Refactoring** | Xem trước đổi tên, phát hiện dead code nhận biết framework, gợi ý theo cộng đồng |
| **Tạo Wiki** | Tự động tạo wiki markdown từ cấu trúc cộng đồng |
| **Quản lý Multi-repo** | Đăng ký nhiều repo, tìm kiếm trên tất cả các repo |
| **Daemon Multi-repo** | `crg-daemon` theo dõi nhiều repo dưới dạng tiến trình con, tự kiểm tra sức khỏe và khởi động lại |
| **MCP Prompts** | 5 mẫu workflow: review, kiến trúc, debug, onboard, kiểm tra trước khi merge |
| **Tìm kiếm toàn văn (FTS)** | Tìm kiếm lai kết hợp FTS5 (từ khóa) và độ tương đồng vector |
| **Lưu trữ nội cục (Local)** | File SQLite nằm trong `.code-review-graph/`. Lưu trữ graph cốt lõi không cần DB ngoài hay đám mây. |
| **Chế độ Watch** | Cập nhật graph liên tục trong khi bạn làm việc |

---

## Hướng dẫn sử dụng

<details>
<summary><strong>Lệnh Slash</strong></summary>
<br>

| Lệnh | Mô tả |
|------|-------|
| `/code-review-graph:build-graph` | Dựng hoặc dựng lại code graph |
| `/code-review-graph:review-delta` | Review các thay đổi kể từ commit cuối |
| `/code-review-graph:review-pr` | Review toàn bộ PR kèm phân tích bán kính ảnh hưởng |

</details>

<details>
<summary><strong>Tham khảo lệnh CLI</strong></summary>
<br>

```bash
code-review-graph install          # Tự động phát hiện và cấu hình mọi nền tảng
code-review-graph install --platform <name>  # Chỉ định nền tảng cụ thể
code-review-graph uninstall --dry-run  # Xem trước thao tác gỡ bỏ an toàn
code-review-graph build            # Phân tích toàn bộ mã nguồn
code-review-graph update           # Cập nhật lũy tiến (chỉ file thay đổi)
code-review-graph status           # Thống kê graph
code-review-graph watch            # Tự động cập nhật khi file thay đổi
code-review-graph visualize        # Tạo graph HTML tương tác
code-review-graph visualize --format json      # Xuất dữ liệu graph cục bộ dưới dạng JSON
code-review-graph visualize --format graphml   # Xuất dạng GraphML
code-review-graph visualize --format svg       # Xuất dạng SVG
code-review-graph visualize --format obsidian  # Xuất dạng Obsidian vault
code-review-graph visualize --format cypher    # Xuất dạng Neo4j Cypher
code-review-graph wiki             # Tạo wiki markdown từ các cộng đồng
code-review-graph detect-changes --brief         # Bảng rủi ro + tiết kiệm token (chỉ đọc)
code-review-graph update --brief                 # Làm mới graph + bảng tương tự
code-review-graph detect-changes --brief --verify  # Đối chiếu với tiktoken
code-review-graph register <path>  # Đăng ký repo vào registry multi-repo
code-review-graph unregister <id>  # Xóa repo khỏi registry
code-review-graph repos            # Danh sách các repository đã đăng ký
code-review-graph daemon start     # Khởi động daemon theo dõi multi-repo
code-review-graph daemon stop      # Dừng daemon
code-review-graph daemon status    # Hiển thị trạng thái daemon và các repo
code-review-graph eval             # Chạy các benchmark đánh giá
code-review-graph serve            # Khởi chạy máy chủ MCP
```

Dữ liệu xuất JSON được lưu bên trong thư mục graph cục bộ (Git bỏ qua mặc định). Chúng có thể chứa đường dẫn tuyệt đối và metadata cấu trúc code, do đó hãy kiểm tra và làm sạch bản xuất trước khi công khai ra ngoài máy của bạn.

</details>

<details>
<summary><strong>Bảng Tiết kiệm Token: <code>detect-changes --brief</code> vs <code>update --brief</code></strong></summary>
<br>

Cả hai lệnh đều in ra một bảng gọn nhẹ hiển thị số lượng token graph đã tiết kiệm cho bạn so với việc đưa trực tiếp các file thay đổi cho AI agent. Chúng chỉ khác nhau ở **một** điểm: graph có được làm mới trước hay không.

```text
┌─────────────────────── Token Savings ────────────────────────┐
│ Full context would be:     12,921 tokens                     │
│ Graph context used:           762 tokens                     │
│ Saved:                     12,159 tokens (~94%)              │
│ Breakdown: Functions 244 · Tests 191 · Risk 244 · Other 83   │
└──────────────────────────────────────────────────────────────┘
```

| Lệnh | Thao tác thực hiện | Khi nào nên dùng |
|---|---|---|
| `detect-changes --brief` | **Chỉ đọc.** Xem các thay đổi hiện tại, truy vấn graph **hiện có**, in ra bảng. ~1 giây. | Hầu hết thời gian — các hook (hoặc `crg-daemon`) giữ graph luôn mới ở nền, nên thao tác này là đủ. |
| `update --brief` | **Phân tích lại các file thay đổi vào graph trước**, sau đó in ra bảng tương tự. ~5 giây. | Sau khi rebase, thay đổi tập file lớn, hoặc khi bạn nghi ngờ graph bị cũ. |

Cả hai đều kết thúc bằng **cùng một bảng** vì đều gọi cùng bước `analyze_changes()` ở cuối. Sự khác biệt là graph bản thân nó có được làm mới trước khi phân tích đó chạy hay không.

Thêm cờ `--verify` vào bất kỳ lệnh nào để đối chiếu các con số hiển thị với tokenizer `cl100k_base` của OpenAI (dòng GPT-4). Yêu cầu `pip install tiktoken`. Ước tính nằm trong khoảng ~1% so với token thực tế trên tập thay đổi điển hình — xem [`docs/REPRODUCING.md`](docs/REPRODUCING.md) để biết dữ liệu hiệu chỉnh.

Metadata `context_savings` tương tự cũng được gắn tự động vào phản hồi JSON của các công cụ MCP `get_impact_radius`, `get_review_context`, `detect_changes`, và `get_architecture_overview`, để AI agent có thể hiển thị phần tiết kiệm cho người dùng trong chat mà không cần prompt thêm.

</details>

<details>
<summary><strong>Daemon Multi-repo</strong></summary>
<br>

Nếu trình soạn thảo của bạn không hỗ trợ hook (vd: Cursor, OpenCode), hoặc bạn chỉ muốn graph của mình luôn được cập nhật ở nền mà không cần bất kỳ tích hợp trình soạn thảo nào, daemon chính là dành cho bạn. Nó theo dõi các file thay đổi trong repo và tự động rebuild graph — không cần chạy thủ công các lệnh `build` hay `update`.

Daemon được đi kèm sẵn với `code-review-graph` — không cần cài đặt riêng.

**Thiết lập nhanh:**

```bash
# 1. Đăng ký các repo bạn muốn theo dõi
crg-daemon add ~/project-a --alias proj-a
crg-daemon add ~/project-b

# 2. Khởi động daemon (chạy ngầm)
crg-daemon start

# 3. Hoàn tất — graph tự động cập nhật
crg-daemon status                 # kiểm tra trạng thái daemon và watcher
crg-daemon logs --repo proj-a -f  # theo dõi log của repo cụ thể
crg-daemon stop                   # dừng daemon và tất cả tiến trình watcher
```

Cũng có thể gọi qua `code-review-graph daemon start|stop|status|...`.

Bản chất `crg-daemon add` ghi vào file cấu hình TOML tại `~/.code-review-graph/watch.toml`. Bạn cũng có thể chỉnh sửa trực tiếp file này:

```toml
[[repos]]
path = "/home/user/project-a"
alias = "proj-a"

[[repos]]
path = "/home/user/project-b"
alias = "project-b"
```

Daemon giám sát file cấu hình này để nhận biết thay đổi và tự động khởi động/dừng các tiến trình watcher khi repo được thêm hay xóa. Kiểm tra sức khỏe mỗi 30 giây sẽ khởi động lại các watcher bị chết. Không yêu cầu phụ thuộc bên ngoài.

Xem [docs/COMMANDS.md](docs/COMMANDS.md#standalone-daemon-cli-crg-daemon) để biết tài liệu cấu hình đầy đủ và tất cả các tùy chọn có sẵn.

</details>

<details>
<summary><strong>30 công cụ MCP (MCP Tools)</strong></summary>
<br>

Trợ lý AI của bạn sẽ tự động sử dụng các công cụ này sau khi graph được dựng.

| Công cụ | Mô tả |
|------|-------------|
| `build_or_update_graph_tool` | Dựng hoặc cập nhật lũy tiến graph |
| `run_postprocess_tool` | Chạy lại phát hiện flow, phát hiện cộng đồng và chỉ mục FTS |
| `get_minimal_context_tool` | Ngữ cảnh siêu gọn (~100 token) — gọi công cụ này đầu tiên |
| `get_impact_radius_tool` | Bán kính ảnh hưởng của các file thay đổi |
| `get_review_context_tool` | Ngữ cảnh review tối ưu token kèm tóm tắt cấu trúc |
| `query_graph_tool` | Truy vấn callers, callees, tests, imports, kế thừa |
| `traverse_graph_tool` | Duyệt BFS/DFS từ bất kỳ node nào với ngân sách token |
| `semantic_search_nodes_tool` | Tìm kiếm đối tượng code theo tên hoặc ý nghĩa |
| `embed_graph_tool` | Tính toán vector embeddings cho tìm kiếm ngữ nghĩa |
| `list_graph_stats_tool` | Kích thước và trạng thái sức khỏe của graph |
| `get_docs_section_tool` | Truy xuất các phần tài liệu |
| `find_large_functions_tool` | Tìm các hàm/class vượt quá ngưỡng số dòng |
| `list_flows_tool` | Danh sách luồng thực thi sắp xếp theo mức độ quan trọng |
| `get_flow_tool` | Lấy chi tiết một luồng thực thi |
| `get_affected_flows_tool` | Tìm các luồng bị ảnh hưởng bởi file thay đổi |
| `list_communities_tool` | Danh sách các cộng đồng code được phát hiện |
| `get_community_tool` | Lấy chi tiết một cộng đồng |
| `get_architecture_overview_tool` | Tổng quan kiến trúc từ cấu trúc cộng đồng |
| `detect_changes_tool` | Phân tích ảnh hưởng thay đổi chấm điểm rủi ro phục vụ review |
| `get_hub_nodes_tool` | Tìm các node kết nối nhiều nhất (điểm nóng kiến trúc) |
| `get_bridge_nodes_tool` | Tìm các điểm thắt cổ chai qua giữa trung tâm |
| `get_knowledge_gaps_tool` | Nhận diện điểm yếu cấu trúc và điểm nóng chưa được test |
| `get_surprising_connections_tool` | Phát hiện liên kết không mong đợi giữa các cộng đồng |
| `get_suggested_questions_tool` | Tự động tạo câu hỏi review từ phân tích |
| `refactor_tool` | Xem trước đổi tên, phát hiện dead code, gợi ý |
| `apply_refactor_tool` | Áp dụng refactoring đã xem trước |
| `generate_wiki_tool` | Tạo wiki markdown từ các cộng đồng |
| `get_wiki_page_tool` | Truy xuất một trang wiki cụ thể |
| `list_repos_tool` | Danh sách các repository đã đăng ký |
| `cross_repo_search_tool` | Tìm kiếm trên tất cả các repository đã đăng ký |

**MCP Prompts** (5 mẫu workflow):
`review_changes`, `architecture_map`, `debug_issue`, `onboard_developer`, `pre_merge_check`

</details>

<details>
<summary><strong>Cấu hình (Configuration)</strong></summary>
<br>

Để loại trừ các đường dẫn khỏi chỉ mục, tạo file `.code-review-graphignore` tại gốc repository của bạn:

```
generated/**
*.generated.ts
vendor/**
node_modules/**
```

Lưu ý: trong repo git, chỉ các file được theo dõi mới được đánh chỉ mục (`git ls-files`), do đó các file trong `.gitignore` tự động được bỏ qua. Sử dụng `.code-review-graphignore` để loại trừ các file đang được theo dõi hoặc khi không có git.

Các nhóm phụ thuộc tùy chọn:

```bash
pip install "code-review-graph[embeddings]"          # Embeddings vector cục bộ (sentence-transformers)
pip install "code-review-graph[google-embeddings]"   # Google Gemini embeddings
pip install "code-review-graph[communities]"         # Phát hiện cộng đồng (igraph)
pip install "code-review-graph[enrichment]"          # Làm giàu phân tích cuộc gọi Python (Jedi)
pip install "code-review-graph[eval]"                # Benchmark đánh giá (matplotlib)
pip install "code-review-graph[wiki]"                # Tạo wiki với tóm tắt LLM (ollama)
pip install "code-review-graph[all]"                 # Tất cả phụ thuộc tùy chọn
```

### Biến môi trường (Environment Variables)

| Biến | Mô tả | Mặc định |
|----------|-------------|---------|
| `CRG_GIT_TIMEOUT` | Thời gian chờ (giây) cho các thao tác Git | `30` |
| `CRG_DATA_DIR` | Thư mục ghi đè cho cơ sở dữ liệu graph và artifact được tạo | - |
| `CRG_EMBEDDING_MODEL` | Mô hình mặc định cho vector embeddings | `all-MiniLM-L6-v2` |
| `CRG_ACCEPT_CLOUD_EMBEDDINGS` | Tắt cảnh báo đẩy dữ liệu ra cloud embedding sau khi xác nhận | - |
| `CRG_ALLOW_REMOTE_CODE` | Cho phép các mô hình HuggingFace yêu cầu `trust_remote_code=True` | `0` |
| `CRG_MAX_IMPACT_NODES` | Số node tối đa trong phân tích ảnh hưởng | `500` |
| `CRG_MAX_IMPACT_DEPTH` | Độ sâu tìm kiếm cho phân tích bán kính ảnh hưởng | `2` |
| `CRG_MAX_BFS_DEPTH` | Độ sâu tối đa cho duyệt graph | `15` |
| `CRG_MAX_CHANGED_FUNCS` | Số lượng hàm thay đổi tối đa trong một báo cáo | `500` |
| `CRG_MAX_TRANSITIVE_FRONTIER` | Kích thước ranh giới tối đa cho mở rộng caller/callee bắc cầu | `50` |
| `CRG_TOOL_TIMEOUT` | Thời gian chờ (giây) tùy chọn cho các công cụ MCP (`0` là tắt) | `0` |
| `CRG_RECURSE_SUBMODULES` | Bao gồm git submodules khi đặt là `1`, `true`, hoặc `yes` | - |
| `CRG_TOOLS` | Danh sách các công cụ MCP cho phép phân tách bằng dấu phẩy | - |
| `GOOGLE_API_KEY` | API key cho Google Gemini embeddings | - |
| `MINIMAX_API_KEY` | API key cho MiniMax embeddings | - |
| `CRG_OPENAI_BASE_URL` | Endpoint embeddings tương thích OpenAI | - |
| `CRG_OPENAI_API_KEY` | API key cho embeddings tương thích OpenAI | - |
| `CRG_OPENAI_MODEL` | Tên mô hình cho embeddings tương thích OpenAI | - |
| `CRG_OPENAI_DIMENSION` | Ghim chiều dài embedding (các mô hình v3 hỗ trợ giảm chiều) | - |
| `NO_COLOR` | Nếu đặt, tắt màu ANSI trong terminal | - |
| `CRG_SERIAL_PARSE` | Nếu `1`, tắt phân tích song song (dùng cho debug) | - |

Embeddings tương thích OpenAI (OpenAI thực tế, Azure, hoặc bất kỳ gateway tự host nào như new-api / LiteLLM / vLLM / LocalAI / Ollama ở chế độ openai) không cần cài đặt thêm — chỉ cần đặt các biến môi trường và truyền `provider="openai"` tới `embed_graph`:

```bash
export CRG_OPENAI_BASE_URL=http://127.0.0.1:3000/v1     # hoặc https://api.openai.com/v1
export CRG_OPENAI_API_KEY=sk-...
export CRG_OPENAI_MODEL=text-embedding-3-small          # bất kỳ model nào gateway của bạn hỗ trợ
# tùy chọn:
export CRG_OPENAI_DIMENSION=1536                        # ghim chiều (model v3 hỗ trợ giảm chiều)
export CRG_OPENAI_BATCH_SIZE=100                        # giảm xuống cho gateway có giới hạn chặt
                                                        # (vd: Qwen text-embedding-v4 giới hạn ở 10)
```

Cảnh báo đẩy dữ liệu cloud được tự động bỏ qua khi URL gốc trỏ tới localhost (`127.0.0.1`, `localhost`, `0.0.0.0`, `::1`).

> **Mẹo chọn mô hình.** Tránh các ID mô hình dạng `-preview` / `-beta` / `-exp` (vd: `google/gemini-embedding-2-preview`) cho bất kỳ nhu cầu lưu trữ lâu dài nào — các mô hình preview có thể thay đổi trọng số (chiều dài khác → yêu cầu re-embed toàn bộ) hoặc bị hỏng mà không báo trước. Ưu tiên các bản phát hành GA ổn định như `text-embedding-3-small` / `text-embedding-3-large` (OpenAI), `Qwen/Qwen3-Embedding-8B` (qua vLLM / LocalAI tự host), hoặc `gemini-embedding-001` (qua provider Gemini bản địa, yêu cầu `GOOGLE_API_KEY` thay vì đường dẫn tương thích OpenAI).
>
> `code-review-graph` nhúng các định danh, chữ ký, ngữ cảnh cấu trúc và tóm tắt đoạn docstring/doc-comment đầu tiên. Nó không truyền nội dung thân hàm. Các graph được tạo trước khi thêm trích xuất tài liệu cần một lần `code-review-graph build` đầy đủ trước khi re-embed để mọi file được re-parse. Các lần build thông thường không bao giờ làm mới embeddings theo mặc định. Để làm mới một chỉ mục hiện có sau khi build, hãy truyền rõ ràng cả `--embedding-provider` và `--embedding-model`; các lựa chọn cloud có thể truyền văn bản nguồn này và phát sinh chi phí API.

#### Lọc công cụ (Tool Filtering)

CRG cung cấp 30 công cụ MCP mặc định. Trong môi trường hạn chế token, bạn có thể giới hạn máy chủ ở một tập hợp công cụ bằng cờ `--tools` hoặc biến môi trường `CRG_TOOLS`:

```bash
# Via CLI flag
code-review-graph serve --tools query_graph_tool,semantic_search_nodes_tool,detect_changes_tool

# Via environment variable
CRG_TOOLS=query_graph_tool,semantic_search_nodes_tool code-review-graph serve
```

Cờ CLI có độ ưu tiên cao hơn biến môi trường. Khi cả hai không được đặt, tất cả các công cụ đều sẵn sàng. Điều này đặc biệt hữu ích cho cấu hình client MCP:

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

## FAQ & So sánh

Trả lời ngắn gọn, trung thực tại [docs/FAQ.md](docs/FAQ.md):

- [So với LSP / language servers](docs/FAQ.md#how-is-this-different-from-lsp-and-language-servers) — một đồ thị đa ngôn ngữ bền vững thay vì các daemon theo từng ngôn ngữ; LSP chính xác hơn trên từng symbol.
- [So với RAG / embeddings](docs/FAQ.md#isnt-this-just-rag) — các cạnh cấu trúc được phân tích từ AST, không phải các đoạn văn bản tương đồng; embeddings là tùy chọn và chỉ hỗ trợ tìm kiếm.
- [So với grep / agentic search](docs/FAQ.md#why-not-just-grep) — grep thắng ở tra cứu 1 bước; graph thắng ở các câu hỏi đa bước (bán kính ảnh hưởng, callers-of-callers, tests-for, luồng ảnh hưởng).
- [So với Serena, codegraph, claude-context, repomix](docs/FAQ.md#how-does-it-compare-to-serena-codegraph-claude-context-and-repomix) — bảng so sánh thực tế.
- [Khi nào KHÔNG nên dùng](docs/FAQ.md#when-should-i-not-use-it) — repo nhỏ, diff 1 file đơn giản, câu hỏi một lần.
- [Có gửi dữ liệu ra ngoài không?](docs/FAQ.md#does-it-phone-home) — không; zero telemetry, cloud embeddings là tùy chọn (opt-in).
- [Làm sao kiểm tra nó đang hoạt động?](docs/FAQ.md#how-do-i-verify-it-is-working) — `status`, `detect-changes --brief`, `/mcp`.

## Xử lý lỗi (Troubleshooting)

### `pip` / `pipx` không thể tải `hatchling` (hoặc `Errno 9` / `Bad file descriptor` đến PyPI)

Cài đặt từ **cây mã nguồn** (ví dụ `pipx install .`) cần các phụ thuộc build từ **PyPI** (ví dụ `hatchling`). Nếu bạn thấy `Could not find a version that satisfies the requirement hatchling` sau các cảnh báo kết nối, Python/pip trong **terminal** đó có thể không mở được HTTPS client tới `pypi.org` (đôi khi gặp trong terminal tích hợp của trình soạn thảo; ít gặp hơn ở mức hệ thống với VPN, firewall, hoặc proxy).

**Các lựa chọn:**

1. Chạy cùng lệnh từ **macOS Terminal.app** (hoặc iTerm) thay vì terminal của IDE, sau đó thử lại `pipx install .` hoặc `pipx install "git+https://..."`.
2. Sử dụng **[uv](https://docs.astral.sh/uv/)** để cài đặt CLI từ bản checkout (sử dụng cơ chế tải khác với `pip` trong nhiều trường hợp):

   ```bash
   cd /path/to/code-review-graph
   uv tool install . --force
   ```

3. Dành cho **phát triển trong bản clone** mà không cần cài đặt toàn cục, sử dụng `uv sync` và `uv run code-review-graph …` (hoặc kích hoạt `.venv` sau `uv sync`).

**Chẩn đoán (tùy chọn):** `python3 scripts/diagnose_pypi_connectivity.py` — nếu in ra `FAILED`, sự cố là do môi trường/mạng, không phải do sai tên package trong repo này.

### Sự cố cấu hình Windows (Invalid JSON / Connection Closed)
Nếu bạn đang dùng Windows và gặp lỗi `Invalid JSON: EOF while parsing` hoặc `MCP error -32000: Connection closed` khi kết nối qua Claude Code, không sử dụng wrapper `cmd /c` trong cấu hình của bạn.

Đảm bảo `fastmcp` được cập nhật lên tối thiểu `3.2.4+`. Sau đó, cấu hình `~/.claude.json` của bạn để thực thi trực tiếp file `.exe` và truyền biến môi trường UTF-8 qua cấu hình:

```json
"code-review-graph": {
  "command": "C:\\path\\to\\your\\venv\\Scripts\\code-review-graph.exe",
  "args": ["serve", "--repo", "C:\\path\\to\\your\\project"],
  "env": { "PYTHONUTF8": "1" }
}
```

## Đóng góp (Contributing)

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

<details>
<summary><strong>Thêm ngôn ngữ mới</strong></summary>
<br>

Chỉnh sửa `code_review_graph/parser.py`, thêm phần mở rộng vào `EXTENSION_TO_LANGUAGE` cùng ánh xạ loại node trong `_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES`, và `_CALL_TYPES`. Kèm theo bài test fixture và mở PR.

</details>

## Giấy phép (License)

MIT. Xem [LICENSE](LICENSE).

<p align="center">
<br>
<a href="https://code-review-graph.com">code-review-graph.com</a><br><br>
<code>pip install code-review-graph && code-review-graph install</code><br>
<sub>Hỗ trợ Codex, Claude Code, CodeBuddy Code, Cursor, Windsurf, Zed, Continue, OpenCode, Antigravity, Gemini CLI, Qwen, Qoder, Kiro, GitHub Copilot, và GitHub Copilot CLI</sub>
</p>
