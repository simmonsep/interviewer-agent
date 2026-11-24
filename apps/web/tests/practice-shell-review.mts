// F07Z-147 acceptance (spec §52-§55): the practice shell (?shell=web) is a public entry, and its
// review must be covered by automation, not only by a private probe and screenshots.
// Real local stack (API 3100 + Vite 5174 + scripted workers); no model call.
//
//   node --experimental-strip-types apps/web/tests/practice-shell-review.mts
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.D147_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.D147_OUT ?? join(root, '.private/f07z147/practice'));
mkdirSync(out, {recursive: true});
const fixture = JSON.parse(readFileSync(join(root, 'fixtures/question-banks/agent-developer-trial.json'), 'utf8'));

const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };
const account = {email: `practice-${randomUUID()}@example.com`, password: `Synthetic-${randomUUID()}`, name: '练习间学员'};
const answer = 'state 是共享的运行状态；checkpointer 按 thread_id 保存检查点，store 放跨线程的长期偏好。';

let browser: Browser | null = null;
let lastPage: import('@playwright/test').Page | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const context = await browser.newContext({viewport: {width: 1366, height: 900}});
  const page = await context.newPage();
  lastPage = page;
  page.on('pageerror', error => pageErrors.push(error.message));

  // ---------------------------------------------------------------- the practice entry itself
  await page.goto(`${base}/?shell=web`, {waitUntil: 'domcontentloaded'});
  await page.getByRole('button', {name: '第一次来？创建账号'}).click();
  await page.getByLabel('称呼').fill(account.name);
  await page.getByLabel('邮箱').fill(account.email);
  await page.getByLabel('密码').fill(account.password);
  await page.getByRole('button', {name: '创建账号', exact: true}).click();
  await page.getByRole('button', {name: '整场面试'}).waitFor({timeout: 60000});
  const shell = await page.evaluate(() => ({
    header: Boolean(document.querySelector('.site-header')),
    sidebar: document.querySelectorAll('nav[aria-label="主导航"]').length,
    desktopShell: document.querySelectorAll('.desktop-shell').length,
  }));
  assert.equal(shell.header, true, 'practice_header_missing');
  assert.equal(shell.sidebar, 0, 'desktop_sidebar_must_not_render_in_practice_shell');
  assert.equal(shell.desktopShell, 0, 'desktop_shell_must_not_render_in_practice_shell');
  mark('练习间入口：有自己的 Header，没有桌面侧栏', shell);

  // ---------------------------------------------------------------- arrange a finished session
  await page.getByRole('button', {name: '整场面试'}).click();
  await page.getByLabel('求职意向或补充说明').fill('想在上海找 AI 应用 / Agent 开发实习。');
  await page.getByRole('button', {name: '保存求职目标'}).click();
  await page.locator('.goal-card').waitFor({timeout: 30000});
  const goalId = await page.evaluate(async () => {
    const response = await fetch('/api/career-goals/', {credentials: 'same-origin'});
    return (await response.json()).items[0] as string;
  });
  const bank = await page.evaluate(async document => {
    const preview = await (await fetch('/api/question-banks/imports', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({document})})).json();
    const commit = await (await fetch(`/api/question-banks/imports/${preview.preview_id}/commit`, {method: 'POST',
      credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({})})).json();
    return {bank_id: commit.bank_id, version: commit.version};
  }, fixture);

  await page.goto(`${base}/?shell=web&goal=${goalId}&view=interview`, {waitUntil: 'domcontentloaded'});
  await page.locator('#interview-bank').waitFor({timeout: 60000});
  await page.locator('#interview-bank').selectOption(`${bank.bank_id}|${bank.version}`);
  await page.getByRole('button', {name: '开始整场面试 →'}).click();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 90000});
  const first = await page.locator('[data-testid="interview-question"]').innerText();
  await page.getByLabel('你的整场面试回答').fill(answer);
  await page.getByRole('button', {name: '提交这一答 →'}).click();
  await page.waitForFunction(previous => {
    const node = document.querySelector('[data-testid="interview-question"]');
    return !!node && node.textContent !== previous;
  }, first, {timeout: 90000});
  await page.getByLabel('你的整场面试回答').fill('第二题：用 Command(resume=...) 恢复，副作用要幂等。');
  await page.getByRole('button', {name: '提交这一答 →'}).click();
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 120000});
  const sessionId = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.ok(sessionId, 'session_id_missing');

  // ---------------------------------------------------------------- the review in this shell
  const review = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="interview-review"]') as HTMLElement;
    return {
      layers: ['interview-report', 'review-answers', 'review-next'].map(id => Boolean(root.querySelector(`[data-testid="${id}"]`))),
      answers: [...root.querySelectorAll('.d-review-answer-text')].map(node => node.textContent ?? ''),
      disclaimer: root.innerText,
      appsCta: root.querySelectorAll('[data-testid="review-to-applications"]').length,
      again: [...root.querySelectorAll('button')].filter(button => button.textContent?.trim() === '另开一场').length,
      label: (root.querySelector('.eyebrow') as HTMLElement | null)?.textContent ?? '',
    };
  });
  assert.deepEqual(review.layers, [true, true, true], `review_layers ${JSON.stringify(review.layers)}`);
  assert.equal(review.answers.length, 2, 'review_must_keep_both_answers');
  assert.ok(review.answers[0].includes('共享的运行状态'), 'answer_text_missing');
  assert.match(review.disclaimer, /脚本流程/);
  assert.match(review.disclaimer, /不提供真实能力评价/);
  assert.equal(review.appsCta, 0, 'practice_shell_must_not_offer_an_application_cta');
  assert.equal(review.again, 1, 'practice_shell_keeps_another_session');
  assert.equal(review.label, '面试复盘', 'practice_review_needs_its_own_label');
  await page.screenshot({path: join(out, '01-practice-review.png'), fullPage: true});
  mark('练习间复盘：四层结构、逐题原文、脚本免责声明都在；没有投递入口（这是真实产品差异）', review);

  // ---------------------------------------------------------------- a finished session can be reopened
  await page.getByRole('button', {name: '另开一场'}).click();
  await page.locator('[data-session-id]').waitFor({state: 'detached', timeout: 15000});
  await page.locator('.interview-history .history-item').first().click();
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 60000});
  assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'), sessionId,
    'history_must_reopen_the_same_session');
  assert.equal(await page.locator('[data-testid="review-to-applications"]').count(), 0);
  await page.screenshot({path: join(out, '02-practice-history-reopen.png'), fullPage: true});
  mark('练习间可以从历史场次重新打开同一份复盘，仍然没有投递入口', {session_id: sessionId});

  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, goal_id: goalId, session_id: sessionId, bank, cases, page_errors: pageErrors,
    note: '真实本地栈；scripted worker，无模型调用。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  await lastPage?.screenshot({path: join(out, 'failure.png'), fullPage: true}).catch(() => {});
  if (lastPage) console.error('BODY', (await lastPage.locator('body').innerText().catch(() => '')).replace(/\s+/g, ' ').slice(0, 700));
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
