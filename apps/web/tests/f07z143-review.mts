// F07Z-143 acceptance (spec §98-100, §105): a finished interview becomes a real review, the
// workbench hands over to it, and the review hands over to the application step.
// Real local stack (API 3100 + Vite 5174 + scripted workers + test DB); no model call.
//
//   node --experimental-strip-types apps/web/tests/f07z143-review.mts
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.D143_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.D143_OUT ?? join(root, '.private/f07z143/evidence'));
mkdirSync(out, {recursive: true});
const fixture = JSON.parse(readFileSync(join(root, 'fixtures/question-banks/agent-developer-trial.json'), 'utf8'));

const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };
// F07Z-151 (arbiter Q3): the heading node exists while the goal is still loading; wait for the goal.
const waitGoalReady = (page: Page) => page.waitForFunction(() => {
  const node = document.querySelector('[data-testid="workbench-goal"]');
  const text = node?.textContent?.trim() ?? '';
  return text !== '' && text !== '创建求职目标' && text !== '正在读取目标…';
}, undefined, {timeout: 90000});

const goalText = '研二，想在上海找 AI 应用 / Agent 开发实习。';
const targetName = 'AI 应用开发实习生';

// Same math as the F07Z-75 contrast suite, applied to the review surface so the new states are
// covered by the existing measure instead of being assumed. AA body text = 4.5:1.
const REVIEW_CONTRAST = () => {
  const parse = (value: string) => {
    const match = value.match(/rgba?\(([^)]+)\)/);
    if (!match) return null;
    const parts = match[1].split(',').map(part => parseFloat(part.trim()));
    return {r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1};
  };
  const lum = (color: {r: number; g: number; b: number}) => {
    const channel = (value: number) => { value /= 255; return value <= 0.03928 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4); };
    return 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b);
  };
  const backgroundOf = (element: Element) => {
    let node: Element | null = element;
    while (node) {
      const bg = parse(getComputedStyle(node).backgroundColor);
      if (bg && bg.a >= 0.99) return bg;
      node = node.parentElement;
    }
    return parse(getComputedStyle(document.body).backgroundColor) ?? {r: 255, g: 255, b: 255, a: 1};
  };
  const targets: [string, string][] = [
    ['.d-review-title h2', '复盘标题'], ['.d-review-meta', '复盘元信息'],
    ['.d-review-badges .tag', '复盘状态标签'], ['.d-review-modenote', '模式说明'],
    ['.d-review-block h3', '分区标题'], ['.d-review-question', '题目'],
    ['.d-review-answer-text', '你的回答'], ['.d-review-next p', '下一步说明'],
    ['.d-review .interview-report-grid span', '事实格子'],
  ];
  const rows: {label: string; ratio: number; ok: boolean}[] = [];
  for (const [selector, label] of targets) {
    const element = document.querySelector(selector) as HTMLElement | null;
    if (!element || !element.offsetParent) continue;
    const style = getComputedStyle(element);
    const fg = parse(style.color);
    if (!fg) continue;
    let opacity = 1;
    let node: Element | null = element;
    while (node) { opacity *= parseFloat(getComputedStyle(node).opacity || '1'); node = node.parentElement; }
    const bg = backgroundOf(element);
    const fgAlpha = Math.min(1, (fg.a ?? 1) * opacity);
    const blended = {
      r: fg.r * fgAlpha + bg.r * (1 - fgAlpha),
      g: fg.g * fgAlpha + bg.g * (1 - fgAlpha),
      b: fg.b * fgAlpha + bg.b * (1 - fgAlpha),
      a: 1,
    };
    const l1 = lum(blended), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(style.fontSize);
    const weight = parseInt(style.fontWeight, 10) || 400;
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    rows.push({label, ratio: Math.round(ratio * 100) / 100, ok: ratio >= (large ? 3 : 4.5)});
  }
  return rows;
};
const answer1 = 'state 是图内共享的运行状态；checkpointer 按 thread_id 保存检查点，store 放跨线程的长期偏好。独立会话要用不同 thread_id。';
const answer2 = 'interrupt 借助检查点暂停，用 Command(resume=...) 恢复；恢复会从含 interrupt 的节点重跑，所以副作用要幂等。';

async function detail(page: Page, goalId: string, sessionId: string) {
  return page.evaluate(async url => {
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
    return {status: response.status, body: await response.json()};
  }, `/api/career-goals/${goalId}/interviews/${sessionId}`);
}

async function importBank(page: Page) {
  return page.evaluate(async document => {
    const previewResponse = await fetch('/api/question-banks/imports', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()}, body: JSON.stringify({document})});
    const preview = await previewResponse.json();
    if (!previewResponse.ok) throw new Error('preview_failed ' + JSON.stringify(preview));
    const commitResponse = await fetch(`/api/question-banks/imports/${preview.preview_id}/commit`, {method: 'POST',
      credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({})});
    const commit = await commitResponse.json();
    if (!commitResponse.ok) throw new Error('commit_failed ' + JSON.stringify(commit));
    return {bank_id: commit.bank_id, version: commit.version};
  }, fixture);
}

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');

  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(800);

  // ---------------------------------------------------------------- arrange: goal + bank
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill(goalText);
  // PART C: a confirmed direction is a real target name; set it through the same optional
  // field a user fills instead of asserting on a hidden default.
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.getByLabel('确认方向（可选）').fill(targetName);
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  const goalId = await page.evaluate(async () => {
    const response = await fetch('/api/career-goals/', {credentials: 'same-origin'});
    const body = await response.json();
    return (body.items ?? [])[0] ?? null;
  });
  assert.ok(goalId, 'goal_id_missing');
  const bank = await importBank(page);
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 60000});
  await page.locator('[data-testid="workbench-goal"]').waitFor({timeout: 60000});
  await page.waitForTimeout(1200);

  // ---------------------------------------------------------------- run the two-question session
  // The rail click is idempotent but the pane can be mid-render right after a reload, so the
  // test states its own precondition instead of relying on one lucky click.
  await page.locator('.d-nav-item').filter({hasText: '面试'}).first().click();
  await page.waitForFunction(() => document.querySelector('.desktop-shell')?.getAttribute('data-pane') === 'interview',
    undefined, {timeout: 20000});
  await page.locator('#interview-bank').waitFor({state: 'visible', timeout: 60000});
  await page.locator('#interview-bank').selectOption(`${bank.bank_id}|${bank.version}`);
  await page.locator('button', {hasText: '开始整场面试 →'}).first().click();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 90000});
  const firstQuestion = await page.locator('[data-testid="interview-question"]').innerText();
  await page.locator('#interview-answer').fill(answer1);
  await page.locator('[data-testid="answer-submit"]').click();
  await page.waitForFunction(previous => {
    const element = document.querySelector('[data-testid="interview-question"]');
    return !!element && element.textContent !== previous;
  }, firstQuestion, {timeout: 90000});
  await page.locator('#interview-answer').fill(answer2);
  await page.locator('[data-testid="answer-submit"]').click();
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 120000});
  const sessionId = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.ok(sessionId, 'session_id_missing');
  const api = await detail(page, goalId, sessionId);
  assert.equal(api.status, 200);
  assert.equal(api.body.status, 'COMPLETED');
  mark('真实两题面试完成；同一场次落在复盘视图里', {session_id: sessionId, bank});

  // ---------------------------------------------------------------- the review itself
  const review = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="interview-review"]') as HTMLElement;
    const text = root.innerText;
    const answerCards = [...root.querySelectorAll('[data-testid="review-answers"] .d-review-answer')];
    return {
      text,
      state: root.dataset.reviewState,
      status: (root.querySelector('[data-testid="review-status"]') as HTMLElement).innerText,
      mode: (root.querySelector('[data-testid="review-mode-note"]') as HTMLElement).innerText,
      completion: (root.querySelector('[data-testid="review-completion"]') as HTMLElement).innerText,
      facts: (root.querySelector('[data-testid="interview-report"]') as HTMLElement).innerText,
      answerCount: answerCards.length,
      answers: answerCards.map(card => ({
        question: (card.querySelector('.d-review-question') as HTMLElement)?.innerText,
        labels: [...card.querySelectorAll('.eyebrow')].map(node => node.textContent),
        answer: (card.querySelector('.d-review-answer-text') as HTMLElement)?.innerText ?? null,
      })),
      observations: root.querySelectorAll('[data-testid="review-observations"] .interview-observation').length,
      primaries: [...root.querySelectorAll('button.primary')].map(button => button.textContent?.trim()),
      entryKind: (root.querySelector('[data-application-entry]') as HTMLElement | null)?.dataset.applicationEntry ?? null,
      next: (root.querySelector('[data-testid="review-next"]') as HTMLElement).innerText,
      goalTitle: (root.querySelector('.d-review-title h2') as HTMLElement).innerText,
    };
  });
  assert.equal(review.state, 'ready');
  assert.equal(review.status, '复盘可查看');
  assert.match(review.mode, /仅整理作答记录/);
  assert.match(review.completion, /本次模拟面试已完成/);
  assert.match(review.completion, /2 \/ 2 道题已作答/);
  assert.match(review.facts, /题数已达到本场上限/);
  assert.match(review.facts, /脚本流程/);
  assert.equal(review.answerCount, 2, 'review_must_list_each_question');
  assert.ok(review.answers.every(item => item.labels.includes('你的回答')), 'each answer must be labelled 你的回答');
  assert.equal(review.answers[0].answer, answer1, 'review_must_show_the_users_own_words');
  assert.equal(review.answers[1].answer, answer2);
  assert.ok(review.observations >= 1, 'scripted observations should be listed');
  // F07Z-145: this goal has no selected job yet, so the one primary must say so.
  assert.deepEqual(review.primaries, ['选择岗位并记录投递'], 'review_must_name_the_job_precondition');
  assert.equal(review.entryKind, 'select-position-first');
  assert.match(review.next, /下一步/);
  assert.equal(review.goalTitle, targetName, 'review_must_name_the_confirmed_target');
  const chrome = [review.status, review.mode, review.completion, review.next, review.facts].join('\n');
  for (const forbidden of ['能力评分', '匹配度', '通过概率', '分数']) {
    // Observation text is backend data (it may say "not a score"); the UI's own words must not
    // offer one.
    assert.equal(chrome.includes(forbidden), false, `review_chrome_forbidden ${forbidden}`);
  }
  mark('复盘分层：本次结果 / 你的作答（逐题原文）/ 事实与观察 / 下一步；唯一 Primary 按岗位前置条件命名，不伪造评分', review);
  await page.screenshot({path: join(out, '01-review-1440.png'), fullPage: true});

  // ---------------------------------------------------------------- review -> position (no job yet)
  if (!review.next.includes('岗位研究')) throw new Error(`select_first_copy_missing ${review.next}`);
  const goalVersion = await page.evaluate(async (id: string) => {
    const response = await fetch(`/api/career-goals/${id}`, {credentials: 'same-origin'});
    return (await response.json()).version as number;
  }, goalId);
  const manual = await page.evaluate(async ([id, version]: [string, number]) => {
    const response = await fetch(`/api/career-goals/${id}/jobs/manual`, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: version, company: '合成科技', title: 'AI 应用开发实习生',
        cities: ['上海'], recruitment_label: '', url: 'https://example.com/jobs/1',
        jd_text: '负责 Agent 应用开发，熟悉 Python 与 LangGraph。'})});
    return {status: response.status, body: await response.json()};
  }, [goalId, goalVersion] as [string, number]);
  assert.ok([200, 201].includes(manual.status), `manual_job_failed ${manual.status} ${JSON.stringify(manual.body)}`);

  await page.locator('[data-testid="review-to-applications"]').click();
  await page.waitForFunction(() => document.querySelector('.desktop-shell')?.getAttribute('data-pane') === 'research',
    undefined, {timeout: 20000});
  assert.equal(await page.locator('.desktop-shell').getAttribute('data-pane'), 'research');
  mark('无岗位：复盘 CTA 直接落到岗位研究，没有先送用户去一个会被拒绝的投递表单');

  // The applications page keeps its own guard (§18) and now offers the way out (§19).
  await page.locator('.d-nav-item').filter({hasText: '申请跟踪'}).first().click();
  await page.locator('[data-testid="apps-job-guard"]').waitFor({timeout: 20000});
  const guardText = await page.locator('[data-testid="apps-job-guard"]').innerText();
  assert.match(guardText, /还没有选定岗位/);
  assert.match(guardText, /投递必须绑定一个岗位/);
  assert.equal(await page.locator('[data-testid="apps-open-research"]').count(), 1, 'guard_must_offer_a_way_out');
  await page.screenshot({path: join(out, '05b-apps-guard-1440.png'), fullPage: true});
  await page.locator('[data-testid="apps-open-research"]').click();
  await page.waitForFunction(() => document.querySelector('.desktop-shell')?.getAttribute('data-pane') === 'research',
    undefined, {timeout: 20000});
  mark('投递页保留自己的岗位校验，并给出「选择岗位」出口（不依赖上游一定正确）', {guard: guardText.replace(/\n/g, ' / ')});

  // The research pane was mounted before the job was created through the API, so ask it to
  // re-read the list once (same button a user has) instead of relying on a remount.
  await page.locator('button', {hasText: '刷新岗位列表'}).first().click();
  // The selection itself stays a real user action (no auto-select anywhere).
  const card = page.locator('.job-card').filter({hasText: 'AI 应用开发实习生'}).first();
  await card.waitFor({timeout: 30000});
  const ack = card.locator('input[type="checkbox"]');
  if (await ack.count()) await ack.first().check().catch(() => {});
  await card.getByRole('button', {name: '选择该岗位'}).click();
  await page.locator('[data-testid="position-to-application"]').waitFor({timeout: 30000});
  const handoff = await page.locator('[data-testid="position-to-application"]').innerText();
  assert.match(handoff, /岗位已选定/);
  assert.match(handoff, /记录一次投递/);
  assert.equal(await page.locator('.desktop-shell').getAttribute('data-pane'), 'research',
    'selecting a job must not navigate on its own');
  mark('选岗后不自动跳转：原地给出「岗位已选定 → 记录一次投递」', {handoff: handoff.replace(/\n/g, ' / ')});
  // The notice sits at the top of the research panel; scroll there so the screenshot shows it.
  await page.evaluate(() => document.querySelector('.desktop-body')?.scrollTo({top: 0, behavior: 'auto'}));
  await page.waitForTimeout(300);
  await page.screenshot({path: join(out, '05a-position-selected-1440.png'), fullPage: true});

  await page.locator('[data-testid="position-to-application-cta"]').click();
  await page.locator('[data-testid="apps-pane"]').waitFor({timeout: 20000});
  assert.equal(await page.locator('.desktop-shell').getAttribute('data-pane'), 'apps');
  await page.waitForTimeout(500);
  const appsHead = await page.locator('[data-testid="apps-head"]').innerText();
  assert.match(appsHead, /申请跟踪/);
  // PART C: the page carries the confirmed target name, not a bare title.
  assert.match(appsHead, new RegExp(targetName), 'apps_page_must_show_the_target_name');
  await page.screenshot({path: join(out, '05-apps-1440.png'), fullPage: true});
  mark('选岗提示 → 投递页；页面带确认过的目标名', {apps_head: appsHead.replace(/\n/g, ' / ')});

  // ---------------------------------------------------------------- materials page context
  await page.locator('.d-nav-item').filter({hasText: '材料与简历'}).first().click();
  await page.locator('[data-testid="materials-head"]').waitFor({timeout: 30000});
  await page.waitForTimeout(300);
  const materialsHead = await page.locator('[data-testid="materials-head"]').innerText();
  assert.match(materialsHead, /材料与简历/);
  assert.match(materialsHead, new RegExp(targetName), 'materials_page_must_show_the_target_name');
  mark('材料页同样带确认过的目标名（跨页 Goal Context）', {materials_head: materialsHead.replace(/\n/g, ' / ')});
  await page.screenshot({path: join(out, '06-materials-1440.png'), fullPage: true});

  // ---------------------------------------------------------------- workbench hand-back
  await page.locator('.d-nav-item').filter({hasText: '工作台'}).first().click();
  await page.waitForTimeout(1200);
  const workbench = await page.evaluate(() => {
    const pane = document.querySelector('.desktop-pane') as HTMLElement;
    const next = pane.querySelector('[data-testid="workbench-next-action"]') as HTMLElement | null;
    return {
      nextLabel: next?.textContent?.trim() ?? null,
      goalHeading: (pane.querySelector('[data-testid="workbench-goal"]') as HTMLElement | null)?.textContent?.trim() ?? null,
      primaries: pane.querySelectorAll('[data-workbench-primary-action]').length,
      accentButtons: [...pane.querySelectorAll('button.primary')].filter(button => !(button as HTMLButtonElement).disabled).length,
      stages: [...pane.querySelectorAll('[data-testid="career-progress"] .d-progress-step')].map(node => ({
        key: (node as HTMLElement).dataset.stage, state: (node as HTMLElement).dataset.state})),
    };
  });
  assert.equal(workbench.nextLabel, '查看本次复盘', 'workbench_must_hand_over_to_the_review');
  assert.equal(workbench.goalHeading, targetName, 'workbench_must_show_the_confirmed_target_name');
  assert.equal(workbench.primaries, 1, 'workbench_must_keep_one_semantic_primary');
  const stage = Object.fromEntries(workbench.stages.map(item => [item.key, item.state]));
  assert.equal(stage.interview, 'completed');
  assert.equal(stage.review, 'current');
  mark('回到工作台后 Primary 变成「查看本次复盘」，进度把面试标为完成、复盘标为当前', workbench);
  await page.screenshot({path: join(out, '02-workbench-after-interview-1440.png'), fullPage: true});

  await page.locator('[data-testid="workbench-next-action"]').click();
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 30000});
  const reopened = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.equal(reopened, sessionId, 'workbench_must_open_the_same_session');
  const reopenedText = await page.locator('[data-testid="interview-review"]').innerText();
  assert.ok(reopenedText.includes(answer1), 'reopened review must still show the user answer');
  const reopenedEntry = await page.evaluate(() => {
    const button = document.querySelector('[data-testid="review-to-applications"]') as HTMLElement | null;
    return {label: button?.textContent?.trim() ?? null, kind: button?.dataset.applicationEntry ?? null};
  });
  assert.equal(reopenedEntry.kind, 'record-application', 'with_a_job_the_entry_records_directly');
  assert.equal(reopenedEntry.label, '记录一次投递');
  mark('工作台 → 查看本次复盘 打开的是同一场次；有岗位后 Primary 变回「记录一次投递」', {session_id: reopened, ...reopenedEntry});

  // ---------------------------------------------------------------- 390px (same signed-in page)
  await page.setViewportSize({width: 390, height: 844});
  await page.waitForTimeout(600);
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 30000});
  const mobile = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
    overflows: [...document.querySelectorAll('[data-testid="interview-review"] *')]
      .filter(node => (node as HTMLElement).scrollWidth > (node as HTMLElement).clientWidth + 1)
      .slice(0, 5)
      .map(node => (node as HTMLElement).className || node.tagName),
  }));
  assert.equal(mobile.scrollWidth, mobile.innerWidth, `review_horizontal_overflow ${JSON.stringify(mobile)}`);
  mark('390px：复盘页无横向溢出', mobile);
  await page.screenshot({path: join(out, '03-review-390.png'), fullPage: true});
  await page.setViewportSize({width: 1440, height: 1000});

  // ---------------------------------------------------------------- contrast of the new surface
  const lightRows = await page.evaluate(REVIEW_CONTRAST);
  assert.ok(lightRows.length >= 6, `review_contrast_too_few ${lightRows.length}`);
  assert.deepEqual(lightRows.filter(row => !row.ok), [], 'review_contrast_light');
  mark('复盘表面（浅色）关键文字达到 AA', lightRows);

  await page.evaluate(() => localStorage.setItem('py-desktop-theme', 'dark'));
  await page.reload({waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 60000});
  await page.locator('[data-testid="workbench-goal"]').waitFor({timeout: 60000});
  // F07Z-150: the reload keeps the session locator, so the review opens directly (no history click).
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 60000});
  const darkRows = await page.evaluate(REVIEW_CONTRAST);
  assert.ok(darkRows.length >= 6, `review_contrast_dark_too_few ${darkRows.length}`);
  assert.deepEqual(darkRows.filter(row => !row.ok), [], 'review_contrast_dark');
  const darkTheme = await page.evaluate(() => document.documentElement.dataset.theme
    ?? document.querySelector('.desktop-shell')?.getAttribute('data-theme'));
  assert.equal(darkTheme, 'dark', 'dark_theme_not_applied');
  mark('复盘表面（深色）关键文字达到 AA', {theme: darkTheme, rows: darkRows});
  await page.screenshot({path: join(out, '04-review-dark-1440.png'), fullPage: true});

  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, goal_id: goalId, session_id: sessionId, bank,
    cases, page_errors: pageErrors,    note: '真实本地栈；scripted worker，无模型调用。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
