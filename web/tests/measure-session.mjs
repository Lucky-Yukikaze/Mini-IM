// Measures the actual Pinia store in fresh Node processes; does not measure Qt or DOM rendering.
import { build } from 'esbuild';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { cpus, platform, release } from 'node:os';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { spawnSync, execFileSync } from 'node:child_process';
const root = fileURLToPath(new URL('../../', import.meta.url));
const counts = process.argv.length > 2 ? process.argv.slice(2).map(Number) : [100, 1000, 10000];
if (!counts.length || counts.some(count => !Number.isInteger(count) || count < 0 || count > 10000)) {
  throw new Error('usage: node web/tests/measure-session.mjs [message-count 0..10000 ...]');
}
const output = resolve(root, 'tmp/session-measurement', new Date().toISOString().replaceAll(':', '-'));
await mkdir(output, { recursive: true });
const driver = resolve(output, 'driver.cjs');
await build({ entryPoints: [resolve(root, 'web/tests/session-benchmark.ts')], outfile: driver,
  bundle: true, platform: 'node', format: 'cjs', target: 'node20' });
const hashes = {};
for (const file of ['web/src/store/session.ts', 'web/tests/session-benchmark.ts', 'web/tests/measure-session.mjs']) {
  hashes[file] = createHash('sha256').update(await readFile(resolve(root, file))).digest('hex');
}
await writeFile(resolve(output, 'environment.json'), JSON.stringify({ counts, batchSize: 100, conversations: 1,
  bodyBytes: 128, node: process.version, platform: platform(), release: release(), cpu: cpus()[0]?.model,
  hashes, driverSha256: createHash('sha256').update(await readFile(driver)).digest('hex'),
  gitCommit: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(),
  gitStatus: execFileSync('git', ['status', '--short'], { cwd: root, encoding: 'utf8' }) }, null, 2));
const results = [];
for (const count of counts) {
  const run = spawnSync(process.execPath, ['--expose-gc', driver, String(count)], {
    cwd: root, encoding: 'utf8', timeout: 300000, maxBuffer: 8 * 1024 * 1024 });
  if (run.error || run.status !== 0) {
    await writeFile(resolve(output, 'error.json'), JSON.stringify({ count, status: run.status,
      error: String(run.error ?? ''), stdout: run.stdout, stderr: run.stderr }, null, 2));
    throw run.error ?? new Error(run.stderr);
  }
  results.push(JSON.parse(run.stdout));
  await writeFile(resolve(output, 'results.json'), JSON.stringify(results, null, 2));
  const { batches, ...summary } = results.at(-1);
  console.log(JSON.stringify(summary));
}
console.log('SESSION MEASUREMENT PASSED ' + output);
