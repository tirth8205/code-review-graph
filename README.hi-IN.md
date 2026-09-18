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
  <strong>एक स्थानीय कोड नॉलेज ग्राफ़, जो MCP के ज़रिए AI कोडिंग टूल्स को सटीक रिव्यू संदर्भ देता है।</strong>
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
  <a href="docs/USAGE.md">उपयोग</a> ·
  <a href="docs/COMMANDS.md">कमांड</a> ·
  <a href="docs/FAQ.md">सामान्य प्रश्न</a> ·
  <a href="docs/TROUBLESHOOTING.md">समस्या निवारण</a> ·
  <a href="docs/GITHUB_ACTION.md">GitHub Action</a> ·
  <a href="docs/REPRODUCING.md">बेंचमार्क दोहराना</a> ·
  <a href="docs/ROADMAP.md">रोडमैप</a>
</p>

<br>

एक बदलाव का रिव्यू करने के लिए AI कोडिंग टूल अक्सर कोडबेस का बड़ा हिस्सा दोबारा पढ़ते हैं। `code-review-graph` [Tree-sitter](https://tree-sitter.github.io/tree-sitter/) से कोड का संरचनात्मक नक्शा बनाता है, उसे इंक्रीमेंटल तरीके से अद्यतन रखता है, और [MCP](https://modelcontextprotocol.io/) के ज़रिए छोटा संदर्भ देता है, ताकि असिस्टेंट सिर्फ़ वही फ़ाइलें पढ़े जिन्हें बदलाव छूता है।

<p align="center">
  <img src="diagrams/diagram1_before_vs_after.png" alt="टोकन की समस्या: flask का पूरा कॉर्पस पढ़ने पर 143,594 टोकन लगते हैं, ग्राफ़ का उत्तर 2,196 टोकन लेता है (65 गुना कम)" width="85%" />
</p>

---

## जल्दी शुरुआत

```bash
pip install code-review-graph          # or: pipx install code-review-graph
code-review-graph install              # detect installed AI coding tools and configure each one
code-review-graph build                # parse the codebase
```

`install` यह पहचानता है कि आपके पास कौन-कौन से AI कोडिंग टूल हैं, हर एक के लिए MCP सर्वर प्रविष्टि लिखता है, जहाँ प्लेटफ़ॉर्म समर्थन करता है वहाँ हुक और स्किल लगाता है, और प्लेटफ़ॉर्म की नियम फ़ाइल में ग्राफ़ संबंधी निर्देश जोड़ता है। Poetry या uv प्रोजेक्ट परिवेश में MCP प्रविष्टि `poetry run` या `uv run` का उपयोग करती है, PATH पर `uvx` होने पर `uvx code-review-graph serve` का, और बाकी स्थितियों में मौजूदा Python इंटरप्रेटर का। उसके बाद एडिटर या टूल को फिर से शुरू करें।

<p align="center">
  <img src="diagrams/diagram8_supported_platforms.png" alt="एक इंस्टॉल, हर प्लेटफ़ॉर्म: Codex, Claude Code, CodeBuddy Code, Cursor, Windsurf, Zed, Continue, OpenCode, Antigravity, Gemini CLI, Qwen, Qoder, Kiro, GitHub Copilot, GitHub Copilot CLI और Hermes Agent को पहचानता है" width="85%" />
</p>

सिर्फ़ एक प्लेटफ़ॉर्म सेट करने के लिए `--platform` के साथ `codex`, `claude-code`, `cursor`, `windsurf`, `zed`, `continue`, `opencode`, `antigravity`, `gemini-cli`, `qwen`, `kiro`, `qoder`, `copilot`, `copilot-cli`, `codebuddy` या `hermes` में से कोई एक दें:

```bash
code-review-graph install --platform cursor
code-review-graph install --platform codebuddy
```

कॉन्फ़िग फ़ाइलों के स्थान [docs/USAGE.md](docs/USAGE.md#supported-platforms) में सूचीबद्ध हैं। Python 3.10+ चाहिए।

`uninstall` किसी Git या SVN वर्किंग ट्री से CRG की अपनी फ़ाइलें और प्रविष्टियाँ हटाता है और दूसरे MCP सर्वर, हुक, स्किल तथा JSONC टिप्पणियाँ जैसी की तैसी छोड़ देता है। इसे ट्री के भीतर कहीं से भी चलाया जा सकता है। साझा कॉन्फ़िग फ़ाइलें एटॉमिक तरीके से बदली जाती हैं, इसलिए लिखने में विफलता होने पर मूल फ़ाइल सुरक्षित रहती है।

```bash
code-review-graph uninstall --dry-run    # preview only
code-review-graph uninstall              # preview, confirm, apply
code-review-graph uninstall --yes        # apply without prompting
code-review-graph uninstall --all-repos  # also clean every registered repository
code-review-graph uninstall --keep-data  # remove integrations, keep graph databases
code-review-graph uninstall --keep-user-configs --repo .  # this project only
```

फिर प्रोजेक्ट खोलें और असिस्टेंट से कहें:

```
Build the code review graph for this project
```

बिल्ड का समय रिपॉज़िटरी के आकार के साथ बढ़ता है; लगभग 3,000 फ़ाइलों वाली रिपॉज़िटरी के कोल्ड बिल्ड में लगभग 40 सेकंड लगे ([मापा गया](docs/REPRODUCING.md#incremental-update-latency))। उसके बाद हुक और watch मोड ग्राफ़ को अद्यतन रखते हैं। अगर कुछ फ़ाइलें पार्स नहीं हो पातीं तो परिणाम की स्थिति `partial` होती है और सारांश में उनके नाम आते हैं; CLI stderr पर एक `Warning:` पंक्ति भी छापता है, और उन फ़ाइलों की पुरानी ग्राफ़ पंक्तियाँ बनी रहती हैं।


## यह कैसे काम करता है

<p align="center">
  <img src="diagrams/diagram7_mcp_integration_flow.png" alt="असिस्टेंट ग्राफ़ का उपयोग कैसे करता है: उपयोगकर्ता रिव्यू माँगता है, असिस्टेंट MCP टूल बुलाता है, ग्राफ़ प्रभाव क्षेत्र और जोखिम स्कोर लौटाता है, असिस्टेंट सिर्फ़ प्रभावित फ़ाइलें पढ़ता है" width="80%" />
</p>

रिपॉज़िटरी को Tree-sitter से AST में पार्स किया जाता है और नोड (फ़ंक्शन, क्लास, इम्पोर्ट) तथा एज (कॉल, इनहेरिटेंस, टेस्ट कवरेज) के ग्राफ़ के रूप में संग्रहीत किया जाता है। रिव्यू के समय ग्राफ़ से वह सबसे छोटा फ़ाइल समूह पूछा जाता है जिसे असिस्टेंट को पढ़ना है।

<p align="center">
  <img src="diagrams/diagram2_architecture_pipeline.png" alt="आर्किटेक्चर पाइपलाइन: रिपॉज़िटरी → Tree-sitter पार्सर → SQLite ग्राफ़ → प्रभाव क्षेत्र → न्यूनतम रिव्यू समूह" width="100%" />
</p>

### प्रभाव क्षेत्र का विश्लेषण

जब कोई फ़ाइल बदलती है, ग्राफ़ हर उस कॉलर, आश्रित और टेस्ट का पता लगाता है जो प्रभावित हो सकता है। असिस्टेंट पूरे प्रोजेक्ट को खंगालने के बजाय वही फ़ाइलें पढ़ता है।

<p align="center">
  <img src="diagrams/diagram3_blast_radius.png" alt="प्रभाव क्षेत्र: login() में बदलाव कॉलर, आश्रितों और टेस्ट तक फैलता है" width="70%" />
</p>

### इंक्रीमेंटल अपडेट

हुक, pre-commit हुक और watch मोड इंक्रीमेंटल अपडेट चलाते हैं। अपडेट बदली हुई फ़ाइलों का diff लेता है, ग्राफ़ के इम्पोर्ट और कॉल एज से उनके आश्रित ढूँढता है, और सिर्फ़ उन्हीं फ़ाइलों को दोबारा पार्स करता है जिनका SHA-256 हैश बदला है। लगभग 3,000 फ़ाइलों वाले प्रोजेक्ट (django) में दो फ़ाइलों का संपादन हुक वाले रास्ते पर लगभग 2.5 सेकंड में दोबारा इंडेक्स होता है, जिसमें से लगभग 1.4 सेकंड प्रोसेस के शुरू होने का समय है; कुछ न बदलने पर सिर्फ़ वही समय लगता है। देखें [इंक्रीमेंटल अपडेट विलंब](docs/REPRODUCING.md#incremental-update-latency)।

<p align="center">
  <img src="diagrams/diagram4_incremental_update.png" alt="इंक्रीमेंटल अपडेट प्रवाह: हुक या watch अपडेट git diff चलाता है, ग्राफ़ के एज से आश्रित मिलते हैं, और सिर्फ़ वही फ़ाइलें दोबारा पार्स होती हैं जिनका SHA-256 हैश बदला है" width="90%" />
</p>

### पूरा कोडबेस, या लक्षित उत्तर?

पूरा कॉर्पस मॉडल को देने के बजाय ग्राफ़ प्रश्न के अनुसार काटा गया हिस्सा लौटाता है। इस रिपॉज़िटरी के `84bde354` पर 2026-08-02 के माप में 208,821 स्रोत टोकन प्रति प्रश्न लगभग 3,190 टोकन बन गए। उस स्नैपशॉट के बाद रिपॉज़िटरी काफ़ी बढ़ी है, इसलिए आज दोनों संख्याएँ बड़ी हैं।

<p align="center">
  <img src="diagrams/diagram6_monorepo_funnel.png" alt="code-review-graph का 84bde354 स्नैपशॉट: 208,821 स्रोत टोकन सिमटकर लगभग 3,190 टोकन के ग्राफ़ उत्तर बनते हैं, प्रति प्रश्न टोकन लगभग 65 गुना कम" width="80%" />
</p>

### भाषा कवरेज और नोटबुक

<p align="center">
  <img src="diagrams/diagram9_language_coverage.png" alt="श्रेणी के अनुसार भाषा कवरेज: वेब, बैकएंड, सिस्टम्स, मोबाइल, स्क्रिप्टिंग, शेल, डोमेन और अन्य, साथ में Jupyter और Databricks नोटबुक" width="90%" />
</p>

पार्सर फ़ंक्शन, क्लास, इम्पोर्ट, कॉल स्थल, इनहेरिटेंस और टेस्ट निकालता है; जहाँ व्याकरण मौजूद है वहाँ Tree-sitter का और बाकी जगह लक्षित फ़ॉलबैक का उपयोग करता है। समर्थित: Python, JavaScript/TypeScript/TSX, Go, Rust, Java, C/C++, C#, VB.NET, Ruby, Kotlin, Swift, PHP, Scala, Solidity, Dart, R, Perl, Lua/Luau, Objective-C, शेल स्क्रिप्ट, Elixir, Zig, PowerShell, Julia, ReScript, GDScript, Nix, Verilog/SystemVerilog, SQL, Terraform/OpenTofu (`.tf`; अन्य `.hcl` फ़ाइलें सिर्फ़ फ़ाइल नोड बनती हैं), Ansible YAML (playbook, role, task), Spring Boot ऐप्लिकेशन कॉन्फ़िग (`application.properties`, `application.yml`, `application.yaml` और इनके `application-<profile>` रूप; सिर्फ़ कुंजी नाम और मान के प्रकार, मान कभी नहीं), Vue/Svelte SFC, Astro फ़ाइलें (TypeScript व्याकरण से पार्स), Jupyter और Databricks नोटबुक (`.ipynb`), तथा Perl XS फ़ाइलें (`.xs`)। अन्य YAML और अन्य `.properties` फ़ाइलों को स्रोत कोड नहीं माना जाता।

PHP प्रोजेक्ट्स को रिपॉज़िटरी तक सीमित Composer PSR-4 समाधान, Blade टेम्पलेट संदर्भ, और जब स्रोत में स्पष्ट फ़्रेमवर्क इम्पोर्ट, मॉडल इनहेरिटेंस तथा रिसीवर का प्रमाण दिखता है तब Laravel Route और Eloquent एज भी मिलते हैं।

Java प्रोजेक्ट्स को Spring डिपेंडेंसी इंजेक्शन का कॉल समाधान, रिक्वेस्ट एंडपॉइंट तथा WebFlux रूट, शेड्यूल्ड ट्रिगर, ऐप्लिकेशन इवेंट के प्रकाशक से श्रोता तक के एज, और Temporal वर्कफ़्लो तथा ऐक्टिविटी एज मिलते हैं। हर रिज़ॉल्वर पार्स के बाद चलता है और उसे इंजेक्ट किया गया फ़ील्ड, प्रकाशित इवेंट या वर्कफ़्लो स्टब रिपॉज़िटरी में दिखाई देना चाहिए।

### अपनी भाषा जोड़ें

अगर आपकी रिपॉज़िटरी ऐसी भाषा इस्तेमाल करती है जिसे पार्सर नहीं समझता, तो `.code-review-graph/` में एक `languages.toml` जोड़ें जो फ़ाइल एक्सटेंशन को `tree_sitter_language_pack` में शामिल किसी भी व्याकरण से जोड़े, साथ में फ़ंक्शन, क्लास, इम्पोर्ट और कॉल के नोड प्रकार दे:

```toml
[languages.erlang]
extensions = [".erl"]
grammar = "erlang"
function_node_types = ["function_clause"]
class_node_types = ["record_decl"]
import_node_types = ["import_attribute"]
call_node_types = ["call"]
```

निकालने का काम सामान्य tree-sitter वॉकर करता है। अंतर्निहित भाषाओं को बदला नहीं जा सकता। स्कीमा, सत्यापन नियम और एक पूरा उदाहरण [docs/CUSTOM_LANGUAGES.md](docs/CUSTOM_LANGUAGES.md) में देखें।

### CI में जोखिम-स्कोर वाले PR रिव्यू (GitHub Action)

यही विश्लेषण एक composite GitHub Action के रूप में भी चलता है। ग्राफ़ आपके CI रनर पर ही बनता और पूछा जाता है; कोई स्रोत कोड किसी बाहरी सेवा को नहीं भेजा जाता। हर पुल रिक्वेस्ट पर यह ऐक्शन एक स्थायी टिप्पणी डालता है जिसमें जोखिम-स्कोर वाले फ़ंक्शन, प्रभावित निष्पादन प्रवाह और टेस्ट की कमियाँ होती हैं, और हर पुश पर वहीं अपडेट हो जाती है। वैकल्पिक `fail-on-risk` इनपुट इसे मर्ज गेट बना देता है।

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

इनपुट, जोखिम स्तर और कैशिंग के लिए [docs/GITHUB_ACTION.md](docs/GITHUB_ACTION.md) देखें, या यह रिपॉज़िटरी खुद पर जो वर्कफ़्लो चलाती है वह [`.github/workflows/pr-review.yml`](.github/workflows/pr-review.yml) में देखें।

---

## बेंचमार्क

<p align="center">
  <img src="diagrams/diagram5_benchmark_board.png" alt="6 रिपॉज़िटरी पर बेंचमार्क: प्रति प्रश्न टोकन कमी का माध्यिका लगभग 63 गुना (अधिकतम 358 गुना), ग्राफ़ से निकाले गए सही उत्तर के मुकाबले औसत प्रभाव F1 0.69" width="85%" />
</p>

6 रिपॉज़िटरी में प्रति प्रश्न टोकन कमी का माध्यिका लगभग **63 गुना** है (पूरे कॉर्पस की आधाररेखा बनाम ग्राफ़ क्वेरी)। **358 गुना** का अधिकतम मान एक ही रिपॉज़िटरी (fastapi, सबसे बड़ा कॉर्पस) का है, यह सामान्य परिणाम नहीं है।

सारी संख्याएँ 6 ओपन-सोर्स रिपॉज़िटरी (13 कमिट) पर चले मूल्यांकन रनर से आती हैं। हर कॉन्फ़िग अपस्ट्रीम SHA को पिन करता है, Leiden तय सीड पर चलता है, और एम्बेडिंग CPU पर नियतात्मक हैं, इसलिए अलग-अलग मशीनों पर दो बार चलाने पर वही संख्याएँ मिलती हैं। दोहराने की विधि [`docs/REPRODUCING.md`](docs/REPRODUCING.md) में है। दो सबसे छोटे कॉन्फ़िग पर साप्ताहिक, केवल-रिपोर्ट वाला रन [`.github/workflows/eval.yml`](.github/workflows/eval.yml) में है।

<details>
<summary><strong>टोकन दक्षता: प्रति प्रश्न कमी का माध्यिका लगभग 63 गुना (दायरा 35 गुना से 358 गुना; पूरा कॉर्पस बनाम ग्राफ़ क्वेरी)</strong></summary>
<br>

एक सामान्य एजेंट प्रश्न (`"how does authentication work"`, `"what is the main entry point"` वगैरह) पर ग्राफ़ हर स्रोत फ़ाइल के बजाय लगभग 2,200 से 3,900 टोकन के खोज परिणाम और पड़ोसी एज लौटाता है। तालिका `code_review_graph/token_benchmark.py` में परिभाषित 5 नमूना प्रश्नों का औसत है।

| रिपॉज़िटरी | स्नैपशॉट SHA | naive_corpus_tokens | avg graph_tokens | कमी |
|------|---|-----------------:|----------------:|----------:|
| fastapi | `22381558` | 948,793 | 2,653 | **357.6x** |
| flask | `a29f88ce` | 143,594 | 2,196 | **65.4x** |
| code-review-graph | `84bde354` | 208,821 | 3,190 | **65.5x** |
| gin | `5c00df8a` | 166,868 | 2,766 | **60.3x** |
| httpx | `b55d4635` | 142,356 | 2,661 | **53.5x** |
| express | `b4ab7d65` | 136,052 | 3,936 | **34.6x** |

> 2026-08-02 को पिन किए गए SHA पर साफ़ क्लोन से लिया गया (crg 2.3.7, स्थानीय `all-MiniLM-L6-v2` एम्बेडिंग)। ये संख्याएँ उस 2026-05-25 के माप से कम हैं जिसकी ये जगह लेती हैं: नोड एम्बेडिंग टेक्स्ट अधिक समृद्ध हो गया, इसलिए हर रिपॉज़िटरी में `avg graph_tokens` बढ़ा। fastapi को हटाए गए `0227991a` के बजाय उसके मौजूदा पिन `22381558` पर मापा गया है।
>
> कमी वाला स्तंभ `naive_corpus_tokens / avg graph_tokens` है, इसलिए वह बगल के दो स्तंभों से भाग देकर निकल आता है। बेंचमार्क का अपना `average_reduction_ratio` इसके बजाय प्रति प्रश्न अनुपातों का औसत लेता है, जो हमेशा बड़ा निकलता है; प्रति प्रश्न आँकड़े [`docs/REPRODUCING.md`](docs/REPRODUCING.md#standalone-token-benchmark-code_review_graphtoken_benchmarkpy) में हैं।
>
> `code-review-graph` वाली पंक्ति एक स्नैपशॉट है, आज का माप नहीं। `84bde354` के बाद रिपॉज़िटरी बढ़ी है, इसलिए उसका कॉर्पस और ग्राफ़ दोनों आज कहीं बड़े हैं।

पूरे कॉर्पस वाली आधाररेखा एक ऊपरी सीमा है जो कोई असली एजेंट नहीं चुकाता; एजेंट पहचानकर्ताओं को grep करता है और सबसे मेल खाती फ़ाइलें पढ़ता है। `agent_baseline` मूल्यांकन बेंचमार्क उसी स्थिति को मापता है (शुद्ध Python से कॉर्पस पर grep, मिलान संख्या के हिसाब से शीर्ष 3 फ़ाइलें, और ग्राफ़ क्वेरी की लागत से टोकन तुलना)। यह `evaluate/results/<repo>_agent_baseline_<date>.csv` लिखता है; अभी तक कोई आधिकारिक माप प्रकाशित नहीं हुआ है।

औपचारिक `token_efficiency` बेंचमार्क एक अलग स्थिति मापता है: किसी कमिट की सिर्फ़ बदली हुई फ़ाइलों की सामग्री के मुकाबले पूरा `get_review_context()` JSON, और छोटे कमिट पर यह 1 से कम अनुपात बताता है क्योंकि उत्तर में प्रभाव क्षेत्र के एज और स्रोत के अंश होते हैं। दोनों बेंचमार्क अलग सवालों के जवाब देते हैं; देखें [`docs/REPRODUCING.md`](docs/REPRODUCING.md#which-benchmark-measures-what)।

रिव्यू और प्रभाव वाले टूल अपने उत्तरों के साथ एक छोटा `context_savings` अनुमान जोड़ते हैं। CLI वही आँकड़े `Token Savings` पैनल में दिखाता है (नीचे उपयोग देखें) और `--verify` उनकी तुलना OpenAI के `cl100k_base` टोकनाइज़र से करता है। 222 नमूना फ़ाइलों पर किए गए अंशांकन के अनुसार कुल मिलाकर यह अनुमान असली टोकन से लगभग 1% के भीतर रहता है ([डेटा](docs/REPRODUCING.md#calibration-table))।

</details>

<details>
<summary><strong>प्रभाव सटीकता: ग्राफ़ से निकाले गए सही उत्तर के मुकाबले औसत F1 0.69 (रिकॉल 1.0 एक चक्रीय ऊपरी सीमा है)</strong></summary>
<br>

प्रभाव क्षेत्र का विश्लेषण मूल्यांकन के सभी 13 कमिट पर सही उत्तर की हर फ़ाइल पकड़ लेता है। इसे "100% रिकॉल" नहीं, ऊपरी सीमा मानकर पढ़ें: सही उत्तर (बदली हुई फ़ाइलें और वे फ़ाइलें जिनके कॉल या इम्पोर्ट एज उनकी ओर जाते हैं) उसी ग्राफ़ से आता है जिस पर भविष्यवक्ता चलता है। कम प्रिसिज़न जानबूझकर है; एक अतिरिक्त फ़ाइल दिखाने की कीमत टूटी हुई निर्भरता छोड़ देने से कम है।

| रिपॉज़िटरी | कमिट | औसत F1 | औसत प्रिसिज़न | रिकॉल (ग्राफ़ से निकली ऊपरी सीमा) |
|------|--------:|-------:|--------------:|-------:|
| httpx | 2 | 0.863 | 0.785 | 1.0 |
| code-review-graph | 2 | 0.734 | 0.584 | 1.0 |
| fastapi | 2 | 0.697 | 0.539 | 1.0 |
| express | 2 | 0.667 | 0.500 | 1.0 |
| flask | 2 | 0.633 | 0.485 | 1.0 |
| gin | 3 | 0.609 | 0.439 | 1.0 |
| **औसत** | **13** | **0.693** | **0.546** | **1.000** |

यह बेंचमार्क एक **सह-परिवर्तन मोड** भी चलाता है: भविष्यवक्ता को एक बदली हुई फ़ाइल दी जाती है और उसी कमिट में लेखक द्वारा छुई गई बाकी फ़ाइलों के आधार पर अंक दिए जाते हैं, यानी प्रमाण ग्राफ़ से नहीं बल्कि git इतिहास से आता है। दोनों मोड परिणाम CSV में दिखते हैं (`ground_truth_mode` स्तंभ)। 2026-08-02 के माप में सह-परिवर्तन मोड ने हर अंकित कमिट पर `predicted_files = 0` लौटाया, इसलिए यह अभी उपयोगी माप नहीं है और कोई सह-परिवर्तन आँकड़ा उद्धृत नहीं किया गया है।

</details>

<details>
<summary><strong>बिल्ड आँकड़े</strong></summary>
<br>

ऊपर दिए पिन किए गए SHA पर उसी 2026-08-02 के साफ़ बिल्ड से। एम्बेडिंग की संख्या नोड की संख्या से कम है क्योंकि File नोड एम्बेड नहीं किए जाते। `code-review-graph` वाली पंक्ति वही स्नैपशॉट है, आज की रिपॉज़िटरी नहीं।

| रिपॉज़िटरी | नोड | एज | एम्बेडिंग |
|------|------:|------:|-----------:|
| fastapi | 6,287 | 32,036 | 5,159 |
| express | 1,990 | 19,492 | 1,849 |
| gin | 1,589 | 17,237 | 1,491 |
| code-review-graph | 1,446 | 9,094 | 1,354 |
| flask | 1,415 | 8,259 | 1,329 |
| httpx | 1,263 | 8,236 | 1,193 |

</details>

### सीमाएँ

- **प्रभाव का "रिकॉल 1.0" चक्रीय है।** ऐतिहासिक सही उत्तर उन्हीं ग्राफ़ एज से आता है जिन पर भविष्यवक्ता चलता है, इसलिए वह बनावट से ही एक ऊपरी सीमा है। सह-परिवर्तन मोड अभी उपयोगी माप नहीं है।
- **छोटे, एकल-फ़ाइल बदलाव।** मामूली संपादन में ग्राफ़ का संदर्भ सादी फ़ाइल पढ़ने से बड़ा हो सकता है। यह अतिरिक्त हिस्सा वही संरचनात्मक मेटाडेटा है जो बहु-फ़ाइल विश्लेषण संभव बनाता है।
- **खोज क्रम।** कीवर्ड खोज आमतौर पर सही परिणाम ऊपर ले आती है, पर क्रम तय करने में सुधार बाकी है। मॉड्यूल पैटर्न वाले नामकरण के कारण Express पर की गई क्वेरी बिना परिणाम के लौट सकती है।
- **प्रवाह पहचान।** प्रवेश बिंदु की पहचान Python और PHP/Laravel में सबसे मज़बूत है। JavaScript और Go की प्रवाह पहचान में सुधार बाकी है।
- **प्रिसिज़न बनाम रिकॉल।** प्रभाव विश्लेषण सतर्क है। यह उन फ़ाइलों को भी दिखाता है जो प्रभावित हो सकती हैं, यानी बड़े निर्भरता ग्राफ़ में झूठे संकेत आते हैं।

---

## विशेषताएँ

| विशेषता | विवरण |
|---------|---------|
| **इंक्रीमेंटल अपडेट** | सिर्फ़ उन्हीं फ़ाइलों को दोबारा पार्स करता है जिनका हैश बदला है। लगभग 3,000 फ़ाइलों वाली रिपॉज़िटरी में दो फ़ाइलों का संपादन हुक वाले रास्ते पर लगभग 2.5 सेकंड लेता है ([मापा गया](docs/REPRODUCING.md#incremental-update-latency))। |
| **भाषा और नोटबुक समर्थन** | ऊपर [भाषा कवरेज](#भाषा-कवरेज-और-नोटबुक) देखें। |
| **फ़्रेमवर्क-सचेत PHP पार्सिंग** | रिपॉज़िटरी तक सीमित Composer PSR-4 इम्पोर्ट, Blade टेम्पलेट संदर्भ, प्रमाण पर आधारित Laravel Route-से-कंट्रोलर और Eloquent संबंध एज |
| **फ़्रेमवर्क-सचेत Java पार्सिंग** | Spring डिपेंडेंसी इंजेक्शन का कॉल समाधान, रिक्वेस्ट एंडपॉइंट तथा WebFlux रूट, शेड्यूल्ड ट्रिगर, ऐप्लिकेशन इवेंट के प्रकाशक से श्रोता तक के एज, Temporal वर्कफ़्लो तथा ऐक्टिविटी एज, और मान के बिना इंडेक्स होने वाली Spring Boot कॉन्फ़िग कुंजियाँ |
| **प्रभाव क्षेत्र विश्लेषण** | किसी बदलाव से कौन-से फ़ंक्शन, क्लास और फ़ाइलें प्रभावित हो सकती हैं |
| **स्वतः अपडेट हुक** | एडिटर हुक, git pre-commit हुक और watch मोड काम करते-करते ग्राफ़ अद्यतन करते हैं |
| **सिमेंटिक खोज** | वैकल्पिक वेक्टर एम्बेडिंग: sentence-transformers, Google Gemini, MiniMax, Voyage AI, या कोई भी OpenAI-संगत एंडपॉइंट (OpenAI, Azure, new-api, LiteLLM, vLLM, LocalAI) |
| **इंटरैक्टिव विज़ुअलाइज़ेशन** | D3.js फ़ोर्स-डायरेक्टेड ग्राफ़ जिसमें खोज, कम्युनिटी लेजेंड टॉगल और डिग्री के अनुसार नोड आकार |
| **हब और ब्रिज पहचान** | सबसे अधिक जुड़े नोड और अड़चनें (बिटवीननेस सेंट्रैलिटी) |
| **आश्चर्य स्कोरिंग** | अप्रत्याशित युग्मन: कम्युनिटी-पार, भाषा-पार, परिधि से हब तक के एज |
| **ज्ञान अंतराल विश्लेषण** | अलग-थलग नोड, बिना टेस्ट वाले हॉटस्पॉट, पतली कम्युनिटी |
| **सुझाए गए प्रश्न** | ब्रिज, हब और आश्चर्यजनक युग्मनों से बने रिव्यू प्रश्न |
| **एज विश्वास स्तर** | दो-स्तरीय विश्वास (EXTRACTED/INFERRED) और एज पर फ़्लोट स्कोर |
| **ग्राफ़ ट्रैवर्सल** | किसी भी नोड से BFS/DFS, गहराई और टोकन बजट सेट किए जा सकते हैं |
| **निर्यात प्रारूप** | GraphML (Gephi/yEd), Neo4j Cypher, Obsidian वॉल्ट, JSON, और SVG (SVG के लिए `eval` एक्स्ट्रा का matplotlib चाहिए) |
| **टोकन बेंचमार्किंग** | `code_review_graph/token_benchmark.py` प्रति प्रश्न पूरे कॉर्पस के टोकन और ग्राफ़ क्वेरी के टोकन मापता है |
| **अनुमानित संदर्भ बचत** | रिव्यू, प्रभाव, detect-changes और आर्किटेक्चर उत्तरों पर `context_savings` मेटाडेटा (`estimated`, `saved_tokens`, `saved_percent`) |
| **कम्युनिटी स्वतः विभाजन** | ग्राफ़ के 25% से बड़ी कम्युनिटी को Leiden से पुनरावर्ती रूप से बाँटा जाता है |
| **निष्पादन प्रवाह** | प्रवेश बिंदुओं से कॉल श्रृंखलाएँ, भारित महत्ता के क्रम में |
| **कम्युनिटी पहचान** | Leiden क्लस्टरिंग, रिज़ॉल्यूशन ग्राफ़ के आकार के अनुसार |
| **आर्किटेक्चर अवलोकन** | कम्युनिटी संरचना पर आधारित आर्किटेक्चर नक्शा और युग्मन चेतावनियाँ |
| **जोखिम-स्कोर वाले रिव्यू** | `detect_changes` diff को प्रभावित फ़ंक्शन, प्रवाह और टेस्ट की कमियों से जोड़ता है |
| **कस्टम भाषाएँ** | `.code-review-graph/languages.toml` से नई भाषाएँ, fork की ज़रूरत नहीं |
| **GitHub Action** | CI में स्थायी, जोखिम-स्कोर वाली PR रिव्यू टिप्पणियाँ, वैकल्पिक `fail-on-risk` मर्ज गेट के साथ |
| **रीफ़ैक्टरिंग टूल** | नाम बदलने का पूर्वावलोकन, फ़्रेमवर्क-सचेत डेड कोड पहचान, कम्युनिटी आधारित सुझाव |
| **विकी निर्माण** | कम्युनिटी संरचना से Markdown विकी |
| **बहु-रिपॉज़िटरी रजिस्ट्री** | कई रिपॉज़िटरी दर्ज करें और उन सब में खोजें |
| **बहु-रिपॉज़िटरी डीमन** | `crg-daemon` कई रिपॉज़िटरी को चाइल्ड प्रोसेस के रूप में देखता है, हेल्थ चेक और पुनः आरंभ के साथ |
| **MCP प्रॉम्प्ट** | 5 वर्कफ़्लो टेम्पलेट: रिव्यू, आर्किटेक्चर, डिबग, ऑनबोर्ड, मर्ज-पूर्व जाँच |
| **पूर्ण-पाठ खोज** | कीवर्ड और वेक्टर समानता को जोड़ने वाली FTS5 हाइब्रिड खोज |
| **स्थानीय भंडारण** | `.code-review-graph/` में एक SQLite फ़ाइल; कोई बाहरी डेटाबेस या क्लाउड सेवा नहीं |

---

## उपयोग

<details>
<summary><strong>स्किल</strong></summary>
<br>

`install` इन चार स्किल को उन प्लेटफ़ॉर्म पर लिखता है जो इन्हें समर्थित करते हैं (Claude Code, Gemini CLI, CodeBuddy Code, Hermes Agent और Qoder)। इन्हें नाम से बुलाएँ।

| स्किल | विवरण |
|-------|-------------|
| `explore-codebase` | नॉलेज ग्राफ़ से कोडबेस की संरचना देखना और समझना |
| `review-changes` | बदलाव पहचान और प्रभाव विश्लेषण से संरचित कोड रिव्यू करना |
| `debug-issue` | ग्राफ़ आधारित कोड नेविगेशन से क्रमबद्ध तरीके से समस्या ढूँढना |
| `refactor-safely` | निर्भरता विश्लेषण से सुरक्षित रीफ़ैक्टरिंग की योजना बनाना और उसे लागू करना |

Qoder को इस रिपॉज़िटरी की `skills/` डायरेक्टरी से `build-graph`, `review-delta` और `review-pr` भी मिलते हैं।

</details>

<details>
<summary><strong>CLI संदर्भ</strong></summary>
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

यह एक चुनी हुई सूची है। `code-review-graph --help` हर कमांड दिखाता है, और [docs/COMMANDS.md](docs/COMMANDS.md) उनके फ़्लैग बताता है।

जब `detect-changes --base` किसी ब्रांच का नाम देता है, तो diff उस ब्रांच और HEAD के मर्ज बेस के मुकाबले चलता है। कमिट हैश और अन्य रिवीज़न जैसे दिए जाते हैं वैसे ही इस्तेमाल होते हैं।

`visualize --format svg` के लिए matplotlib चाहिए, जो `eval` एक्स्ट्रा में आता है (`pip install "code-review-graph[eval]"`)। बाकी निर्यात प्रारूपों के लिए कोई अतिरिक्त इंस्टॉल नहीं चाहिए।

JSON निर्यात स्थानीय ग्राफ़ डेटा डायरेक्टरी में लिखे जाते हैं, जिसे Git डिफ़ॉल्ट रूप से अनदेखा करता है। इनमें निरपेक्ष पथ और कोड संरचना का मेटाडेटा हो सकता है, इसलिए प्रकाशित करने से पहले निर्यात की जाँच करें।

</details>

<details>
<summary><strong>Token Savings पैनल: <code>detect-changes --brief</code> बनाम <code>update --brief</code></strong></summary>
<br>

दोनों कमांड एक ही पैनल छापते हैं, जो बताता है कि बदली हुई फ़ाइलें एजेंट को कच्ची देने की तुलना में ग्राफ़ ने कितने टोकन बचाए। इनमें फ़र्क़ सिर्फ़ एक है: ग्राफ़ पहले ताज़ा किया जाता है या नहीं।

```text
┌─────────────────────── Token Savings ────────────────────────┐
│ Full context would be:     12,921 tokens                     │
│ Graph context used:           762 tokens                     │
│ Saved:                     12,159 tokens (~94%)              │
│ Breakdown: Functions 244 · Tests 191 · Risk 244 · Other 83   │
└──────────────────────────────────────────────────────────────┘
```

| कमांड | यह क्या करता है | कब इस्तेमाल करें |
|---|---|---|
| `detect-changes --brief` | केवल पढ़ता है। मौजूदा बदलावों के लिए मौजूदा ग्राफ़ से पूछता है और पैनल छापता है। | ज़्यादातर मौकों पर; हुक या `crg-daemon` ग्राफ़ को ताज़ा रखते हैं। |
| `update --brief` | पहले बदली हुई फ़ाइलों को ग्राफ़ में दोबारा पार्स करता है, फिर वही पैनल छापता है। | रीबेस के बाद, बड़े बदलाव समूह पर, या जब ग्राफ़ पुराना हो सकता हो। |

किसी भी कमांड में `--verify` जोड़कर आँकड़ों की तुलना OpenAI के `cl100k_base` टोकनाइज़र से करें (`pip install tiktoken` चाहिए)। कुल मिलाकर अनुमान असली टोकन से लगभग 1% के भीतर रहता है; देखें [`docs/REPRODUCING.md`](docs/REPRODUCING.md#calibration-table)।

यही `context_savings` मेटाडेटा `get_impact_radius`, `get_review_context`, `detect_changes` और `get_architecture_overview` MCP टूल के JSON उत्तरों के साथ भी जुड़ता है।

</details>

<details>
<summary><strong>बहु-रिपॉज़िटरी डीमन</strong></summary>
<br>

अगर आपका एडिटर हुक का समर्थन नहीं करता (जैसे Cursor या OpenCode), या आप एडिटर एकीकरण के बिना ग्राफ़ ताज़ा रखना चाहते हैं, तो डीमन आपकी रिपॉज़िटरी देखता है और उनके ग्राफ़ अद्यतन करता है। यह `code-review-graph` के साथ आता है; अलग से इंस्टॉल नहीं करना पड़ता।

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

इसे `code-review-graph daemon start|stop|status|...` के रूप में भी चलाया जा सकता है।

`crg-daemon add` `~/.code-review-graph/watch.toml` में लिखता है, जिसे आप सीधे भी संपादित कर सकते हैं:

```toml
[[repos]]
path = "/home/user/project-a"
alias = "proj-a"

[[repos]]
path = "/home/user/project-b"
alias = "project-b"
```

डीमन इस फ़ाइल को देखता रहता है और रिपॉज़िटरी जुड़ने या हटने पर वॉचर प्रोसेस शुरू या बंद करता है। हर 30 सेकंड की हेल्थ जाँच मरे हुए वॉचर फिर से चालू कर देती है।

पूरा कॉन्फ़िग संदर्भ [docs/COMMANDS.md](docs/COMMANDS.md#standalone-daemon-cli-crg-daemon) में देखें।

</details>

<details>
<summary><strong>30 MCP टूल</strong></summary>
<br>

ग्राफ़ बन जाने के बाद असिस्टेंट इनका उपयोग करता है।

| टूल | विवरण |
|------|-------------|
| `build_or_update_graph_tool` | ग्राफ़ बनाना या इंक्रीमेंटल रूप से अद्यतन करना |
| `run_postprocess_tool` | प्रवाह पहचान, कम्युनिटी पहचान और FTS इंडेक्सिंग फिर से चलाना |
| `get_minimal_context_tool` | छोटा संदर्भ (लगभग 100 टोकन); सबसे पहले इसे बुलाएँ |
| `get_impact_radius_tool` | बदली हुई फ़ाइलों का प्रभाव क्षेत्र |
| `get_review_context_tool` | संरचनात्मक सारांश के साथ रिव्यू संदर्भ |
| `query_graph_tool` | कॉलर, कॉली, टेस्ट, इम्पोर्ट, इनहेरिटेंस संबंधी क्वेरी |
| `traverse_graph_tool` | किसी भी नोड से BFS/DFS ट्रैवर्सल, टोकन बजट के साथ |
| `semantic_search_nodes_tool` | नाम या अर्थ से कोड इकाइयाँ खोजना |
| `embed_graph_tool` | सिमेंटिक खोज के लिए वेक्टर एम्बेडिंग बनाना |
| `list_graph_stats_tool` | ग्राफ़ का आकार और स्वास्थ्य |
| `get_docs_section_tool` | दस्तावेज़ के खंड लाना |
| `find_large_functions_tool` | पंक्ति-सीमा से बड़े फ़ंक्शन, क्लास या फ़ाइलें |
| `list_flows_tool` | महत्ता के क्रम में निष्पादन प्रवाह |
| `get_flow_tool` | एक निष्पादन प्रवाह |
| `get_affected_flows_tool` | बदली हुई फ़ाइलों से प्रभावित प्रवाह |
| `list_communities_tool` | पहचानी गई कोड कम्युनिटी |
| `get_community_tool` | एक कम्युनिटी |
| `get_architecture_overview_tool` | कम्युनिटी संरचना से आर्किटेक्चर अवलोकन |
| `detect_changes_tool` | जोखिम-स्कोर वाला बदलाव प्रभाव विश्लेषण |
| `get_hub_nodes_tool` | सबसे अधिक जुड़े नोड |
| `get_bridge_nodes_tool` | बिटवीननेस सेंट्रैलिटी से निकली अड़चनें |
| `get_knowledge_gaps_tool` | संरचनात्मक कमज़ोरियाँ और बिना टेस्ट वाले हॉटस्पॉट |
| `get_surprising_connections_tool` | अप्रत्याशित कम्युनिटी-पार युग्मन |
| `get_suggested_questions_tool` | विश्लेषण से बने रिव्यू प्रश्न |
| `refactor_tool` | नाम बदलने का पूर्वावलोकन, डेड कोड पहचान, सुझाव |
| `apply_refactor_tool` | पहले देखी गई रीफ़ैक्टरिंग लागू करना |
| `generate_wiki_tool` | कम्युनिटी से Markdown विकी |
| `get_wiki_page_tool` | एक विकी पृष्ठ |
| `list_repos_tool` | दर्ज रिपॉज़िटरी |
| `cross_repo_search_tool` | दर्ज रिपॉज़िटरी में खोज; `repos` से खोज को एक उपसमूह तक सीमित किया जा सकता है |

**MCP प्रॉम्प्ट** (5 वर्कफ़्लो टेम्पलेट):
`review_changes`, `architecture_map`, `debug_issue`, `onboard_developer`, `pre_merge_check`

</details>

<details>
<summary><strong>कॉन्फ़िगरेशन</strong></summary>
<br>

इंडेक्सिंग से कुछ पथ हटाने के लिए रिपॉज़िटरी की जड़ में `.code-review-graphignore` फ़ाइल बनाएँ:

```
generated/**
*.generated.ts
vendor/**
node_modules/**
```

git रिपॉज़िटरी में सिर्फ़ ट्रैक की गई फ़ाइलें इंडेक्स होती हैं (`git ls-files`), इसलिए gitignore की गई फ़ाइलें छूट जाती हैं। ट्रैक की गई फ़ाइलें हटाने के लिए, या जब git उपलब्ध न हो, तब `.code-review-graphignore` का उपयोग करें। डिफ़ॉल्ट अनदेखी सूची [docs/USAGE.md](docs/USAGE.md#ignore-patterns) में है।

वैकल्पिक निर्भरता समूह:

```bash
pip install "code-review-graph[embeddings]"          # Local vector embeddings (sentence-transformers)
pip install "code-review-graph[google-embeddings]"   # Google Gemini embeddings
pip install "code-review-graph[communities]"         # Community detection (igraph)
pip install "code-review-graph[enrichment]"          # Python call-resolution enrichment (Jedi)
pip install "code-review-graph[eval]"                # Evaluation benchmarks and SVG export (matplotlib)
pip install "code-review-graph[wiki]"                # ollama client (not used by the current wiki generator)
pip install "code-review-graph[all]"                 # All optional dependencies
```

### पर्यावरण चर

| चर | विवरण | डिफ़ॉल्ट |
|----------|-------------|---------|
| `CRG_GIT_TIMEOUT` | Git संक्रियाओं की समय सीमा (सेकंड में; build / update / watch) | `30` |
| `CRG_DISCOVERY_TIMEOUT` | जब फ़ाइल सूची स्पष्ट रूप से नहीं दी गई हो, तब बदलाव पता करने वाली प्रत्येक Git कमांड की समय सीमा (सेकंड में)। सीमा पार होने पर त्रुटि मिलती है, "कोई बदलाव नहीं" नहीं | `5` (`CRG_GIT_TIMEOUT` स्पष्ट रूप से सेट हो तो वही) |
| `CRG_DATA_DIR` | ग्राफ़ डेटाबेस और बनी हुई फ़ाइलों की डायरेक्टरी | - |
| `CRG_HOOK_WORKTREES` | `1` करने पर pre-commit हुक जुड़े हुए git worktree में भी चलता है | - |
| `CRG_EMBEDDING_MODEL` | स्थानीय वेक्टर एम्बेडिंग का डिफ़ॉल्ट मॉडल | `all-MiniLM-L6-v2` |
| `CRG_ACCEPT_CLOUD_EMBEDDINGS` | `1` करने पर क्लाउड एम्बेडिंग की बाहर-भेजने वाली चेतावनी दबती है | - |
| `CRG_ALLOW_REMOTE_CODE` | `trust_remote_code=True` माँगने वाले HuggingFace मॉडल की अनुमति | `0` |
| `CRG_MAX_IMPACT_NODES` | प्रभाव विश्लेषण में अधिकतम नोड | `500` |
| `CRG_MAX_IMPACT_DEPTH` | प्रभाव क्षेत्र विश्लेषण की खोज गहराई | `2` |
| `CRG_MAX_BFS_DEPTH` | ग्राफ़ ट्रैवर्सल की अधिकतम गहराई | `15` |
| `CRG_MAX_CHANGED_FUNCS` | एक बदलाव रिपोर्ट में विश्लेषित अधिकतम बदले हुए फ़ंक्शन | `500` |
| `CRG_MAX_TRANSITIVE_FRONTIER` | सकर्मक कॉलर/कॉली विस्तार में अधिकतम फ़्रंटियर आकार | `50` |
| `CRG_TOOL_TIMEOUT` | केवल-पठन MCP टूल की समय सीमा (सेकंड; `0` से बंद)। लिखने वाले टूल (build / postprocess / embed / wiki / apply-refactor) पर लागू नहीं | `0` |
| `CRG_CHURN_WINDOW_DAYS` | `detect-changes --churn` की कमिट गिनती की अवधि | `90` |
| `CRG_LEIDEN_SEED` | Leiden कम्युनिटी पहचान का सीड | `42` |
| `CRG_RECURSE_SUBMODULES` | `1`, `true` या `yes` होने पर git सबमॉड्यूल शामिल करें | - |
| `CRG_TOOLS` | सर्व करते समय दिखाए जाने वाले MCP टूल की अल्पविराम से अलग सूची | - |
| `GOOGLE_API_KEY` | Google Gemini एम्बेडिंग की API कुंजी | - |
| `MINIMAX_API_KEY` | MiniMax एम्बेडिंग की API कुंजी | - |
| `VOYAGE_API_KEY` | Voyage एम्बेडिंग की API कुंजी | - |
| `CRG_VOYAGE_MODEL` | Voyage एम्बेडिंग का मॉडल | `voyage-code-3` |
| `CRG_VOYAGE_OUTPUT_DIMENSION` | Voyage एम्बेडिंग का आउटपुट डाइमेंशन | `1024` |
| `CRG_VOYAGE_OUTPUT_DTYPE` | Voyage एम्बेडिंग का आउटपुट dtype | `float` |
| `CRG_VOYAGE_BASE_URL` | Voyage एम्बेडिंग एंडपॉइंट | `https://api.voyageai.com/v1` |
| `CRG_VOYAGE_BATCH_SIZE` | Voyage अनुरोधों का बैच आकार | `100` |
| `CRG_VOYAGE_MIN_INTERVAL_SEC` | Voyage अनुरोधों के बीच न्यूनतम अंतराल | `0` |
| `CRG_OPENAI_BASE_URL` | OpenAI-संगत एम्बेडिंग एंडपॉइंट | - |
| `CRG_OPENAI_API_KEY` | OpenAI-संगत एम्बेडिंग की API कुंजी | - |
| `CRG_OPENAI_MODEL` | OpenAI-संगत एम्बेडिंग का मॉडल | - |
| `CRG_OPENAI_DIMENSION` | एम्बेडिंग डाइमेंशन तय करें (v3 मॉडल घटाने का समर्थन करते हैं) | - |
| `CRG_OPENAI_BATCH_SIZE` | OpenAI-संगत अनुरोधों का बैच आकार | `100` |
| `NO_COLOR` | टर्मिनल में ANSI रंग बंद करें | - |
| `CRG_SERIAL_PARSE` | `1` करने पर समानांतर पार्सिंग बंद (डिबगिंग के लिए) | - |

OpenAI-संगत एम्बेडिंग (OpenAI, Azure, या new-api, LiteLLM, vLLM, LocalAI, या OpenAI मोड में Ollama जैसा स्व-होस्टेड गेटवे) के लिए अतिरिक्त इंस्टॉल नहीं चाहिए। चर सेट करें और `embed_graph` को `provider="openai"` दें:

```bash
export CRG_OPENAI_BASE_URL=http://127.0.0.1:3000/v1     # or https://api.openai.com/v1
export CRG_OPENAI_API_KEY=sk-...
export CRG_OPENAI_MODEL=text-embedding-3-small          # whatever your gateway serves
# optional:
export CRG_OPENAI_DIMENSION=1536                        # pin dim (v3 models support reduction)
export CRG_OPENAI_BATCH_SIZE=100                        # lower for gateways with tight limits
                                                        # (e.g. Qwen text-embedding-v4 caps at 10)
```

जब बेस URL localhost (`127.0.0.1`, `localhost`, `0.0.0.0`, `::1`) की ओर इशारा करता है, तब क्लाउड चेतावनी नहीं दिखती।

Voyage एम्बेडिंग के लिए भी अतिरिक्त इंस्टॉल नहीं चाहिए। `VOYAGE_API_KEY` सेट करें और `embed_graph` को `provider="voyage"` दें; डिफ़ॉल्ट मॉडल `voyage-code-3` है:

```bash
export VOYAGE_API_KEY=pa-...
export CRG_ACCEPT_CLOUD_EMBEDDINGS=1
code-review-graph embed --provider voyage --model voyage-code-3
```

> **मॉडल चुनना।** जिस इंडेक्स को लंबे समय तक रखना हो, उसके लिए `-preview`, `-beta` या `-exp` वाले मॉडल ID से बचें; प्रीव्यू मॉडल के वेट बदल सकते हैं (डाइमेंशन बदलने पर पूरा दोबारा एम्बेड करना पड़ता है) या उन्हें हटाया जा सकता है। `text-embedding-3-small` / `text-embedding-3-large` (OpenAI), `Qwen/Qwen3-Embedding-8B` (स्व-होस्टेड vLLM या LocalAI), या `gemini-embedding-001` (नेटिव Gemini प्रोवाइडर, जिसे `GOOGLE_API_KEY` चाहिए) जैसे GA रिलीज़ चुनें।
>
> एम्बेडिंग टेक्स्ट में पहचानकर्ता, हस्ताक्षर, संरचनात्मक संदर्भ, और पहले अनुच्छेद के docstring या डॉक-टिप्पणी का सीमित सारांश होता है। फ़ंक्शन के बॉडी नहीं भेजे जाते। दस्तावेज़ निकालने की सुविधा आने से पहले बने ग्राफ़ को दोबारा एम्बेड करने से पहले एक पूरा `code-review-graph build` चाहिए। सामान्य बिल्ड एम्बेडिंग कभी ताज़ा नहीं करते; बिल्ड के बाद ताज़ा करने के लिए `--embedding-provider` और `--embedding-model` दोनों दें। क्लाउड प्रोवाइडर को यह स्रोत-आधारित टेक्स्ट मिलता है और वे उसका शुल्क ले सकते हैं।

#### टूल फ़िल्टरिंग

CRG डिफ़ॉल्ट रूप से 30 MCP टूल दिखाता है। सर्वर को एक उपसमूह तक सीमित करने के लिए `--tools` या `CRG_TOOLS` पर्यावरण चर का उपयोग करें:

```bash
# CLI flag
code-review-graph serve --tools query_graph_tool,semantic_search_nodes_tool,detect_changes_tool

# Environment variable
CRG_TOOLS=query_graph_tool,semantic_search_nodes_tool code-review-graph serve
```

फ़्लैग चर से पहले आता है। जब कोई भी सेट न हो, तब सारे टूल उपलब्ध रहते हैं। MCP क्लाइंट कॉन्फ़िग में:

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

## सामान्य प्रश्न और तुलना

उत्तर [docs/FAQ.md](docs/FAQ.md) में हैं:

- [LSP / लैंग्वेज सर्वर से अंतर](docs/FAQ.md#how-is-this-different-from-lsp-and-language-servers): हर भाषा के लिए अलग डीमन के बजाय एक स्थायी, बहुभाषी ग्राफ़; प्रति सिंबल सटीकता में LSP आगे रहता है।
- [RAG / एम्बेडिंग से अंतर](docs/FAQ.md#isnt-this-just-rag): समानता के टुकड़े नहीं, AST से पार्स किए गए संरचनात्मक एज; एम्बेडिंग वैकल्पिक हैं और सिर्फ़ खोज में मदद करती हैं।
- [grep / एजेंट खोज से अंतर](docs/FAQ.md#why-not-just-grep): एक-हॉप खोज में grep जीतता है; बहु-हॉप सवालों (प्रभाव क्षेत्र, कॉलर के कॉलर, tests-for, प्रभावित प्रवाह) में ग्राफ़ जीतता है।
- [Serena, codegraph, claude-context, repomix से तुलना](docs/FAQ.md#how-does-it-compare-to-serena-codegraph-claude-context-and-repomix): तुलना तालिका।
- [कब इस्तेमाल न करें](docs/FAQ.md#when-should-i-not-use-it): छोटी रिपॉज़िटरी, मामूली एकल-फ़ाइल diff, एक बार के सवाल।
- [क्या यह डेटा बाहर भेजता है?](docs/FAQ.md#does-it-phone-home): कोई टेलीमेट्री नहीं; क्लाउड एम्बेडिंग तभी चलती हैं जब आप चालू करें।
- [कैसे जाँचें कि यह काम कर रहा है?](docs/FAQ.md#how-do-i-verify-it-is-working): `status`, `detect-changes --brief`, `/mcp`।

## समस्या निवारण

Windows/WSL सहित और भी मामले [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) में हैं।

### `pip` / `pipx` `hatchling` डाउनलोड नहीं कर पाता (या PyPI के लिए `Errno 9` / `Bad file descriptor`)

स्रोत ट्री से इंस्टॉल करने पर (जैसे `pipx install .`) PyPI से बिल्ड निर्भरताएँ चाहिए होती हैं। अगर कनेक्शन चेतावनियों के बाद `Could not find a version that satisfies the requirement hatchling` दिखे, तो हो सकता है उस टर्मिनल का Python `pypi.org` से HTTPS कनेक्शन न खोल पा रहा हो। यह एडिटर के अंदरूनी टर्मिनल में सबसे अधिक दिखता है, और कभी-कभी VPN, फ़ायरवॉल या प्रॉक्सी के साथ।

1. वही कमांड एडिटर के टर्मिनल के बजाय Terminal.app या iTerm से चलाएँ।
2. [uv](https://docs.astral.sh/uv/) से चेकआउट से इंस्टॉल करें, जो अलग डाउनलोड तंत्र इस्तेमाल करता है:

   ```bash
   cd /path/to/code-review-graph
   uv tool install . --force
   ```

3. क्लोन में विकास के लिए `uv sync` और `uv run code-review-graph ...` का उपयोग करें।

जाँच के लिए: `python3 scripts/diagnose_pypi_connectivity.py`। अगर यह `FAILED` छापे, तो समस्या नेटवर्क परिवेश की है, पैकेज के नाम की नहीं।

### Windows: `Invalid JSON: EOF while parsing` या `MCP error -32000: Connection closed`

Claude Code कॉन्फ़िग में `cmd /c` रैपर का उपयोग न करें। `~/.claude.json` को सीधे `.exe` की ओर इंगित करें और UTF-8 कॉन्फ़िग से सेट करें:

```json
"code-review-graph": {
  "command": "C:\\path\\to\\your\\venv\\Scripts\\code-review-graph.exe",
  "args": ["serve", "--repo", "C:\\path\\to\\your\\project"],
  "env": { "PYTHONUTF8": "1" }
}
```

## योगदान

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

पुल रिक्वेस्ट `staging` (डिफ़ॉल्ट ब्रांच) पर भेजें। बदलाव
`staging` → `testing` → `main` के क्रम में आगे बढ़ते हैं, और रिलीज़ `main` से टैग होते हैं। पूरा प्रवाह
[CONTRIBUTING.md](CONTRIBUTING.md#branching-and-promotion) में देखें।

अंतर्निहित भाषा जोड़ने के लिए `code_review_graph/parser.py` संपादित करें: एक्सटेंशन को `EXTENSION_TO_LANGUAGE` में और नोड प्रकार मैपिंग को `_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES` तथा `_CALL_TYPES` में जोड़ें। एक टेस्ट फ़िक्स्चर साथ दें और PR खोलें। अगर भाषा सिर्फ़ एक ही रिपॉज़िटरी में चाहिए, तो उसके बजाय [`languages.toml`](docs/CUSTOM_LANGUAGES.md) का उपयोग करें।

## लाइसेंस

MIT। देखें [LICENSE](LICENSE)।

<p align="center">
<br>
<a href="https://code-review-graph.com">code-review-graph.com</a><br><br>
<code>pip install code-review-graph && code-review-graph install</code>
</p>
