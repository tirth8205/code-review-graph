#!/usr/bin/env node
import { fileURLToPath } from 'node:url';
import { api, BridgeError, configuration, safeError } from '../bridge/api.mjs';
import { MemoryBridge } from '../bridge/client.mjs';
import { codexHook } from '../bridge/hooks.mjs';
import { installCodexHooks } from '../bridge/install.mjs';
import { serveMcp } from '../bridge/mcp.mjs';

const [command, ...args] = process.argv.slice(2);
const output = value => process.stdout.write(JSON.stringify(value) + '\n');
const help = 'Memory Lens: ingest (JSON stdin), graph, context, command (text stdin), flush, sessions, health, hook <event>, mcp, install-codex-hooks --project <directory>. Select manually with --session-id <API id> or --source <tool> --session <native id>. Set MEMORY_LENS_API_URL and MEMORY_LENS_TOKEN.';
function options(args) {
  const values = {};
  for (let i = 0; i < args.length; i += 2) {
    const name = args[i];
    if (!['--source', '--session', '--session-id', '--turn', '--query', '--limit', '--request-id', '--project', '--title'].includes(name) || !args[i + 1] || args[i + 1].startsWith('--')) throw new BridgeError('invalid_input', 'Unknown or incomplete CLI option');
    values[name.slice(2)] = args[i + 1];
  }
  return values;
}
async function stdin() {
  let text = '';
  for await (const chunk of process.stdin) { text += chunk; if (text.length > 1000000) throw new BridgeError('invalid_input', 'Input exceeds 1 MB'); }
  return text;
}
function selection(opts) {
  return opts['session-id'] ? { session_id: opts['session-id'] } : { source: opts.source, external_session_id: opts.session, ...(opts.title ? { title: opts.title } : {}) };
}
async function main() {
  if (!command || ['help', '--help', '-h'].includes(command)) { process.stdout.write(help + '\n'); return; }
  if (command === 'install-codex-hooks') { output(await installCodexHooks(options(args).project, fileURLToPath(import.meta.url))); return; }
  if (command === 'hook') {
    try { const input = JSON.parse(await stdin()); output(await codexHook(new MemoryBridge(configuration()), args[0], input)); }
    catch (error) { process.stderr.write(`Memory Lens hook skipped (${safeError(error).code}); agent continues.\n`); output({}); }
    return;
  }
  const config = configuration();
  if (command === 'health') { output(await api(config, { action: 'health' }, { unauthenticated: true })); return; }
  const bridge = new MemoryBridge(config);
  if (command === 'mcp') { await serveMcp(bridge); return; }
  const opts = options(args); let result;
  if (command === 'ingest') {
    let input; try { input = JSON.parse(await stdin()); } catch { throw new BridgeError('invalid_input', 'ingest requires JSON on stdin'); }
    result = await bridge.ingest({ ...input, ...(opts.source ? { source: opts.source } : {}), ...(opts.session ? { external_session_id: opts.session } : {}), ...(opts['session-id'] ? { session_id: opts['session-id'] } : {}), ...(opts.turn ? { turn_id: opts.turn } : {}) });
  } else if (command === 'graph') result = await bridge.graph(selection(opts));
  else if (command === 'context') result = await bridge.context(selection(opts), opts.query, opts.limit === undefined ? undefined : Number(opts.limit));
  else if (command === 'command') result = await bridge.command(selection(opts), (await stdin()).trim(), opts['request-id']);
  else if (command === 'flush') result = await bridge.flush(opts['session-id'] || opts.session ? selection(opts) : null);
  else if (command === 'sessions') result = await api(config, { action: 'list_sessions' });
  else throw new BridgeError('invalid_input', 'Unknown command; use --help');
  output(result); if (result.status === 'queued' || result.pending > 0) process.exitCode = 1;
}
try { await main(); } catch (error) { output({ error: safeError(error) }); process.exitCode = 1; }
