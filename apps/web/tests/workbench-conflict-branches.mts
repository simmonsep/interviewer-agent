// F07Z-166: the conflict branches users can actually hit — an orphaned lease, a second
// preparation while one is active, a second session while one is active, and a plan that went
// stale after the goal version moved.
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.CONFLICT_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.CONFLICT_OUT ?? join(root, '.private/f07z166/conflict-branches'));
const python = process.env.CONFLICT_PYTHON ?? 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
mkdirSync(out, {recursive: true});
const runId = randomUUID();
const execute = promisify(execFile);
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };
const JD = '岗位职责：负责 Agent 工具链与 Python 后端开发。';

async function scoped(goal: string, script: string, mode?: string) {
  const args = ['-X', 'utf8', script, '--goal', goal, '--run-id', runId];
  if (mode) args.push('--mode', mode);
  if (script.includes('prep-web')) args.push('--watch', '120');
  const result = await execute(python, args, {cwd: root, windowsHide: true, timeout: 170000,
    env: {...process.env, PYTHONPATH: 'backend/src'}});
  return JSON.parse(result.stdout) as {status?: string; session_id?: string | null; orphaned?: string | null};
}

async function api<T>(page: Page, method: string, path: string, body?: unknown): Promise<T> {
  return page.evaluate(async ([verb, url, data]) => {
    const response = await fetch(url as string, {
      method: verb as string, credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: data === undefined ? undefined : JSON.stringify(data)});
    const text = await response.text();
    if (!response.ok) throw new Error(`${response.status} ${text.slice(0, 200)}`);
    return JSON.parse(text);
  }, [method, path, body]) as Promise<T>;
}

async function refusal(page: Page, method: string, path: string, body?: unknown) {
  return page.evaluate(async ([verb, url, data]) => {
    const response = await fetch(url as string, {
      method: verb as string, credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: data === undefined ? undefined : JSON.stringify(data)});
    const text = await response.text();
    return {status: response.status, code: JSON.parse(text).code};
  }, [method, path, body]) as Promise<{status: number; code: string}>;
}

const primary = (page: Page) => page.locator('[data-testid="workbench-next-action"]');

async function waitPrimary(page: Page, text: string, timeout = 60000) {
  await page.waitForFunction(value => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.includes(value) === true;
  }, text, {timeout});
}

async function openWorkbench(page: Page, goalId: string) {
  await page.goto(`${base}/?shell=desktop&goal=${goalId}`, {waitUntil: 'domcontentloaded'});
  await page.locator('.desktop-shell[data-pane]').waitFor({timeout: 30000});
}

async function createTrainingGoal(page: Page): Promise<string> {
  // The entry path itself is covered by workbench-train-once; here the goal is seeded so the
  // conflict branches are the subject.
  const created = await api<{id: string}>(page, 'POST', '/api/career-goals/', {
    intent: 'training', request: 'training', message: '按这份 JD 练一场 AI 应用面试。',
    jd_text: JD, conditions: {direction: 'AI 应用开发'}});
  await page.goto(`${base}/?shell=desktop&goal=${created.id}`, {waitUntil: 'domcontentloaded'});
  await page.locator('.desktop-shell[data-pane]').waitFor({timeout: 30000});
  return created.id;
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

  // ---- A. orphaned lease, active preparation, active session -----------------
  const goal = await createTrainingGoal(page);
  await scoped(goal, 'scripts/career-web-support.py');
  await waitPrimary(page, '正在准备');

  const orphan = await scoped(goal, 'scripts/prep-web-support.py', 'orphan');
  assert.ok(orphan.orphaned, JSON.stringify(orphan));
  assert.equal(orphan.status, 'RUNNING');
  await page.waitForTimeout(800);
  assert.match((await primary(page).innerText()) ?? '', /正在准备/);
  mark('租约丢失：任务仍显示准备中，没有新建第二个任务', {orphaned: orphan.orphaned});

  const planned = await scoped(goal, 'scripts/prep-web-support.py', 'plan');
  assert.equal(planned.status, 'WAITING_FOR_USER', JSON.stringify(planned));
  await waitPrimary(page, '开始这场');
  const goalVersion = await api<{version: number}>(page, 'GET', `/api/career-goals/${goal}`);
  const duplicatePrep = await refusal(page, 'POST', `/api/career-goals/${goal}/interview-preparations`,
    {expected_version: goalVersion.version, role: 'ai-application', mode: 'interview', max_questions: 2});
  assert.equal(duplicatePrep.status, 409);
  assert.equal(duplicatePrep.code, 'preparation_in_progress');
  mark('已有待确认计划：再次创建准备被 409 拒绝', duplicatePrep);

  await primary(page).click();
  const advanced = await scoped(goal, 'scripts/prep-web-support.py', 'advance');
  assert.equal(advanced.status, 'SUCCEEDED', JSON.stringify(advanced));
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 150000});
  const goalState = await api<{version: number}>(page, 'GET', `/api/career-goals/${goal}`);
  const duplicateSession = await refusal(page, 'POST', `/api/career-goals/${goal}/interviews`,
    {expected_version: goalState.version, role: 'ai-application', mode: 'interview', max_questions: 2});
  assert.equal(duplicateSession.status, 409);
  assert.equal(duplicateSession.code, 'interview_in_progress');
  const sessions = await page.evaluate(async id =>
    (await (await fetch(`/api/career-goals/${id}/interviews`, {credentials: 'same-origin'})).json()).items.length, goal);
  assert.equal(sessions, 1);
  await openWorkbench(page, goal);
  await waitPrimary(page, '继续');
  await page.screenshot({path: join(out, '01-active-session.png'), fullPage: true});
  mark('已有进行中场次：再次创建场次被 409 拒绝，工作台仍只有一个继续入口',
    {...duplicateSession, sessions});

  // ---- B. the plan goes stale when the goal version moves --------------------
  const staleGoal = await createTrainingGoal(page);
  await scoped(staleGoal, 'scripts/career-web-support.py');
  await waitPrimary(page, '正在准备');
  const stalePlan = await scoped(staleGoal, 'scripts/prep-web-support.py', 'plan');
  assert.equal(stalePlan.status, 'WAITING_FOR_USER', JSON.stringify(stalePlan));
  await openWorkbench(page, staleGoal);
  await waitPrimary(page, '开始这场');
  assert.equal(await page.locator('[data-testid="pipeline-plan"]').count(), 1);

  const live = await api<{version: number; conditions: unknown; jd_text: string}>(
    page, 'GET', `/api/career-goals/${staleGoal}`);
  await api(page, 'POST', `/api/career-goals/${staleGoal}/messages`, {
    expected_version: live.version, message: '补充一句：保持同一份 JD。', jd_text: live.jd_text,
    conditions: live.conditions});
  await openWorkbench(page, staleGoal);
  await waitPrimary(page, '重新准备', 90000);
  assert.equal(await page.locator('[data-testid="pipeline-plan"]').count(), 0);
  const current = await api<{status: string}>(page, 'GET',
    `/api/career-goals/${staleGoal}/interview-preparations/current`);
  assert.equal(current.status, 'STALE');
  const staleSessions = await page.evaluate(async id =>
    (await (await fetch(`/api/career-goals/${id}/interviews`, {credentials: 'same-origin'})).json()).items.length, staleGoal);
  assert.equal(staleSessions, 0);
  await page.screenshot({path: join(out, '02-stale-plan.png'), fullPage: true});
  mark('目标版本变化后旧计划变 STALE：主操作切到「重新准备」，没有场次', {status: current.status});

  assert.deepEqual(pageErrors, [], JSON.stringify(pageErrors.slice(0, 2)));
  writeFileSync(join(out, 'result.json'), JSON.stringify({goal, staleGoal, cases, pageErrors}, null, 2));
  console.log(JSON.stringify({status: 'passed', cases: cases.length, out}));
} catch (error) {
  writeFileSync(join(out, 'result.json'), JSON.stringify({cases, pageErrors, error: String(error)}, null, 2));
  await browser?.close();
  console.error('FAIL', error);
  process.exit(1);
} finally {
  await browser?.close();
}
