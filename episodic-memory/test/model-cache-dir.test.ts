import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

// transformers.js caches the downloaded model in `.cache` inside its own
// package directory unless told otherwise. Every `npm install` deletes that,
// and a service under systemd `ProtectSystem=strict` cannot write it back, so
// search and sync failed on every call with `ENOENT: mkdir
// '.../node_modules/@huggingface/transformers/.cache'`. The cache must live
// with the index instead.
const env: Record<string, unknown> = {};
vi.mock('@huggingface/transformers', () => ({
  env,
  pipeline: async () => async () => ({ data: new Float32Array(384) }),
}));

import { getModelCacheDir } from '../src/paths.js';
import { initEmbeddings } from '../src/embeddings.js';

describe('embedding model cache directory', () => {
  let dir: string;
  const saved = { ...process.env };

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'episodic-model-cache-'));
    process.env.EPISODIC_MEMORY_CONFIG_DIR = dir;
    delete process.env.EPISODIC_MEMORY_MODEL_CACHE;
  });

  afterEach(() => {
    process.env = { ...saved };
    fs.rmSync(dir, { recursive: true, force: true });
  });

  it('defaults to <superpowers dir>/models and creates it', () => {
    const cache = getModelCacheDir();
    expect(cache).toBe(path.join(dir, 'models'));
    expect(fs.statSync(cache).isDirectory()).toBe(true);
  });

  it('EPISODIC_MEMORY_MODEL_CACHE overrides it', () => {
    const other = path.join(dir, 'elsewhere');
    process.env.EPISODIC_MEMORY_MODEL_CACHE = other;
    expect(getModelCacheDir()).toBe(other);
  });

  it('initEmbeddings points transformers.js at it, never its package directory', async () => {
    await initEmbeddings();
    expect(env.cacheDir).toBe(path.join(dir, 'models'));
    expect(String(env.cacheDir)).not.toContain('node_modules');
  });
});
