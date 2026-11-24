// F07Z-166 step 3: the workbench primary follows the preparation state (preparing -> start ->
// continue), with one primary instead of several competing buttons.
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.PREP_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.PREP_OUT ?? join(root, '.private/f07z166/prep-states'));
const python = process.env.PREP_PYTHON ?? 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
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

async function openWorkbench(page: Page, goalId: string) {
  await page.goto(`${base}/?shell=desktop&goal=${goalId}`, {waitUntil: 'domcontentloaded'});
  await page.locator('.desktop-shell[data-pane]').waitFor({timeout: 30000});
}

const primary = (page: Page) => page.locator('[data-testid="workbench-next-action"]');

// F07Z-166: the same three states must be readable and single-primary at 390px, not only on
// the wide desktop viewport.
async function narrowCheck(page: Page, goalId: string, expect: string, label: string, file: string) {
  const narrow = await page.context().newPage();
  await narrow.setViewportSize({width: 390, height: 844});
  await narrow.goto(`${base}/?shell=desktop&goal=${goalId}`, {waitUntil: 'domcontentloaded'});
  await narrow.locator('.desktop-shell[data-pane]').waitFor({timeout: 30000});
  await narrow.waitForFunction(text => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.includes(text) === true;
  }, expect, {timeout: 60000});
  const result = await narrow.evaluate(() => ({
    horizontal: document.documentElement.scrollWidth > window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    primaries: document.querySelectorAll('.d-pipeline-cta button.primary').length,
    disabled: (document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement).disabled,
  }));
  assert.equal(result.horizontal, false, `narrow_horizontal_scroll ${result.scrollWidth}`);
  assert.equal(result.primaries, 1, 'narrow_must_keep_one_primary');
  await narrow.screenshot({path: join(out, file), fullPage: true});
  await narrow.close();
  mark(label, result);
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
  await scoped(goalId!, 'scripts/career-web-support.py');

  const setup = await page.evaluate(async id => {
    const response = await fetch(`/api/career-goals/${id}/interview-preparations`, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: 1, role: 'ai-application', mode: 'interview', max_questions: 2})});
    return {status: response.status, body: await response.json()};
  }, goalId);
  assert.equal(setup.status, 202, JSON.stringify(setup).slice(0, 200));

  await openWorkbench(page, goalId!);
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.includes('正在准备') === true;
  }, undefined, {timeout: 60000});
  assert.equal(await primary(page).isDisabled(), true);
  assert.equal(await page.locator('[data-testid="pipeline-quick-prep"]').isDisabled(), true);
  assert.equal(await page.locator('.d-pipeline-cta button.primary').count(), 1);
  mark('准备中：工作台唯一主操作显示「正在准备…」，其他入口同步禁用');
  await narrowCheck(page, goalId!, '正在准备', '390px 准备中：无横向滚动，只有一个禁用的主操作', 'n1-preparing.png');

  const planned = await scoped(goalId!, 'scripts/prep-web-support.py', 'plan');
  assert.equal(planned.status, 'WAITING_FOR_USER', JSON.stringify(planned));
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.trim() === '开始这场';
  }, undefined, {timeout: 60000});
  assert.equal(await page.locator('[data-testid="pipeline-plan"]').count(), 1);
  assert.equal(await page.locator('.d-pipeline-cta button.primary').count(), 1);
  await page.screenshot({path: join(out, '01-waiting.png'), fullPage: true});
  assert.equal(await page.locator('[data-testid="pipeline-confirm"]').count(), 0);
  mark('计划就绪：主操作切到「开始这场」，计划卡不再放第二个确认按钮');
  await narrowCheck(page, goalId!, '开始这场', '390px 待开场：无横向滚动，只有一个「开始这场」', 'n2-ready.png');

  await primary(page).click();
  const advanced = await scoped(goalId!, 'scripts/prep-web-support.py', 'advance');
  assert.equal(advanced.status, 'SUCCEEDED', JSON.stringify(advanced));
  // One confirm lands on the first question; no second click, no manual refresh.
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 150000});
  const opened = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.equal(opened, advanced.session_id);
  const sessions = await page.evaluate(async id =>
    (await (await fetch(`/api/career-goals/${id}/interviews`, {credentials: 'same-origin'})).json()).items.length, goalId);
  assert.equal(sessions, 1);
  await page.screenshot({path: join(out, '02-interviewing.png'), fullPage: true});
  mark('确认一次直达首题：只创建一场，工作台确认后自动打开', {session: opened});

  await openWorkbench(page, goalId!);
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.includes('继续') === true;
  }, undefined, {timeout: 60000});
  assert.equal(await page.locator('.d-pipeline-cta button.primary').count(), 1);
  await primary(page).click();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 150000});
  assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'), advanced.session_id);
  mark('回到工作台：主操作是「继续本场」，打开同一场不重复创建');
  await narrowCheck(page, goalId!, '继续', '390px 继续本场：无横向滚动，只有一个主操作', 'n3-continue.png');

  // Step 3 (stubbed): the two remaining states. Only the read endpoint is stubbed, so these
  // assertions are about the workbench's single primary, not about the worker.
  // Same context, so the signed-in session cookie applies; a fresh context would land on login.
  const failed = await page.context().newPage();
  failed.on('pageerror', error => pageErrors.push(error.message));
  await failed.route('**/interview-preparations/current', route => route.fulfill({status: 200,
    contentType: 'application/json',
    body: JSON.stringify({id: 'stub-failed', status: 'FAILED', session_id: null,
      error_code: 'planning_crashed', version: 2, plan_sha256: null})}));
  const retries: string[] = [];
  failed.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === 'POST' && path.endsWith('/interview-preparations')) retries.push(path);
  });
  await openWorkbench(failed, goalId!);
  await failed.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.trim() === '重新准备';
  }, undefined, {timeout: 60000});
  assert.equal(await failed.locator('.d-pipeline-cta button.primary').count(), 1);
  assert.equal(await primary(failed).isDisabled(), false);
  await failed.screenshot({path: join(out, '03-failed.png'), fullPage: true});
  await primary(failed).click();
  await failed.waitForTimeout(1200);
  assert.ok(retries.length >= 1, 'retry_click_must_post_a_preparation');
  mark('失败态：唯一主操作是「重新准备」，点击真的重新发起准备', {posts: retries.length});

  const idle = await page.context().newPage();
  idle.on('pageerror', error => pageErrors.push(error.message));
  await idle.route('**/interview-preparations/current', route => route.fulfill({status: 404,
    contentType: 'application/json', body: JSON.stringify({detail: 'not_found'})}));
  await openWorkbench(idle, goalId!);
  await idle.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.trim() === '继续面试';
  }, undefined, {timeout: 60000});
  assert.equal(await idle.locator('.d-pipeline-cta button.primary').count(), 1);
  mark('待输入/无准备：没有准备记录时回落到原有下一步，不出现第二个主操作');
  await failed.close();
  await idle.close();

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
