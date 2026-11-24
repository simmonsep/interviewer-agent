/** PM0-01 inventory and workspace checks. No provider, network or dependency installation. */
import { createHash, randomUUID } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { arch, release, totalmem } from 'node:os';
import { dirname, isAbsolute, relative, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const manifest = JSON.parse(readFileSync(resolve(root, 'manifests/toolchain.json'), 'utf8'));
const packageJson = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
const checks = [];

function check(name, passed, detail, required = true) {
  checks.push({ name, status: passed ? 'passed' : required ? 'failed' : 'deferred', required, detail });
}

function probe(command, args) {
  const result = spawnSync(command, args, { cwd: root, encoding: 'utf8', timeout: 10000, windowsHide: true });
  return {
    exit_code: result.status,
    error_code: result.error?.code ?? null,
    output: result.stdout?.trim() ?? '',
    stderr: result.stderr?.trim() ?? '',
  };
}

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

function localPath(value) {
  const path = resolve(root, value);
  const remainder = relative(root, path);
  if (isAbsolute(remainder) || remainder === '..' || remainder.startsWith('..\\') || remainder.startsWith('../')) {
    throw new Error('Toolchain manifest path is outside this workspace');
  }
  return path;
}

const nodePath = localPath(manifest.node_relative_path);
check('node.version', process.versions.node === manifest.node_version && process.versions.node === packageJson.engines.node, process.versions.node);
check('node.project_owned', realpathSync(process.execPath).toLowerCase() === realpathSync(nodePath).toLowerCase(), relative(root, process.execPath));
check('platform', process.platform === 'win32' && arch() === 'x64', `${process.platform}/${arch()}`);

const npm = probe(process.execPath, [localPath(manifest.npm_relative_path), '--version']);
check('npm.version', npm.exit_code === 0 && npm.output === manifest.npm_version && npm.output === packageJson.engines.npm, npm);
for (const [label, pathKey, hashKey] of [
  ['toolchain.archive', 'archive_relative_path', 'archive_sha256'],
  ['toolchain.checksum_file', 'checksum_relative_path', 'checksum_sha256'],
]) {
  const path = localPath(manifest[pathKey]);
  const actual = existsSync(path) ? sha256(path) : null;
  check(label, actual === manifest[hashKey], { path: manifest[pathKey], sha256: actual });
}
const checksumEntry = readFileSync(localPath(manifest.checksum_relative_path), 'utf8')
  .split(/\r?\n/).find(line => line.endsWith('  node-v24.19.0-win-x64.zip'));
check('toolchain.official_checksum_entry', checksumEntry?.split(/\s+/)[0] === manifest.archive_sha256, checksumEntry ?? null);

const git = probe('git', ['--version']);
check('git.available', git.exit_code === 0, git);
const gitRoot = probe('git', ['rev-parse', '--show-toplevel']);
check('git.independent_root', gitRoot.exit_code === 0 && resolve(gitRoot.output).toLowerCase() === root.toLowerCase(), gitRoot);
check('workspace.private_root', packageJson.private === true, packageJson.private);
const contract = JSON.parse(readFileSync(resolve(root, 'packages/contracts/package.json'), 'utf8'));
check('workspace.contracts_manifest', contract.private === true && contract.name === '@pi-interview/contracts', contract.name);

for (const path of ['.env', '.env.local', '.env.production', '.private/key.json', '.bootstrap/file.zip', 'fixtures/private/resume.pdf', 'evals/private-labels/answers.json', 'packages/contracts/node_modules/private-package/index.js']) {
  const result = probe('git', ['check-ignore', '--no-index', '--quiet', '--', path]);
  check(`git.ignore:${path}`, result.exit_code === 0, result.error_code);
}
const example = probe('git', ['check-ignore', '--no-index', '--quiet', '--', '.env.example']);
check('git.example_not_ignored', example.exit_code === 1, example.error_code);

const docker = probe('docker', ['version', '--format', '{{json .}}']);
check('docker.client_and_daemon', docker.exit_code === 0, docker, false);
const passed = checks.every(item => !item.required || item.status === 'passed');
const report = {
  schema_version: 1,
  checked_at: new Date().toISOString(),
  stage: 'PM0-01',
  status: passed ? 'passed_with_deferred_services' : 'failed',
  scope: 'Toolchain, npm workspace and Git ignore checks; not Pi SDK or application acceptance',
  platform: { platform: process.platform, arch: arch(), os_release: release(), total_memory_bytes: totalmem() },
  checks,
  readiness: { toolchain: passed, pi_sdk: false, postgres: false, redis: false, docker: docker.exit_code === 0, product: false },
  external_model_calls: 0,
  network_requests_by_this_script: 0,
};
const directory = resolve(root, '.private/doctor');
mkdirSync(directory, { recursive: true });
const target = resolve(directory, `${randomUUID()}.json`);
writeFileSync(target, `${JSON.stringify(report, null, 2)}\n`, { encoding: 'utf8', flag: 'wx' });
console.log(JSON.stringify({ ...report, report_path: relative(root, target) }, null, 2));
process.exitCode = passed ? 0 : 1;
