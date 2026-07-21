# code-review-graph

**Ngừng đốt token. Bắt đầu review thông minh hơn.**

[English](https://github.com/tirth8205/code-review-graph/blob/main/README.md) | [简体中文](https://github.com/tirth8205/code-review-graph/blob/main/README.zh-CN.md) | [日本語](https://github.com/tirth8205/code-review-graph/blob/main/README.ja-JP.md) | [한국어](https://github.com/tirth8205/code-review-graph/blob/main/README.ko-KR.md) | [हिन्दी](https://github.com/tirth8205/code-review-graph/blob/main/README.hi-IN.md) | Tiếng Việt

[![PyPI](https://img.shields.io/pypi/v/code-review-graph?style=flat-square&color=blue)](https://pypi.org/project/code-review-graph/) [![Downloads](https://img.shields.io/pepy/dt/code-review-graph?style=flat-square)](https://pepy.tech/project/code-review-graph) [![Stars](https://img.shields.io/github/stars/tirth8205/code-review-graph?style=flat-square)](https://github.com/tirth8205/code-review-graph/stargazers) [![MIT Licence](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](https://opensource.org/licenses/MIT) [![CI](https://github.com/tirth8205/code-review-graph/actions/workflows/ci.yml/badge.svg)](https://github.com/tirth8205/code-review-graph/actions/workflows/ci.yml) [![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg?style=flat-square)](https://www.python.org/) [![MCP](https://img.shields.io/badge/MCP-compatible-green.svg?style=flat-square)](https://modelcontextprotocol.io/) [![Website](https://img.shields.io/badge/website-code--review--graph.com-blue?style=flat-square)](https://code-review-graph.com) [![Discord](https://img.shields.io/badge/discord-join-5865F2?style=flat-square&logo=discord&logoColor=white)](https://discord.gg/3p58KXqGFN)

Các công cụ AI hỗ trợ lập trình thường đọc lại toàn bộ codebase của bạn mỗi khi thực hiện một tác vụ. `code-review-graph` giải quyết vấn đề đó. Nó xây dựng một bản đồ cấu trúc của mã nguồn bằng [Tree-sitter](https://tree-sitter.github.io/tree-sitter/), theo dõi các thay đổi theo kiểu tăng dần (incremental), và cung cấp cho trợ lý AI của bạn ngữ cảnh chính xác thông qua [MCP](https://modelcontextprotocol.io/) để nó chỉ đọc những phần thực sự cần thiết.

---

## Bắt đầu nhanh

```
pip install code-review-graph                     # hoặc: pipx install code-review-graph
code-review-graph install          # tự động phát hiện và cấu hình mọi nền tảng được hỗ trợ
code-review-graph build            # phân tích codebase của bạn
```

Chỉ một lệnh để thiết lập mọi thứ. Lệnh `install` sẽ tự động phát hiện các công cụ AI coding bạn đang có, ghi đúng cấu hình MCP cho từng công cụ, cài đặt hook/skill gốc theo nền tảng (nếu được hỗ trợ), và chèn hướng dẫn nhận biết graph vào quy tắc của nền tảng đó. Nó tự phát hiện xem bạn cài qua `uvx` hay `pip`/`pipx` để sinh cấu hình phù hợp. Hãy khởi động lại editor/công cụ sau khi cài đặt.

Để nhắm vào một nền tảng cụ thể:

```
code-review-graph install --platform codex       # chỉ cấu hình Codex
code-review-graph install --platform cursor      # chỉ cấu hình Cursor
code-review-graph install --platform claude-code  # chỉ cấu hình Claude Code
code-review-graph install --platform gemini-cli   # chỉ cấu hình Gemini CLI
code-review-graph install --platform kiro         # chỉ cấu hình Kiro
code-review-graph install --platform copilot      # chỉ cấu hình GitHub Copilot (VS Code)
code-review-graph install --platform copilot-cli  # chỉ cấu hình GitHub Copilot CLI
```

Yêu cầu Python 3.10+. Để có trải nghiệm tốt nhất, hãy cài [uv](https://docs.astral.sh/uv/) (cấu hình MCP sẽ dùng `uvx` nếu có, nếu không sẽ dùng trực tiếp lệnh `code-review-graph`).

Sau đó mở project của bạn và hỏi trợ lý AI:

```
Build the code review graph for this project
```

Lần build đầu tiên mất khoảng ~10 giây cho một project 500 file. Sau đó, graph sẽ tự động cập nhật mỗi khi bạn chỉnh sửa file hoặc commit git.

## Cách hoạt động

Repository của bạn được phân tích thành AST bằng Tree-sitter, lưu trữ dưới dạng đồ thị gồm các node (hàm, class, import) và cạnh (lời gọi hàm, kế thừa, độ phủ test), sau đó được truy vấn tại thời điểm review để tính ra tập hợp file tối thiểu mà trợ lý AI của bạn cần đọc.

### Phân tích "bán kính ảnh hưởng" (blast-radius)

Khi một file thay đổi, graph sẽ truy vết mọi nơi gọi đến, phụ thuộc vào, và test có thể bị ảnh hưởng. Đây gọi là "bán kính ảnh hưởng" của thay đổi. AI của bạn chỉ đọc những file này thay vì quét toàn bộ project.

### Cập nhật tăng dần trong dưới 2 giây

Mỗi khi git commit hoặc lưu file, một hook sẽ được kích hoạt. Graph so sánh các file đã thay đổi, tìm các phụ thuộc thông qua kiểm tra hash SHA-256, và chỉ phân tích lại những gì đã thay đổi. Một project 2.900 file có thể lập chỉ mục lại trong dưới 2 giây.

### Giải quyết vấn đề monorepo

Các monorepo lớn là nơi lãng phí token nhiều nhất. Graph cắt bỏ phần nhiễu — loại trừ hơn 27.700 file khỏi ngữ cảnh review, chỉ thực sự đọc khoảng ~15 file.

### Hỗ trợ 24 ngôn ngữ + Jupyter notebook

Hỗ trợ đầy đủ ngữ pháp Tree-sitter cho hàm, class, import, lời gọi hàm, kế thừa và phát hiện test trong mọi ngôn ngữ. Bao gồm Zig, PowerShell, Julia, Svelte SFC, và hỗ trợ Nix nhận biết flake. Ngoài ra còn phân tích notebook Jupyter/Databricks (`.ipynb`) hỗ trợ nhiều ngôn ngữ trong cell (Python, R, SQL), và file Perl XS (`.xs`).

---

## Benchmark

Tất cả số liệu đến từ trình chạy đánh giá tự động trên 6 repository mã nguồn mở thực tế (tổng 13 commit). Tái hiện lại bằng `code-review-graph eval --all`. Dữ liệu thô tại [`evaluate/reports/summary.md`](https://github.com/tirth8205/code-review-graph/blob/main/evaluate/reports/summary.md).

**Hiệu suất token: giảm trung bình 8,2 lần (naive so với graph)**

Graph thay thế việc đọc toàn bộ file mã nguồn bằng một ngữ cảnh cấu trúc gọn nhẹ, bao phủ bán kính ảnh hưởng, chuỗi phụ thuộc và các khoảng trống về test coverage.

| Repo | Số commit | Token trung bình (Naive) | Token trung bình (Graph) | Mức giảm |
| --- | --- | --- | --- | --- |
| express | 2 | 693 | 983 | 0.7x |
| fastapi | 2 | 4,944 | 614 | 8.1x |
| flask | 2 | 44,751 | 4,252 | 9.1x |
| gin | 3 | 21,972 | 1,153 | 16.4x |
| httpx | 2 | 12,044 | 1,728 | 6.9x |
| nextjs | 2 | 9,882 | 1,249 | 8.0x |
| **Trung bình** | **13** | | | **8.2x** |

**Vì sao express cho kết quả <1x:** Với các thay đổi chỉ trong một file ở package nhỏ, ngữ cảnh graph (metadata, cạnh, hướng dẫn review) có thể lớn hơn kích thước file gốc. Cách tiếp cận bằng graph phát huy hiệu quả khi thay đổi liên quan đến nhiều file, nơi nó lọc bỏ được mã không liên quan.

**Độ chính xác phân tích ảnh hưởng: recall 100%, F1 trung bình 0,54**

Phân tích bán kính ảnh hưởng không bao giờ bỏ sót một file thực sự bị ảnh hưởng (recall hoàn hảo). Nó có xu hướng dự đoán dư trong một số trường hợp — đây là sự đánh đổi mang tính thận trọng, thà cảnh báo dư file còn hơn bỏ sót một phụ thuộc bị hỏng.

**Giới hạn và điểm yếu đã biết**

- **Thay đổi nhỏ trong một file:** Ngữ cảnh graph có thể lớn hơn việc đọc file thô cho các chỉnh sửa nhỏ (xem kết quả express ở trên).
- **Chất lượng tìm kiếm (MRR 0.35):** Tìm kiếm theo từ khóa tìm ra kết quả đúng trong top-4 cho hầu hết truy vấn, nhưng cần cải thiện việc xếp hạng.
- **Phát hiện luồng (flow) (recall 33%):** Chỉ phát hiện đáng tin cậy các entry point trong repo Python (fastapi, httpx). Việc phát hiện luồng ở JavaScript và Go còn cần cải thiện.
- **Đánh đổi giữa precision và recall:** Phân tích ảnh hưởng cố tình mang tính thận trọng, có thể tạo ra một số kết quả dương tính giả trong đồ thị phụ thuộc lớn.

---

## Tính năng

| Tính năng | Chi tiết |
| --- | --- |
| **Cập nhật tăng dần** | Chỉ phân tích lại các file đã thay đổi. Các lần cập nhật sau hoàn tất trong dưới 2 giây. |
| **24 ngôn ngữ + notebook** | Python, TypeScript/TSX, JavaScript, Vue, Svelte, Go, Rust, Java, Scala, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++, Dart, R, Perl, Lua, Zig, PowerShell, Julia, Nix, Jupyter/Databricks (.ipynb) |
| **Phân tích bán kính ảnh hưởng** | Hiển thị chính xác những hàm, class và file nào bị ảnh hưởng bởi bất kỳ thay đổi nào |
| **Hook tự động cập nhật** | Graph cập nhật mỗi khi chỉnh sửa file và commit git, không cần can thiệp thủ công |
| **Tìm kiếm ngữ nghĩa** | Vector embedding tùy chọn qua sentence-transformers, Google Gemini, MiniMax, hoặc bất kỳ endpoint tương thích OpenAI nào |
| **Trực quan hóa tương tác** | Đồ thị lực D3.js với tìm kiếm, bật/tắt chú giải cộng đồng, node theo bậc kết nối |
| **Phát hiện hub & bridge** | Tìm các node kết nối nhiều nhất và các điểm nghẽn kiến trúc |
| **Chấm điểm bất ngờ** | Phát hiện sự kết dính không mong đợi: xuyên cộng đồng, xuyên ngôn ngữ, từ ngoại vi đến hub |
| **Phân tích khoảng trống kiến thức** | Xác định các node bị cô lập, điểm nóng thiếu test, cộng đồng mỏng và điểm yếu cấu trúc |
| **Câu hỏi gợi ý** | Tự động sinh câu hỏi review từ phân tích graph (bridge, hub, bất ngờ) |
| **Độ tin cậy của cạnh** | Chấm điểm 3 mức (EXTRACTED/INFERRED/AMBIGUOUS) với điểm số float trên các cạnh |
| **Duyệt graph** | Duyệt BFS/DFS tự do từ bất kỳ node nào với độ sâu và ngân sách token tùy chỉnh |
| **Định dạng xuất** | GraphML (Gephi/yEd), Neo4j Cypher, Obsidian vault với wikilink, đồ thị tĩnh SVG |
| **So sánh graph** | So sánh các bản snapshot của graph theo thời gian: node/cạnh mới/đã xóa, thay đổi cộng đồng |
| **Đo lường token** | Đo token naive full-corpus so với token truy vấn graph, có tỷ lệ theo từng câu hỏi |
| **Vòng lặp bộ nhớ** | Lưu kết quả Q&A dưới dạng markdown để nạp lại, giúp graph phát triển từ các truy vấn |
| **Tự động chia nhỏ cộng đồng** | Cộng đồng quá lớn (>25% graph) được tự động chia bằng thuật toán Leiden |
| **Luồng thực thi** | Truy vết chuỗi lời gọi từ entry point, sắp xếp theo mức độ quan trọng |
| **Phát hiện cộng đồng** | Phân cụm mã liên quan bằng thuật toán Leiden, có điều chỉnh độ phân giải cho graph lớn |
| **Tổng quan kiến trúc** | Bản đồ kiến trúc tự động sinh, kèm cảnh báo về sự kết dính |
| **Review có chấm điểm rủi ro** | `detect_changes` ánh xạ diff vào các hàm, luồng bị ảnh hưởng và khoảng trống test |
| **Công cụ refactor** | Xem trước đổi tên, phát hiện dead code theo framework, gợi ý theo cộng đồng |
| **Sinh wiki** | Tự động sinh wiki markdown từ cấu trúc cộng đồng |
| **Registry đa repo** | Đăng ký nhiều repo, tìm kiếm trên tất cả |
| **Daemon đa repo** | `crg-daemon` theo dõi nhiều repo dưới dạng tiến trình con, có health check và tự khởi động lại |
| **MCP prompts** | 5 mẫu quy trình làm việc: review, kiến trúc, debug, onboard, kiểm tra trước merge |
| **Tìm kiếm full-text** | Tìm kiếm lai (hybrid) kết hợp từ khóa và độ tương đồng vector qua FTS5 |
| **Lưu trữ local** | File SQLite trong `.code-review-graph/`. Không cần database ngoài, không phụ thuộc cloud. |
| **Chế độ theo dõi (watch)** | Cập nhật graph liên tục khi bạn làm việc |

---

## Cách sử dụng

**Slash commands**

| Lệnh | Mô tả |
| --- | --- |
| `/code-review-graph:build-graph` | Xây dựng hoặc xây dựng lại code graph |
| `/code-review-graph:review-delta` | Review các thay đổi kể từ commit gần nhất |
| `/code-review-graph:review-pr` | Review PR đầy đủ kèm phân tích bán kính ảnh hưởng |

**Tham chiếu CLI**

```
code-review-graph install          # Tự động phát hiện và cấu hình mọi nền tảng
code-review-graph install --platform <name>  # Nhắm vào một nền tảng cụ thể
code-review-graph build            # Phân tích toàn bộ codebase
code-review-graph update           # Cập nhật tăng dần (chỉ file đã thay đổi)
code-review-graph status           # Thống kê graph
code-review-graph watch            # Tự động cập nhật khi file thay đổi
code-review-graph visualize        # Sinh đồ thị HTML tương tác
code-review-graph visualize --format graphml   # Xuất dạng GraphML
code-review-graph visualize --format svg       # Xuất dạng SVG
code-review-graph visualize --format obsidian  # Xuất dạng Obsidian vault
code-review-graph visualize --format cypher    # Xuất dạng Neo4j Cypher
code-review-graph wiki             # Sinh wiki markdown từ các cộng đồng
code-review-graph detect-changes   # Phân tích ảnh hưởng thay đổi có chấm điểm rủi ro
code-review-graph register <path>  # Đăng ký repo vào registry đa repo
code-review-graph unregister <id>  # Gỡ repo khỏi registry
code-review-graph repos            # Liệt kê các repo đã đăng ký
code-review-graph daemon start     # Khởi động daemon theo dõi đa repo
code-review-graph daemon stop      # Dừng daemon
code-review-graph daemon status    # Hiển thị trạng thái daemon và các repo
code-review-graph eval             # Chạy các benchmark đánh giá
code-review-graph serve            # Khởi động MCP server
```

**Cấu hình**

Để loại trừ đường dẫn khỏi việc lập chỉ mục, tạo file `.code-review-graphignore` ở thư mục gốc repository:

```
generated/**
*.generated.ts
vendor/**
node_modules/**
```

Lưu ý: trong các repo git, chỉ những file đã được theo dõi (`git ls-files`) mới được lập chỉ mục, nên các file bị gitignore sẽ tự động bị bỏ qua. Dùng `.code-review-graphignore` để loại trừ file đã theo dõi hoặc khi không có git.

Các nhóm phụ thuộc tùy chọn:

```
pip install code-review-graph[embeddings]          # Vector embedding cục bộ (sentence-transformers)
pip install code-review-graph[google-embeddings]   # Embedding Google Gemini
pip install code-review-graph[communities]         # Phát hiện cộng đồng (igraph)
pip install code-review-graph[eval]                # Benchmark đánh giá (matplotlib)
pip install code-review-graph[wiki]                # Sinh wiki với tóm tắt LLM (ollama)
pip install code-review-graph[all]                 # Tất cả phụ thuộc tùy chọn
```

## Đóng góp

```
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

**Thêm một ngôn ngữ mới**

Chỉnh sửa `code_review_graph/parser.py` và thêm phần mở rộng của bạn vào `EXTENSION_TO_LANGUAGE` cùng với ánh xạ kiểu node trong `_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES`, và `_CALL_TYPES`. Kèm theo một test fixture và mở PR.

## Giấy phép

MIT. Xem [LICENSE](https://github.com/tirth8205/code-review-graph/blob/main/LICENSE).

---

*Đây là bản dịch tiếng Việt không chính thức của README.md gốc. Nếu có sai lệch, vui lòng tham khảo [bản tiếng Anh](https://github.com/tirth8205/code-review-graph/blob/main/README.md).*