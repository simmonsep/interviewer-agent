// F07Z-166 D1: a same-context training turn carries the selection; the plan says so and the
// "reselect" entry actually removes it before sending the user back to the job list.
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.CARRY_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.CARRY_OUT ?? join(root, '.private/f07z166/selection-carry'));
const python = process.env.CARRY_PYTHON ?? 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
mkdirSync(out, {recursive: true});
const runId = randomUUID();
const execute = promisify(execFile);
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };
const JD = '岗位职责：负责 Java 服务端开发。';

async function scoped(goal: string, script: string, mode?: string) {
  const args = ['-X', 'utf8', script, '--goal', goal, '--run-id', runId];
  if (mode) args.push('--mode', mode);
  if (script.includes('prep-web')) args.push('--watch', '120');
  const result = await execute(python, args, {cwd: root, windowsHide: true, timeout: 170000,
    env: {...process.env, PYTHONPATH: 'backend/src'}});
  return JSON.parse(result.stdout) as {status: string};
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

const primary = (page: Page) => page.locator('[data-testid="workbench-next-action"]');

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
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('先保存这份 JD，稍后再训练。');
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.getByLabel('确认方向（可选）').fill('Java 后端');
  await page.locator('textarea[aria-label="职位说明 JD"]').fill(JD);
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  const goalId = await page.evaluate(() => new URLSearchParams(location.search).get('goal'));
  assert.ok(goalId, 'goal_id_missing');

  const job = await api<{snapshot_id: string; items: {snapshot_id: string; run_id: string; position_id: string}[]}>(
    page, 'POST', `/api/career-goals/${goalId}/jobs/manual`,
    {expected_version: 1, company: '合成公司', title: 'Java实习', cities: [], jd_text: JD});
  const item = job.items.find(entry => entry.snapshot_id === job.snapshot_id);
  assert.ok(item, 'manual_job_snapshot_missing');
  await api(page, 'POST', `/api/career-goals/${goalId}/selection`,
    {expected_version: 1, run_id: item.run_id, snapshot_id: item.snapshot_id, acknowledge_unknown: true});
  mark('版本 1 已选好岗位（作为沿用来源）', {snapshot: item.snapshot_id});

  // A reply that keeps the JD/conditions: same version 1 -> new version 2.
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-details') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.locator('#desktop-goal-form textarea[aria-label="求职意向或补充说明"]').fill('开始准备训练。');
  await page.locator('#desktop-goal-form button.primary').click();
  const coordinated = await scoped(goalId!, 'scripts/career-web-support.py');
  assert.equal(coordinated.status, 'WAITING_FOR_TOOL', JSON.stringify(coordinated));

  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.includes('正在准备') === true;
  }, undefined, {timeout: 60000});
  const planned = await scoped(goalId!, 'scripts/prep-web-support.py', 'plan');
  assert.equal(planned.status, 'WAITING_FOR_USER', JSON.stringify(planned));
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.trim() === '开始这场';
  }, undefined, {timeout: 60000});

  const carried = await page.locator('[data-testid="pipeline-carried"]').innerText();
  assert.match(carried, /沿用上一版选岗与绑定简历/);
  assert.match(carried, /目标版本 1/);
  const current = await api<{plan: {selection: {carried_from_version: number}}}>(
    page, 'GET', `/api/career-goals/${goalId}/interview-preparations/current`);
  assert.equal(current.plan.selection.carried_from_version, 1);
  await page.screenshot({path: join(out, '01-carried-plan.png'), fullPage: true});
  mark('同上下文回复后，计划卡标注沿用来源版本 1', {carried});

  await page.locator('[data-testid="pipeline-reselect"]').click();
  await page.locator('.desktop-shell[data-pane="research"]').waitFor({timeout: 30000});
  const after = await api<{selected: unknown}>(
    page, 'GET', `/api/career-goals/${goalId}/jobs`);
  assert.equal(after.selected, null);
  assert.equal(await page.locator('[data-testid="pipeline-carried"]').count(), 0);
  await page.screenshot({path: join(out, '02-after-reselect.png'), fullPage: true});
  mark('重新选岗：本版本 selection 已删除并回到岗位页', {selected: after.selected});

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
