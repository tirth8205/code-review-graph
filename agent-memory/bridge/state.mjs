import { createHash, randomUUID } from 'node:crypto';
import { mkdir, lstat, chmod, open, rename, readFile, readdir, unlink } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { homedir } from 'node:os';
import { BridgeError } from './api.mjs';

export const digest = value => createHash('sha256').update(value).digest('hex');
async function privateDirectory(path) {
  await mkdir(path, { recursive: true, mode: 0o700 });
  const info = await lstat(path);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new BridgeError('local_error', 'State directory must be a real private directory');
  await chmod(path, 0o700);
}
export class PrivateState {
  constructor(config) {
    this.root = resolve(config.env.MEMORY_LENS_STATE_DIR || join(homedir(), '.local', 'state', 'memory-lens'));
    this.scope = digest(JSON.stringify([config.url, config.token]));
    this.directory = join(this.root, this.scope);
  }
  async prepare(kind) {
    await privateDirectory(this.root); await privateDirectory(this.directory);
    const dir = join(this.directory, kind); await privateDirectory(dir); return dir;
  }
  key(event) { return digest(JSON.stringify([event.source, event.external_session_id, event.session_id, event.turn_id])); }
  async path(kind, key) { return join(await this.prepare(kind), `${digest(key)}.json`); }
  async write(kind, key, value) {
    const path = await this.path(kind, key);
    const temp = `${path}.${randomUUID()}.tmp`;
    const file = await open(temp, 'wx', 0o600);
    try { await file.writeFile(JSON.stringify(value)); await file.sync(); } finally { await file.close(); }
    try { await rename(temp, path); const directory = await open(dirname(path), 'r'); try { await directory.sync(); } finally { await directory.close(); } } catch (error) { await unlink(temp).catch(() => {}); throw error; }
    return path;
  }
  async read(kind, key) {
    const path = await this.path(kind, key);
    try { const info = await lstat(path); if (!info.isFile() || info.isSymbolicLink()) throw new Error(); await chmod(path, 0o600); return JSON.parse(await readFile(path, 'utf8')); } catch (error) { if (error.code === 'ENOENT') return null; throw new BridgeError('local_error', 'Private state record is invalid'); }
  }
  async remove(kind, key) { await unlink(await this.path(kind, key)).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
  async entries(kind) {
    const directory = await this.prepare(kind); const entries = [];
    for (const name of (await readdir(directory)).filter(name => /^[a-f0-9]{64}\.json$/.test(name)).sort()) {
      const path = join(directory, name); const info = await lstat(path);
      if (!info.isFile() || info.isSymbolicLink()) continue;
      try { await chmod(path, 0o600); entries.push({ path, value: JSON.parse(await readFile(path, 'utf8')) }); } catch { /* Corrupt records stay private for manual inspection. */ }
    }
    return entries;
  }
  async pruneCompleted() {
    const pendingPrompts = new Set((await this.entries('outbox')).map(({ value }) => value.prompt_key).filter(Boolean));
    for (const { path, value } of await this.entries('prompts')) {
      const key = JSON.stringify([value.session_id, value.native_turn_id]);
      if (value.completed_at && Date.now() - value.completed_at > 86400000 && !pendingPrompts.has(key)) {
        if (value.capture_kind === 'user_prompt') {
          if (Object.hasOwn(value, 'prompt')) {
            const { prompt, ...tombstone } = value;
            await this.write('prompts', key, { ...tombstone, prompt_hash: value.prompt_hash ?? digest(prompt), compacted_at: Date.now() });
          }
        } else await unlink(path);
      }
    }
  }
}
