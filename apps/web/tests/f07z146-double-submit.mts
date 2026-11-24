// F07Z-146 acceptance: double-submit produces exactly one business record, the technical detail
// is collapsed behind the human sentence, and the start/answer/resume labels share one vocabulary.
// Real local stack (API 3100 + Vite 5174 + scripted workers), no model call.
//
//   node --experimental-strip-types apps/web/tests/f07z146-double-submit.mts
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.D146_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.D146_OUT ?? join(root, '.private/f07z146/evidence'));
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

  // Count every mutating request so "only one record" can be proven at the wire, not just in the UI.
  const posts: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST') posts.push(new URL(request.url()).pathname);
  });
  const countPosts = (suffix: string) => posts.filter(path => path.endsWith(suffix)).length;

  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(700);

  const api = async <T,>(path: string, init?: RequestInit) => page.evaluate(async ([url, options]) => {
    const response = await fetch(url as string, {credentials: 'same-origin', ...(options ?? {})});
    return {status: response.status, body: await response.json()};
  }, [path, init ?? null] as [string, RequestInit | null]) as Promise<{status: number; body: T}>;

  // ---------------------------------------------------------------- goal create
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('想在上海找 AI 应用 / Agent 开发实习。');
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.getByLabel('确认方向（可选）').fill(targetName);
  await page.locator('[data-testid="workbench-primary"]').dblclick();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  await page.waitForTimeout(1200);
  const goals = await api<{items: string[]}>('/api/career-goals/');
  const goalId = goals.body.items[0];
  assert.ok(goalId, 'goal_missing');
  assert.equal(goals.body.items.length, 1, 'double_click_created_two_goals');
  assert.equal(countPosts('/career-goals/'), 1, `goal_posts ${JSON.stringify(posts)}`);
  mark('创建目标：双击只写 1 个目标，也只发出 1 次请求', {goals: goals.body.items.length});

  // ---------------------------------------------------------------- material save
  await page.locator('.d-nav-item').filter({hasText: '材料与简历'}).first().click();
  await page.locator('textarea[aria-label="材料正文"]').waitFor({timeout: 30000});
  await page.locator('textarea[aria-label="材料正文"]').fill('在实习中负责订单查询接口，用 Java 与 MyBatis 重写慢查询，P95 从 1.2s 降到 300ms。');
  await page.locator('button', {hasText: '保存这段材料'}).first().dblclick();
  await page.locator('.d-source').first().waitFor({timeout: 30000});
  await page.waitForTimeout(800);
  const materials = await api<{sources: {id: string}[]}>(
    `/api/career-goals/${goalId}/materials`);
  assert.equal(materials.body.sources.length, 1, 'double_click_created_two_materials');
  assert.equal(countPosts('/materials/sources'), 1, 'material_posts');
  mark('保存材料：双击只写 1 份材料，来源列表只出现一条', {sources: materials.body.sources.length});

  // ---------------------------------------------------------------- error detail stays collapsed
  await page.evaluate(() => {
    const details = document.querySelector('details.d-source-add') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.locator('textarea[aria-label="材料正文"]').fill('第二段材料：负责把检索接口加上缓存。');
  let injected = true;
  await page.route('**/materials/sources', async route => {
    if (injected && route.request().method() === 'POST') {
      injected = false;
      return route.fulfill({status: 500, contentType: 'application/json',
        body: JSON.stringify({code: 'resource_unavailable', message: 'pool closed'})});
    }
    return route.continue();
  });
  await page.locator('button', {hasText: '保存这段材料'}).first().click();
  await page.locator('[data-testid="error-details"]').waitFor({timeout: 20000});
  const errorUi = await page.evaluate(() => {
    const details = document.querySelector('[data-testid="error-details"]') as HTMLDetailsElement;
    const alert = document.querySelector('[role="alert"].error') as HTMLElement | null;
    const textarea = document.querySelector('textarea[aria-label="材料正文"]') as HTMLTextAreaElement;
    return {
      open: details.open,
      technical: details.querySelector('pre')?.textContent ?? '',
      human: alert?.innerText ?? '',
      keeptext: textarea.value,
    };
  });
  assert.equal(errorUi.open, false, 'technical_details_must_start_collapsed');
  assert.match(errorUi.technical, /HTTP 500/);
  assert.match(errorUi.technical, /resource_unavailable/);
  assert.doesNotMatch(errorUi.human, /resource_unavailable|HTTP 500/, 'human_sentence_must_not_embed_the_code');
  assert.equal(errorUi.keeptext.includes('第二段材料'), true, 'failed_save_must_keep_the_input');
  await page.screenshot({path: join(out, '01-error-details-1440.png'), fullPage: true});
  mark('保存失败：人话在前、技术详情默认折叠，输入仍在', errorUi);
  await page.unroute('**/materials/sources');

  // ---------------------------------------------------------------- application create (needs a job)
  const goal = await api<{version: number}>(`/api/career-goals/${goalId}`);
  const manual = await page.evaluate(async ([id, version]: [string, number]) => {
    const response = await fetch(`/api/career-goals/${id}/jobs/manual`, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: version, company: '合成科技', title: 'AI 应用开发实习生',
        cities: ['上海'], recruitment_label: '', url: 'https://example.com/jobs/1', jd_text: 'Agent 应用开发。'})});
    return {status: response.status, body: await response.json()};
  }, [goalId, goal.body.version] as [string, number]);
  const snapshotId = manual.body.snapshot_id as string | undefined;
  assert.ok(snapshotId, `manual_job_failed ${manual.status}`);
  const listing = await api<{items: {snapshot_id: string; run_id: string}[]}>(`/api/career-goals/${goalId}/jobs`);
  const runId = listing.body.items.find(item => item.snapshot_id === snapshotId)?.run_id;
  assert.ok(runId, `run_id_missing ${JSON.stringify(listing.body.items.map(item => item.snapshot_id))}`);
  const selected = await page.evaluate(async ([id, snapshot, run, version]: [string, string, string, number]) => {
    const response = await fetch(`/api/career-goals/${id}/selection`, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({expected_version: version, run_id: run,
        snapshot_id: snapshot, acknowledge_unknown: true})});
    return {status: response.status, body: await response.json()};
  }, [goalId, snapshotId, runId, goal.body.version] as [string, string, string, number]);
  assert.equal(selected.status, 200, `job_selection_failed ${JSON.stringify(selected.body)}`);

  await page.locator('.d-nav-item').filter({hasText: '申请跟踪'}).first().click();
  await page.locator('[data-testid="apps-head"]').waitFor({timeout: 30000});
  await page.waitForTimeout(800);
  await page.getByRole('textbox', {name: '公司'}).fill('合成科技');
  await page.getByRole('textbox', {name: '职位'}).fill('AI 应用开发实习生');
  const beforeApp = countPosts('/applications');
  await page.locator('button', {hasText: '记录为草稿'}).first().dblclick();
  await page.locator('[data-testid="apps-head"]').waitFor({timeout: 20000});
  await page.waitForTimeout(1000);
  const applications = await api<{items: {id: string}[]}>(`/api/career-goals/${goalId}/applications`);
  assert.equal(applications.body.items.length, 1, 'double_click_created_two_applications');
  assert.equal(countPosts('/applications') - beforeApp, 1, 'application_posts');
  mark('记录投递：双击只写 1 条投递', {applications: applications.body.items.length});

  // ---------------------------------------------------------------- interview start + answer
  const bank = await page.evaluate(async document => {
    const preview = await (await fetch('/api/question-banks/imports', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({document})})).json();
    const commit = await (await fetch(`/api/question-banks/imports/${preview.preview_id}/commit`, {method: 'POST',
      credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({})})).json();
    return {bank_id: commit.bank_id, version: commit.version};
  }, fixture);
  await page.locator('.d-nav-item').filter({hasText: '面试'}).first().click();
  await page.locator('#interview-bank').waitFor({state: 'visible', timeout: 30000});
  // The setup form was mounted before the import landed, so ask it to re-read the bank list.
  await page.locator('button', {hasText: '刷新题库'}).first().click();
  await page.locator('#interview-bank option', {hasText: 'Agent开发岗位试运行'}).first()
    .waitFor({state: 'attached', timeout: 30000});
  await page.locator('#interview-bank').selectOption(`${bank.bank_id}|${bank.version}`);
  const beforeStart = countPosts('/interviews');
  await page.locator('button', {hasText: '开始整场面试 →'}).first().dblclick();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 120000});
  await page.waitForTimeout(1200);
  const history = await api<{items: {id: string}[]}>(`/api/career-goals/${goalId}/interviews`);
  assert.equal(history.body.items.length, 1, 'double_click_created_two_sessions');
  assert.equal(countPosts('/interviews') - beforeStart, 1, 'start_posts');
  const sessionId = history.body.items[0].id;
  mark('开始面试：双击只创建 1 场，首题出现', {sessions: history.body.items.length});

  const first = await page.locator('[data-testid="interview-question"]').innerText();
  await page.locator('#interview-answer').fill('state 是共享的运行状态；checkpointer 按 thread_id 保存检查点。');
  const beforeAnswer = countPosts('/answers');
  await page.locator('[data-testid="answer-submit"]').dblclick();
  await page.waitForFunction(previous => {
    const node = document.querySelector('[data-testid="interview-question"]');
    return !!node && node.textContent !== previous;
  }, first, {timeout: 120000});
  await page.waitForTimeout(800);
  const detail = await api<{events: {kind: string}[]}>(`/api/career-goals/${goalId}/interviews/${sessionId}`);
  const answers = detail.body.events.filter(event => event.kind === 'answer').length;
  assert.equal(answers, 1, 'double_click_wrote_two_answers');
  assert.equal(countPosts('/answers') - beforeAnswer, 1, 'answer_posts');
  mark('提交作答：双击只写 1 条回答，第二题出现', {answers});

  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, goal_id: goalId, session_id: sessionId, bank, cases, page_errors: pageErrors,
    note: '真实本地栈；双击与 500 都是真实前端行为，scripted worker，无模型调用。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
