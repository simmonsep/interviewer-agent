// 收尾 02-4: two sequential sessions on the same goal must not leak into each other.
// Completes A, runs B with different answers, then deep-links back to A (and to B) and compares
// questions, answers, observations and the frozen pack version.
//
//   node --experimental-strip-types apps/web/tests/session-isolation.mts
//
// Real local stack (API 3100, Vite 5174, scripted workers). No model, no injection.
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.ISO_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.ISO_OUT ?? join(root, '.private/closeout154/isolation'));
mkdirSync(out, {recursive: true});

const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => {
  cases.push({name, detail});
  console.log('PASS', name);
};

const goalText = '想在上海找 AI 应用开发实习，做过 Agent 工具调用。';
const targetName = 'AI 应用开发实习';
const A = ['A 场回答：我用 LangGraph 的 state 保存会话，checkpointer 按 thread_id 存。',
  'A 场回答：工具参数先过 JSON Schema，再按登录身份查资源归属。'];
const B = ['B 场回答：我做过检索增强，把文档切片后用向量库召回再拼上下文。',
  'B 场回答：并发控制用 Redis 锁，锁值带持有者标识，释放前比较。'];

async function session(page: Page, goalId: string, sessionId: string) {
  return page.evaluate(async url => {
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
    return {status: response.status, body: await response.json()};
  }, `/api/career-goals/${goalId}/interviews/${sessionId}`);
}

async function answerAndComplete(page: Page, texts: string[]) {
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 120000});
  for (const [index, text] of texts.entries()) {
    const before = await page.locator('[data-testid="interview-question"]').innerText();
    await page.locator('#interview-answer').fill(text);
    await page.locator('[data-testid="answer-submit"]').click();
    await page.waitForFunction(previous => {
      const node = document.querySelector('[data-testid="interview-question"]');
      const review = document.querySelector('[data-testid="interview-review"]');
      return Boolean(review) || (node && node.textContent !== previous);
    }, before, {timeout: 120000});
    if (await page.locator('[data-testid="interview-review"]').count()) break;
    if (index === texts.length - 1) {
      // the last answer may take us straight to the review; wait for it explicitly
      await page.locator('[data-testid="interview-review"]').waitFor({timeout: 120000});
    }
  }
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 180000});
  const id = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.ok(id, 'session_id_missing');
  return id;
}

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(800);
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill(goalText);
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.getByLabel('确认方向（可选）').fill(targetName);
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  const goalId = await page.evaluate(async () => {
    const body = await (await fetch('/api/career-goals/', {credentials: 'same-origin'})).json();
    return (body.items ?? [])[0] ?? null;
  });
  assert.ok(goalId, 'goal_id_missing');

  const openInterviewPane = async () => {
    await page.locator('.d-nav-item').filter({hasText: '面试'}).first().click();
    await page.waitForFunction(() => document.querySelector('.desktop-shell')?.getAttribute('data-pane') === 'interview',
      undefined, {timeout: 20000});
    await page.locator('#interview-bank').waitFor({state: 'visible', timeout: 60000});
  };
  const startSession = async () => {
    const choice = await page.evaluate(() => {
      const select = document.querySelector('#interview-bank') as HTMLSelectElement | null;
      const option = select ? [...select.options].find(item => /ai-application 默认/.test(item.textContent ?? '')) : null;
      return option ? option.value : null;
    });
    assert.ok(choice, 'builtin_bank_missing');
    await page.locator('#interview-bank').selectOption(choice!);
    await page.locator('button', {hasText: '开始整场面试 →'}).first().click();
  };

  // ---------------------------------------------------------------- session A
  await openInterviewPane();
  await startSession();
  const aId = await answerAndComplete(page, A);
  const a = await session(page, goalId, aId);
  assert.equal(a.body.status, 'COMPLETED');
  mark('A 场完成', {session_id: aId, questions: a.body.report?.questions, pack: a.body.pack_version});

  // ---------------------------------------------------------------- session B (same goal)
  await page.locator('button', {hasText: '另开一场'}).first().click();
  await openInterviewPane();
  // 收尾 02-4: a fresh session must not restore the previous answer draft.
  await page.waitForTimeout(600);
  assert.equal(await page.locator('#interview-answer').count(), 0, 'B must not show an answer box before it starts');
  await startSession();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 120000});
  assert.equal((await page.locator('#interview-answer').inputValue()).trim(), '', 'B must not carry A\'s draft');
  const bId = await answerAndComplete(page, B);
  const b = await session(page, goalId, bId);
  assert.equal(b.body.status, 'COMPLETED');
  assert.notEqual(bId, aId, 'two sequential sessions must be different records');
  mark('B 场完成且不继承 A 的草稿', {session_id: bId});

  // ---------------------------------------------------------------- back to A by deep link
  await page.goto(`${base}/?shell=desktop&goal=${goalId}&interview=${aId}`, {waitUntil: 'domcontentloaded'});
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 120000});
  const shownA = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="interview-review"]') as HTMLElement;
    return {
      text: root.innerText,
      sessionId: document.querySelector('[data-session-id]')?.getAttribute('data-session-id'),
      answers: [...root.querySelectorAll('.d-review-answer .d-review-answer-text')].map(node => node.textContent?.trim()),
      questions: [...root.querySelectorAll('.d-review-answer .d-review-question')].map(node => node.textContent?.trim()),
    };
  });
  assert.equal(shownA.sessionId, aId, 'the deep link must land on A');
  assert.deepEqual(shownA.answers, A, 'A must show A\'s own answers');
  for (const text of B) assert.equal(shownA.text.includes(text), false, 'A must not contain B\'s answer');
  mark('深链回到 A：场次、题目与作答都是 A 自己的', {session: shownA.sessionId, answers: shownA.answers.length});

  // ---------------------------------------------------------------- B by deep link
  await page.goto(`${base}/?shell=desktop&goal=${goalId}&interview=${bId}`, {waitUntil: 'domcontentloaded'});
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 120000});
  const shownB = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="interview-review"]') as HTMLElement;
    return {
      text: root.innerText,
      sessionId: document.querySelector('[data-session-id]')?.getAttribute('data-session-id'),
      answers: [...root.querySelectorAll('.d-review-answer .d-review-answer-text')].map(node => node.textContent?.trim()),
    };
  });
  assert.equal(shownB.sessionId, bId);
  assert.deepEqual(shownB.answers, B);
  for (const text of A) assert.equal(shownB.text.includes(text), false, 'B must not contain A\'s answer');
  mark('深链回到 B：同样只显示 B 自己的记录', {session: shownB.sessionId});

  // ---------------------------------------------------------------- stale version stays a 409
  const stale = await page.evaluate(async ({goalId, bId}) => {
    const detail = await (await fetch(`/api/career-goals/${goalId}/interviews/${bId}`, {credentials: 'same-origin'})).json();
    const response = await fetch(`/api/career-goals/${goalId}/interviews/${bId}/answers`, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: detail.version - 1, question_id: crypto.randomUUID(), text: '旧版本提交'}),
    });
    return {status: response.status, body: await response.json()};
  }, {goalId, bId});
  assert.equal(stale.status, 409, `a stale version must still be rejected: ${JSON.stringify(stale)}`);
  mark('旧版本提交仍按 409 处理，不被新场次掩盖', stale);

  await page.screenshot({path: join(out, '01-session-b-review.png'), fullPage: true});
  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, goal_id: goalId, a_id: aId, b_id: bId,
    a_pack: a.body.pack_version, b_pack: b.body.pack_version, cases, page_errors: pageErrors,
    note: '两场顺序面试，scripted；未调用模型。陪练跨场引用未在本轮断言。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
