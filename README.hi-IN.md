<h1 align="center">code-review-graph</h1>

<p align="center">
  <strong>टोकन बर्बाद करना बंद करें। स्मार्ट रिव्यू शुरू करें।</strong>
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

AI कोडिंग टूल्स हर टास्क पर आपका पूरा कोडबेस दोबारा पढ़ते हैं। `code-review-graph` इस समस्या को हल करता है। यह [Tree-sitter](https://tree-sitter.github.io/tree-sitter/) से आपके कोड का स्ट्रक्चरल मैप बनाता है, बदलावों को इंक्रीमेंटली ट्रैक करता है, और [MCP](https://modelcontextprotocol.io/) के ज़रिए आपके AI असिस्टेंट को सटीक कॉन्टेक्स्ट देता है ताकि वह केवल ज़रूरी कोड ही पढ़े।

<p align="center">
  <img src="diagrams/diagram1_before_vs_after.png" alt="टोकन समस्या: 6 वास्तविक रिपॉज़िटरीज़ में औसतन 8.2 गुना टोकन कमी" width="85%" />
</p>

---

## त्वरित शुरुआत

```bash
pip install code-review-graph                     # या: pipx install code-review-graph
code-review-graph install          # सभी समर्थित प्लेटफ़ॉर्म को स्वचालित रूप से पहचानता और कॉन्फ़िगर करता है
code-review-graph build            # अपना कोडबेस पार्स करें
```

एक कमांड सब कुछ सेट कर देता है। `install` पहचान लेता है कि आपके पास कौन से AI कोडिंग टूल हैं, प्रत्येक के लिए सही MCP कॉन्फ़िगरेशन लिखता है, और आपके प्लेटफ़ॉर्म रूल्स में ग्राफ-अवेयर निर्देश जोड़ता है। यह स्वचालित रूप से पहचानता है कि आपने `uvx` या `pip`/`pipx` से इंस्टॉल किया है और उसके अनुसार कॉन्फ़िग बनाता है। इंस्टॉल के बाद अपना एडिटर/टूल रीस्टार्ट करें।

<p align="center">
  <img src="diagrams/diagram8_supported_platforms.png" alt="एक इंस्टॉल, हर प्लेटफ़ॉर्म: Codex, Claude Code, Cursor, Windsurf, Zed, Continue, OpenCode, Antigravity, और Kiro को स्वचालित रूप से पहचानता है" width="85%" />
</p>

किसी विशेष प्लेटफ़ॉर्म को टार्गेट करने के लिए:

```bash
code-review-graph install --platform codex       # केवल Codex कॉन्फ़िगर करें
code-review-graph install --platform cursor      # केवल Cursor कॉन्फ़िगर करें
code-review-graph install --platform claude-code  # केवल Claude Code कॉन्फ़िगर करें
code-review-graph install --platform kiro         # केवल Kiro कॉन्फ़िगर करें
```

Python 3.10+ आवश्यक है। सबसे अच्छे अनुभव के लिए [uv](https://docs.astral.sh/uv/) इंस्टॉल करें (MCP कॉन्फ़िग उपलब्ध होने पर `uvx` का उपयोग करेगा, अन्यथा सीधे `code-review-graph` कमांड पर फ़ॉलबैक करेगा)।

फिर अपना प्रोजेक्ट खोलें और अपने AI असिस्टेंट से कहें:

```
Build the code review graph for this project
```

प्रारंभिक बिल्ड 500 फ़ाइलों के प्रोजेक्ट के लिए लगभग 10 सेकंड लेता है। उसके बाद, हर फ़ाइल एडिट और git कमिट पर ग्राफ स्वचालित रूप से अपडेट होता रहता है।

---

## यह कैसे काम करता है

<p align="center">
  <img src="diagrams/diagram7_mcp_integration_flow.png" alt="आपका AI असिस्टेंट ग्राफ का उपयोग कैसे करता है: यूज़र रिव्यू मांगता है, AI MCP टूल्स चेक करता है, ग्राफ ब्लास्ट रेडियस और रिस्क स्कोर लौटाता है, AI केवल ज़रूरी कोड पढ़ता है" width="80%" />
</p>

आपकी रिपॉज़िटरी को Tree-sitter से AST में पार्स किया जाता है, नोड्स (फ़ंक्शन, क्लासेज़, इम्पोर्ट्स) और एज़ेज़ (कॉल्स, इनहेरिटेंस, टेस्ट कवरेज) के ग्राफ के रूप में स्टोर किया जाता है, और फिर रिव्यू के समय क्वेरी करके उन फ़ाइलों का न्यूनतम सेट निकाला जाता है जो आपके AI असिस्टेंट को पढ़ने की ज़रूरत है।

<p align="center">
  <img src="diagrams/diagram2_architecture_pipeline.png" alt="आर्किटेक्चर पाइपलाइन: रिपॉज़िटरी से Tree-sitter पार्सर, SQLite ग्राफ, ब्लास्ट रेडियस, न्यूनतम रिव्यू सेट" width="100%" />
</p>

### ब्लास्ट-रेडियस विश्लेषण

जब कोई फ़ाइल बदलती है, तो ग्राफ हर कॉलर, डिपेंडेंट, और टेस्ट को ट्रेस करता है जो प्रभावित हो सकता है। यह बदलाव का "ब्लास्ट रेडियस" है। आपका AI पूरे प्रोजेक्ट को स्कैन करने की बजाय केवल इन फ़ाइलों को पढ़ता है।

<p align="center">
  <img src="diagrams/diagram3_blast_radius.png" alt="ब्लास्ट रेडियस विज़ुअलाइज़ेशन: login() में बदलाव कॉलर्स, डिपेंडेंट्स, और टेस्ट्स तक कैसे फैलता है" width="70%" />
</p>

### इंक्रीमेंटल अपडेट < 2 सेकंड में

हर git कमिट या फ़ाइल सेव पर एक हुक चलता है। ग्राफ SHA-256 हैश चेक के ज़रिए बदली हुई फ़ाइलों और उनके डिपेंडेंट्स को ढूंढता है, और केवल बदले हुए कोड को री-पार्स करता है। 2,900 फ़ाइलों का प्रोजेक्ट 2 सेकंड से कम में री-इंडेक्स होता है।

<p align="center">
  <img src="diagrams/diagram4_incremental_update.png" alt="इंक्रीमेंटल अपडेट फ़्लो: git कमिट ट्रिगर करता है, डिफ़ ढूंढता है, केवल 5 फ़ाइलें री-पार्स होती हैं जबकि 2,910 स्किप होती हैं" width="90%" />
</p>

### मोनोरिपो समस्या, हल

बड़े मोनोरिपो में टोकन बर्बादी सबसे ज़्यादा होती है। ग्राफ शोर को काटता है — 27,700+ फ़ाइलें रिव्यू कॉन्टेक्स्ट से बाहर, केवल ~15 फ़ाइलें वास्तव में पढ़ी गईं।

<p align="center">
  <img src="diagrams/diagram6_monorepo_funnel.png" alt="Next.js मोनोरिपो: 27,732 फ़ाइलें code-review-graph से होकर ~15 फ़ाइलों तक — 49 गुना कम टोकन" width="80%" />
</p>

### 23 भाषाएं + Jupyter नोटबुक

<p align="center">
  <img src="diagrams/diagram9_language_coverage.png" alt="19 भाषाएं श्रेणी के अनुसार: वेब, बैकेंड, सिस्टम्स, मोबाइल, स्क्रिप्टिंग, और Jupyter/Databricks नोटबुक सपोर्ट" width="90%" />
</p>

हर भाषा में फ़ंक्शन, क्लासेज़, इम्पोर्ट्स, कॉल साइट्स, इनहेरिटेंस, और टेस्ट डिटेक्शन के लिए पूर्ण Tree-sitter ग्रामर सपोर्ट। इसमें Zig, PowerShell, Julia, और Svelte SFC सपोर्ट शामिल है। साथ ही मल्टी-लैंग्वेज सेल सपोर्ट (Python, R, SQL) के साथ Jupyter/Databricks नोटबुक पार्सिंग (`.ipynb`), और Perl XS फ़ाइलें (`.xs`)।

---

## बेंचमार्क

<p align="center">
  <img src="diagrams/diagram5_benchmark_board.png" alt="वास्तविक रिपॉज़ में बेंचमार्क: 4.9 गुना से 27.3 गुना तक कम टोकन, बेहतर रिव्यू गुणवत्ता" width="85%" />
</p>

सभी आंकड़े 6 वास्तविक ओपन-सोर्स रिपॉज़िटरीज़ (कुल 13 कमिट्स) पर स्वचालित मूल्यांकन रनर से आते हैं। `code-review-graph eval --all` से पुनः प्राप्त करें। विस्तृत बेंचमार्क डेटा के लिए [अंग्रेज़ी README](README.md) देखें।

---

## विशेषताएं

| विशेषता | विवरण |
|---------|--------|
| **इंक्रीमेंटल अपडेट** | केवल बदली हुई फ़ाइलों को री-पार्स करता है। बाद के अपडेट 2 सेकंड से कम में पूरे होते हैं। |
| **23 भाषाएं + नोटबुक** | Python, TypeScript/TSX, JavaScript, Vue, Svelte, Go, Rust, Java, Scala, C#, Ruby, Kotlin, Swift, PHP, Solidity, C/C++, Dart, R, Perl, Lua, Zig, PowerShell, Julia, Jupyter/Databricks (.ipynb) |
| **ब्लास्ट-रेडियस विश्लेषण** | दिखाता है कि किसी भी बदलाव से कौन से फ़ंक्शन, क्लासेज़, और फ़ाइलें प्रभावित होती हैं |
| **ऑटो-अपडेट हुक्स** | बिना मैन्युअल हस्तक्षेप के हर फ़ाइल एडिट और git कमिट पर ग्राफ अपडेट होता है |
| **सिमेंटिक सर्च** | sentence-transformers, Google Gemini, या MiniMax के ज़रिए वैकल्पिक वेक्टर एम्बेडिंग |
| **इंटरैक्टिव विज़ुअलाइज़ेशन** | सर्च, कम्युनिटी लीजेंड टॉगल, और डिग्री-स्केल्ड नोड्स के साथ D3.js फ़ोर्स-डायरेक्टेड ग्राफ |
| **हब और ब्रिज डिटेक्शन** | betweenness centrality के ज़रिए सबसे ज़्यादा कनेक्टेड नोड्स और आर्किटेक्चरल चोकपॉइंट्स खोजें |
| **सरप्राइज़ स्कोरिंग** | अप्रत्याशित कपलिंग का पता लगाएं: क्रॉस-कम्युनिटी, क्रॉस-लैंग्वेज, पेरीफ़ेरल-टू-हब एज़ेज़ |
| **नॉलेज गैप विश्लेषण** | अलग-थलग नोड्स, अनटेस्टेड हॉटस्पॉट्स, पतली कम्युनिटीज़, और स्ट्रक्चरल कमज़ोरियों की पहचान |
| **सुझाए गए प्रश्न** | ग्राफ विश्लेषण (ब्रिजेज़, हब्स, सरप्राइज़ेज़) से स्वतः-जनित रिव्यू प्रश्न |
| **एज कॉन्फ़िडेंस** | एज़ेज़ पर फ़्लोट स्कोर के साथ तीन-स्तरीय कॉन्फ़िडेंस स्कोरिंग (EXTRACTED/INFERRED/AMBIGUOUS) |
| **ग्राफ ट्रैवर्सल** | कॉन्फ़िगर करने योग्य डेप्थ और टोकन बजट के साथ किसी भी नोड से फ़्री-फ़ॉर्म BFS/DFS एक्सप्लोरेशन |
| **एक्सपोर्ट फ़ॉर्मैट** | GraphML (Gephi/yEd), Neo4j Cypher, विकीलिंक्स के साथ Obsidian वॉल्ट, SVG स्टैटिक ग्राफ |
| **ग्राफ डिफ़** | समय के साथ ग्राफ स्नैपशॉट्स की तुलना: नए/हटाए गए नोड्स, एज़ेज़, कम्युनिटी बदलाव |
| **टोकन बेंचमार्किंग** | प्रति-प्रश्न अनुपात के साथ नैव फ़ुल-कॉर्पस टोकन बनाम ग्राफ क्वेरी टोकन मापें |
| **मेमोरी लूप** | री-इन्जेशन के लिए Q&A परिणामों को मार्कडाउन के रूप में सहेजें, ताकि ग्राफ क्वेरीज़ से बढ़े |
| **कम्युनिटी ऑटो-स्प्लिट** | बड़ी कम्युनिटीज़ (ग्राफ का >25%) को Leiden के ज़रिए पुनरावर्ती रूप से विभाजित किया जाता है |
| **एक्ज़ीक्यूशन फ़्लोज़** | भारित क्रिटिकैलिटी के अनुसार क्रमबद्ध, एंट्री पॉइंट्स से कॉल चेन ट्रेस करें |
| **कम्युनिटी डिटेक्शन** | बड़े ग्राफ़ के लिए रेज़ोल्यूशन स्केलिंग के साथ Leiden एल्गोरिदम से संबंधित कोड क्लस्टर करें |
| **आर्किटेक्चर ओवरव्यू** | कपलिंग चेतावनियों के साथ स्वतः-जनित आर्किटेक्चर मैप |
| **रिस्क-स्कोर्ड रिव्यूज़** | `detect_changes` डिफ़ को प्रभावित फ़ंक्शन, फ़्लोज़, और टेस्ट गैप्स से मैप करता है |
| **रिफ़ैक्टरिंग टूल्स** | रीनेम प्रीव्यू, फ़्रेमवर्क-अवेयर डेड कोड डिटेक्शन, कम्युनिटी-ड्रिवन सुझाव |
| **विकी जनरेशन** | कम्युनिटी संरचना से स्वतः मार्कडाउन विकी जनरेट करें |
| **मल्टी-रिपो रजिस्ट्री** | कई रिपॉज़ रजिस्टर करें, सभी में सर्च करें |
| **MCP प्रॉम्प्ट्स** | 5 वर्कफ़्लो टेम्प्लेट: review, architecture, debug, onboard, pre-merge |
| **फ़ुल-टेक्स्ट सर्च** | कीवर्ड और वेक्टर सिमिलैरिटी को मिलाकर FTS5-संचालित हाइब्रिड सर्च |
| **लोकल स्टोरेज** | `.code-review-graph/` में SQLite फ़ाइल। कोई बाहरी डेटाबेस नहीं, कोई क्लाउड निर्भरता नहीं। |
| **वॉच मोड** | काम करते समय लगातार ग्राफ अपडेट |

---

## उपयोग

<details>
<summary><strong>स्लैश कमांड</strong></summary>
<br>

| कमांड | विवरण |
|-------|--------|
| `/code-review-graph:build-graph` | कोड ग्राफ बनाएं या रीबिल्ड करें |
| `/code-review-graph:review-delta` | पिछले कमिट के बाद से बदलावों की समीक्षा करें |
| `/code-review-graph:review-pr` | ब्लास्ट-रेडियस विश्लेषण के साथ पूर्ण PR रिव्यू |

</details>

<details>
<summary><strong>CLI संदर्भ</strong></summary>
<br>

```bash
code-review-graph install          # सभी प्लेटफ़ॉर्म को स्वचालित रूप से पहचानें और कॉन्फ़िगर करें
code-review-graph install --platform <name>  # किसी विशेष प्लेटफ़ॉर्म को टार्गेट करें
code-review-graph build            # पूरा कोडबेस पार्स करें
code-review-graph update           # इंक्रीमेंटल अपडेट (केवल बदली हुई फ़ाइलें)
code-review-graph status           # ग्राफ़ आंकड़े
code-review-graph watch            # फ़ाइल बदलाव पर ऑटो-अपडेट
code-review-graph visualize        # इंटरैक्टिव HTML ग्राफ जनरेट करें
code-review-graph visualize --format graphml   # GraphML के रूप में एक्सपोर्ट
code-review-graph visualize --format svg       # SVG के रूप में एक्सपोर्ट
code-review-graph visualize --format obsidian  # Obsidian वॉल्ट के रूप में एक्सपोर्ट
code-review-graph visualize --format cypher    # Neo4j Cypher के रूप में एक्सपोर्ट
code-review-graph wiki             # कम्युनिटीज़ से मार्कडाउन विकी जनरेट करें
code-review-graph detect-changes   # रिस्क-स्कोर्ड चेंज इम्पैक्ट विश्लेषण
code-review-graph register <path>  # मल्टी-रिपो रजिस्ट्री में रिपो रजिस्टर करें
code-review-graph unregister <id>  # रजिस्ट्री से रिपो हटाएं
code-review-graph repos            # रजिस्टर्ड रिपॉज़िटरीज़ की सूची
code-review-graph eval             # मूल्यांकन बेंचमार्क चलाएं
code-review-graph serve            # MCP सर्वर शुरू करें
```

</details>

<details>
<summary><strong>28 MCP टूल्स</strong></summary>
<br>

ग्राफ बनने के बाद आपका AI असिस्टेंट इन्हें स्वचालित रूप से उपयोग करता है।

| टूल | विवरण |
|-----|--------|
| `build_or_update_graph_tool` | ग्राफ बनाएं या इंक्रीमेंटली अपडेट करें |
| `get_minimal_context_tool` | अल्ट्रा-कॉम्पैक्ट कॉन्टेक्स्ट (~100 टोकन) — इसे पहले कॉल करें |
| `get_impact_radius_tool` | बदली हुई फ़ाइलों का ब्लास्ट रेडियस |
| `get_review_context_tool` | स्ट्रक्चरल सारांश के साथ टोकन-ऑप्टिमाइज़्ड रिव्यू कॉन्टेक्स्ट |
| `query_graph_tool` | कॉलर्स, कॉलीज़, टेस्ट, इम्पोर्ट्स, इनहेरिटेंस क्वेरीज़ |
| `traverse_graph_tool` | टोकन बजट के साथ किसी भी नोड से BFS/DFS ट्रैवर्सल |
| `semantic_search_nodes_tool` | नाम या अर्थ से कोड एंटिटीज़ खोजें |
| `embed_graph_tool` | सिमेंटिक सर्च के लिए वेक्टर एम्बेडिंग कम्प्यूट करें |
| `list_graph_stats_tool` | ग्राफ़ का आकार और स्वास्थ्य |
| `get_docs_section_tool` | दस्तावेज़ सेक्शन प्राप्त करें |
| `find_large_functions_tool` | लाइन-काउंट सीमा से अधिक फ़ंक्शन/क्लासेज़ खोजें |
| `list_flows_tool` | क्रिटिकैलिटी के अनुसार क्रमबद्ध एक्ज़ीक्यूशन फ़्लोज़ की सूची |
| `get_flow_tool` | किसी एक एक्ज़ीक्यूशन फ़्लो का विवरण प्राप्त करें |
| `get_affected_flows_tool` | बदली हुई फ़ाइलों से प्रभावित फ़्लोज़ खोजें |
| `list_communities_tool` | पहचानी गई कोड कम्युनिटीज़ की सूची |
| `get_community_tool` | किसी एक कम्युनिटी का विवरण प्राप्त करें |
| `get_architecture_overview_tool` | कम्युनिटी संरचना से आर्किटेक्चर ओवरव्यू |
| `detect_changes_tool` | कोड रिव्यू के लिए रिस्क-स्कोर्ड चेंज इम्पैक्ट विश्लेषण |
| `get_hub_nodes_tool` | सबसे ज़्यादा कनेक्टेड नोड्स (आर्किटेक्चरल हॉटस्पॉट्स) खोजें |
| `get_bridge_nodes_tool` | betweenness centrality से चोकपॉइंट्स खोजें |
| `get_knowledge_gaps_tool` | स्ट्रक्चरल कमज़ोरियों और अनटेस्टेड हॉटस्पॉट्स की पहचान |
| `get_surprising_connections_tool` | अप्रत्याशित क्रॉस-कम्युनिटी कपलिंग का पता लगाएं |
| `get_suggested_questions_tool` | विश्लेषण से स्वतः-जनित रिव्यू प्रश्न |
| `refactor_tool` | रीनेम प्रीव्यू, डेड कोड डिटेक्शन, सुझाव |
| `apply_refactor_tool` | पहले प्रीव्यू किए गए रिफ़ैक्टरिंग को लागू करें |
| `generate_wiki_tool` | कम्युनिटीज़ से मार्कडाउन विकी जनरेट करें |
| `get_wiki_page_tool` | कोई विशेष विकी पेज प्राप्त करें |
| `list_repos_tool` | रजिस्टर्ड रिपॉज़िटरीज़ की सूची |
| `cross_repo_search_tool` | सभी रजिस्टर्ड रिपॉज़िटरीज़ में सर्च करें |

**MCP प्रॉम्प्ट्स** (5 वर्कफ़्लो टेम्प्लेट):
`review_changes`, `architecture_map`, `debug_issue`, `onboard_developer`, `pre_merge_check`

</details>

<details>
<summary><strong>कॉन्फ़िगरेशन</strong></summary>
<br>

इंडेक्सिंग से पथ बाहर करने के लिए, अपनी रिपॉज़िटरी रूट में `.code-review-graphignore` फ़ाइल बनाएं:

```
generated/**
*.generated.ts
vendor/**
node_modules/**
```

नोट: git रिपॉज़ में, केवल ट्रैक की गई फ़ाइलें इंडेक्स होती हैं (`git ls-files`), इसलिए gitignore की गई फ़ाइलें स्वचालित रूप से छोड़ दी जाती हैं। `.code-review-graphignore` का उपयोग ट्रैक की गई फ़ाइलों को बाहर करने या git उपलब्ध न होने पर करें।

वैकल्पिक डिपेंडेंसी ग्रुप:

```bash
pip install code-review-graph[embeddings]          # लोकल वेक्टर एम्बेडिंग (sentence-transformers)
pip install code-review-graph[google-embeddings]   # Google Gemini एम्बेडिंग
pip install code-review-graph[communities]         # कम्युनिटी डिटेक्शन (igraph)
pip install code-review-graph[eval]                # मूल्यांकन बेंचमार्क (matplotlib)
pip install code-review-graph[wiki]                # LLM सारांश के साथ विकी जनरेशन (ollama)
pip install code-review-graph[all]                 # सभी वैकल्पिक डिपेंडेंसीज़
```

</details>

---

## योगदान

```bash
git clone https://github.com/tirth8205/code-review-graph.git
cd code-review-graph
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

<details>
<summary><strong>नई भाषा जोड़ना</strong></summary>
<br>

`code_review_graph/parser.py` में अपना एक्सटेंशन `EXTENSION_TO_LANGUAGE` में जोड़ें, साथ ही `_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES`, और `_CALL_TYPES` में नोड टाइप मैपिंग जोड़ें। एक टेस्ट फ़िक्सचर शामिल करें और PR खोलें।

</details>

## लाइसेंस

MIT। [LICENSE](LICENSE) देखें।

<p align="center">
<br>
<a href="https://code-review-graph.com">code-review-graph.com</a><br><br>
<code>pip install code-review-graph && code-review-graph install</code><br>
<sub>Codex, Claude Code, Cursor, Windsurf, Zed, Continue, OpenCode, Antigravity, और Kiro के साथ काम करता है</sub>
</p>
