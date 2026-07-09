# Custom Languages (Bring Your Own Language)

code-review-graph ships parsers for 30+ languages, but the
[tree-sitter-language-pack](https://github.com/Goldziher/tree-sitter-language-pack)
it depends on bundles many more grammars than the built-in list. If your repo
uses a language the graph does not cover yet — Erlang, Haskell, OCaml,
Fortran, Ada, Clojure, ... — you can teach the parser about it with a small
config file. No fork, no code changes.

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
uv run code-review-graph build
```

Files matching the configured extensions are now parsed with the named
grammar, and the resulting Function/Class nodes and CALLS/IMPORTS_FROM edges
flow through every downstream feature (impact radius, search, communities,
wiki, MCP tools) exactly like built-in languages. Nodes carry the custom
language name (here `erlang`) in their `language` field.

## Schema reference

Each custom language is one `[languages.<name>]` table.

| Key | Type | Required | Meaning |
|-----|------|----------|---------|
| `<name>` | table key | yes | Language identifier stored on every parsed node. Lowercase letters, digits, `_`, `-`; max 32 chars; must start with a letter. |
| `extensions` | list of strings | yes | File extensions to claim, each starting with a dot (e.g. `".erl"`). Matched case-insensitively. |
| `grammar` | string | yes | A grammar name shipped by `tree_sitter_language_pack` (probe availability — see below). |
| `function_node_types` | list of strings | no* | Tree-sitter node types that define functions/methods. Matching nodes become `Function` nodes (or `Test` nodes when the name/file looks like a test). |
| `class_node_types` | list of strings | no* | Node types that define classes/records/types. Matching nodes become `Class` nodes. |
| `import_node_types` | list of strings | no* | Node types for import/include statements. Each yields an `IMPORTS_FROM` edge. |
| `call_node_types` | list of strings | no* | Node types for call expressions. Each yields a `CALLS` edge from the enclosing function. |
| `comment` | string | no | Free-form note for humans; ignored by the parser. |

\* At least one of the four node-type lists must be non-empty, otherwise the
entry is skipped (there would be nothing to extract).

### Validation rules (safety first)

The loader never crashes a build. Anything invalid is skipped with a
`WARNING` log line:

- **Built-ins always win.** A custom language cannot claim a built-in
  extension (`.py`, `.ts`, `.ex`, ...) and cannot reuse a built-in language
  name (`python`, `elixir`, ...).
- `grammar` must load from `tree_sitter_language_pack`; unknown grammars are
  skipped.
- Every extension must start with a dot.
- Two custom languages cannot claim the same extension (first one wins).
- At most **20** custom languages are loaded per repo.
- Malformed TOML disables custom languages for that build (with a warning).

## Finding the right node type names

Node type names are grammar-specific, so you need to look at the tree the
grammar actually produces. Two easy options:

**Option 1 — tree-sitter playground.** Paste a snippet into
<https://tree-sitter.github.io/tree-sitter/7-playground.html> and read the
node names off the parse tree (select the matching grammar first).

**Option 2 — probe locally with Python.** The exact grammar version your
build uses is the one in `tree_sitter_language_pack`, so probing locally is
the most reliable source of truth:

```bash
uv run python - <<'EOF'
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
EOF
```

Pick the node types that wrap whole definitions (`function_clause`, not the
inner `atom`) and whole call expressions (`call`, not the callee identifier).

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

- `Function` nodes `add`, `helper`, `scale` (from `function_clause`),
  each with `language = "erlang"`.
- A `Class` node `point` (from `record_decl`).
- `CALLS` edges `add → helper` and `scale → add`, resolved to their
  same-file qualified names, plus `scale → lists:map` for the remote call.
- An `IMPORTS_FROM` edge targeting `lists` (from `import_attribute`).
- `CONTAINS` edges from the file to every definition.

## How extraction works (and its limits)

Custom languages run through the same generic tree-sitter walker as built-in
languages — there is no per-language code path to maintain. That keeps the
feature simple, but the generic heuristics have limits:

- **Name extraction uses the default name-field heuristics.** The walker
  looks for a child node of a common identifier type (`identifier`, `name`,
  `type_identifier`, ...) and falls back to the grammar's `name` field
  (`node.child_by_field_name("name")`). Grammars that store definition names
  in another shape (e.g. nested two levels deep with a non-standard field)
  will produce unnamed — and therefore skipped — definitions.
- **Callee extraction probes common field names** (`function`, `callee`,
  `expr`, `name`) and descends through curried applications. Exotic call
  shapes may be missed.
- **Import targets** come from the grammar's `module`/`name`/`path`/`source`
  field when present, otherwise the raw statement text is recorded.
- **No cross-file module resolution.** Import edges keep the module name as
  written (e.g. `lists`); they are not resolved to file paths the way
  built-in languages with dedicated resolvers are.
- **No language-specific extras**: things like decorator-based test
  detection, framework annotations (Spring, Temporal), or SFC handling only
  exist for built-in languages.

If a language needs deeper support than the generic walker can give, please
open an issue — config-driven support is the on-ramp, not the ceiling.

## Troubleshooting

- Run a build with `-v`/logging enabled and look for `languages.toml`
  warnings — every skipped entry says exactly why it was skipped.
- Probe grammar availability:
  `uv run python -c "import tree_sitter_language_pack as t; t.get_language('erlang')"`
  (raises `LookupError` if the grammar is not bundled).
- The config is read when a parser is constructed (every `build`/`update`),
  so config changes take effect on the next build — re-run
  `uv run code-review-graph build` after editing.
