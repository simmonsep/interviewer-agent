import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const runId = randomUUID();
const target = join(root, '.private', 'rebuild', runId);
mkdirSync(target, { recursive: true });
// Deliberately exclude .env, .git, private materials, bootstrap, and node_modules.
const inputs = ['package.json', 'package-lock.json', 'tsconfig.json',
  'packages/contracts/package.json', 'packages/contracts/src/index.ts', 'packages/runtime-pi/package.json',
  'packages/runtime-pi/sdk-contract.ts', 'scripts/check-pi-imports.mjs',
  'packages/runtime-pi/src/adapter.ts', 'packages/runtime-pi/src/scoped-store.ts', 'packages/runtime-pi/src/tools.ts',
  'packages/runtime-pi/test-support/scripted-stream.ts', 'packages/runtime-pi/tests/runtime.test.ts',
  'fixtures/pm0-03/bundle.json', 'scripts/no-network.mjs', 'scripts/protocol-check.mjs',
  'packages/runtime-pi/src/budget.ts', 'packages/runtime-pi/src/checkpoint.ts', 'packages/runtime-pi/src/trace.ts',
  'packages/runtime-pi/src/lifecycle.ts', 'packages/runtime-pi/tests/lifecycle.test.ts', 'scripts/lifecycle-check.mjs',
  'packages/contracts/src/catalog.ts', 'packages/runtime-pi/src/catalog.ts', 'packages/runtime-pi/tests/catalog.test.ts',
  'scripts/fixture-check.mjs', 'fixtures/pm0-05/sources.json', 'fixtures/pm0-05/questions.json',
  'fixtures/pm0-05/role-package.json', 'fixtures/pm0-05/resumes.json', 'fixtures/pm0-05/jds.json',
  'fixtures/pm0-05/seeds.json', 'fixtures/pm0-05/answers.json', 'fixtures/pm0-05/trajectories.json',
  'evals/pm0-05/labels.json', 'packages/runtime-pi/src/deepseek-live.ts',
  'packages/runtime-pi/tests/live-offline.test.ts', 'scripts/live-offline-check.mjs', 'scripts/pm006-live.ts',
  'prompts/assessment-baseline-v1.md', 'configs/pm006-deepseek-flash.json',
  'packages/storage/package.json', 'packages/storage/src/migrate.ts', 'packages/storage/src/repository.ts',
  'packages/storage/migrations/001_better_auth.sql', 'packages/storage/migrations/002_training_targets.sql', 'packages/storage/migrations/003_target_deletion_state.sql',
  'apps/api/package.json', 'apps/api/src/auth.ts', 'apps/api/src/server.ts', 'apps/api/src/local-config.ts',
  'apps/api/src/main.ts', 'apps/api/src/generate-auth-migration.ts', 'apps/api/tests/storage.test.ts',
  'scripts/db-migrate.ts', 'scripts/storage-check.mjs', 'scripts/postgres-local.mjs', 'scripts/database-local.mjs',
  'scripts/setup-postgres.mjs', 'manifests/postgres-PI-07.json',
  'packages/storage/src/operations.ts', 'packages/storage/migrations/004_operations_outbox.sql',
  'apps/worker/package.json', 'apps/worker/src/queue.ts', 'apps/worker/src/main.ts', 'apps/worker/tests/queue.test.ts',
  'scripts/queue-check.mjs', 'scripts/redis-local.mjs', 'manifests/redis-PI-08.json',
  'packages/contracts/src/practice.ts', 'packages/domain/package.json', 'packages/domain/src/practice.ts',
  'packages/storage/src/training.ts', 'packages/storage/migrations/005_practice_sessions.sql',
  'apps/api/src/training-routes.ts', 'apps/api/src/web.ts', 'apps/worker/src/assessment.ts',
  'apps/worker/tests/training-support.ts', 'apps/worker/tests/training.test.ts', 'scripts/training-check.mjs',
  'configs/pi09-deepseek-flash.json', 'apps/web/package.json', 'apps/web/tsconfig.json', 'apps/web/vite.config.ts',
  'apps/web/index.html', 'apps/web/src/vite-env.d.ts', 'apps/web/src/main.tsx', 'apps/web/src/api.ts',
  'apps/web/src/App.tsx', 'apps/web/src/style.css', 'apps/web/tests/flow.ts', 'scripts/web-check.mjs', 'apps/web/tests/live-flow.ts', 'scripts/pi09-live-web.mjs', 'apps/worker/tests/api-process.ts'];
const hash = path => createHash('sha256').update(readFileSync(path)).digest('hex');
const before = hash(join(root, 'package-lock.json'));
for (const name of inputs) {
  mkdirSync(dirname(join(target, name)), { recursive: true });
  cpSync(join(root, name), join(target, name), { errorOnExist: true, force: false });
}
const npmCli = join(root, '.bootstrap/node-v24.19.0-win-x64/node_modules/npm/bin/npm-cli.js');
const commands = [];
function run(args, log) {
  const result = spawnSync(process.execPath, [npmCli, ...args], {
    cwd: target, encoding: 'utf8', timeout: 120000,
    env: { ...process.env, PATH: dirname(process.execPath) + ';' + process.env.PATH },
  });
  writeFileSync(join(target, log), (result.stdout ?? '') + (result.stderr ?? ''));
  commands.push({ args, exitCode: result.status, error: result.error?.code ?? null, log });
  return result.status === 0 && !result.error;
}
let passed = run(['ci', '--offline', '--ignore-scripts', '--no-audit', '--no-fund',
  '--cache', join(root, '.bootstrap/npm-cache'), '--registry=https://registry.npmjs.org'], 'ci.log');
if (passed) passed = run(['run', 'check:sdk'], 'imports.log');
if (passed) passed = run(['run', 'typecheck'], 'typecheck.log');
if (passed) passed = run(['run', 'web:build'], 'web-build.log');
if (passed) passed = run(['run', 'check:protocol'], 'protocol.log');
if (passed) passed = run(['run', 'check:lifecycle'], 'lifecycle.log');
if (passed) passed = run(['run', 'check:fixtures'], 'fixtures.log');
if (passed) passed = run(['run', 'check:live-offline'], 'live-offline.log');
if (passed) passed = run(['ls', '--all', '--json'], 'dependency-tree.json');
let packageIdentity = [];
if (passed) {
  const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'));
  packageIdentity = Object.entries(lock.packages).filter(([path, entry]) => path && !entry.link && path.includes('node_modules/')).map(([path, entry]) => {
    const sourcePath = join(root, path, 'package.json');
    const rebuiltPath = join(target, path, 'package.json');
    const matches = (values, value) => !values || (!values.includes('!' + value) && (values.every(v => v.startsWith('!')) || values.includes(value)));
    const incompatible = !matches(entry.os, process.platform) || !matches(entry.cpu, process.arch);
    if (!existsSync(sourcePath) || !existsSync(rebuiltPath)) {
      const expectedAbsent = entry.optional === true && incompatible && !existsSync(sourcePath) && !existsSync(rebuiltPath);
      return { path, version: entry.version, equal: expectedAbsent, skipped: expectedAbsent, reason: expectedAbsent ? 'optional package for another OS/CPU' : 'unexpected missing package manifest' };
    }
    const sourceHash = hash(sourcePath);
    const rebuiltHash = hash(rebuiltPath);
    return { path, version: entry.version, equal: sourceHash === rebuiltHash, skipped: false, packageJsonSha256: sourceHash };
  });
  passed = packageIdentity.every(x => x.equal);
}
const lockUnchanged = before === hash(join(root, 'package-lock.json')) && before === hash(join(target, 'package-lock.json'));
passed &&= lockUnchanged;
const receipt = { runId, checkedAt: new Date().toISOString(), status: passed ? 'passed' : 'failed',
  scope: 'same Windows host, initially empty independent install directory, populated shared npm cache, offline npm ci, lifecycle scripts disabled; not a clean-cache or cross-platform install',
  node: process.version, platform: process.platform, arch: process.arch,
  lockSha256: before, lockUnchanged, commands, packageIdentity, modelCalls: 0 };
writeFileSync(join(target, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
console.log(JSON.stringify({ ...receipt, packageIdentity: { compared: packageIdentity.filter(x => !x.skipped).length, platformOptionalSkipped: packageIdentity.filter(x => x.skipped).length }, receipt: join(target, 'receipt.json') }, null, 2));
process.exitCode = passed ? 0 : 1;
