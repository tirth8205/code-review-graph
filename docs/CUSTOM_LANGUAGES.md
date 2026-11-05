# Custom Languages (Bring Your Own Language)

code-review-graph has built-in parsers for more than 35 languages. The
[tree-sitter-language-pack](https://github.com/Goldziher/tree-sitter-language-pack)
it depends on bundles many more grammars. If your repository uses a language
the graph does not cover (Erlang, Haskell, OCaml, Fortran, Ada, Clojure, ...),
you can add it with a config file. No fork, no code changes.

## Quick start

Create `<repo_root>/.code-review-graph/languages.toml`:

```toml
[languages.erlang]
extensions = [".erl"]
grammar = "erlang"
function_node_types = ["function_clause"]
class_node_types = ["record_decl"]
import_node_types = ["import_attribute"]
call_node_types = ["call"]
comment = "Erlang via the bundled tree-sitter-erlang grammar"
```

Then rebuild:

```bash
code-review-graph build
```

Files with the configured extensions are parsed with the named grammar. The
resulting `Function` and `Class` nodes and `CALLS` and `IMPORTS_FROM` edges go
through every downstream feature (impact radius, search, communities, wiki,
MCP tools) like built-in languages. Nodes carry the custom language name (here
`erlang`) in their `language` field.

## Schema reference

Each custom language is one `[languages.<name>]` table.

| Key | Type | Required | Meaning |
|-----|------|----------|---------|
| `<name>` | table key | yes | Language identifier stored on every parsed node. A lowercase letter followed by lowercase letters, digits, `_` or `-`; at most 32 characters. |
| `extensions` | list of strings | yes | File extensions to claim. Each is a dot followed by 1 to 15 characters from `a-z`, `0-9`, `_`, `+`, `-`. Matched case-insensitively. |
| `grammar` | string | yes | A grammar name shipped by `tree_sitter_language_pack` (see [Troubleshooting](#troubleshooting) for how to check). |
| `function_node_types` | list of strings | no* | Node types that define functions or methods. Each becomes a `Function` node, or a `Test` node when its name or location marks it as a test. |
| `class_node_types` | list of strings | no* | Node types that define classes, records or types. Each becomes a `Class` node. |
| `import_node_types` | list of strings | no* | Node types for import or include statements. Each yields an `IMPORTS_FROM` edge. |
| `call_node_types` | list of strings | no* | Node types for call expressions. Each yields a `CALLS` edge from the enclosing function. |
| `name_field` | string or list of strings | no | Ordered candidates for finding a definition's name when it is not in a `name` field or an identifier child (see below). At most 8. |
| `comment` | string | no | Free text for humans; ignored by the parser. |

\* At least one of the four node-type lists must be non-empty, otherwise the
entry is skipped.

### Validation rules

The loader never fails a build. An invalid entry is skipped with a `WARNING`
log line that says why:

- Built-ins win. A custom language cannot claim a built-in extension (`.py`,
  `.ts`, `.ex`, ...) or reuse a built-in language name (`python`, `elixir`, ...).
- `grammar` must load from `tree_sitter_language_pack`.
- Every extension must start with a dot.
- Two custom languages cannot claim the same extension; the first one wins.
- At most 20 custom languages are loaded per repository; the rest are ignored.
- Malformed TOML disables custom languages for that build.
- `name_field` must be a string or a list of non-empty strings, at most 8
  entries.

### Naming definitions with `name_field`

Without `name_field`, the parser reads a definition's name from the grammar's
`name` field, then from the first child whose type is identifier-like
(`identifier`, `name`, `type_identifier`, `property_identifier`,
`simple_identifier`, `constant`, `field_identifier`). Many grammars keep the
name elsewhere: in a differently named field, or nested a level or two down.
A definition whose name cannot be found is dropped. `name_field` says where to
look.

Candidates are tried in two passes:

1. Fields. Each candidate is tried as a tree-sitter field name on the
   definition node. All candidates are tried as fields before any type search,
   so a precise field beats a broader match.
2. Typed descendants. If no field matched, the first descendant (up to 4 levels
   down) whose node type equals a candidate.

The matched node is reduced to its first text-bearing leaf of a known name type
(`identifier`, `word`, `name`, `type_identifier`, ...) or, failing that, its own
text. Surrounding braces, quotes and whitespace are stripped. Text that is
empty, spans lines or is longer than 256 characters is rejected. If no
candidate resolves, the `name` field fallback above applies.

```toml
[languages.bibtex]
extensions = [".bib"]
grammar = "bibtex"
class_node_types = ["entry"]
name_field = ["key"]            # @article{smith2020,...} -> "smith2020"

[languages.latex]
extensions = [".tex"]
grammar = "latex"
class_node_types = ["section", "chapter", "subsection"]
function_node_types = ["new_command_definition"]
name_field = ["name", "text", "declaration"]
#   \section{Introduction}   -> "Introduction" (via `text`)
#   \newcommand{\foo}{bar}    -> "\foo"        (via `declaration`)

[languages.markdown]
extensions = [".md"]
grammar = "markdown"
class_node_types = ["section"]
name_field = ["inline"]         # "# My Heading" -> "My Heading" (typed descendant)
```

Use a list when node types keep their names in different places (LaTeX
`section` uses `text`, `\newcommand` uses `declaration`): the first candidate
that resolves wins.

## Finding the right node type names

Node type names are grammar-specific. Two ways to see them:

**Tree-sitter playground.** Paste a snippet into
<https://tree-sitter.github.io/tree-sitter/7-playground.html>, select the
grammar, and read the node names off the tree.

**Probe locally.** The grammar version your build uses is the one in
`tree_sitter_language_pack`, so this is the reliable source:

```bash
python - <<'EOP'
import tree_sitter_language_pack as tslp

source = b"""
-module(math_utils).
add(A, B) -> helper(A) + B.
helper(X) -> X * 2.
"""

def dump(node, depth=0):
    print("  " * depth + node.type, node.text.decode()[:40].replace("\n", " "))
    for child in node.children:
        dump(child, depth + 1)

dump(tslp.get_parser("erlang").parse(source).root_node)
EOP
```

Pick node types that wrap whole definitions (`function_clause`, not the inner
`atom`) and whole call expressions (`call`, not the callee identifier).

## Worked example: Erlang end to end

`src/math_utils.erl`:

```erlang
-module(math_utils).
-export([add/2, scale/2]).
-import(lists, [map/2]).

-record(point, {x, y}).

add(A, B) ->
    helper(A) + B.

helper(X) -> X * 2.

scale(Points, F) ->
    lists:map(fun(P) -> add(P, F) end, Points).
```

With the `[languages.erlang]` config from the quick start, a build produces:

- `Function` nodes `add`, `helper`, `scale` (from `function_clause`), each
  with `language = "erlang"`.
- A `Class` node `point` (from `record_decl`).
- `CALLS` edges `add -> helper` and `scale -> add`, resolved to their
  same-file qualified names, plus `scale -> lists:map` for the remote call.
- An `IMPORTS_FROM` edge targeting `lists` (from `import_attribute`).
- `CONTAINS` edges from the file to every definition.

## How extraction works (and its limits)

Custom languages go through the same generic tree-sitter walker as built-in
languages. There is no per-language code path, which keeps the feature simple
and sets its limits:

- Names come from `name_field`, then the grammar's `name` field, then an
  identifier-like child (see above). Definitions with no resolvable name are
  dropped.
- Callees are read from the call node's `function`, `callee`, `expr` or `name`
  field, in that order, descending through nested applications of the same
  field (curried calls). Callee text that spans lines or exceeds 256 characters
  is dropped. Other call shapes are missed.
- Import targets come from the statement's `module`, `name`, `path` or `source`
  field; otherwise the whole statement text is recorded.
- No cross-file module resolution. Import edges keep the module name as written
  (`lists`); they are not resolved to file paths as built-in languages with
  dedicated resolvers are.
- No language-specific extras: decorator-based test detection, framework
  annotations (Spring, Temporal) and SFC handling exist only for built-in
  languages.

If a language needs more than the generic walker gives, open an issue.

## Troubleshooting

- The loader logs a `WARNING` for every skipped entry, naming the config file,
  the language and the reason. The CLI prints log lines to stderr during
  `build` and `update`; `-q` does not hide warnings.
- Check a grammar is bundled:
  `python -c "import tree_sitter_language_pack as t; t.get_language('erlang')"`.
  It raises `LookupError` if it is not.
- The config is read when a parser is constructed and cached by file mtime and
  size. `update` re-parses only changed files, so run `code-review-graph build`
  after editing the config to apply it to every file.
