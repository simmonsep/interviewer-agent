// F07Z-166: a preparation left by a previous visit is recoverable by goal, not by a local id.
// The plan is read back after a reload, confirmation opens exactly one session, and another
// reload offers to reopen that same session.
//
//   node --experimental-strip-types apps/web/tests/prep-recovery.mts
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.PREP_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.PREP_OUT ?? join(root, '.private/f07z166/prep-recovery'));
const python = process.env.PREP_PYTHON ?? 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
mkdirSync(out, {recursive: true});
const runId = randomUUID();
const execute = promisify(execFile);
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

async function prepWorker(goal: string, mode: 'plan' | 'advance') {
  const result = await execute(python, ['-X', 'utf8', 'scripts/prep-web-support.py', '--goal', goal,
    '--run-id', runId, '--watch', '120', '--mode', mode],
  {cwd: root, windowsHide: true, timeout: 150000,
   env: {...process.env, PYTHONPATH: 'backend/src'}});
  return JSON.parse(result.stdout) as {status: string; has_plan: boolean; session_id: string | null};
}

async function openWorkbench(page: Page, goalId: string) {
  await page.goto(`${base}/?shell=desktop&goal=${goalId}`, {waitUntil: 'domcontentloaded'});
  await page.locator('.desktop-shell[data-pane]').waitFor({timeout: 30000});
}

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  assert.ok((await fetch(base + '/', {signal: AbortSignal.timeout(4000)})).ok, 'vite_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(600);
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('想在上海找 AI 应用开发实习，做过 Agent 工具调用。');
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.getByLabel('确认方向（可选）').fill('AI 应用开发实习');
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  const goalId = await page.evaluate(() => new URLSearchParams(location.search).get('goal'));
  assert.ok(goalId, 'goal_id_missing');

  const setup = await page.evaluate(async id => {
    const source = await fetch(`/api/career-goals/${id}/materials/sources`, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: 1, expected_revision: 0, name: '项目.md', format: 'markdown',
        text: '项目经历\n负责 Agent 工具调用，完成参数校验与失败回退。'})});
    if (!source.ok) return {error: await source.text()};
    const prep = await fetch(`/api/career-goals/${id}/interview-preparations`, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: 1, role: 'ai-application', mode: 'interview', max_questions: 2})});
    return {source: source.status, prep: prep.status, body: await prep.json()};
  }, goalId);
  assert.equal(setup.source, 201, JSON.stringify(setup).slice(0, 200));
  assert.equal(setup.prep, 202, JSON.stringify(setup).slice(0, 200));
  mark('准备任务已由真实接口创建', {goal: goalId, prep: setup.body.id});

  const planned = await prepWorker(goalId!, 'plan');
  assert.equal(planned.status, 'WAITING_FOR_USER', JSON.stringify(planned));

  // Reload without any local state: the plan must come back from the server.
  await openWorkbench(page, goalId!);
  const plan = page.locator('[data-testid="pipeline-plan"]');
  await plan.waitFor({timeout: 60000});
  const planText = await page.locator('[data-testid="pipeline-plan-text"]').innerText();
  assert.match(planText, /AI 应用 \/ Agent/);
  assert.equal((await page.locator('[data-testid="pipeline-plan-sha"]').innerText()).length, 12);
  mark('刷新后按目标读回待确认计划', {plan: planText.replace(/\s+/g, ' ').slice(0, 80)});

  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.trim() === '开始这场';
  }, undefined, {timeout: 60000});
  await page.locator('[data-testid="workbench-next-action"]').click();
  const advanced = await prepWorker(goalId!, 'advance');
  assert.equal(advanced.status, 'SUCCEEDED', JSON.stringify(advanced));
  assert.ok(advanced.session_id, 'session_id_missing');
  await page.waitForFunction(() => !document.querySelector('[data-testid="pipeline-plan"]'),
    undefined, {timeout: 60000});

  // Second reload: the confirmed preparation offers the same session, not a new one.
  await openWorkbench(page, goalId!);
  const recovered = page.locator('[data-testid="pipeline-recovered-session"]');
  await recovered.waitFor({timeout: 60000});
  const sessions = await page.evaluate(async id =>
    (await (await fetch(`/api/career-goals/${id}/interviews`, {credentials: 'same-origin'})).json()).items.length, goalId);
  assert.equal(sessions, 1, 'confirmation must create exactly one session');
  await page.locator('[data-testid="pipeline-open-recovered"]').click();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 120000});
  const openedId = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.equal(openedId, advanced.session_id);
  await page.screenshot({path: join(out, '01-recovered-session.png'), fullPage: true});
  mark('再次刷新：提示可打开同一场面试，不重复开场', {session: openedId});

  assert.deepEqual(pageErrors, [], JSON.stringify(pageErrors.slice(0, 2)));
  writeFileSync(join(out, 'result.json'), JSON.stringify({cases, pageErrors, goalId}, null, 2));
  console.log(JSON.stringify({status: 'passed', cases: cases.length, out}));
} catch (error) {
  writeFileSync(join(out, 'result.json'), JSON.stringify({cases, pageErrors, error: String(error)}, null, 2));
  await browser?.close();
  console.error('FAIL', error);
  process.exit(1);
} finally {
  await browser?.close();
}
