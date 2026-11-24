// F07Z-165 D: the coordinator's ask_user proposal must hand focus back to the input and must not
// enqueue any work. The suite temporarily allows a visible one-click card (smart_approve) and
// restores the local policy mode afterwards; it never leaves a grant behind.
//
//   D06A_OUTPUT=<new dir> node apps/web/tests/d01-agent-ask-user.mts
//
// Requires: API, Vite on 5174, PG/Redis, and the scripted career worker. Do not run the research
// worker. No model calls.
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {waitGoalReady} from './desktop-nav.mts';

const root = resolve(import.meta.dirname, '../../..');
const output = resolve(process.env.D06A_OUTPUT ?? join(root, '.private/f07z165/ask-user'));
const base = process.env.D06A_BASE_URL ?? 'http://127.0.0.1:5174';
mkdirSync(join(output, 'shots'), {recursive: true});

const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const evidence: Record<string, unknown> = {run: new Date().toISOString(), cases, page_errors: pageErrors};
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

type Goal = {id: string; status: string; action: {type: string; text: string} | null};

const policy = (page: Page, mode?: string) => page.evaluate(async value => {
  const response = await fetch('/api/runtime/agent-policy', {
    method: value ? 'PUT' : 'GET', credentials: 'same-origin',
    headers: value ? {'Content-Type': 'application/json'} : undefined,
    body: value ? JSON.stringify({mode: value}) : undefined});
  return await response.json() as {mode: string; effects: Record<string, string>};
}, mode);

let browser: Browser | null = null;
let originalMode = '';
let page: Page | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  assert.ok((await fetch(base + '/', {signal: AbortSignal.timeout(4000)})).ok, 'vite_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  page = await (await browser.newContext({viewport: {width: 1280, height: 860}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('.desktop-shell[data-pane]').waitFor({timeout: 30000});
  const current = await policy(page);
  originalMode = current.mode;
  evidence.policy_mode_before = originalMode;
  if (originalMode !== 'smart_approve') await policy(page, 'smart_approve');
  evidence.policy_mode_during = (await policy(page)).mode;
  // The console loads the policy once on mount; announce the change before the goal exists.
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('py-agent-policy-changed')));
  await delay(300);

  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('帮我找实习，还没想好方向。');
  await page.locator('[data-testid="workbench-primary"]').click();
  await waitGoalReady(page);
  const goalId = await page.evaluate(() => new URLSearchParams(location.search).get('goal'));
  assert.ok(goalId, 'goal_id_missing');

  let goal: Goal | null = null;
  for (let i = 0; i < 90; i++) {
    goal = await page.evaluate(async id => await (await fetch(`/api/career-goals/${id}`,
      {credentials: 'same-origin', cache: 'no-store'})).json() as Goal, goalId);
    if (goal && !['PENDING', 'RUNNING'].includes(goal.status) && goal.action) break;
    await delay(500);
  }
  assert.ok(goal?.action, `no_proposal status=${goal?.status}`);
  assert.equal(goal.action.type, 'ask_user', `expected_ask_user_got_${goal.action.type}`);
  await page.locator('[data-testid="agent-proposal"]').waitFor({timeout: 20000});
  const proposal = (await page.locator('[data-testid="agent-proposal"]').innerText()).replace(/\s+/g, ' ');
  assert.match(proposal, /需要你补充/);

  const before = await page.evaluate(async id => (await (await fetch(`/api/career-goals/${id}/jobs`,
    {credentials: 'same-origin', cache: 'no-store'})).json() as {runs?: unknown[]}).runs?.length ?? 0, goalId);
  const execute = page.locator('[data-testid="agent-execute"]');
  await execute.waitFor({timeout: 20000});
  await execute.click();
  await delay(600);
  const focused = await page.evaluate(() => document.activeElement?.getAttribute('aria-label'));
  assert.equal(focused, '一句话交给 Agent', 'ask_user_did_not_focus_omni_input');
  const after = await page.evaluate(async id => (await (await fetch(`/api/career-goals/${id}/jobs`,
    {credentials: 'same-origin', cache: 'no-store'})).json() as {runs?: unknown[]}).runs?.length ?? 0, goalId);
  assert.equal(after, before, 'ask_user_enqueued_work');
  const notice = await page.locator('.d-agent .notice').first().innerText().catch(() => '');
  mark('ask_user 提议：点击「去补充」把焦点交回输入，不入队任务、不代替用户回答',
    {action_type: goal.action.type, policy_mode: 'smart_approve', focused, runs_before: before,
     runs_after: after, notice: notice.slice(0, 120)});
  await page.screenshot({path: join(output, 'shots', '01-ask-user-focused.png')});

  assert.deepEqual(pageErrors, [], 'page_errors');
  evidence.summary = {passed: cases.length, failed: 0, page_errors: pageErrors.length};
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  evidence.error = String(error);
  evidence.summary = {passed: cases.length, failed: 1, page_errors: pageErrors.length};
  console.error('FAILED:', error);
  process.exitCode = 1;
} finally {
  if (page) {
    try {
      if (originalMode) await policy(page, originalMode);
      evidence.policy_mode_after = (await policy(page)).mode;
    } catch (error) {
      evidence.policy_restore_error = String(error);
    }
  }
  await browser?.close();
  writeFileSync(join(output, 'result.json'), JSON.stringify(evidence, null, 2));
  console.log('OUTPUT', output);
}
