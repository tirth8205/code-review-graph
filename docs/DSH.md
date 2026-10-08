# DeepSeek Harness (DSH)

The native [dsh-code-review-graph](https://github.com/sumingwang233/dsh-code-review-graph)
bundle provides all 30 CRG 2.3.9 tools, five workflows and seven skills, with
Agent-local tool selection and a Web/Desktop graph sidebar. Platform request:
[#1100](https://github.com/tirth8205/code-review-graph/issues/1100).

## Install and ownership

Supported release baselines: DSH **0.1.5-rc.2** and **0.2.0-rc.2**. Alpha versions
are outside this adapter's compatibility contract. Follow the release-specific
[official bundle guide](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.0-rc.2/docs/user/develop/basic/publish.md).
The [official MCP guide](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.0-rc.2/packages/mcp/mcp-client/README.md)
describes raw MCP composition; this integration intentionally uses native DSH tools
so refactors pass through DSH's filesystem intents and session policy.

```sh
code-review-graph install --platform dsh                 # profile web
code-review-graph install --platform dsh --dsh-profile headless
npx dsh-code-review-graph@0.1.2 prepare                  # optional CLI setup; UI also prepares it
```

The installer invokes `dsh plugin --profile <name> add dsh-code-review-graph@0.1.2`.
DSH owns the profile manifest at `$DSH_HOME/profiles/<name>/package.json`, defaulting
to `~/.dsh/profiles/<name>/package.json`. Its relevant native schema is:

```json
{
  "dependencies": { "dsh-code-review-graph": "0.1.2" },
  "dsh": { "profile": { "bundles": ["dsh-code-review-graph"] } }
}
```

This is a bundle dependency and ordered bundle list, not an MCP server object or
array; no `type` field is used. `format="native-bundle"` in the existing
`PLATFORMS` inventory delegates lifecycle operations to DSH instead of mutating
its generated manifest or YAML. Malformed manifests are skipped. Reinstallation
does not call DSH when both owned entries exist, leaving bytes unchanged.

Desktop profile ownership belongs to the running application. Use the matching
release's native plugin manager; an external CLI cannot modify `desktop`.
The installer reports this requirement and leaves the profile untouched.
After native Desktop installation, choose **Enable now**. Existing 0.1.0 users
should remove that version through the native manager before installing 0.1.2;
an already-registered bundle remains an idempotent no-op for this installer.

```sh
code-review-graph uninstall --platform dsh --dsh-profile web --yes
```

Scoped uninstall delegates `remove dsh-code-review-graph` to DSH and preserves
graph data, user settings and other plugins. The adapter never installs repository
instruction files or legacy hooks for a DSH-only installation.

## Workspace and refactors

### Direct graph controls

In the project session, open the right sidebar → New tab → Start → Code graph.
Click **Set up and generate graph**. This prepares the isolated pinned Python
engine on the first explicit action, then generates and displays the graph.
Later use **Update graph**, or export the standalone HTML. The operation supports
cancellation and retry; no model prompt or JSON parameters are needed.

Native user commands `/crg-setup` and `/crg-graph` perform the same preparation
and generation without arguments or model inference. First preparation needs
uv or real Python 3.10+ and network access. Ordinary startup and model tool calls
do not download dependencies; custom engine configurations remain user-managed.

### Workspace binding and refactors

The adapter derives the canonical root from `agent.session.header.cwd`, removes
`repo_root` from model schemas and confines paths and symlinks to that root.
Cross-repository access uses only the user's configured authorized roots.
Backend workers and incremental watchers are shared within the Host by canonical
root and released when the final owning Agent exits.
Core build, review and flow source reads also validate filesystem identity,
including source paths retrieved from cached graph records, so linked parent
directories cannot bypass the adapter's argument checks.

`get_refactor_edit_plan_tool` is an additional **read-only** core interface. It
reuses the current refactor calculation and returns every
`files[{path,before_sha256,after_content,edit_count}]`, without write-side effects,
consuming the preview or presentation truncation. Invalid UTF-8, missing files,
expired previews and stale edits fail the entire plan. UTF-8 BOM and line endings
are retained. The adapter does not expose this internal tool to the model:
`apply_refactor_tool` obtains the plan, checks original byte hashes and native
file intents, then calls `ctx.fs.writeText` with the session policy. Each write is
atomic; a failure stops subsequent writes and returns committed paths and the
complete hash-guarded recovery patch.

## Evidence

The adapter repository publishes release-tagged deterministic session transcripts
from actual DSH ToolRuntime, Session and AgentRegistry packages invoking a real
CRG engine. They explicitly record **no real model inference**. Its CI matrix
covers Windows, Linux, macOS, both releases, native Web/headless profile lifecycle
and the shared Web/Desktop client graph contribution. See the adapter's
`docs/validation.md` for completed checks and pending platform runs. The browser
check executes the published Client factory with workspace-reader fixtures.
A separate check uses the actual Windows Desktop 0.2.0-rc.2 application for
isolated install/enable/remove/reinstall, native graph reading/search/HTML export,
and preservation of other plugins and settings. Other Desktop platforms/releases
have not been exercised as complete applications. Upstream reviewers decide
whether this evidence meets the platform contribution requirement.
