import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { randomUUID, createHash } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const runId = randomUUID();
const output = join(root, '.private/pm003', runId);
mkdirSync(output, { recursive: true });
const startedAt = new Date().toISOString();
const guardProbe = spawnSync(process.execPath, ['--import', './scripts/no-network.mjs', '--input-type=module', '-e', 'try { await fetch("https://example.invalid"); } catch {}'], {
  cwd: root, encoding: 'utf8', timeout: 5000,
  env: { ...process.env, PI_PM003_AUDIT_PATH: join(output, 'network-negative-control.json') },
});
const child = spawnSync(process.execPath, ['--import', './scripts/no-network.mjs', '--test', 'packages/runtime-pi/tests/runtime.test.ts'], {
  cwd: root, encoding: 'utf8', timeout: 30000,
  env: { ...process.env, PI_PM003_AUDIT_PATH: join(output, 'network.json'), PI_PM003_RECEIPT_PATH: join(output, 'cases.json') },
});
writeFileSync(join(output, 'tests.log'), (child.stdout ?? '') + (child.stderr ?? ''));
let cases, network, guardAudit;
try { cases = JSON.parse(readFileSync(join(output, 'cases.json'), 'utf8')); network = JSON.parse(readFileSync(join(output, 'network.json'), 'utf8')); guardAudit = JSON.parse(readFileSync(join(output, 'network-negative-control.json'), 'utf8')); }
catch { /* A missing receipt is a failed run, not success with zero cases. */ }
const guardVerified = guardProbe.status === 1 && !guardProbe.error && guardAudit?.attempts === 1 && guardAudit.observedEntryPoints[0] === 'global.fetch';
const categories = cases?.map(item => item.name.slice(0, 3));
const coverageVerified = JSON.stringify(categories) === JSON.stringify(Array.from({length:16}, (_,i) => 'C' + String(i+1).padStart(2,'0')));
const passed = coverageVerified && guardVerified && child.status === 0 && !child.error && cases?.length > 0 && cases.every(x => x.status === 'passed') && network?.attempts === 0;
const inputs = ['packages/runtime-pi/src/budget.ts', 'packages/runtime-pi/src/trace.ts', 'packages/runtime-pi/src/checkpoint.ts', 'packages/runtime-pi/src/lifecycle.ts', 'package-lock.json', 'tsconfig.json', 'packages/contracts/src/index.ts', 'packages/runtime-pi/src/adapter.ts',
  'packages/runtime-pi/src/tools.ts', 'packages/runtime-pi/src/scoped-store.ts', 'packages/runtime-pi/test-support/scripted-stream.ts',
  'packages/runtime-pi/tests/runtime.test.ts', 'fixtures/pm0-03/bundle.json', 'scripts/no-network.mjs', 'scripts/protocol-check.mjs'];
const receipt = { step: 'PM0-03 / PI-03', runId, startedAt, completedAt: new Date().toISOString(),
  status: passed ? 'passed' : 'failed', node: process.version, exitCode: child.status, childError: child.error?.code ?? null,
  coverageVerified, cases: cases ?? null, networkAudit: network ?? null, networkGuardNegativeControl: { passed: guardVerified, exitCode: guardProbe.status, audit: guardAudit ?? null, note: 'one deliberate fetch entry rejected before I/O in a separate process; not a live request' },
  sourceSha256: Object.fromEntries(inputs.map(path => [path, createHash('sha256').update(readFileSync(join(root, path))).digest('hex')])),
  limits: ['synthetic fixture only; no semantic accuracy gold or real model', 'basic development ceilings only; PM0-04 lifecycle/batch/trace matrix not accepted', 'no SQL transaction, authentication HTTP API, durable learning memory or deployment'] };
writeFileSync(join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n');
console.log((child.stdout ?? '') + (child.stderr ?? ''));
console.log(JSON.stringify({ status: receipt.status, caseCount: cases?.length ?? null, runId, receipt: join(output, 'receipt.json'), networkAudit: network, networkGuardNegativeControlPassed: guardVerified }, null, 2));
process.exitCode = passed ? 0 : 1;
