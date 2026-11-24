// F07Z-149 acceptance: a 10-15 question review must stay readable and must not break the layout.
// The product's interview contract caps a session at 6 questions (contracts.py), so this suite
// renders a fixed 12-question fixture through the real review component instead of pretending a
// 12-question live session exists. Every answer kind the spec lists is represented: short, long
// Chinese, long English, mixed, code, URL, a 200-char token, newlines and a list.
//
//   node --experimental-strip-types apps/web/tests/review-long-session.mts
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.D149_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.D149_OUT ?? join(root, '.private/f07z149/long'));
mkdirSync(out, {recursive: true});
const fixture = JSON.parse(readFileSync(join(root, 'fixtures/question-banks/agent-developer-trial.json'), 'utf8'));

const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };
const targetName = 'AI 应用开发实习生';

const chinese = '这一题我按“定位问题—复现—验证—回归”的顺序说明：先看错误日志与调用链，再用最小用例复现，确认是并发写入导致的重复记录；修复时先加业务幂等键，再用数据库唯一约束兜底，最后补一条并发回归用例。';
const english = 'I would first isolate the failure by reading the structured logs, then reproduce it with the smallest possible request. The fix needs two layers: an idempotency key in the API and a unique constraint in the database, so a retried request cannot create a second record even when two workers race.';
const code = ['```python', 'async def submit(session_id: str, text: str) -> None:', '    key = f"answer:{session_id}:{hashlib.sha256(text.encode()).hexdigest()}"', '    await repository.append_answer(session_id, text, idempotency_key=key)', '    await publisher.publish("answers", {"session_id": session_id, "key": key})', '```'].join('\n');
const longToken = `token_${'a'.repeat(190)}`;
const longUrl = `https://example.com/very/long/path/${'segment-'.repeat(12)}reference?query=${'x'.repeat(120)}`;

const answers = [
  '短答：先复现，再修。',
  `${chinese}${chinese}${chinese}${chinese}${chinese}尾部标记-中文长答结束。`,
  `${english} ${english} ${english}${english} TAIL-EN-END.`,
  `${chinese}\n\n${english}\n\n混排结束 TAIL-MIXED-END。`,
  code,
  `参考链接：${longUrl}`,
  `超长 token：${longToken}`,
  '第一行：现象。\n第二行：原因。\n第三行：修复。\n第四行：回归。\n第五行结束。',
  '- 定位：日志与调用链\n- 复现：最小用例\n- 修复：幂等键 + 唯一约束\n- 回归：并发用例\n- 观测：错误率与重试数',
  `${chinese}${chinese}${chinese}尾部标记-中文长答二结束。`,
  '短答：先看状态机，再动手。',
  `${english}\n${code}\n${chinese} TAIL-FINAL-END.`,
];

interface Event {id: string; seq: number; kind: string; payload: Record<string, unknown>}
const events: Event[] = [];
answers.forEach((text, index) => {
  events.push({id: randomUUID(), seq: index * 2 + 1, kind: 'question',
    payload: {topic_id: `topic-${index}`, question_id: `q-${index}`, text: index === 8
      ? '这一题的口径很长，用来验证问题本身不折叠：请结合一次真实故障，说明你如何在不引入重复写的前提下完成修复，并给出你会观察到哪些指标、如何回滚、以及怎样向非技术同学解释这次改动的风险边界。'
      : `第 ${index + 1} 题：${index % 3 === 0 ? '说明你的判断依据。' : '结合一次真实经历说明。'}`,
      kind: 'open', pack_version: 'long-fixture', assisted: false}});
  events.push({id: randomUUID(), seq: index * 2 + 2, kind: 'answer',
    payload: {text, question_id: events[events.length - 1].id, topic_id: `topic-${index}`, assisted: false}});
});
const observations = answers.map((text, index) => ({
  id: randomUUID(), answer_id: `answer-${index}`, label: index % 2 ? 'needs_probe' : 'supported_in_this_answer',
  summary: `第 ${index + 1} 题的回答里给出了可核对的做法；${index % 2 ? '还可以补充一个量化结果。' : '引用了真实材料里的句子。'}`,
  answer_quote: text.slice(0, 160), evidence_id: `doc#${index + 1}`,
  evidence_quote: `${chinese.slice(0, 80)}${english.slice(0, 80)}`,
  origin: 'scripted_protocol',
  evidence_source: {id: `doc#${index + 1}`, url: longUrl, origin: 'scripted_protocol', version: '1'},
}));
const longReport = {
  stop_reason: 'question_limit', model_summary: null, questions: 12, answers: 12, assisted_answers: 0,
  observations, semantic_scoring_eligible: false, unreviewed_answer_ids: [], next_actions: [],
};

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  // ---------------------------------------------------------------- a real terminal session to hang the fixture on
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
  await page.locator('.d-nav-item[title="面试"]').click();
  await page.locator('#interview-bank').waitFor({state: 'visible', timeout: 30000});
  await page.locator('button', {hasText: '刷新题库'}).first().click();
  await page.locator('#interview-bank option', {hasText: 'Agent开发岗位试运行'}).first().waitFor({state: 'attached', timeout: 30000});
  await page.locator('#interview-bank').selectOption(`${bank.bank_id}|${bank.version}`);
  await page.locator('button', {hasText: '开始整场面试 →'}).first().click();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 120000});
  const sessionId = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.ok(sessionId, 'session_id_missing');

  // The review only renders for a terminal session; finish the real two-question one, then
  // replace its payload with the 12-question fixture on every read.
  const first = await page.locator('[data-testid="interview-question"]').innerText();
  await page.locator('#interview-answer').fill('第一答。');
  await page.locator('[data-testid="answer-submit"]').click();
  await page.waitForFunction(previous => {
    const node = document.querySelector('[data-testid="interview-question"]');
    return !!node && node.textContent !== previous;
  }, first, {timeout: 120000});
  await page.locator('#interview-answer').fill('第二答。');
  await page.locator('[data-testid="answer-submit"]').click();
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 180000});

  await page.route('**/api/career-goals/*/interviews/*', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const response = await route.fetch();
    const body = await response.json();
    return route.fulfill({response, json: {...body, status: 'COMPLETED', version: body.version + 1,
      events, observations, report: longReport}});
  });
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 90000});

  // ---------------------------------------------------------------- 1440 layout + content
  const wide = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="interview-review"]') as HTMLElement;
    const cards = [...root.querySelectorAll('.d-review-answer')];
    const facts = (root.querySelector('[data-testid="interview-report"]') as HTMLElement).innerText;
    const toggles = [...root.querySelectorAll('.d-answer-toggle')];
    const clamp = root.querySelector('.d-review-answer-text.d-clamp') as HTMLElement | null;
    return {
      cards: cards.length,
      toggles: toggles.length,
      facts,
      gridColumns: getComputedStyle(root.querySelector('.interview-report-grid')!).gridTemplateColumns.split(' ').length,
      overflow: document.documentElement.scrollWidth > window.innerWidth,
      collapsed: Boolean(clamp),
      codeOverflow: [...root.querySelectorAll('.d-review-answer-text')]
        .some(node => (node as HTMLElement).scrollWidth > (node as HTMLElement).clientWidth + 1),
      domNodes: root.querySelectorAll('*').length,
      longQuestionVisible: [...root.querySelectorAll('.d-review-question')]
        .some(node => (node.textContent ?? '').includes('向非技术同学解释这次改动的风险边界')),
      questionClamped: [...root.querySelectorAll('.d-review-question')]
        .some(node => node.classList.contains('d-clamp')),
    };
  });
  assert.equal(wide.cards, 12, 'fixture_must_render_12_answers');
  assert.match(wide.facts, /题目\s*12/);
  assert.match(wide.facts, /回答\s*12/);
  assert.match(wide.facts, /答题观察\s*12/);
  assert.ok(wide.toggles >= 2, `long_answers_must_offer_a_toggle ${wide.toggles}`);
  assert.equal(wide.gridColumns, 4, `wide_grid_should_keep_four_columns ${wide.gridColumns}`);
  assert.equal(wide.overflow, false, 'wide_page_must_not_scroll_horizontally');
  assert.equal(wide.codeOverflow, false, 'code_answer_must_wrap_inside_its_card');
  // §39/§40: the prompt itself is never folded - understanding the answer needs all of it.
  assert.equal(wide.longQuestionVisible, true, 'long_question_must_render_in_full');
  assert.equal(wide.questionClamped, false, 'question_must_not_be_clamped');
  mark('1440：12 题、计数两位数不破版、四列摘要、长答案默认折叠、代码/URL 不撑宽', wide);
  await page.screenshot({path: join(out, '01-long-review-1440.png'), fullPage: true});

  // ---------------------------------------------------------------- fold keeps the full text and is not an accordion
  const folded = await page.evaluate(() => {
    const cards = [...document.querySelectorAll('.d-review-answer')];
    const texts = cards.map(card => (card.querySelector('.d-review-answer-text') as HTMLElement)?.textContent ?? '');
    return {detail: texts[1].includes('尾部标记-中文长答结束'), englishTail: texts[2].includes('TAIL-EN-END'), final: texts[11].includes('TAIL-FINAL-END')};
  });
  assert.equal(folded.detail, true, 'clamped_answer_must_still_keep_the_full_text');
  assert.equal(folded.englishTail, true);
  assert.equal(folded.final, true, 'mixed_answer_with_code_must_keep_its_tail');
  await page.locator('[data-testid="review-answer-toggle-2"]').click();
  await page.locator('[data-testid="review-answer-toggle-3"]').click();
  const expanded = await page.evaluate(() => ({
    two: document.querySelector('[data-testid="review-answer-toggle-2"]')?.getAttribute('aria-expanded'),
    three: document.querySelector('[data-testid="review-answer-toggle-3"]')?.getAttribute('aria-expanded'),
  }));
  assert.equal(expanded.two, 'true');
  assert.equal(expanded.three, 'true', 'expanding one answer must not collapse another');
  mark('展开：原文一直在 DOM 里；aria-expanded 正确；同时展开多题不会互相收起', expanded);
  await page.screenshot({path: join(out, '02-long-review-expanded.png'), fullPage: true});

  // ---------------------------------------------------------------- 390
  // Back to the default (folded) state: the 390 shot must show what a user sees on arrival.
  await page.locator('[data-testid="review-answer-toggle-2"]').click();
  await page.locator('[data-testid="review-answer-toggle-3"]').click();
  await page.setViewportSize({width: 390, height: 844});
  await page.waitForTimeout(600);
  const narrow = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="interview-review"]') as HTMLElement;
    return {
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      gridColumns: getComputedStyle(root.querySelector('.interview-report-grid')!).gridTemplateColumns.split(' ').length,
      toggleBox: (() => { const b = root.querySelector('.d-answer-toggle') as HTMLElement | null; if (!b) return null; const r = b.getBoundingClientRect(); return {w: Math.round(r.width), h: Math.round(r.height)}; })(),
    };
  });
  assert.equal(narrow.scrollWidth, narrow.innerWidth, `narrow_overflow ${JSON.stringify(narrow)}`);
  assert.equal(narrow.gridColumns, 2, 'narrow_grid_should_become_two_columns');
  assert.ok(narrow.toggleBox && narrow.toggleBox.h >= 24, `toggle_tap_target_too_small ${JSON.stringify(narrow.toggleBox)}`);
  mark('390：无横向滚动、摘要 2×2、展开按钮可点', narrow);
  await page.evaluate(() => {
    document.querySelector('.desktop-body')?.scrollTo({top: 0});
    document.querySelector('.d-question-scroll')?.scrollTo({top: 0});
    window.scrollTo(0, 0);
  });
  await page.waitForTimeout(300);
  await page.screenshot({path: join(out, '03-long-review-390.png'), fullPage: true});
  await page.setViewportSize({width: 1440, height: 1000});

  // ---------------------------------------------------------------- history reopen keeps the same 12
  await page.locator('button', {hasText: '另开一场'}).first().click();
  await page.locator('[data-session-id]').waitFor({state: 'detached', timeout: 20000});
  await page.locator('.interview-history .history-item').first().click();
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 60000});
  assert.equal(await page.locator('.d-review-answer').count(), 12, 'reopen_must_keep_the_same_fixture');
  mark('历史场次重新打开：仍是同一份 12 题复盘');

  // ---------------------------------------------------------------- practice shell (same page, same cookies)
  await page.goto(`${base}/?shell=web&goal=${goalId}&interview=${sessionId}`, {waitUntil: 'domcontentloaded'});
  await page.locator('.site-header').first().waitFor({timeout: 60000});
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 90000});
  const practiceView = await page.evaluate(() => ({
    answers: document.querySelectorAll('.d-review-answer').length,
    sidebar: document.querySelectorAll('nav[aria-label="主导航"]').length,
    appsCta: document.querySelectorAll('[data-testid="review-to-applications"]').length,
    overflow: document.documentElement.scrollWidth > window.innerWidth,
  }));
  assert.equal(practiceView.answers, 12);
  assert.equal(practiceView.sidebar, 0, 'practice_shell_must_not_show_the_desktop_sidebar');
  assert.equal(practiceView.appsCta, 0, 'practice_shell_must_not_offer_an_application_cta');
  assert.equal(practiceView.overflow, false);
  mark('练习间：同一份 12 题复盘，无桌面侧栏、无投递 CTA、无横向溢出', practiceView);
  await page.screenshot({path: join(out, '04-long-review-practice.png'), fullPage: true});

  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, goal_id: goalId, session_id: sessionId, bank, cases, page_errors: pageErrors,
    fixture: {questions: 12, answers: 12, observations: 12},
    note: '12 题 fixture 注入真实终态场次的读取；scripted worker 跑了真实两题以拿到终态场次，无模型调用。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
