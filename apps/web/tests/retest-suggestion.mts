// 收尾 01 acceptance: a finished interview's unsolved technical observation becomes one
// authored retest question beside that observation - and nothing appears where the server did
// not resolve a suggestion (inconclusive / supported / no reference).
//
//   node --experimental-strip-types apps/web/tests/retest-suggestion.mts
//
// Real local stack (API 3100, Vite 5174, scripted workers). No model, no injection.
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.REM_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.REM_OUT ?? join(root, '.private/closeout153/browser'));
mkdirSync(out, {recursive: true});

const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => {
  cases.push({name, detail});
  console.log('PASS', name);
};

const goalText = '想在上海找 Java 后端实习，做过接口和数据库。';
const targetName = 'Java 后端开发实习';
// The scripted interviewer labels an answer `needs_probe` only for these markers; that is the
// frozen protocol this suite builds on, not a semantic judgement.
const weakAnswer = '不知道，这块我没做过，也没想清楚。';
const plainAnswer = '我会先看日志和订单表，再和同事核对当时的请求。';

async function session(page: Page, goalId: string, sessionId: string) {
  return page.evaluate(async url => {
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
    return {status: response.status, body: await response.json()};
  }, `/api/career-goals/${goalId}/interviews/${sessionId}`);
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

  // ---------------------------------------------------------------- goal (Java role)
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

  // ---------------------------------------------------------------- two questions, one weak
  await page.locator('.d-nav-item').filter({hasText: '面试'}).first().click();
  await page.waitForFunction(() => document.querySelector('.desktop-shell')?.getAttribute('data-pane') === 'interview',
    undefined, {timeout: 20000});
  const bank = page.locator('#interview-bank');
  await bank.waitFor({state: 'visible', timeout: 60000});
  // The interview role comes from the goal (this goal is an AI-application direction), so the
  // built-in bank must be the matching one; picking another role's bank is rejected by the API.
  const builtin = await page.evaluate(() => {
    const select = document.querySelector('#interview-bank') as HTMLSelectElement | null;
    if (!select) return null;
    const option = [...select.options].find(item => /ai-application 默认/.test(item.textContent ?? ''));
    return option ? option.value : null;
  });
  assert.ok(builtin, 'builtin_bank_missing');
  await bank.selectOption(builtin);
  await page.locator('button', {hasText: '开始整场面试 →'}).first().click();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 90000});
  const firstQuestion = await page.locator('[data-testid="interview-question"]').innerText();
  await page.locator('#interview-answer').fill(weakAnswer);
  await page.locator('[data-testid="answer-submit"]').click();
  await page.waitForFunction(previous => {
    const element = document.querySelector('[data-testid="interview-question"]');
    return !!element && element.textContent !== previous;
  }, firstQuestion, {timeout: 120000});
  await page.locator('#interview-answer').fill(plainAnswer);
  await page.locator('[data-testid="answer-submit"]').click();
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 180000});

  const sessionId = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.ok(sessionId, 'session_id_missing');
  const api = await session(page, goalId, sessionId);
  assert.equal(api.status, 200);
  assert.equal(api.body.status, 'COMPLETED');

  const observations = (api.body.report?.observations ?? []) as {id: string; label: string; topic_id?: string; correction?: string | null}[];
  const resolved = (api.body.report?.retest ?? []) as {observation_id: string; question: string; topic_title: string; pack_version: string}[];
  const eligible = observations.filter(item => item.label === 'needs_probe' && !item.correction && item.topic_id !== 'project');
  assert.ok(observations.length >= 1, 'scripted session must record observations');
  assert.ok(eligible.length >= 1, `a weak answer must produce a needs_probe observation: ${JSON.stringify(observations)}`);
  // F07Z-156: retest.py keeps one drill per topic, so two flagged answers on the same topic
  // collapse into a single suggestion (and must not produce duplicates).
  const distinctTopics = new Set(eligible.map(item => item.topic_id));
  assert.equal(resolved.length, distinctTopics.size, 'one suggestion per distinct topic, no more');
  const topics = resolved.map(item => item.topic_id);
  assert.equal(new Set(topics).size, topics.length, 'no duplicated topic in the suggestions');
  const target = resolved.find(item => eligible.some(o => o.id === item.observation_id));
  assert.ok(target, 'suggestion_must_point_at_the_stored_observation');
  assert.ok(target!.question.length > 8, 'suggestion must carry a real question');
  mark('服务端：needs_probe 观察解析出题干，数量与合格观察一致', {
    observations: observations.map(item => [item.label, item.topic_id]),
    suggestions: resolved.length,
  });

  // ---------------------------------------------------------------- the review page
  const dom = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="interview-review"]') as HTMLElement;
    const blocks = [...root.querySelectorAll('[data-testid^="review-retest-"]')] as HTMLElement[];
    const cards = [...root.querySelectorAll('[data-testid="review-observations"] .interview-observation')] as HTMLElement[];
    return {
      retestCount: blocks.length,
      retests: blocks.map(block => ({
        id: block.dataset.testid,
        text: block.innerText,
        question: (block.querySelector('.d-review-retest-question') as HTMLElement | null)?.innerText ?? '',
      })),
      cards: cards.length,
    };
  });
  assert.equal(dom.retestCount, resolved.length, 'the page must show exactly the server-resolved suggestions');
  const shown = dom.retests.find(item => item.question === target!.question);
  assert.ok(shown, `the authored question must be shown verbatim: ${JSON.stringify(dom.retests)}`);
  assert.match(shown!.text, /可以练这一题/);
  assert.match(shown!.text, /题库版次/);
  assert.match(shown!.text, /练这一题/, 'the card must offer the direct targeted session');
  await page.locator('[data-testid^="review-retest-"]').first().scrollIntoViewIfNeeded();
  await page.screenshot({path: join(out, '01-retest-suggestion.png'), fullPage: true});
  mark('页面：建议块与观察一一对应，逐字显示题干与版次，并给出「练这一题」', {
    retest: dom.retestCount, observations: dom.cards,
  });

  // F07Z-165 A: one click opens a coaching session whose first question is that same text.
  await page.locator(`[data-testid="review-start-retest-${target!.observation_id}"]`).click();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 120000});
  const focusedId = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.ok(focusedId && focusedId !== sessionId, 'a new session must be opened');
  const focused = await session(page, goalId, focusedId!);
  assert.equal(focused.status, 200, JSON.stringify(focused.body).slice(0, 200));
  assert.equal(focused.body.policy.mode, 'coaching');
  const focusedQuestion = (focused.body.events as {kind: string; payload: Record<string, unknown>}[])
    .filter(event => event.kind === 'question').at(-1)!;
  assert.equal(focusedQuestion.payload.text, target!.question);
  assert.equal(focusedQuestion.payload.source, 'retest_suggestion');
  assert.equal((focusedQuestion.payload.focus as {observation_id: string}).observation_id, target!.observation_id);
  assert.equal(focused.body.run?.model_requests, 0, 'the frozen first question must not spend a model round');
  const sourceAfter = await session(page, goalId, sessionId!);
  assert.equal(sourceAfter.body.report.questions, api.body.report.questions, 'the source session stays read-only');
  await page.screenshot({path: join(out, '02-focused-session.png'), fullPage: true});
  mark('点击「练这一题」：新开 coaching 场，首题逐字等于建议题，来源=retest_suggestion，0 次模型轮次', {
    source: sessionId, focused: focusedId, text: focusedQuestion.payload.text,
  });

  // ---------------------------------------------------------------- negative control
  // The plain answer became `inconclusive`; it must not get a drill, and the page must not
  // invent one for observations the server did not resolve.
  const inconclusive = observations.filter(item => item.label !== 'needs_probe');
  for (const item of inconclusive) {
    assert.equal(resolved.some(entry => entry.observation_id === item.id), false,
      `inconclusive observation ${item.id} must not become a targeted drill`);
  }
  const observedIds = new Set(observations.map(item => item.id));
  for (const entry of resolved) assert.ok(observedIds.has(entry.observation_id), 'a suggestion must belong to this session');
  mark('对照：非 needs_probe / 未解析的观察没有建议，也不出现编造的题', {
    inconclusive: inconclusive.length,
  });

  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, goal_id: goalId, session_id: sessionId,
    observations, retest: resolved, cases, page_errors: pageErrors,
    note: 'scripted 协议；题干来自 authored 题库，未调用模型。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
