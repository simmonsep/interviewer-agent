// F07Z-166 step 1: from the workbench's current JD, prepare and confirm one session without the
// resume-rewrite phase, then land on its first question.
//
//   node --experimental-strip-types apps/web/tests/workbench-jd-prep.mts
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.PREP_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.PREP_OUT ?? join(root, '.private/f07z166/jd-prep'));
const python = process.env.PREP_PYTHON ?? 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
mkdirSync(out, {recursive: true});
const runId = randomUUID();
const execute = promisify(execFile);
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

async function careerWorker(goal: string) {
  const result = await execute(python, ['-X', 'utf8', 'scripts/career-web-support.py', '--goal', goal,
    '--run-id', runId],
  {cwd: root, windowsHide: true, timeout: 170000,
   env: {...process.env, PYTHONPATH: 'backend/src'}});
  return JSON.parse(result.stdout) as {status: string};
}

async function prepWorker(goal: string, mode: 'plan' | 'advance') {
  const result = await execute(python, ['-X', 'utf8', 'scripts/prep-web-support.py', '--goal', goal,
    '--run-id', runId, '--watch', '120', '--mode', mode],
  {cwd: root, windowsHide: true, timeout: 150000,
   env: {...process.env, PYTHONPATH: 'backend/src'}});
  return JSON.parse(result.stdout) as {status: string; has_plan: boolean; session_id: string | null};
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

  // The quick path waits for the coordinator, so advance it once through the scoped worker first.
  const settled = await careerWorker(goalId!);
  assert.notEqual(settled.status, 'PENDING', JSON.stringify(settled));
  assert.notEqual(settled.status, 'RUNNING', JSON.stringify(settled));
  // Start the scoped prep watcher first so it freezes a baseline before the UI creates the prep.
  const planJob = prepWorker(goalId!, 'plan');
  await delay(1500);
  const quick = page.locator('[data-testid="pipeline-quick-prep"]');
  await quick.waitFor({timeout: 30000});
  await quick.click();
  const planned = await planJob;
  assert.equal(planned.status, 'WAITING_FOR_USER', JSON.stringify(planned));
  await page.locator('[data-testid="pipeline-plan"]').waitFor({timeout: 60000});
  assert.equal(await page.locator('[data-testid="pipeline-quick-prep"]').isDisabled(), true);
  const text = await page.locator('[data-testid="pipeline-plan-text"]').innerText();
  assert.match(text, /AI 应用 \/ Agent/);
  mark('一句话目标 + 当前方向：一键准备出待确认计划（未走简历改写）', {
    plan: text.replace(/\s+/g, ' ').slice(0, 90),
  });

  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.trim() === '开始这场';
  }, undefined, {timeout: 60000});
  await page.locator('[data-testid="workbench-next-action"]').click();
  const advanced = await prepWorker(goalId!, 'advance');
  assert.equal(advanced.status, 'SUCCEEDED', JSON.stringify(advanced));
  assert.ok(advanced.session_id, 'session_id_missing');
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 150000});
  const opened = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.equal(opened, advanced.session_id);
  const sessions = await page.evaluate(async id =>
    (await (await fetch(`/api/career-goals/${id}/interviews`, {credentials: 'same-origin'})).json()).items.length, goalId);
  assert.equal(sessions, 1, 'one confirmation must create exactly one session');
  await page.screenshot({path: join(out, '01-first-question.png'), fullPage: true});
  mark('确认后直接进入该场首题，只创建一场', {session: opened});

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
