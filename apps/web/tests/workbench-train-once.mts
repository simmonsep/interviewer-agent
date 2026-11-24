// F07Z-166: one explicit training submit prepares the task, and one confirm lands on the first
// question. No manual quick-prep click, no refresh, no second session.
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.TRAIN_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.TRAIN_OUT ?? join(root, '.private/f07z166/train-once'));
const python = process.env.TRAIN_PYTHON ?? 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
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
  return JSON.parse(result.stdout) as {status: string; session_id: string | null};
}

const primary = (page: Page) => page.locator('[data-testid="workbench-next-action"]');

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  assert.ok((await fetch(base + '/', {signal: AbortSignal.timeout(4000)})).ok, 'vite_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true});
  // Record the whole submit -> confirm -> first-question run so the operation count can be
  // checked against the video instead of only against the assertions.
  const context = await browser.newContext({viewport: {width: 1440, height: 1000},
    recordVideo: {dir: out, size: {width: 1440, height: 1000}}});
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(600);
  await page.locator('textarea[aria-label="求职意向或补充说明"]')
    .fill('帮我按这份 JD 练一场 AI 应用开发面试。');
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.getByLabel('确认方向（可选）').fill('AI 应用开发');
  await page.locator('textarea[aria-label="职位说明 JD"]').fill(
    '岗位职责：负责 Agent 工具链与后端接口开发，熟悉 Python、FastAPI、PostgreSQL。'
    + '要求：能独立完成服务端功能，理解权限与幂等。');
  await page.locator('[data-testid="workbench-train"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  const goalId = await page.evaluate(() => new URLSearchParams(location.search).get('goal'));
  assert.ok(goalId, 'goal_id_missing');

  const coordinated = await scoped(goalId!, 'scripts/career-web-support.py');
  assert.ok(coordinated, 'scoped coordinator returned nothing');

  // The workbench was never reloaded: the preparation was created by the backend and found by
  // the panel's own polling, which is the "submit once" contract.
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.includes('正在准备') === true;
  }, undefined, {timeout: 60000});
  assert.equal(await primary(page).isDisabled(), true);
  await page.screenshot({path: join(out, '01-preparing.png'), fullPage: true});
  mark('提交一次后无需刷新：工作台直接进入「正在准备…」');

  const planned = await scoped(goalId!, 'scripts/prep-web-support.py', 'plan');
  assert.equal(planned.status, 'WAITING_FOR_USER', JSON.stringify(planned));
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.trim() === '开始这场';
  }, undefined, {timeout: 60000});
  assert.equal(await page.locator('[data-testid="pipeline-plan"]').count(), 1);
  assert.equal(await page.locator('[data-testid="pipeline-confirm"]').count(), 0);
  const planText = await page.locator('[data-testid="pipeline-plan-text"]').innerText();
  assert.match(planText, /AI 应用 \/ Agent/);
  assert.match(await page.locator('[data-testid="pipeline-bank-source"]').innerText(), /角色默认题源/);
  await page.screenshot({path: join(out, '02-ready.png'), fullPage: true});
  mark('计划就绪：只有一个「开始这场」，角色和题源按实际生效值显示', {planText});

  await primary(page).click();
  const advanced = await scoped(goalId!, 'scripts/prep-web-support.py', 'advance');
  assert.equal(advanced.status, 'SUCCEEDED', JSON.stringify(advanced));
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 150000});
  assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'), advanced.session_id);
  const sessions = await page.evaluate(async id => {
    const response = await fetch(`/api/career-goals/${id}/interviews`, {credentials: 'same-origin'});
    return (await response.json()).items.length;
  }, goalId);
  assert.equal(sessions, 1);
  await page.screenshot({path: join(out, '03-first-question.png'), fullPage: true});
  mark('确认一次直达首题，只创建一场', {session: advanced.session_id, sessions});

  assert.deepEqual(pageErrors, [], JSON.stringify(pageErrors.slice(0, 2)));
  const video = await page.video()?.path();
  await context.close();
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    goalId, cases, pageErrors, video,
    clicks: {submit: 1, confirm: 1, reload: 0},
  }, null, 2));
  console.log(JSON.stringify({status: 'passed', cases: cases.length, out}));
} catch (error) {
  writeFileSync(join(out, 'result.json'), JSON.stringify({cases, pageErrors, error: String(error)}, null, 2));
  await browser?.close();
  console.error('FAIL', error);
  process.exit(1);
} finally {
  await browser?.close();
}
