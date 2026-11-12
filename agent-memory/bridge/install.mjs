import { mkdir, readFile, open, rename, lstat, unlink } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { BridgeError, required } from './api.mjs';

const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
export async function installCodexHooks(project, cliPath) {
  const root = resolve(required(project, '--project', 4096));
  const info = await lstat(root); if (!info.isDirectory() || info.isSymbolicLink()) throw new BridgeError('invalid_input', 'Project must be an existing real directory');
  const directory = join(root, '.codex'); await mkdir(directory, { recursive: true });
  if ((await lstat(directory)).isSymbolicLink()) throw new BridgeError('invalid_input', '.codex must not be a symlink');
  const path = join(directory, 'hooks.json'); let original = null, config = { hooks: {} };
  try {
    if ((await lstat(path)).isSymbolicLink()) throw new BridgeError('invalid_input', 'hooks.json must not be a symlink');
    original = await readFile(path, 'utf8'); config = JSON.parse(original);
  } catch (error) { if (error.code !== 'ENOENT') throw new BridgeError('invalid_input', 'Existing hooks.json is invalid; it was not changed'); }
  if (!config || typeof config !== 'object' || Array.isArray(config) || (config.hooks && (typeof config.hooks !== 'object' || Array.isArray(config.hooks)))) throw new BridgeError('invalid_input', 'Existing hooks.json has an invalid shape; it was not changed');
  config.hooks ??= {};
  for (const event of ['UserPromptSubmit', 'Stop', 'SessionStart']) {
    const command = `${quote(process.execPath)} ${quote(resolve(cliPath))} hook ${quote(event)}`;
    const groups = config.hooks[event] ?? [];
    if (!Array.isArray(groups)) throw new BridgeError('invalid_input', 'Existing lifecycle definitions have an invalid shape; they were not changed');
    if (!groups.some(group => group.hooks?.some(handler => handler.type === 'command' && handler.command === command))) {
      groups.push({ ...(event === 'SessionStart' ? { matcher: 'startup|resume|clear|compact' } : {}), hooks: [{ type: 'command', command, timeout: 120, ...(event === 'Stop' ? {} : { additionalContextLimit: 3500 }) }] });
    }
    config.hooks[event] = groups;
  }
  const text = JSON.stringify(config, null, 2) + '\n';
  if (text !== original) {
    if (original !== null) { const backup = await open(`${path}.memory-lens-${randomUUID()}.bak`, 'wx', 0o600); try { await backup.writeFile(original); await backup.sync(); } finally { await backup.close(); } }
    const temp = `${path}.${randomUUID()}.tmp`; const file = await open(temp, 'wx', 0o600);
    try { await file.writeFile(text); await file.sync(); } finally { await file.close(); }
    try { await rename(temp, path); } catch (error) { await unlink(temp).catch(() => {}); throw error; }
  }
  return { path, installed: true, trust_required: true, message: 'Open /hooks in Codex to review and trust the new project hooks. Credentials remain in your environment.' };
}
