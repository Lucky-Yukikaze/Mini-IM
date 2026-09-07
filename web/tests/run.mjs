import { build } from 'esbuild';
import { readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve, basename } from 'node:path';
import { spawnSync } from 'node:child_process';

const root = fileURLToPath(new URL('../', import.meta.url));
const tests = (await readdir(resolve(root, 'tests')))
  .filter(name => name.endsWith('.test.ts'))
  .map(name => resolve(root, 'tests', name));
const outdir = resolve(root, '../tmp/web-tests');
await build({
  entryPoints: tests, outdir, bundle: true, platform: 'node', format: 'cjs',
  target: 'node20', outExtension: { '.js': '.cjs' }, sourcemap: 'inline'
});
const result = spawnSync(process.execPath, [
  '--test', ...tests.map(path => resolve(outdir, basename(path).replace(/\.ts$/, '.cjs')))
], { stdio: 'inherit' });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
