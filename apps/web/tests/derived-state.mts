// F07Z-148 (derived state): a finished session must update the rail badge, the workbench
// progress, the primary action and the recent-session list by itself - no 刷新目标 click and no
// page reload. Real local stack; scripted worker; no model call.
//
//   node --experimental-strip-types apps/web/tests/derived-state.mts
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.D148_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.D148_OUT ?? join(root, '.private/f07z148/derived'));
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
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

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

  // ---------------------------------------------------------------- an in-progress session
  await page.locator('.d-nav-item').filter({hasText: '面试'}).first().click();
  await page.locator('#interview-bank').waitFor({state: 'visible', timeout: 30000});
  await page.locator('button', {hasText: '刷新题库'}).first().click();
  await page.locator('#interview-bank option', {hasText: 'Agent开发岗位试运行'}).first().waitFor({state: 'attached', timeout: 30000});
  await page.locator('#interview-bank').selectOption(`${bank.bank_id}|${bank.version}`);
  await page.locator('button', {hasText: '开始整场面试 →'}).first().click();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 120000});
  await page.waitForTimeout(1500);
  const badgeWhileActive = (await page.locator('.d-nav-item[title="面试"] .d-nav-badge').innerText().catch(() => '')).trim();
  assert.match(badgeWhileActive, /进行中/, `badge_should_show_active ${badgeWhileActive}`);
  mark('进行中：侧栏角标显示进行中', {badge: badgeWhileActive});

  // ---------------------------------------------------------------- finish it, stay on the pane
  const sessionId = await page.locator('[data-session-id]').getAttribute('data-session-id');
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
  await page.waitForTimeout(1200);

  // The badge lives in the shell, four panes away from this one: it must already be 0-active.
  const badgeAfter = (await page.locator('.d-nav-item[title="面试"] .d-nav-badge').innerText().catch(() => '')).trim();
  assert.doesNotMatch(badgeAfter, /进行中/, `stale_active_badge ${badgeAfter}`);
  assert.match(badgeAfter, /场/, `badge_should_still_count_sessions ${badgeAfter}`);
  const railCounts = await page.evaluate(async (id: string) => {
    const response = await fetch(`/api/career-goals/${id}/interviews`, {credentials: 'same-origin'});
    const body = await response.json();
    return body.items.map((item: {status: string}) => item.status);
  }, goalId);
  assert.deepEqual(railCounts, ['COMPLETED'], 'server_should_have_one_completed_session');
  mark('面试完成后侧栏角标立即掉到 0 进行中（事件驱动，不等 6s 轮询、不点刷新）', {badge: badgeAfter, server: railCounts});
  await page.screenshot({path: join(out, '01-rail-after-complete.png'), fullPage: true});

  // ---------------------------------------------------------------- workbench without a refresh click
  await page.locator('.d-nav-item').filter({hasText: '工作台'}).first().click();
  await page.waitForTimeout(900);
  const workbench = await page.evaluate(() => {
    const pane = document.querySelector('.desktop-pane') as HTMLElement;
    const next = pane.querySelector('[data-testid="workbench-next-action"]') as HTMLElement | null;
    const recent = [...pane.querySelectorAll('.d-session-item')].slice(0, 1)
      .map(node => (node as HTMLElement).innerText.replace(/\s+/g, ' '));
    return {
      nextLabel: next?.textContent?.trim() ?? null,
      stages: Object.fromEntries([...pane.querySelectorAll('[data-testid="career-progress"] .d-progress-step')]
        .map(node => [(node as HTMLElement).dataset.stage, (node as HTMLElement).dataset.state])),
      recent,
    };
  });
  assert.equal(workbench.nextLabel, '查看本次复盘', 'workbench_primary_wrong');
  assert.equal(workbench.stages.interview, 'completed');
  assert.equal(workbench.stages.review, 'current');
  assert.equal(workbench.recent.length, 1, 'recent_session_missing');
  assert.match(workbench.recent[0], /已结束/, `recent_session_status ${workbench.recent[0]}`);
  mark('工作台：进度、Primary、近期场次都跟着真实状态走，没有点「刷新目标」', workbench);
  await page.screenshot({path: join(out, '02-workbench-derived.png'), fullPage: true});

  // ---------------------------------------------------------------- refresh keeps the session
  await page.locator('.d-nav-item').filter({hasText: '面试'}).first().click();
  await page.locator('[data-session-id]').waitFor({timeout: 30000});
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(1500);
  const reopened = await page.locator('[data-session-id]').getAttribute('data-session-id').catch(() => null);
  assert.equal(reopened, sessionId, 'reload_must_restore_the_same_session');
  assert.equal(await page.locator('[data-testid="interview-review"]').count(), 1, 'reload_must_still_show_the_review');
  mark('刷新浏览器后仍回到同一场次与复盘（不退回题库选择）', {session_id: reopened});

  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, goal_id: goalId, session_id: sessionId, bank, cases, page_errors: pageErrors,
    note: '真实本地栈；scripted worker，无模型调用。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  await browser?.close();
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
