// F07Z-152: one walkthrough across the four domains — material -> resume -> position -> interview
// -> review -> application -> timeline. Real local stack (API 3100 + Vite 5174 + scripted workers).
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.FD_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.FD_OUT ?? join(root, '.private/closeout152/four-domain'));
mkdirSync(out, {recursive: true});
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

const answer1 = '我在课程项目里负责订单接口：用唯一键和状态表实现幂等，重复请求返回同一结果，并补了并发测试。';
const answer2 = '缓存那块我把热点查询按 P99 分层，先测基线再改索引，最后用压测对照，没有把一次优化说成整体性能提升。';

async function pane(page: Page, name: string) {
  await page.getByRole('button', {name}).first().click();
}

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('dialog', dialog => void dialog.accept());

  // ---------------------------------------------------------------- 1. goal + resume v1
  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(600);
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('想在上海找 Java 后端实习。');
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});

  const setup = await page.evaluate(async () => {
    const goals = await (await fetch('/api/career-goals/', {credentials: 'same-origin'})).json();
    const goalId = goals.items[0];
    const source = await fetch(`/api/career-goals/${goalId}/materials/sources`, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: 1, expected_revision: 0, name: '项目.md', format: 'markdown',
        text: '项目经历\n负责订单接口开发，实现幂等请求记录。\n优化查询，P99 从 800ms 降到 200ms。'})});
    return {goalId, source: source.status};
  });
  assert.equal(setup.source, 201);

  await pane(page, /材料与简历/);
  await page.locator('[data-testid="resume-import"]').click();
  await page.locator('[data-testid="draft-complete"]').waitFor({timeout: 60000});
  const draft = await page.evaluate(async (goalId) => {
    const view = await (await fetch(`/api/career-goals/${goalId}/materials`, {credentials: 'same-origin'})).json();
    return view.drafts[0];
  }, setup.goalId);
  mark('材料原文导入为简历 v1（0 模型调用）', {version: draft.version, origin: draft.origin});

  // ---------------------------------------------------------------- 2. position + selection
  await pane(page, /岗位研究/);
  const form = page.locator('[data-testid="manual-job-form"]');
  await form.waitFor({timeout: 30000});
  if (!(await form.getAttribute('open'))) await form.locator('summary').click();
  await page.locator('input[aria-label="自建岗位公司"]').fill('合成公司');
  await page.locator('input[aria-label="自建岗位职位"]').fill('Java实习');
  await page.locator('input[aria-label="自建岗位城市"]').fill('上海');
  await page.locator('textarea[aria-label="自建岗位JD"]').fill('负责Java服务开发，写接口与事务。');
  await page.locator('[data-testid="manual-job-save"]').click();
  const card = page.locator('.job-card').first();
  await card.waitFor({timeout: 60000});
  const ack = card.locator('input[type="checkbox"]');
  if (await ack.count()) await ack.first().check().catch(() => {});
  await card.getByRole('button', {name: '选择该岗位'}).click();
  await page.waitForFunction(async (goalId) => {
    const view = await (await fetch(`/api/career-goals/${goalId}/workflow`, {credentials: 'same-origin'})).json();
    return Boolean(view.selected_job);
  }, setup.goalId, {timeout: 30000});
  const binding = await page.evaluate(async (goalId) => {
    const view = await (await fetch(`/api/career-goals/${goalId}/jobs`, {credentials: 'same-origin'})).json();
    const item = view.items.find((x: {position_id?: string}) => x.position_id);
    return (await fetch(`/api/career-goals/${goalId}/positions/${item.position_id}/binding`,
      {credentials: 'same-origin'})).json();
  }, setup.goalId);
  assert.equal(binding.active.resume_version, 1);
  mark('自建岗位保存并选定后绑定简历 v1', {position: binding.position_id});

  // ---------------------------------------------------------------- 3. interview via the UI
  await pane(page, /面试/);
  await page.locator('#interview-bank').waitFor({state: 'visible', timeout: 60000});
  const bank = page.locator('#interview-bank option').filter({hasText: /^ai-application/}).first();
  const bankValue = await bank.getAttribute('value');
  assert.ok(bankValue, 'no ai-application bank');
  await page.locator('#interview-bank').selectOption(bankValue);
  await page.locator('button', {hasText: '开始整场面试 →'}).first().click();
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 90000});
  const firstQuestion = await page.locator('[data-testid="interview-question"]').innerText();
  await page.locator('#interview-answer').fill(answer1);
  await page.locator('[data-testid="answer-submit"]').click();
  await page.waitForFunction(previous => {
    const element = document.querySelector('[data-testid="interview-question"]');
    return !!element && element.textContent !== previous;
  }, firstQuestion, {timeout: 120000});
  await page.locator('#interview-answer').fill(answer2);
  await page.locator('[data-testid="answer-submit"]').click();
  await page.locator('[data-testid="interview-review"]').waitFor({timeout: 150000});
  const sessionId = await page.locator('[data-session-id]').getAttribute('data-session-id');
  assert.ok(sessionId, 'session_id_missing');
  const session = await page.evaluate(async ([goalId, id]) =>
    (await fetch(`/api/career-goals/${goalId}/interviews/${id}`, {credentials: 'same-origin'})).json(),
    [setup.goalId, sessionId] as [string, string]);
  assert.equal(session.status, 'COMPLETED');
  assert.equal(session.draft_id, draft.id, 'the session must freeze the position-bound resume');
  assert.ok(session.observations.length > 0, 'scripted session needs an observation');
  mark('面试默认使用岗位绑定简历 v1，并产出观察', {session: sessionId, draft: draft.id});

  // ---------------------------------------------------------------- 4. application
  await page.locator('[data-testid="review-to-applications"]').click();
  await page.locator('[data-testid="apps-pane"]').waitFor({timeout: 30000});
  await page.locator('input[placeholder^="例如：某科技公司"]').fill('合成公司');
  await page.locator('input[placeholder^="例如：后端开发实习生"]').fill('Java实习');
  const create = page.locator('section[aria-label="新增投递"] button.primary');
  await create.click();
  await page.waitForFunction(() =>
    !document.body.textContent?.includes('还没有投递记录'), undefined, {timeout: 60000});
  const apps = await page.evaluate(async (goalId) =>
    (await fetch(`/api/career-goals/${goalId}/applications`, {credentials: 'same-origin'})).json(), setup.goalId);
  assert.equal(apps.items.length, 1);
  mark('复盘 → 投递页创建了第一条申请记录', {application: apps.items[0].id});

  // ---------------------------------------------------------------- 5. timeline back-check
  await pane(page, /材料与简历/);
  const timeline = page.locator('[data-testid="resume-timeline"]');
  await timeline.waitFor({timeout: 60000});
  if (!(await timeline.getAttribute('open'))) await timeline.locator('summary').click();
  await page.locator('[data-testid="timeline-open-interview_session"]').first().click();
  const detail = page.locator('[data-testid="timeline-detail"]');
  await detail.waitFor({timeout: 30000});
  const sessionText = (await detail.innerText()).replace(/\s+/g, ' ');
  assert.match(sessionText, /COMPLETED/);
  await page.locator('[data-testid="timeline-session-observations"] button').first().click();
  await detail.locator('[data-testid="link-resume"]').waitFor({timeout: 30000});
  const observationText = (await detail.innerText()).replace(/\s+/g, ' ');
  assert.match(observationText, /本场所用简历：v1/);
  await detail.locator('[data-testid="link-resume"]').click();
  await page.waitForFunction(() =>
    document.querySelector('[data-testid="timeline-detail"]')?.textContent?.includes('resume_bullet'),
    undefined, {timeout: 30000});
  const bulletText = (await detail.innerText()).replace(/\s+/g, ' ');
  assert.match(bulletText, /负责订单接口开发/);
  mark('时间线 → 场次 → 观察 → 本场所用简历 → 简历条目', {
    observation: observationText.slice(0, 160), bullet: bulletText.slice(0, 120)});

  await page.locator('[data-testid="timeline-open-application"]').first().click();
  await page.waitForFunction(() =>
    document.querySelector('[data-testid="timeline-detail"]')?.textContent?.includes('application_action'),
    undefined, {timeout: 30000});
  const applicationText = (await detail.innerText()).replace(/\s+/g, ' ');
  assert.match(applicationText, /DRAFT/);
  mark('时间线 → 申请记录详情', {text: applicationText.slice(0, 160)});

  await page.screenshot({path: join(out, '01-four-domain.png'), fullPage: true});
  assert.equal(pageErrors.length, 0, JSON.stringify(pageErrors.slice(0, 2)));
  writeFileSync(join(out, 'result.json'), JSON.stringify({cases, pageErrors}, null, 2));
  console.log(JSON.stringify({status: 'passed', cases: cases.length, out}));
} catch (error) {
  writeFileSync(join(out, 'result.json'), JSON.stringify({cases, pageErrors, error: String(error)}, null, 2));
  await browser?.close();
  console.error('FAIL', error);
  process.exit(1);
} finally {
  await browser?.close();
}
