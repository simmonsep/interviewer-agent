// F07Z-150 acceptance: one stable session locator. A session has a URL, the URL survives a
// reload, Back goes to where the user came from, an active session opens the question view, a
// completed one opens the review, and a missing id gets an honest route error with a way out.
// Real local stack; scripted worker; no model call.
//
//   node --experimental-strip-types apps/web/tests/session-route.mts
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.D150_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.D150_OUT ?? join(root, '.private/f07z150/route'));
mkdirSync(out, {recursive: true});
const fixture = JSON.parse(readFileSync(join(root, 'fixtures/question-banks/agent-developer-trial.json'), 'utf8'));

const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };
const targetName = 'AI 应用开发实习生';

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  // ---------------------------------------------------------------- arrange
  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(700);
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('想在上海找 AI 应用 / Agent 开发实习。');
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.getByLabel('确认方向（可选）').fill(targetName);
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  const goalId = await page.evaluate(async () => (await (await fetch('/api/career-goals/', {credentials: 'same-origin'})).json()).items[0]);
  const bank = await page.evaluate(async document => {
    const preview = await (await fetch('/api/question-banks/imports', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()}, body: JSON.stringify({document})})).json();
    const commit = await (await fetch(`/api/question-banks/imports/${preview.preview_id}/commit`, {method: 'POST',
      credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: '{}'})).json();
    return {bank_id: commit.bank_id, version: commit.version};
  }, fixture);
  await page.locator('.d-nav-item').filter({hasText: '面试'}).first().click();
  await page.locator('#interview-bank').waitFor({state: 'visible', timeout: 30000});
  await page.locator('button', {hasText: '刷新题库'}).first().click();
  await page.locator('#interview-bank option', {hasText: 'Agent开发岗位试运行'}).first().waitFor({state: 'attached', timeout: 30000});
  await page.locator('#interview-bank').selectOption(`${bank.bank_id}|${bank.version}`);
  await page.locator('button', {hasText: '开始整场面试 →'}).first().click();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 120000});
  const sessionId = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.ok(sessionId, 'session_id_missing');

  // ---------------------------------------------------------------- the URL appears when opened
  const url = new URL(page.url());
  assert.equal(url.searchParams.get('interview'), sessionId, 'opening_a_session_must_put_its_id_in_the_url');
  assert.equal(url.searchParams.get('goal'), goalId);
  assert.equal(url.searchParams.has('answer'), false, 'url_must_not_carry_content');
  mark('打开场次后 URL 只带 goal + interview 两个 id', {search: url.search, session_id: sessionId});

  // ---------------------------------------------------------------- reload restores the same session
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.locator('[data-session-id]').waitFor({timeout: 60000});
  assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'), sessionId, 'reload_lost_the_session');
  assert.equal(await page.locator('[data-testid="interview-question"]').count(), 1, 'active_session_must_show_the_question');
  assert.equal(await page.locator('[data-testid="interview-review"]').count(), 0, 'active_session_must_not_show_a_review');
  mark('刷新深链：恢复同一进行中场次，显示当前题目而不是复盘');

  // ---------------------------------------------------------------- finish → the same URL shows the review
  const first = await page.locator('[data-testid="interview-question"]').innerText();
  await page.locator('#interview-answer').fill('state 是共享的运行状态；checkpointer 按 thread_id 保存检查点。');
  await page.locator('[data-testid="answer-submit"]').click();
  await page.waitForFunction(previous => {
    const node = document.querySelector('[data-testid="interview-question"]');
    return !!node && node.textContent !== previous;
  }, first, {timeout: 120000});
  await page.locator('#interview-answer').fill('用 Command(resume=...) 恢复；副作用要幂等。');
  await page.locator('[data-testid="answer-submit"]').click();
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 180000});
  assert.equal(new URL(page.url()).searchParams.get('interview'), sessionId, 'url_changed_after_completion');
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 90000});
  assert.equal(new URL(page.url()).searchParams.get('interview'), sessionId, 'completed_deep_link_lost');
  mark('完成后同一 URL 显示复盘；刷新仍是同一场复盘', {session_id: sessionId});
  await page.screenshot({path: join(out, '01-completed-deep-link.png'), fullPage: true});

  // ---------------------------------------------------------------- Back / Forward from a user click
  // A user-initiated open pushes a history entry, so Back can return to where they came from.
  // ---------------------------------------------------------------- Back / Forward
  // The app must apply the URL on popstate, not merely let it change. Push the session entry
  // explicitly so the previous entry is the clean workbench one.
  await page.locator('[data-testid="agent-home"]').click();
  await page.waitForTimeout(700);
  await page.evaluate(id => {
    const url = new URL(location.href);
    url.searchParams.set('interview', id);
    history.pushState(null, '', url);
  }, sessionId);
  await page.waitForTimeout(400);
  await page.goBack();
  await page.waitForTimeout(800);
  const afterBack = await page.evaluate(() => ({
    pane: document.querySelector('.desktop-shell')?.getAttribute('data-pane'),
    interview: new URLSearchParams(location.search).get('interview'),
  }));
  assert.equal(afterBack.pane, 'workbench', `back_must_return_to_the_workbench ${JSON.stringify(afterBack)}`);
  assert.equal(afterBack.interview, null);
  mark('Back：URL 回到工作台条目，界面跟着回到工作台（popstate 生效）', afterBack);
  await page.goForward();
  await page.waitForTimeout(1200);
  assert.equal(await page.locator('[data-testid="interview-review"]').count(), 1, 'forward_must_reopen_the_review');
  assert.equal(new URL(page.url()).searchParams.get('interview'), sessionId, 'forward_lost_the_session');
  mark('Forward：回到同一场次的复盘');

  // ---------------------------------------------------------------- missing session
  const missingId = randomUUID();
  await page.goto(`${base}/?shell=desktop&goal=${goalId}&interview=${missingId}`, {waitUntil: 'domcontentloaded'});
  await page.locator('[data-testid="session-missing"]').waitFor({timeout: 60000});
  const missing = await page.locator('[data-testid="session-missing"]').innerText();
  assert.match(missing, /找不到这场面试记录/);
  assert.match(missing, /返回面试/);
  assert.doesNotMatch(missing, /404|not_found|resource_unavailable/, 'missing_session_must_not_print_a_code');
  await page.screenshot({path: join(out, '02-missing-session.png'), fullPage: true});
  await page.locator('[data-testid="session-missing-back"]').click();
  await page.waitForTimeout(600);
  assert.equal(await page.locator('[data-session-id]').count(), 0, 'back_must_leave_the_missing_route');
  assert.equal(await page.locator('#interview-bank').count(), 1, 'back_must_show_the_interview_setup');
  mark('不存在的场次：明确「找不到这场面试记录」+ 返回面试，不打印技术码', {missing: missing.replace(/\n/g, ' / ')});

  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, goal_id: goalId, session_id: sessionId, missing_id: missingId, bank, cases,
    page_errors: pageErrors, note: '真实本地栈；scripted worker，无模型调用。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
