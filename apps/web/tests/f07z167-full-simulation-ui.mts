// F07Z-167 Phase 4: the full-simulation entry and the skip control, on the real local stack.
// Creates a goal without the one-click training request, asks for a full simulation from the
// workbench, confirms the frozen 12-question / 45-minute plan, opens the first question and
// skips it. Scripted only: no model calls, no credentials.
//
//   node --experimental-strip-types apps/web/tests/f07z167-full-simulation-ui.mts
//
// The suite starts Vite 5174, the API 3100 and the scripted interview worker itself.
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {execFile, spawn, type ChildProcess} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const root = resolve(import.meta.dirname, '../../..');
const out = resolve(process.env.FULL_OUT ?? join(root, '.private/f07z167/ui'));
const python = process.env.FULL_PYTHON ?? 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
const base = 'http://127.0.0.1:5174';
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
  const result = await execute(python, args, {cwd: root, windowsHide: true, timeout: 180000,
    env: {...process.env, PYTHONPATH: 'backend/src'}});
  return JSON.parse(result.stdout) as {status?: string; session_id?: string | null};
}

const primary = (page: Page) => page.locator('[data-testid="workbench-next-action"]');

let browser: Browser | null = null;
let vite: ChildProcess | null = null;
let api: ChildProcess | null = null;
let worker: ChildProcess | null = null;
try {
  const vitePath = join(root, 'apps/web/node_modules/vite/bin/vite.js');
  vite = spawn(process.env.FULL_NODE ?? 'node',
    [vitePath, '--host', '127.0.0.1', '--port', '5174', '--strictPort'],
    {cwd: join(root, 'apps/web'), stdio: 'ignore'});
  api = spawn(python, ['-X', 'utf8', '-m', 'interview_agent.api',
    '--config', join(root, '.private/postgres/test.json')],
    {cwd: join(root, 'backend'), stdio: 'ignore',
      env: {...process.env, PYTHONPATH: join(root, 'backend/src'), INTERVIEW_KEY_VAULT: 'off'}});
  worker = spawn(python, ['-X', 'utf8', '-m', 'interview_agent.interviews.worker',
    '--config', join(root, '.private/redis/test-worker.json'), '--provider', 'scripted'],
    {cwd: root, stdio: 'ignore',
      env: {...process.env, PYTHONPATH: join(root, 'backend/src')}});
  const started = Date.now();
  while (Date.now() - started < 60000) {
    try {
      const health = await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(1200)});
      const app = await fetch(base + '/', {signal: AbortSignal.timeout(1200)});
      if (health.ok && app.ok) break;
    } catch {}
    await delay(500);
  }

  browser = await chromium.launch({executablePath:
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(600);

  // A plain goal (no training request), so the workbench still offers both preparation entries.
  const goal = await page.evaluate(async () => {
    const response = await fetch('/api/career-goals/', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({message: '按这份 JD 练一场 AI 应用面试。', request: 'save_jd',
        jd_text: '岗位职责：负责 Agent 工具链与 Python 后端开发，熟悉 FastAPI、PostgreSQL 与接口幂等。',
        conditions: {direction: 'AI 应用开发'}})});
    return (await response.json()) as {id: string; version: number};
  });
  const coordinated = await scoped(goal.id, 'scripts/career-web-support.py');
  assert.ok(coordinated);
  await page.goto(`${base}/?shell=desktop&goal=${goal.id}`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 60000});
  await page.waitForTimeout(800);

  const fullButton = page.locator('[data-testid="pipeline-full-sim"]');
  await fullButton.waitFor({timeout: 30000});
  await fullButton.click();
  const planned = await scoped(goal.id, 'scripts/prep-web-support.py', 'plan');
  assert.equal(planned.status, 'WAITING_FOR_USER', JSON.stringify(planned));
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.trim() === '开始这场';
  }, undefined, {timeout: 60000});
  const planText = await page.locator('[data-testid="pipeline-plan-text"]').innerText();
  assert.match(planText, /完整模拟/);
  assert.match(planText, /12 题/);
  assert.match(planText, /45 分钟/);
  await page.screenshot({path: join(out, '01-plan.png'), fullPage: true});
  mark('完整模拟入口：计划显示 12 题 / 45 分钟，确认前不开场', {planText});

  await primary(page).click();
  const advanced = await scoped(goal.id, 'scripts/prep-web-support.py', 'advance');
  assert.equal(advanced.status, 'SUCCEEDED', JSON.stringify(advanced));
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 150000});
  const eyebrow = await page.locator('.interview-workspace .eyebrow', {hasText: '当前题'}).innerText();
  assert.match(eyebrow, /1\/12/);
  mark('确认一次直奔首题，题目进度按服务端配置显示', {eyebrow});

  await page.locator('[data-testid="answer-clarify"]').click();
  const clarification = page.locator('[data-testid="interview-clarification"]');
  await clarification.waitFor({timeout: 30000});
  assert.match(await clarification.innerText(), /本题考查/);
  assert.match(await page.locator('.interview-workspace .eyebrow', {hasText: '当前题'}).innerText(), /1\/12/);
  mark('澄清题意：只重述范围，题号与题位不变', {text: (await clarification.innerText()).slice(0, 60)});

  await page.locator('[data-testid="answer-skip"]').click();
  await page.locator('.interview-notice', {hasText: '已跳过本题'}).waitFor({timeout: 30000});
  await page.waitForFunction(() => {
    const node = document.querySelector('.interview-workspace .eyebrow');
    return node?.textContent?.includes('2/12') === true;
  }, undefined, {timeout: 120000});
  const skipped = await page.evaluate(async id => {
    const response = await fetch(`/api/career-goals/${id}/interviews?`, {credentials: 'same-origin'});
    const body = await response.json() as {items: {id: string}[]};
    const session = await (await fetch(`/api/career-goals/${id}/interviews/${body.items[0].id}`,
      {credentials: 'same-origin'})).json() as {events: {kind: string}[]};
    return session.events.filter(event => event.kind === 'skip').length;
  }, goal.id);
  assert.equal(skipped, 1);
  await page.screenshot({path: join(out, '02-skipped.png'), fullPage: true});
  mark('跳过本题：记一条 skip 事件、不产生观察，下一题正常出现', {skips: skipped});

  assert.deepEqual(pageErrors, [], JSON.stringify(pageErrors.slice(0, 2)));
  writeFileSync(join(out, 'result.json'), JSON.stringify({goal: goal.id, cases, pageErrors}, null, 2));
  console.log(JSON.stringify({status: 'passed', cases: cases.length, out}));
} catch (error) {
  writeFileSync(join(out, 'result.json'), JSON.stringify({cases, pageErrors, error: String(error)}, null, 2));
  console.error('FAIL', error);
  process.exitCode = 1;
} finally {
  await browser?.close().catch(() => {});
  for (const child of [worker, api, vite]) if (child && child.exitCode === null) child.kill();
}
