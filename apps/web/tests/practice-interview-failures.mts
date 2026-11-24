// F07Z-148 (failure regression): the business exceptions that the retired d01-interview-flow used
// to cover, now against the real shell (?shell=web) the product actually ships.
//   401 -> understandable message + sign-in entry, no white screen
//   409 -> re-read the server state, never blind-retry
//   network -> input kept, unknown result, reconcile (no duplicate write)
//   expiry -> no more input, records still readable
//   reload -> the same session comes back
// Real local stack; scripted worker; injected failures are labelled MOCK.
//
//   node --experimental-strip-types apps/web/tests/practice-interview-failures.mts
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.D148_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.D148_OUT ?? join(root, '.private/f07z148/failures'));
mkdirSync(out, {recursive: true});
const fixture = JSON.parse(readFileSync(join(root, 'fixtures/question-banks/agent-developer-trial.json'), 'utf8'));

const cases: {name: string; type: 'real' | 'mock'; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, type: 'real' | 'mock' = 'real', detail?: unknown) => {
  cases.push({name, type, detail}); console.log(`PASS [${type}] ${name}`);
};
const account = {email: `failures-${randomUUID()}@example.com`, password: `Synthetic-${randomUUID()}`, name: '异常验收学员'};
const answer1 = 'state 是共享的运行状态；checkpointer 按 thread_id 保存检查点。';

async function sessionDetail(page: Page, goalId: string, sessionId: string) {
  return page.evaluate(async url => {
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
    return {status: response.status, body: await response.json()};
  }, `/api/career-goals/${goalId}/interviews/${sessionId}`);
}

let browser: Browser | null = null;
let page: Page | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const context = await browser.newContext({viewport: {width: 1366, height: 900}});
  page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  // ---------------------------------------------------------------- arrange
  await page.goto(`${base}/?shell=web`, {waitUntil: 'domcontentloaded'});
  await page.getByRole('button', {name: '第一次来？创建账号'}).click();
  await page.getByLabel('称呼').fill(account.name);
  await page.getByLabel('邮箱').fill(account.email);
  await page.getByLabel('密码').fill(account.password);
  await page.getByRole('button', {name: '创建账号', exact: true}).click();
  await page.getByRole('button', {name: '整场面试'}).waitFor({timeout: 60000});
  await page.getByRole('button', {name: '整场面试'}).click();
  await page.getByLabel('求职意向或补充说明').fill('想在上海找 AI 应用 / Agent 开发实习。');
  await page.getByRole('button', {name: '保存求职目标'}).click();
  await page.locator('.goal-card').waitFor({timeout: 30000});
  const goalId = await page.evaluate(async () => (await (await fetch('/api/career-goals/', {credentials: 'same-origin'})).json()).items[0]);
  const bank = await page.evaluate(async document => {
    const preview = await (await fetch('/api/question-banks/imports', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()}, body: JSON.stringify({document})})).json();
    const commit = await (await fetch(`/api/question-banks/imports/${preview.preview_id}/commit`, {method: 'POST',
      credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: '{}'})).json();
    return {bank_id: commit.bank_id, version: commit.version};
  }, fixture);
  await page.goto(`${base}/?shell=web&goal=${goalId}&view=interview`, {waitUntil: 'domcontentloaded'});
  await page.locator('#interview-bank').waitFor({timeout: 60000});
  await page.locator('#interview-bank').selectOption(`${bank.bank_id}|${bank.version}`);

  // ---------------------------------------------------------------- 409 on start
  let conflict = true;
  await page.route('**/api/career-goals/*/interviews', async route => {
    if (conflict && route.request().method() === 'POST') {
      return route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({code: 'version_conflict'})});
    }
    return route.continue();
  });
  await page.getByRole('button', {name: '开始整场面试 →'}).click();
  await page.getByText(/状态已变化|版本冲突/).first().waitFor({timeout: 30000});
  await delay(800);
  const afterConflict = await page.evaluate(async id => {
    const response = await fetch(`/api/career-goals/${id}/interviews`, {credentials: 'same-origin'});
    return (await response.json()).items.length;
  }, goalId);
  assert.equal(afterConflict, 0, 'conflict_must_not_create_a_session');
  assert.equal(await page.getByRole('button', {name: '用同一请求重试'}).count(), 0, 'conflict_must_not_offer_blind_retry');
  mark('409：不盲目重试、不创建场次，回读服务端状态后给出可理解提示', 'mock', {sessions: afterConflict});
  conflict = false;
  await page.unroute('**/api/career-goals/*/interviews');

  // ---------------------------------------------------------------- start for real
  await page.getByRole('button', {name: '开始整场面试 →'}).click();
  await page.locator('[data-session-id]').waitFor({timeout: 30000});
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 120000});
  const sessionId = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.ok(sessionId, 'session_id_missing');
  mark('对照：冲突解除后正常开场', 'real', {session_id: sessionId});

  // ---------------------------------------------------------------- reload recovery
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.locator('[data-session-id]').waitFor({timeout: 60000});
  assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'), sessionId,
    'reload_must_restore_the_same_session');
  assert.equal(await page.locator('#interview-bank').count(), 0, 'reload_must_not_fall_back_to_the_setup_form');
  mark('刷新浏览器：回到同一场次，没有退回题库选择', 'real');

  // ---------------------------------------------------------------- expiry (server already finished)
  const detail = (await sessionDetail(page, goalId, sessionId)).body;
  const expiredReport = {stop_reason: 'deadline', model_summary: null, questions: 2, answers: 1,
    assisted_answers: 0, observations: [], semantic_scoring_eligible: false, unreviewed_answer_ids: [], next_actions: []};
  // One handler with a mode switch: re-registering the same pattern while requests are in
  // flight made Playwright report "Route is already handled".
  let detailMode: 'none' | 'completed' | 'waiting-past' = 'completed';
  await page.route('**/api/career-goals/*/interviews/*', async route => {
    if (detailMode === 'none' || route.request().method() !== 'GET') return route.continue();
    // A reload while the handler awaits route.fetch() can invalidate the route; the page has
    // already moved on, and the assertion that matters is on the rendered state.
    try {
      const response = await route.fetch();
      const body = await response.json();
      const deadline_at = new Date(Date.now() - 60_000).toISOString();
      if (detailMode === 'completed') {
        return await route.fulfill({response, json: {...body, status: 'COMPLETED', deadline_at, report: expiredReport}});
      }
      return await route.fulfill({response, json: {...body, status: 'WAITING_FOR_USER', deadline_at, report: null}});
    } catch { /* route superseded by a navigation */ }
  });
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 60000});
  const expired = await page.evaluate(() => ({
    inputs: document.querySelectorAll('#interview-answer').length,
    endReason: document.querySelector('[data-testid="deadline-end"]')?.textContent ?? '',
    status: document.querySelector('[data-testid="review-status"]')?.textContent ?? '',
  }));
  assert.equal(expired.inputs, 0, 'expired_session_must_not_show_an_answer_box');
  assert.match(expired.status, /复盘/);
  mark('Session 到期（服务端已终态）：不再显示作答框，历史记录仍可查看', 'mock', expired);

  // Negative control: deadline is past but the server still says WAITING_FOR_USER. The UI must
  // stop accepting input locally instead of letting the user write and then be refused.
  detailMode = 'waiting-past';
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.getByTestId('deadline-notice').waitFor({timeout: 60000});
  const pastDeadline = await page.evaluate(() => ({
    textareaDisabled: (document.querySelector('#interview-answer') as HTMLTextAreaElement | null)?.disabled ?? null,
    submitDisabled: (document.querySelector('[data-testid="answer-submit"]') as HTMLButtonElement | null)?.disabled ?? null,
  }));
  assert.equal(pastDeadline.textareaDisabled, true, 'expired_deadline_must_disable_the_textarea');
  assert.equal(pastDeadline.submitDisabled, true, 'expired_deadline_must_disable_submit');
  mark('本地已过 deadline（服务端仍 WAITING_FOR_USER）：输入与提交一起停用', 'mock', pastDeadline);
  detailMode = 'none';
  await page.unroute('**/api/career-goals/*/interviews/*');

  // ---------------------------------------------------------------- network failure + reconcile
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.locator('[data-session-id]').waitFor({timeout: 60000});
  await page.getByLabel('你的整场面试回答').fill(answer1);
  let aborted = true;
  await page.route('**/answers', async route => {
    if (aborted && route.request().method() === 'POST') return route.abort('failed');
    return route.continue();
  });
  await page.getByRole('button', {name: '提交这一答 →'}).click();
  await page.getByText(/请求未完成/).first().waitFor({timeout: 30000});
  const afterNetwork = await page.evaluate(() => ({
    text: (document.querySelector('#interview-answer') as HTMLTextAreaElement | null)?.value ?? '',
    retry: Boolean(document.querySelector('[data-testid="unknown-retry"]')),
  }));
  assert.equal(afterNetwork.text, answer1, 'network_failure_must_keep_the_input');
  assert.equal(afterNetwork.retry, true, 'unknown_result_must_offer_the_reconcile_path');
  const beforeRetry = (await sessionDetail(page, goalId, sessionId)).body;
  const beforeAnswers = beforeRetry.events.filter((event: {kind: string}) => event.kind === 'answer').length;
  assert.equal(beforeAnswers, 0, 'network_failure_must_not_write_anything');
  mark('断网：输入保留、提供 reconcile 入口、服务端没有多写任何回答', 'mock', {answers: beforeAnswers});
  aborted = false;
  await page.getByRole('button', {name: '用同一请求重试'}).click();
  await page.waitForFunction(previous => {
    const node = document.querySelector('[data-testid="interview-question"]');
    return !!node && node.textContent !== previous;
  }, await page.locator('[data-testid="interview-question"]').innerText(), {timeout: 120000}).catch(() => null);
  await delay(1500);
  const afterRetry = (await sessionDetail(page, goalId, sessionId)).body;
  const answersAfterRetry = afterRetry.events.filter((event: {kind: string}) => event.kind === 'answer').length;
  assert.equal(answersAfterRetry, beforeAnswers + 1, 'reconcile_must_write_exactly_one_answer');
  mark('用同一请求重试后只写入 1 条新回答（幂等键生效）', 'mock', {answers: answersAfterRetry});
  await page.unroute('**/answers');

  // ---------------------------------------------------------------- 409 on an answer
  let answerConflict = true;
  await page.route('**/answers', async route => {
    if (answerConflict && route.request().method() === 'POST') {
      return route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({code: 'version_conflict'})});
    }
    return route.continue();
  });
  const beforeConflict = (await sessionDetail(page, goalId, sessionId)).body
    .events.filter((event: {kind: string}) => event.kind === 'answer').length;
  await page.locator('#interview-answer').fill('409 场景：这一答不应该被自动重放。');
  await page.getByRole('button', {name: '提交这一答 →'}).click();
  await page.getByText(/状态已变化|版本冲突/).first().waitFor({timeout: 30000});
  await delay(600);
  const afterConflictAnswer = await page.evaluate(() => ({
    input: (document.querySelector('#interview-answer') as HTMLTextAreaElement | null)?.value ?? '',
    retry: Boolean(document.querySelector('[data-testid="unknown-retry"]')),
  }));
  assert.ok(afterConflictAnswer.input.includes('409 场景'), 'conflict_must_keep_the_answer_text');
  assert.equal(afterConflictAnswer.retry, false, 'confirmed_conflict_must_not_offer_replay');
  const conflictAnswers = (await sessionDetail(page, goalId, sessionId)).body
    .events.filter((event: {kind: string}) => event.kind === 'answer').length;
  assert.equal(conflictAnswers, beforeConflict, 'conflict_must_not_write_an_answer');
  mark('答题 409：不自动重放、不提供旧请求重试、回答原文保留', 'mock', {answers: conflictAnswers});
  answerConflict = false;
  await page.unroute('**/answers');
  // A fresh submission (new body + new idempotency key) is the only path forward.
  await page.getByRole('button', {name: '提交这一答 →'}).click();
  await page.waitForFunction(previous => {
    const node = document.querySelector('[data-testid="interview-question"]');
    return !!node && node.textContent !== previous;
  }, await page.locator('[data-testid="interview-question"]').innerText(), {timeout: 120000}).catch(() => null);
  await delay(1200);
  const afterFresh = (await sessionDetail(page, goalId, sessionId)).body
    .events.filter((event: {kind: string}) => event.kind === 'answer').length;
  assert.equal(afterFresh, beforeConflict + 1, 'fresh_submission_should_write_exactly_once');
  mark('409 之后用最新状态重新提交：只写入 1 条回答', 'mock', {answers: afterFresh});

  // ---------------------------------------------------------------- 401 (session read)
  // The answer flow has reached the question limit, so the 401 is exercised on the deep-link
  // read that every entry performs. Same user-visible contract: explain + sign-in, no white screen.
  await page.route('**/api/career-goals/*/interviews/*', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({status: 401, contentType: 'application/json',
      body: JSON.stringify({code: 'authentication_required'})});
  });
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.getByText('登录已过期，请重新登录。').first().waitFor({timeout: 30000});
  const auth = await page.evaluate(() => ({
    signIn: Boolean(document.querySelector('input[name="email"]')),
    white: document.body.innerText.trim().length < 20,
    inside: document.body.innerText.includes('登录已过期'),
  }));
  assert.equal(auth.signIn, true, '401_must_offer_a_sign_in_entry');
  assert.equal(auth.white, false, '401_must_not_white_screen');
  assert.equal(auth.inside, true, '401_must_explain_the_identity_problem');
  mark('401：给出可理解的提示与登录入口，不白屏、不暴露技术码', 'mock', auth);
  await page.screenshot({path: join(out, '01-after-401.png'), fullPage: true});
  await page.unroute('**/api/career-goals/*/interviews/*');

  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, goal_id: goalId, session_id: sessionId, bank, cases, page_errors: pageErrors,
    note: '真实本地栈 + 注入异常（409/到期/断网/401）；scripted worker，无模型调用。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  await page?.screenshot({path: join(out, 'failure.png'), fullPage: true}).catch(() => {});
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
