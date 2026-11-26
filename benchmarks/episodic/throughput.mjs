#!/usr/bin/env node
// Embedding throughput per model on a sample of exchange texts (CPU).
//   node benchmarks/episodic/throughput.mjs <sample.json> <model> [dtype] [pooling]
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
const here = path.dirname(fileURLToPath(import.meta.url));
const { pipeline, env } = await import(path.resolve(here, '../../episodic-memory/node_modules/@huggingface/transformers/dist/transformers.node.mjs'));
env.cacheDir = process.env.EPISODIC_MEMORY_MODEL_CACHE || path.join(process.env.HOME, '.config/superpowers/models');
const [sampleFile, model, dtype = 'q8', pooling = 'mean'] = process.argv.slice(2);
const texts = JSON.parse(fs.readFileSync(sampleFile, 'utf8')).map((x) => x.text);
let t = Date.now();
const pipe = await pipeline('feature-extraction', model, { dtype });
const load = Date.now() - t;
t = Date.now();
let dims = 0;
for (const x of texts) {
  const o = await pipe(x, { pooling, normalize: true });
  dims = o.dims.at(-1);
}
const ms = Date.now() - t;
console.log(`${model} ${dtype} ${pooling}: load ${load} ms, ${(ms / texts.length).toFixed(1)} ms/exchange, dims ${dims}, 72k exchanges ≈ ${(ms / texts.length * 72000 / 60000).toFixed(0)} min`);
