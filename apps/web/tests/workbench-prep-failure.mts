// F07Z-166: a real worker failure (planning_crashed) must be visible and recoverable, without
// creating a session, and the retry must produce exactly one session that reaches the first
// question.
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.FAIL_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.FAIL_OUT ?? join(root, '.private/f07z166/prep-failure'));
const python = process.env.FAIL_PYTHON ?? 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
mkdirSync(out, {recursive: true});
const runId = randomUUID();
const execute = promisify(execFile);
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

async function scoped(goal: string, script: string, mode?: string) {
  const args = ['-X', 'utf8', script, '--goal', goal, '--run-id', runId];
  if (mode) args.push('--mode', mode);
  if (script.includes('prep-web')) args.push('--watch', '120');
  const result = await execute(python, args, {cwd: root, windowsHide: true, timeout: 170000,
    env: {...process.env, PYTHONPATH: 'backend/src'}});
  return JSON.parse(result.stdout) as {status: string; session_id: string | null; error_code: string | null};
}

const primary = (page: Page) => page.locator('[data-testid="workbench-next-action"]');

async function waitLabel(page: Page, text: string) {
  await page.waitForFunction(value => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.includes(value) === true;
  }, text, {timeout: 90000});
}

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  assert.ok((await fetch(base + '/', {signal: AbortSignal.timeout(4000)})).ok, 'vite_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(600);
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('按这份 JD 练一场 AI 应用面试，先看失败时怎么恢复。');
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.getByLabel('确认方向（可选）').fill('AI 应用开发');
  await page.locator('[data-testid="workbench-train"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  const goalId = await page.evaluate(() => new URLSearchParams(location.search).get('goal'));
  assert.ok(goalId, 'goal_id_missing');
  await scoped(goalId!, 'scripts/career-web-support.py');
  await waitLabel(page, '正在准备');

  const failed = await scoped(goalId!, 'scripts/prep-web-support.py', 'fail');
  assert.equal(failed.status, 'FAILED', JSON.stringify(failed));
  assert.equal(failed.error_code, 'planning_crashed', JSON.stringify(failed));
  assert.equal(failed.session_id, null);

  await waitLabel(page, '重新准备');
  const sessionsAfterFailure = await page.evaluate(async id =>
    (await (await fetch(`/api/career-goals/${id}/interviews`, {credentials: 'same-origin'})).json()).items.length, goalId);
  assert.equal(sessionsAfterFailure, 0);
  assert.equal(await page.locator('.d-pipeline-cta button.primary').count(), 1);
  await page.screenshot({path: join(out, '01-failed.png'), fullPage: true});
  mark('真实 worker 失败：任务 FAILED(planning_crashed)、未开场、主操作是「重新准备」', {code: failed.error_code});

  await primary(page).click();
  const planned = await scoped(goalId!, 'scripts/prep-web-support.py', 'plan');
  assert.equal(planned.status, 'WAITING_FOR_USER', JSON.stringify(planned));
  await waitLabel(page, '开始这场');
  await page.screenshot({path: join(out, '02-retry-ready.png'), fullPage: true});
  mark('重试生成新计划：旧任务未被复用，确认前仍没有场次');

  await primary(page).click();
  const advanced = await scoped(goalId!, 'scripts/prep-web-support.py', 'advance');
  assert.equal(advanced.status, 'SUCCEEDED', JSON.stringify(advanced));
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 150000});
  const sessions = await page.evaluate(async id =>
    (await (await fetch(`/api/career-goals/${id}/interviews`, {credentials: 'same-origin'})).json()).items.length, goalId);
  assert.equal(sessions, 1);
  await page.screenshot({path: join(out, '03-recovered.png'), fullPage: true});
  mark('恢复后只创建一场并直达首题', {session: advanced.session_id});

  assert.deepEqual(pageErrors, [], JSON.stringify(pageErrors.slice(0, 2)));
  writeFileSync(join(out, 'result.json'), JSON.stringify({goalId, cases, pageErrors}, null, 2));
  console.log(JSON.stringify({status: 'passed', cases: cases.length, out}));
} catch (error) {
  writeFileSync(join(out, 'result.json'), JSON.stringify({cases, pageErrors, error: String(error)}, null, 2));
  await browser?.close();
  console.error('FAIL', error);
  process.exit(1);
} finally {
  await browser?.close();
}
