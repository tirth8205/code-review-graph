<h1 align="center">code-review-graph</h1>

<p align="center">
  <strong>ٹوکن ضائع کرنا بند کریں۔ ذہین جائزہ لینا شروع کریں۔</strong>
</p>

<p align="center">
  <a href="README.md">انگریزی</a> |
  <a href="README.zh-CN.md">简体中文</a> |
  <a href="README.ja-JP.md">日本語</a> |
  <a href="README.ko-KR.md">한국어</a> |
  <a href="README.hi-IN.md">हिन्दी</a> |
  <a href="README.ur-PK.md">اردو</a>
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
  <a href="docs/USAGE.md">استعمال</a> ·
  <a href="docs/COMMANDS.md">کمانڈز</a> ·
  <a href="docs/FAQ.md">سوالات متداول</a> ·
  <a href="docs/TROUBLESHOOTING.md">مشکلات کا حل</a> ·
  <a href="docs/GITHUB_ACTION.md">GitHub ایکشن</a> ·
  <a href="docs/REPRODUCING.md">بینچمارک دوبارہ پیش کرنا</a> ·
  <a href="docs/ROADMAP.md">راہِ کار</a>
</p>

<br>

AI کوڈنگ ٹولز کوڈ کے جائزے کے دوران آپ کے کوڈ بیس کے بڑے حصوں کو دوبارہ پڑھ سکتے ہیں۔ `code-review-graph` یہ مسئلہ حل کرتا ہے۔ یہ [Tree-sitter](https://tree-sitter.github.io/tree-sitter/) کے ساتھ آپ کے کوڈ کا ایک ساختی نقشہ بناتا ہے، تبدیلیوں کو قدم بہ قدم ٹریک کرتا ہے، اور [MCP](https://modelcontextprotocol.io/) کے ذریعے اپنے AI معاون کو درست سیاق و سباق فراہم کرتا ہے تاکہ وہ صرف وہی پڑھے جو اہم ہو۔

<p align="center">
  <img src="diagrams/diagram1_before_vs_after.png" alt="ٹوکن مسئلہ: 6 حقیقی ریپوزٹریوں میں 38x سے 528x تک ٹوکن کمی" width="85%" />
</p>

---

## فوری آغاز

```bash
pip install code-review-graph                     # or: pipx install code-review-graph
code-review-graph install          # auto-detects and configures all supported platforms
code-review-graph build            # parse your codebase
```

ایک کمانڈ سب کچھ ترتیب دے دیتی ہے۔ `install` خود اس بات کی شناخت کرتی ہے کہ آپ کے پاس کون سے AI کوڈنگ ٹولز موجود ہیں، ہر ایک کے لیے درست MCP ترتیب لکھتی ہے، معاون ہک/اسکلز کو جہاں ممکن ہو وہاں انسٹال کرتی ہے، اور اپنے پلیٹ فارم اصولوں میں گراف سے آگاہ ہدایات داخل کرتی ہے۔ یہ خودکار طور پر معلوم کرتی ہے کہ آپ نے `uvx` یا `pip`/`pipx` کے ذریعے انسٹال کیا ہے اور مناسب ترتیب تیار کرتی ہے۔ انسٹال کے بعد اپنے ایڈیٹر/ٹول کو دوبارہ شروع کریں۔

<p align="center">
  <img src="diagrams/diagram8_supported_platforms.png" alt="ایک انسٹال، ہر پلیٹ فارم: Codex، Claude Code، CodeBuddy Code، Cursor، Windsurf، Zed، Continue، OpenCode، Antigravity، Gemini CLI، Qwen، Qoder، Kiro، اور GitHub Copilot کی خودکار شناخت اور ترتیب" width="85%" />
</p>

کسی خاص پلیٹ فارم کو ہدف بنانے کے لیے:

```bash
code-review-graph install --platform codex       # configure only Codex
code-review-graph install --platform cursor      # configure only Cursor
code-review-graph install --platform claude-code  # configure only Claude Code
code-review-graph install --platform gemini-cli   # configure only Gemini CLI
code-review-graph install --platform kiro         # configure only Kiro
code-review-graph install --platform copilot      # configure only GitHub Copilot (VS Code)
code-review-graph install --platform copilot-cli  # configure only GitHub Copilot CLI
code-review-graph install --platform codebuddy    # configure only CodeBuddy Code
```

Python 3.10+ درکار ہے۔ بہترین تجربے کے لیے [uv](https://docs.astral.sh/uv/) انسٹال کریں (MCP ترتیب اگر دستیاب ہو تو `uvx` استعمال کرے گی، ورنہ براہِ راست `code-review-graph` کمانڈ پر واپس جائے گی)۔

کسی Git یا SVN پروجیکٹ سے CRG ہٹانے کے لیے اس کی working tree کے اندر کسی بھی جگہ سے متوازن `uninstall` کمانڈ استعمال کریں۔ ہدف کو working tree کی جڑ تک معمول پر لایا جاتا ہے، اور غیر repository ڈائریکٹریاں مسترد کر دی جاتی ہیں۔ یہ صرف CRG کی ملکیت والی فائلیں اور اندراجات ہٹاتی ہے؛ غیر متعلقہ MCP سرورز، hooks، skills، اور JSONC comments برقرار رہتے ہیں۔ مشترکہ config میں تبدیلیاں atomic replacement استعمال کرتی ہیں، اس لیے ناکام write اصل فائل کو جوں کا توں رکھتی ہے۔

```bash
code-review-graph uninstall --dry-run    # preview every action; write nothing
code-review-graph uninstall              # preview, ask for confirmation, then apply
code-review-graph uninstall --yes        # apply without prompting
code-review-graph uninstall --all-repos  # also clean every registered repository
code-review-graph uninstall --keep-data  # remove integrations but keep graph databases
code-review-graph uninstall --keep-user-configs --repo .  # clean this project only
```

اپنا پروجیکٹ کھولیں اور اپنے AI معاون سے یہ پوچھیں:

```
Build the code review graph for this project
```

ابتدائی تعمیر 500-فائل پروجیکٹ کے لیے تقریباً 10 سیکنڈ لیتی ہے۔ اس کے بعد، واچ موڈ اور معاون ہکس کے ذریعے گراف خودکار طور پر اپ ڈیٹ رہ سکتا ہے۔


## یہ کیسے کام کرتا ہے

<p align="center">
  <img src="diagrams/diagram7_mcp_integration_flow.png" alt="آپ کا AI معاون گراف کو کیسے استعمال کرتا ہے: صارف جائزہ مانگتا ہے، AI MCP ٹولز چیک کرتا ہے، گراف بلاسٹ ریڈیئس اور رسک اسکور واپس کرتا ہے، AI صرف وہی پڑھتا ہے جو اہم ہو" width="80%" />
</p>

آپ کی ریپوزٹری کو Tree-sitter کے ذریعے AST میں تجزیہ کیا جاتا ہے، نوڈز (فنکشنز، کلاسز، امپورٹس) اور ایجز (کالز، وراثت، ٹیسٹ کور) کے گراف کے طور پر محفوظ کیا جاتا ہے، پھر جائزے کے وقت اس سے کم سے کم تعداد میں فائلوں کا مجموعہ حاصل کیا جاتا ہے جنہیں آپ کا AI معاون پڑھنے کی ضرورت ہو۔

<p align="center">
  <img src="diagrams/diagram2_architecture_pipeline.png" alt="آرکیٹیکچر پائپ لائن: ریپوزٹری سے Tree-sitter پارسر، SQLite گراف، بلاسٹ ریڈیئس، کم سے کم جائزہ سیٹ" width="100%" />
</p>

### بلاسٹ ریڈیئس تجزیہ

جب کوئی فائل بدلتی ہے، تو گراف ہر کالر، انحصار کرنے والے، اور ٹیسٹ کو اس حد تک ٹریک کرتا ہے جو متاثر ہو سکتے ہیں۔ یہ تبدیلی کا "بلاسٹ ریڈیئس" ہے۔ آپ کا AI پورے پروجیکٹ کو اسکین کرنے کے بجائے صرف ان فائلوں کو پڑھتا ہے۔

<p align="center">
  <img src="diagrams/diagram3_blast_radius.png" alt="بلاسٹ ریڈیئس ویژولائزیشن: login() میں تبدیلی کے نتیجے میں کالرز، انحصار کرنے والے، اور ٹیسٹوں تک کیسے پھیلتی ہے" width="70%" />
</p>

### 2 سیکنڈ سے کم میں incremental updates

جب ہکس یا واچ موڈ فعال ہوں، تو فائلوں کے محفوظ ہونے اور معاون commit ہکس سے incremental updates شروع ہو جاتے ہیں۔ گراف بدلتی ہوئی فائلوں اور ان کے انحصار کرنے والوں کو SHA-256 ہیش چیک کے ذریعے تلاش کرتا ہے، اور صرف بدلا ہوا حصہ دوبارہ پارس ہوتا ہے۔ 2,900 فائلوں پر مبنی پروجیکٹ 2 سیکنڈ سے کم میں دوبارہ انڈیکس ہو جاتا ہے۔

<p align="center">
  <img src="diagrams/diagram4_incremental_update.png" alt="incremental update flow: معاون ہک یا واچ اپ ڈیٹ کو ٹرگر کرتا ہے، فرق تلاش کرتا ہے، صرف 5 فائلیں دوبارہ پارس ہوتی ہیں جبکہ 2,910 چھوڑ دی جاتی ہیں" width="90%" />
</p>

### مونو ریپوزٹری کا مسئلہ، حل

بڑے مونو ریپوزٹری میں ٹوکن کی بے ترتیبی سب سے زیادہ محسوس ہوتی ہے۔ گراف شور کو حذف کر دیتا ہے — 27,700+ فائلوں کو جائزے کے سیاق و سباق سے خارج کر دیا جاتا ہے، صرف تقریباً 15 فائلیں واقعی پڑھی جاتی ہیں۔

<p align="center">
  <img src="diagrams/diagram6_monorepo_funnel.png" alt="code-review-graph ریپوزٹری: 208,821 سورس ٹوکن ایک چھوٹے سے گراف جواب میں تقریباً 2,495 ٹوکن تک آتے ہیں — سوال کے لیے 93x کم ٹوکن" width="80%" />
</p>

### وسیع زبان کی کوریج + Jupyter نوٹ بک

<p align="center">
  <img src="diagrams/diagram9_language_coverage.png" alt="زبان کی کوریج کو زمرے کے مطابق ترتیب دیا گیا ہے: ویب، بیک اینڈ، سسٹمز، موبائل، اسکرپٹنگ، کنفیگریشن، اور Jupyter اور Databricks نوٹ بک سپورٹ" width="90%" />
</p>

پارسر سپورٹ میں موجودہ پارسر سطح پر فنکشنز، کلاسز، امپورٹس، کال سائٹس، وراثت، اور ٹیسٹ کا پتہ لگانے کی سہولت شامل ہے، اور جہاں مل سکے وہاں Tree-sitter استعمال ہوتا ہے، اور جہاں ضرورت ہو وہاں ہدفی فالبیکس استعمال کیے جاتے ہیں۔ موجودہ سپورٹ میں Python، JavaScript/TypeScript/TSX، Go، Rust، Java، C/C++، C#، Ruby، Kotlin، Swift، PHP، Scala، Solidity، Dart، R، Perl، Lua/Luau، Objective-C، shell scripts، Elixir، Zig، PowerShell، Julia، ReScript، GDScript، Nix، Verilog/SystemVerilog، SQL، Vue/Svelte SFCs، TypeScript parser کے ذریعے پارس ہونے والی Astro فائلیں، Jupyter/Databricks نوٹ بک (`.ipynb`)، اور Perl XS فائلیں (`.xs`) شامل ہیں۔

PHP پروجیکٹس کو مزید repository-bounded Composer PSR-4 resolution، Blade template references، اور Laravel Route/Eloquent semantic edges ملتی ہیں، جب source میں واضح framework imports، model inheritance، اور receiver evidence موجود ہوں۔

### اپنی زبان شامل کریں (کوئی فورک درکار نہیں)

اگر آپ کے ریپو میں ایسی زبان استعمال ہوتی ہے جسے پارسر ابھی تک سپورٹ نہیں کرتا، تو `.code-review-graph/` میں ایک `languages.toml` فائل ڈالیں جو فائل ایکسٹینشنز کو `tree_sitter_language_pack` میں موجود کسی بھی گرامر سے اور فنکشنز، کلاسز، امپورٹس، اور کالز کے لیے tree-sitter node types سے مربوط کرے:

```toml
[languages.erlang]
extensions = [".erl"]
grammar = "erlang"
function_node_types = ["function_clause"]
class_node_types = ["record_decl"]
import_node_types = ["import_attribute"]
call_node_types = ["call"]
```

عام tree-sitter واکر اس سے استخراج کرتا ہے — کوئی کوڈ تبدیلی نہیں، اور built-in زبانوں کو override کیا نہیں جا سکتا۔ اس اسکیمے کے حوالہ، توثیق کے اصولوں، اور ایک مکمل مثال کے لیے [docs/CUSTOM_LANGUAGES.md](docs/CUSTOM_LANGUAGES.md) دیکھیں۔

### CI میں رسک-اسکور شدہ PR جائزے (GitHub Action)

یہی تجزیہ ایک composite GitHub Action کے طور پر بھی چلتا ہے — اور یہ مقامی طور پر ہی رہتا ہے: knowledge graph آپ کے CI runner پر ہی بنایا اور استفسار کیا جاتا ہے، اور سورس کوڈ کسی بیرونی سروس کو بھیجا نہیں جاتا۔ ہر pull request پر یہ ایک sticky comment پوسٹ کرتا ہے جس میں رسک-اسکور شدہ فنکشنز، متاثرہ execution flows، اور ٹیسٹ کوریج کے خلا شامل ہوتے ہیں، اور ہر push پر اسی جگہ اپ ڈیٹ ہوتے رہتے ہیں۔ ایک اختیاری `fail-on-risk` ان پٹ اس جائزے کو merge gate بھی بنا سکتا ہے۔

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

ان پٹس، رسک لیولز، اور کیشنگ تفصیلات کے لیے [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md) دیکھیں، یا اس ریپو میں خود اس کے لیے استعمال ہونے والے dogfood workflow کو [`.github/workflows/pr-review.yml`](.github/workflows/pr-review.yml) میں دیکھیں۔

---

## بینچمارکس

<p align="center">
  <img src="diagrams/diagram5_benchmark_board.png" alt="6 حقیقی ریپوزٹریوں پر بینچمارکس: ~82x اوسط per-question ٹوکن کمی (528x زیادہ سے زیادہ), 0.71 اوسط impact F1 graph-derived ground truth کے خلاف" width="85%" />
</p>

**اہم نمبر: 6 ریپوزٹریوں میں per-question ٹوکن کمی کا اوسط ~82x ہے** (پورے کورپس بیس لائن کے مقابلے میں گراف استفسار). اکثر مذکور **528x** زیادہ سے زیادہ ہے — ایک واحد بہترین کیس ریپو (fastapi) — عام نتائج نہیں۔

تمام اعداد و شمار 6 حقیقی اوپن سورس ریپوزٹریوں (13 commits) کے خلاف خودکار eval runner سے آئے ہیں۔ ہر کنفیگریشن ایک اپ اسٹریم SHA کو پِن کرتی ہے، Leiden community detector ایک مقررہ سیڈ کے ساتھ چلتا ہے، اور embeddings CPU پر متعین ہوتے ہیں — لہٰذا مختلف مشینوں پر دو بار چلانے سے ایک جیسا نتیجہ آتا ہے۔ مکمل دوبارہ پیش کرنے کی ترکیب اور متوقع نتائج [docs/REPRODUCING.md](docs/REPRODUCING.md) میں موجود ہیں۔ دو چھوٹے کنفیگریشنز پر ہفتہ وار صرف-رپورٹ چلانے کا عمل [`.github/workflows/eval.yml`](.github/workflows/eval.yml) میں ہے۔

<details>
<summary><strong>ٹوکن کارکردگی: ~82x اوسط per-question کمی (حدود 38x – 528x؛ پورا کورپس کے مقابلے میں گراف استفسار)</strong></summary>
<br>

ایک عام ایجنٹ سوال (مثلاً "how does authentication work"، "what is the main entry point" وغیرہ) کے لیے، گراف مخصوص سرچ ہٹس اور نیبر ایجز سے تقریباً 2,000–3,500 ٹوکن واپس کرتا ہے، اس کے بجائے کہ ایجنٹ کو پورے سورس کوڈ کو پڑھنا پڑے۔ جدول ذیل 5 نمونہ سوالات کا اوسط دکھاتا ہے جو `code_review_graph/token_benchmark.py` میں تعریف کیے گئے ہیں۔

| Repo | Snapshot SHA | naive_corpus_tokens | avg graph_tokens | Reduction |
|------|---|-----------------:|----------------:|----------:|
| fastapi | `0227991a` | 951,071 | 2,169 | **528.4x** |
| code-review-graph | `84bde354` | 208,821 | 2,495 | **93.0x** |
| gin | `5c00df8a` | 166,868 | 1,990 | **91.8x** |
| flask | `a29f88ce` | 125,022 | 1,986 | **71.4x** |
| express | `b4ab7d65` | 135,955 | 3,465 | **40.6x** |
| httpx | `b55d4635` | 89,492 | 2,438 | **38.0x** |

6 ریپوزٹریوں پر per-question کمی کا وسط: **~82x**۔ حد 38x – 528x ہے، جہاں **528x** بہترین کیس ہے (fastapi، سب سے بڑا کورپس)، نہ کہ یہ مرکزی اعداد۔

اوپر دکھایا گیا پورا کورپس بیس لائن ایک حد ہے جو حقیقی ایجنٹ ادا نہیں کرتا: ایک قابل ایجنٹ identifiers کی تلاش کرے گا اور صرف سب سے زیادہ ملنے والی فائلوں کو پڑھے گا۔ `agent_baseline` eval benchmark یہ حقیقی بیس لائن ماپتا ہے — کورپس پر pure-python grep، match count کے مطابق سب سے اوپر 3 فائلیں، ٹوکن-گنتی کے ساتھ، اور گراف استفسار کے اخراجات کے ساتھ موازنہ (`evaluate/results/<repo>_agent_baseline_*.csv`)۔

رسمی `eval/benchmarks/token_efficiency.py` benchmark ایک مختلف منظر کو ماپتا ہے — مکمل `get_review_context()` JSON کے مقابلے میں commit کی صرف بدلتی ہوئی فائلوں کا مواد — اور چھوٹے commits پر ratios 1 سے کم ظاہر کرتا ہے، کیونکہ review-context جواب میں impact-radius edges اور سورس snippets شامل ہوتے ہیں جو ایک چھوٹی single-file diff سے زیادہ ہوتے ہیں۔ یہ ایک خرابی نہیں؛ یہ دو بینچمارکس مختلف سوالات کا جواب دیتے ہیں۔ مکمل طریقہ کار کے لیے [docs/REPRODUCING.md](docs/REPRODUCING.md) دیکھیں۔

v2.3.4 کے بعد سے، review اور impact ٹولز ایک کمپیکٹ `context_savings` تخمینہ بھی لگاتے ہیں تاکہ MCP کلائنٹس یہ دیکھ سکیں کہ ہر کال پر تقریباً کتنی سیاق و سباق بچائی گئی۔ v2.3.5 میں CLI اس کو اوپر دکھائے گئے باکسڈ `Token Savings` پینل کے طور پر ظاہر کرتی ہے (استعمال کے حصے میں "ٹوکن بچت پینل" دیکھیں) اور `--verify` شامل کرتی ہے تاکہ OpenAI کے `cl100k_base` tokenizer کے مقابلے میں جانچ کی جا سکے۔ [docs/REPRODUCING.md](docs/REPRODUCING.md) میں موجود calibration data سے ظاہر ہوتا ہے کہ یہ تخمینہ 222 نمونہ فائلوں پر مجموعی طور پر حقیقی GPT-4 ٹوکن سے تقریباً 1% کے اندر ہے۔

</details>

<details>
<summary><strong>Impact accuracy: 0.71 اوسط F1 graph-derived ground truth کے خلاف (recall 1.0 ایک circular upper bound ہے، "100% recall" نہیں)</strong></summary>
<br>

بلاسٹ ریڈیئس تجزیہ 13 eval commits پر ground truth میں موجود ہر فائل کو حاصل کر لیتا ہے — **لیکن اسے ایک upper bound سمجھ کر پڑھیں، نہ کہ "100% recall" کے طور پر**: اس موڈ میں ground truth (بدلتی ہوئی فائلوں + ان فائلوں کے ساتھ جس میں call/import edges ہیں) خود اسی گراف سے اخذ کی گئی ہے جسے predictor طے کرتا ہے، لہٰذا یہ by construction circular ہے۔ precision کالم میں نظر آنے والی over-prediction ایک جان بوجھ کر اختیار کی گئی trade-off ہے: بُری ڈپینڈنسی کو missed کرنے سے بہتر ہے کہ بہت زیادہ فائلوں کو flag کیا جائے۔

| Repo | Commits | Avg F1 | Avg Precision | Recall (graph-derived upper bound) |
|------|--------:|-------:|--------------:|-------:|
| httpx | 2 | 0.864 | 0.786 | 1.0 |
| fastapi | 2 | 0.834 | 0.750 | 1.0 |
| code-review-graph | 2 | 0.734 | 0.584 | 1.0 |
| express | 2 | 0.667 | 0.500 | 1.0 |
| flask | 2 | 0.628 | 0.481 | 1.0 |
| gin | 3 | 0.609 | 0.439 | 1.0 |
| **Average** | **13** | **0.714** | **0.578** | **1.000** |

یہ بینچمارک ایک ایماندار **co-change mode** بھی چلتا ہے: predictor کو ایک بدلتی ہوئی فائل سے شروع کیا جاتا ہے اور اسے اسی commit میں مصنف واقعی نے جن فائلوں کو چھوا تھا ان کے خلاف graded کیا جاتا ہے — git history سے حاصل شدہ تقریباً آزاد ثبوت، گراف سے نہیں۔ دونوں موڈز result CSVs میں ایک ساتھ دکھائے جاتے ہیں (`ground_truth_mode` کالم)۔ canonical stats میں co-change numbers کو پھر شامل کیا جائے گا جب eval runner انہیں حاصل کر لے گا؛ ہم ان کو ماپنے سے پہلے نہیں لکھتے۔

</details>

<details>
<summary><strong>بنیادی کارکردگی</strong></summary>
<br>

| Repo | Files | Nodes | Edges | Flow Detection | Search Latency |
|------|------:|------:|------:|---------------:|---------------:|
| express | 141 | 1,910 | 17,553 | 106ms | 0.7ms |
| fastapi | 1,122 | 6,285 | 27,117 | 128ms | 1.5ms |
| flask | 83 | 1,446 | 7,974 | 95ms | 0.7ms |
| gin | 99 | 1,286 | 16,762 | 111ms | 0.5ms |
| httpx | 60 | 1,253 | 7,896 | 96ms | 0.4ms |

</details>

### محدودیات اور معلوم کمزوریاں

- **Impact "recall 1.0" graph-derived اور circular ہے:** تاریخی ground truth اسی گراف edges سے آتی ہے جسے predictor طے کرتا ہے، اس لیے یہ by construction upper bound ہے۔ ایماندار co-change mode (اسی commit میں واقعی co-changed فائلوں کے خلاف grade) بھی اس کے ساتھ ماپا جاتا ہے؛ توقع ہے کہ یہ اعداد بہت کم ہوں گے۔
- **چھوٹی single-file تبدیلیاں:** گراف سیاق و سباق naive file reads سے زیادہ ہو سکتا ہے۔ یہ اوور ہیڈ ساختی میٹا ڈیٹا ہے جو کئی فائلوں کے تجزیہ کے لیے ضروری ہے۔
- **Search quality (MRR 0.35):** keyword search زیادہ تر سوالات کے لیے درست نتیجہ top-4 میں تلاش کر لیتا ہے، مگر ranking میں بہتری کی ضرورت ہے۔ Express queries سے 0 hits آتے ہیں کیونکہ module-pattern naming میں مسئلہ ہے۔
- **Flow detection (33% recall):** framework اور روایتی entry patterns کی شناخت Python اور PHP/Laravel کے لیے سب سے مضبوط ہے۔ JavaScript اور Go کے لیے flow detection میں اب بھی بہتری درکار ہے۔
- **Precision vs recall trade-off:** Impact analysis جان بوجھ کر conservative ہے۔ یہ فائلوں کو flag کرتا ہے جو متاثر ہو سکتی ہیں، اس لیے بڑے dependency graphs میں کچھ false positives بھی آتے ہیں۔

---

## فیچر

| فیچر | تفصیلات |
|---------|---------|
| **incremental updates** | صرف بدلتی ہوئی فائلوں کو دوبارہ پارس کرتا ہے۔ اگلے updates 2 سیکنڈ سے کم میں مکمل ہو جاتے ہیں۔ |
| **وسیع زبان + notebook سپورٹ** | Python، JavaScript/TypeScript/TSX، Go، Rust، Java، C/C++، C#، Ruby، Kotlin، Swift، PHP، Scala، Solidity، Dart، R، Perl، Lua/Luau، Objective-C، shell scripts، Elixir، Zig، PowerShell، Julia، ReScript، GDScript، Nix، Verilog/SystemVerilog، SQL، Vue/Svelte SFCs، TypeScript parser کے ذریعے پارس ہونے والی Astro فائلیں، Jupyter/Databricks (.ipynb)، اور Perl XS (.xs) |
| **Framework-aware PHP parsing** | Repository-bounded Composer PSR-4 imports، Blade template references، اور evidence-gated Laravel Route-to-controller اور Eloquent relationship edges |
| **بلاسٹ ریڈیئس تجزیہ** | دکھاتا ہے کہ تبدیلی سے کون سے فنکشنز، کلاسز، اور فائلیں متاثر ہو سکتی ہیں |
| **خودکار اپ ڈیٹ ہکس** | ہکس اور واچ موڈ فائلوں کے محفوظ ہونے اور معاون commit ہکس پر گراف کو اپ ڈیٹ کر سکتے ہیں |
| **Semantic search** | sentence-transformers، Google Gemini، MiniMax، یا کوئی OpenAI-compatible endpoint کے ذریعے اختیاری vector embeddings |
| **انٹرایکٹو visualisation** | D3.js force-directed graph جس میں search، community legend toggles، اور degree-scaled nodes شامل ہیں |
| **Hub & bridge detection** | betweenness centrality کے ذریعے سب سے زیادہ جڑے ہوئے nodes اور آرکیٹیکچرل chokepoints تلاش کریں |
| **Surprise scoring** | غیر متوقع coupling کا پتہ لگائیں: cross-community، cross-language، peripheral-to-hub edges |
| **Knowledge gap analysis** | الگ تھلگ nodes، غیر ٹیسٹ شدہ hotspots، پتلی communities، اور ساختی کمزوریوں کی شناخت |
| **Suggested questions** | گراف تجزیہ سے خودکار سوالات (bridges، hubs، surprises) |
| **Edge confidence** | edges کے لیے تین درجے کی confidence scoring (EXTRACTED/INFERRED/AMBIGUOUS) اور float scores |
| **Graph traversal** | کسی بھی node سے free-form BFS/DFS exploration، قابلِ ترتیبات depth اور token budget |
| **Export formats** | GraphML (Gephi/yEd)، Neo4j Cypher، Obsidian vault with wikilinks، SVG static graph |
| **Graph diff** | وقت کے ساتھ گراف snapshots کا موازنہ کریں: نئی/ہٹائی گئی nodes، edges، community changes |
| **Token benchmarking** | naive full-corpus tokens اور graph query tokens کے مابین per-question ratios ماپیں |
| **Estimated context savings** | متعلقہ MCP/CLI review outputs پر کمپیکٹ `context_savings` metadata، جسے estimated علامت سے نشان زد کیا جاتا ہے اور صرف تین چھوٹے فیلڈوں میں رکھا جاتا ہے |
| **Memory loop** | Q&A نتائج کو markdown کے طور پر محفوظ کریں تاکہ گراف سوالات سے بڑھے |
| **Community auto-split** | بہت بڑے communities (>25% of graph) کو Leiden کے ذریعے recursively split کیا جاتا ہے |
| **Execution flows** | entry points سے call chains کا پتہ لگائیں، weighted criticality کے مطابق ترتیب دیے گئے |
| **Community detection** | Leiden algorithm کے ذریعے متعلقہ کوڈ کو cluster کریں، resolution scaling کے ساتھ بڑے graphs کے لیے |
| **Architecture overview** | خودکار طور پر architecture map اور coupling warnings تیار کریں |
| **Risk-scored reviews** | `detect_changes` diffs کو متاثرہ فنکشنز، flows، اور test gaps سے جوڑتا ہے |
| **Custom languages** | `.code-review-graph/languages.toml` کے ذریعے نئی زبانیں شامل کریں — فورک یا کوڈ تبدیلیوں کی ضرورت نہیں |
| **GitHub Action** | CI میں sticky risk-scored PR review comments، اختیاری `fail-on-risk` merge gate کے ساتھ |
| **Refactoring tools** | Rename preview، framework-aware dead code detection، community-driven suggestions |
| **Wiki generation** | community structure سے markdown wiki خودکار طور پر بنائیں |
| **Multi-repo registry** | کئی ریپو رجسٹر کریں اور ان سب میں تلاش کریں |
| **Multi-repo daemon** | `crg-daemon` کئی ریپوزٹری کو child processes کے طور پر دیکھتا ہے، health checks اور خودکار ریستارٹ کے ساتھ |
| **MCP prompts** | 5 workflow templates: review، architecture، debug، onboard، pre-merge |
| **Full-text search** | keyword اور vector similarity کو ملا کر FTS5-powered hybrid search |
| **Local storage** | `.code-review-graph/` میں SQLite فائل۔ بنیادی گراف اسٹوریج کے لیے بیرونی database یا cloud service کی ضرورت نہیں۔ |
| **Watch mode** | جب تک آپ کام کرتے رہیں، گراف خودکار طور پر اپ ڈیٹ ہوتا رہتا ہے |

---

## استعمال

<details>
<summary><strong>اسلش کمانڈز</strong></summary>
<br>

| کمانڈ | تفصیل |
|---------|-------------|
| `/code-review-graph:build-graph` | گراف بنائیں یا دوبارہ بنائیں |
| `/code-review-graph:review-delta` | آخری commit کے بعد تبدیلیوں کا جائزہ لیں |
| `/code-review-graph:review-pr` | بلاسٹ ریڈیئس تجزیہ کے ساتھ مکمل PR جائزہ |

</details>

<details>
<summary><strong>CLI reference</strong></summary>
<br>

```bash
code-review-graph install          # Auto-detect and configure all platforms
code-review-graph install --platform <name>  # Target a specific platform
code-review-graph uninstall --dry-run  # Preview safe removal of installed artifacts
code-review-graph build            # Parse entire codebase
code-review-graph update           # Incremental update (changed files only)
code-review-graph status           # Graph statistics
code-review-graph watch            # Auto-update on file changes
code-review-graph visualize        # Generate interactive HTML graph
code-review-graph visualize --format json      # Export local graph data as JSON
code-review-graph visualize --format graphml   # Export as GraphML
code-review-graph visualize --format svg       # Export as SVG
code-review-graph visualize --format obsidian  # Export as Obsidian vault
code-review-graph visualize --format cypher    # Export as Neo4j Cypher
code-review-graph wiki             # Generate markdown wiki from communities
code-review-graph detect-changes --brief         # Risk panel + token savings (read-only)
code-review-graph update --brief                 # Refresh graph + same panel
code-review-graph detect-changes --brief --verify  # Cross-check vs tiktoken
code-review-graph register <path>  # Register repo in multi-repo registry
code-review-graph unregister <id>  # Remove repo from registry
code-review-graph repos            # List registered repositories
code-review-graph daemon start     # Start multi-repo watch daemon
code-review-graph daemon stop      # Stop the daemon
code-review-graph daemon status    # Show daemon status and repos
code-review-graph eval             # Run evaluation benchmarks
code-review-graph serve            # Start MCP server
```

JSON exports مقامی graph data directory کے اندر رہتی ہیں، جسے Git بطورِ ڈیفالٹ ignore کرتا ہے۔ ان میں absolute paths اور code-structure metadata ہو سکتا ہے، اس لیے اپنی machine سے باہر publish کرنے سے پہلے export کا جائزہ لیں اور حساس معلومات صاف کریں۔

</details>

<details>
<summary><strong>Token Savings پینل: <code>detect-changes --brief</code> vs <code>update --brief</code></strong></summary>
<br>

دونوں کمانڈز ایک ہی کمپیکٹ پینل print کرتے ہیں جو دکھاتا ہے کہ گراف نے آپ کو بدلتی ہوئی فائلوں کو خام ایجنٹ کے حوالے کرنے کے مقابلے میں کتنے ٹوکن بچائے۔ ان میں ایک ہی فرق ہے: گراف کو پہلے تازہ کیا جاتا ہے یا نہیں۔

```text
┌─────────────────────── Token Savings ────────────────────────┐
│ Full context would be:     12,921 tokens                     │
│ Graph context used:           762 tokens                     │
│ Saved:                     12,159 tokens (~94%)              │
│ Breakdown: Functions 244 · Tests 191 · Risk 244 · Other 83   │
└──────────────────────────────────────────────────────────────┘
```

| کمانڈ | یہ کیا کرتا ہے | کب استعمال کریں |
|---|---|---|
| `detect-changes --brief` | **صرف پڑھنے کے لیے۔** آپ کی موجودہ تبدیلیوں کو دیکھتا ہے، موجودہ گراف سے سوال کرتا ہے، پینل print کرتا ہے۔ تقریباً 1 سیکنڈ۔ | زیادہ تر اوقات — ہکس (یا `crg-daemon`) پس منظر میں گراف تازہ رکھتے ہیں، اس لیے یہ کافی ہے۔ |
| `update --brief` | **پہلے اپنی بدلتی ہوئی فائلوں کو گراف میں دوبارہ پارس کرتا ہے**، پھر وہی پینل print کرتا ہے۔ تقریباً 5 سیکنڈ۔ | rebase کے بعد، بڑے change set کے بعد، یا جب آپ کو شبہ ہو کہ گراف پرانا ہے۔ |

دونوں کے آخر میں **ایک ہی پینل** آتا ہے کیونکہ دونوں آخر میں ایک ہی `analyze_changes()` step کو call کرتے ہیں۔ فرق یہ ہے کہ اس تجزیہ سے پہلے گراف خود تازہ کیا گیا تھا یا نہیں۔

ان میں سے کسی ایک کمانڈ میں `--verify` شامل کریں تاکہ دکھائے جانے والے اعداد و شمار کو OpenAI کے `cl100k_base` tokenizer سے موازنہ کیا جا سکے (GPT-4 خاندان). اس کے لیے `pip install tiktoken` کی ضرورت ہے۔ عام change set پر تخمینہ حقیقی ٹوکن سے تقریباً 1% کے اندر رہتا ہے — calibration data کے لیے [docs/REPRODUCING.md](docs/REPRODUCING.md) دیکھیں۔

ایک ہی `context_savings` metadata خود بخود `get_impact_radius`، `get_review_context`، `detect_changes`، اور `get_architecture_overview` MCP tools کے JSON responses میں بھی منسلک ہوتی ہے، تاکہ AI agents chat میں لوگوں کو ٹوکن کی بچت دکھا سکیں بغیر کسی اضافی prompting کے۔

</details>

<details>
<summary><strong>ملٹی-ریپو daemon</strong></summary>
<br>

اگر آپ کے ایڈیٹر میں ہکس کی سپورٹ نہیں ہے (مثلاً Cursor، OpenCode)، یا آپ بس چاہتے ہیں کہ گراف پس منظر میں تازہ رہے بغیر کسی ایڈیٹر انٹیگریشن کے، تو daemon مناسب ہے۔ یہ آپ کے ریپوزٹری پر فائلوں کی تبدیلیوں کو دیکھتا ہے اور خودکار طور پر گراف دوبارہ تعمیر کرتا ہے — `build` یا `update` کی دستی کمانڈز کی ضرورت نہیں۔

یہ daemon `code-review-graph` کے ساتھ شامل ہے — الگ سے انسٹال کی ضرورت نہیں۔

**فوری سیٹ اپ:**

```bash
# 1. Register the repos you want to watch
crg-daemon add ~/project-a --alias proj-a
crg-daemon add ~/project-b

# 2. Start the daemon (runs in the background)
crg-daemon start

# 3. That's it — graphs stay up to date automatically
crg-daemon status                 # check daemon and per-repo watcher status
crg-daemon logs --repo proj-a -f  # tail logs for a specific repo
crg-daemon stop                   # stop daemon and all watcher processes
```

یہ `code-review-graph daemon start|stop|status|...` کے طور پر بھی دستیاب ہے۔

داخلے میں، `crg-daemon add` `~/.code-review-graph/watch.toml` نامی TOML config فائل میں لکھتا ہے۔ آپ اس فائل کو براہِ راست بھی ایڈٹ کر سکتے ہیں:

```toml
[[repos]]
path = "/home/user/project-a"
alias = "proj-a"

[[repos]]
path = "/home/user/project-b"
alias = "project-b"
```

Daemon یہ config فائل میں تبدیلیوں کو دیکھتا ہے اور جب ریپو شامل یا حذف کیے جاتے ہیں تو watcher processes خودکار طور پر شروع/بند کرتا ہے۔ ہر 30 سیکنڈ بعد health checks کے ذریعے مردہ watcher دوبارہ شروع ہو جاتے ہیں۔ بیرونی dependencies کی ضرورت نہیں۔

مکمل config reference اور تمام دستیاب آپشنز کے لیے [docs/COMMANDS.md](docs/COMMANDS.md#standalone-daemon-cli-crg-daemon) دیکھیں۔

</details>

<details>
<summary><strong>30 MCP tools</strong></summary>
<br>

آپ کا AI معاون گراف بنانے کے بعد خودکار طور پر یہ استعمال کرتا ہے۔

| Tool | تفصیل |
|------|-------------|
| `build_or_update_graph_tool` | گراف بنائیں یا incremental طور پر اپ ڈیٹ کریں |
| `run_postprocess_tool` | flow detection، community detection، اور FTS indexing دوبارہ چلائیں |
| `get_minimal_context_tool` | انتہائی کمپیکٹ سیاق و سباق (~100 ٹوکن) — پہلے یہی call کریں |
| `get_impact_radius_tool` | بدلتی ہوئی فائلوں کا بلاسٹ ریڈیئس |
| `get_review_context_tool` | ساختی خلاصہ کے ساتھ ٹوکن-آفٹمائزڈ review context |
| `query_graph_tool` | callers، callees، tests، imports، inheritance queries |
| `traverse_graph_tool` | کسی بھی node سے BFS/DFS traversal، token budget کے ساتھ |
| `semantic_search_nodes_tool` | code entities کو نام یا معنی سے تلاش کریں |
| `embed_graph_tool` | semantic search کے لیے vector embeddings حساب کریں |
| `list_graph_stats_tool` | گراف کا سائز اور صحت |
| `get_docs_section_tool` | دستاویزی حصے حاصل کریں |
| `find_large_functions_tool` | line-count threshold سے زیادہ فنکشنز/کلاسز تلاش کریں |
| `list_flows_tool` | execution flows کو criticality کے مطابق فہرست بنائیں |
| `get_flow_tool` | ایک execution flow کی تفصیل حاصل کریں |
| `get_affected_flows_tool` | بدلتی ہوئی فائلوں سے متاثرہ flows تلاش کریں |
| `list_communities_tool` | پتہ لگائی گئی code communities کی فہرست |
| `get_community_tool` | ایک community کی تفصیل حاصل کریں |
| `get_architecture_overview_tool` | community structure سے architecture overview |
| `detect_changes_tool` | code review کے لیے رسک-اسکور شدہ change impact analysis |
| `get_hub_nodes_tool` | زیادہ سے زیادہ جڑے ہوئے nodes تلاش کریں (معماریاتی hotspots) |
| `get_bridge_nodes_tool` | betweenness centrality کے ذریعے chokepoints تلاش کریں |
| `get_knowledge_gaps_tool` | ساختی کمزوریاں اور غیر ٹیسٹ شدہ hotspots کی شناخت کریں |
| `get_surprising_connections_tool` | غیر متوقع cross-community coupling کا پتہ لگائیں |
| `get_suggested_questions_tool` | تجزیہ سے خودکار review questions |
| `refactor_tool` | Rename preview، dead code detection، suggestions |
| `apply_refactor_tool` | پہلے سے پیش کردہ refactoring لاگو کریں |
| `generate_wiki_tool` | communities سے markdown wiki بنائیں |
| `get_wiki_page_tool` | ایک خاص wiki page حاصل کریں |
| `list_repos_tool` | رجسٹرڈ ریپوزٹری کی فہرست |
| `cross_repo_search_tool` | تمام رجسٹرڈ ریپوزٹری میں تلاش کریں |

**MCP پرامپٹس** (5 workflow templates):
`review_changes`، `architecture_map`، `debug_issue`، `onboard_developer`، `pre_merge_check`

</details>

<details>
<summary><strong>ترتیب</strong></summary>
<br>

انڈیکس سے باہر رکھنے کے لیے اپنی ریپو کی جڑ میں ایک `.code-review-graphignore` فائل بنائیں:

```
generated/**
*.generated.ts
vendor/**
node_modules/**
```

نوٹ: git ریپو میں صرف tracked فائلوں کو انڈیکس کیا جاتا ہے (`git ls-files`)، اس لیے gitignored فائلیں خودکار طور پر چھوڑ دی جاتی ہیں۔ `.code-review-graphignore` کا استعمال tracked فائلوں کو خارج کرنے کے لیے یا پھر جب git دستیاب نہ ہو۔

اختیاری dependency groups:

```bash
pip install "code-review-graph[embeddings]"          # Local vector embeddings (sentence-transformers)
pip install "code-review-graph[google-embeddings]"   # Google Gemini embeddings
pip install "code-review-graph[communities]"         # Community detection (igraph)
pip install "code-review-graph[enrichment]"          # Python call-resolution enrichment (Jedi)
pip install "code-review-graph[eval]"                # Evaluation benchmarks (matplotlib)
pip install "code-review-graph[wiki]"                # Wiki generation with LLM summaries (ollama)
pip install "code-review-graph[all]"                 # All optional dependencies
```

### محیطی متغیرات

| متغیر | تفصیل | ڈیفالٹ |
|----------|-------------|---------|
| `CRG_GIT_TIMEOUT` | Git operations کے لیے timeout سیکنڈوں میں | `30` |
| `CRG_DATA_DIR` | گراف databases اور تیار کردہ graph artefacts کے لیے override directory | - |
| `CRG_EMBEDDING_MODEL` | vector embeddings کے لیے ڈیفالٹ model | `all-MiniLM-L6-v2` |
| `CRG_ACCEPT_CLOUD_EMBEDDINGS` | واضح قبولیت کے بعد cloud embedding egress warning خاموش کر دیں | - |
| `CRG_ALLOW_REMOTE_CODE` | HuggingFace models کی اجازت دیں جن کے لیے `trust_remote_code=True` ضروری ہو | `0` |
| `CRG_MAX_IMPACT_NODES` | impact analysis میں شامل کیے جانے والے زیادہ سے زیادہ nodes | `500` |
| `CRG_MAX_IMPACT_DEPTH` | بلاسٹ ریڈیئس تجزیہ کے لیے search depth | `2` |
| `CRG_MAX_BFS_DEPTH` | گراف traversal کے لیے زیادہ سے زیادہ depth | `15` |
| `CRG_MAX_CHANGED_FUNCS` | ایک change report میں تجزیہ کیے جانے والے زیادہ سے زیادہ changed functions | `500` |
| `CRG_MAX_TRANSITIVE_FRONTIER` | transitive caller/callee expansion کے لیے زیادہ سے زیادہ frontier size | `50` |
| `CRG_TOOL_TIMEOUT` | bounded MCP tools کے لیے اختیاری timeout سیکنڈوں میں (`0` disables timeout) | `0` |
| `CRG_RECURSE_SUBMODULES` | اگر `1`، `true`، یا `yes` set کیا جائے تو git submodules بھی شامل کریں | - |
| `CRG_TOOLS` | serving کے وقت expose کیے جانے والے MCP tools کی comma-separated allowlist | - |
| `GOOGLE_API_KEY` | Google Gemini embeddings کے لیے API key | - |
| `MINIMAX_API_KEY` | MiniMax embeddings کے لیے API key | - |
| `CRG_OPENAI_BASE_URL` | OpenAI-compatible embeddings endpoint | - |
| `CRG_OPENAI_API_KEY` | OpenAI-compatible embeddings کے لیے API key | - |
| `CRG_OPENAI_MODEL` | OpenAI-compatible embeddings کے لیے model name | - |
| `CRG_OPENAI_DIMENSION` | embedding dimension pin کریں (v3 models support reduction) | - |
| `NO_COLOR` | اگر set کیا جائے تو terminal میں ANSI colors غیر فعال ہو جائیں | - |
| `CRG_SERIAL_PARSE` | اگر `1` ہو تو parallel parsing غیر فعال کر دیں (debugging کے لیے) | - |

OpenAI-compatible embeddings (اصل OpenAI، Azure، یا کوئی self-hosted gateway جیسے
new-api / LiteLLM / vLLM / LocalAI / Ollama openai mode میں) کے لیے اضافی انسٹال کی ضرورت نہیں — بس محیطی متغیرات set کریں اور `embed_graph` میں `provider="openai"` پاس کریں:

```bash
export CRG_OPENAI_BASE_URL=http://127.0.0.1:3000/v1     # or https://api.openai.com/v1
export CRG_OPENAI_API_KEY=sk-...
export CRG_OPENAI_MODEL=text-embedding-3-small          # whatever your gateway serves
# optional:
export CRG_OPENAI_DIMENSION=1536                        # pin dim (v3 models support reduction)
export CRG_OPENAI_BATCH_SIZE=100                        # lower for gateways with tight limits
                                                        # (e.g. Qwen text-embedding-v4 caps at 10)
```

Cloud-egress warning خودکار طور پر نظر انداز کر دی جاتی ہے جب base URL localhost
(`127.0.0.1`، `localhost`، `0.0.0.0`، `::1`) کی طرف ہو۔

> **ماڈل انتخاب کی مشورہ:** `-preview` / `-beta` / `-exp` model IDs سے بچیں
> (مثلاً `google/gemini-embedding-2-preview`) اگر آپ انہیں طویل مدتی کے لیے استعمال کرنے کا ارادہ رکھتے ہیں — preview models کے weights بدل سکتے ہیں (different dimension → full re-embed ضروری) یا بغیر اطلاع deprecate ہو سکتے ہیں۔ مستحکم GA releases جیسے `text-embedding-3-small` / `text-embedding-3-large` (OpenAI)، `Qwen/Qwen3-Embedding-8B` (self-hosted vLLM / LocalAI کے ذریعے)، یا `gemini-embedding-001` (اصل Gemini provider کے ذریعے، جس کے لیے `GOOGLE_API_KEY` درکار ہے OpenAI-compatible path کے بجائے) ترجیح دیں۔
>
> نیز یاد رکھیں: `code-review-graph` فی الحال **function signatures** ہی embed کرتا ہے
> (~10 ٹوکن per node، مثال کے طور پر `"parse_file function (path: str) returns Tree"`)۔ ایسے models جن کی بنیادی معیاریت long-context body understanding سے آتی ہے (مثلاً Gemini 2 یا Qwen3-8B اپنے MTEB-code SOTA scores پر) وہ اس input length پر چھوٹے models کے مقابلے میں بہت زیادہ فرق نہیں دکھائیں گے۔ Body/docstring embedding کو follow-up enhancement کے طور پر زیرِ نظر رکھا گیا ہے۔

#### ٹول فلٹرنگ

CRG پہلے سے ہی 30 MCP tools expose کرتا ہے۔ token-constrained environments میں، آپ server کو tools کے ایک ذیلی سیٹ تک محدود کر سکتے ہیں `--tools` یا `CRG_TOOLS` environment variable کے ذریعے:

```bash
# Via CLI flag
code-review-graph serve --tools query_graph_tool,semantic_search_nodes_tool,detect_changes_tool

# Via environment variable
CRG_TOOLS=query_graph_tool,semantic_search_nodes_tool code-review-graph serve
```

CLI flag environment variable پر مقدمہ رکھتا ہے۔ جب دونوں set نہ ہوں، تو تمام tools دستیاب ہوں گے۔ یہ MCP client configurations کے لیے خاص طور پر مفید ہے:

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

## FAQ اور یہ کس چیز سے مختلف ہے

[docs/FAQ.md](docs/FAQ.md) میں مختصر، ایماندار جواب:

- [vs LSP / language servers](docs/FAQ.md#how-is-this-different-from-lsp-and-language-servers) — per-language daemons کے بجائے ایک مستقل cross-language graph؛ LSP ہر symbol کے لیے زیادہ درست رہتا ہے۔
- [vs RAG / embeddings](docs/FAQ.md#isnt-this-just-rag) — AST سے نکالی گئی ساختی edges، similarity chunks نہیں؛ embeddings اختیاری ہیں اور صرف search میں مدد دیتے ہیں۔
- [vs grep / agentic search](docs/FAQ.md#why-not-just-grep) — grep one-hop lookups پر بہتر ہے؛ graph multi-hop سوالات (impact radius، callers-of-callers، tests-for، affected flows) میں بہتر ہے۔
- [vs Serena, codegraph, claude-context, repomix](docs/FAQ.md#how-does-it-compare-to-serena-codegraph-claude-context-and-repomix) — حقیقت پر مبنی موازنہ جدول۔
- [When NOT to use it](docs/FAQ.md#when-should-i-not-use-it) — چھوٹے ریپو، trivial single-file diffs، one-off questions۔
- [Does it phone home?](docs/FAQ.md#does-it-phone-home) — نہیں؛ صفر telemetry، cloud embeddings اختیاری ہیں۔
- [How do I verify it is working?](docs/FAQ.md#how-do-i-verify-it-is-working) — `status`، `detect-changes --brief`، `/mcp`۔

## مشکلات کا حل

### `pip` / `pipx` `hatchling` ڈاؤن لوڈ نہیں کر سکتے (یا PyPI پر `Errno 9` / `Bad file descriptor`)

**source tree** سے انسٹال کرنے پر (مثلاً `pipx install .`) کو **PyPI** سے build dependencies درکار ہوتی ہیں (مثلاً `hatchling`)۔ اگر آپ کو connection warnings کے بعد `Could not find a version that satisfies the requirement hatchling` نظر آئے، تو اس **ٹرمینل** میں استعمال ہونے والا Python/pip HTTPS client سے `pypi.org` کھول نہیں سکتا (بعض اوقات integrated editor terminal میں دیکھا گیا ہے؛ کم اکثر VPN، firewall، یا proxy سے بھی)۔

**اختیارات:**

1. وہی کمانڈ **macOS Terminal.app** (یا iTerm) سے چلائیں، اس کے بعد `pipx install .` یا `pipx install "git+https://..."` دوبارہ کوشش کریں۔
2. **[uv](https://docs.astral.sh/uv/)** سے CLI انسٹال کریں (بہت سے معاملات میں `pip` سے مختلف ڈاؤن لوڈ machinery استعمال کرتا ہے):

   ```bash
   cd /path/to/code-review-graph
   uv tool install . --force
   ```

3. **clone** میں development کے لیے، بغیر global install کے، `uv sync` اور `uv run code-review-graph …` استعمال کریں (یا `uv sync` کے بعد `.venv` چالو کریں)۔

**تشخیص (اختیاری):** `python3 scripts/diagnose_pypi_connectivity.py` — اگر یہ `FAILED` پرنٹ کرے، تو مسئلہ پیکیج نام کی بجائے environment/network ہے۔

### Windows ترتیب کے مسائل (Invalid JSON / Connection Closed)
اگر آپ Windows استعمال کر رہے ہیں اور Claude Code کے ذریعے رابطے کے وقت `Invalid JSON: EOF while parsing` یا `MCP error -32000: Connection closed` کا سامنا ہو، تو اپنی config میں `cmd /c` wrapper استعمال نہ کریں۔

یقینی بنائیں کہ `fastmcp` کم از کم `3.2.4+` پر اپ ڈیٹ ہے۔ پھر اپنے `~/.claude.json` میں config دے کر `.exe` کو براہِ راست چلائیں اور UTF-8 environment variable بھی شامل کریں:

```json
"code-review-graph": {
  "command": "C:\\path\\to\\your\\venv\\Scripts\\code-review-graph.exe",
  "args": ["serve", "--repo", "C:\\path\\to\\your\\project"],
  "env": { "PYTHONUTF8": "1" }
}
```

## شراکت

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

<details>
<summary><strong>نئی زبان شامل کرنا</strong></summary>
<br>

`code_review_graph/parser.py` میں ترمیم کریں اور `EXTENSION_TO_LANGUAGE` میں اپنی ایکسٹینشن شامل کریں، ساتھ ہی `_CLASS_TYPES`، `_FUNCTION_TYPES`، `_IMPORT_TYPES`، اور `_CALL_TYPES` میں node type mappings۔ ایک test fixture شامل کریں اور PR بھیجیں۔

</details>

## لائسنس

MIT۔ [LICENSE](LICENSE) دیکھیں۔

<p align="center">
<br>
<a href="https://code-review-graph.com">code-review-graph.com</a><br><br>
<code>pip install code-review-graph && code-review-graph install</code><br>
<sub>Codex، Claude Code، CodeBuddy Code، Cursor، Windsurf، Zed، Continue، OpenCode، Antigravity، Gemini CLI، Qwen، Qoder، Kiro، GitHub Copilot، اور GitHub Copilot CLI کے ساتھ کام کرتا ہے</sub>
</p>
