// F07Z-166: when the user explicitly says "no resume this session", the bound resume must not be
// reintroduced by either repository. Scaffolding (materials, draft, manual job, binding) is
// created through the real API at the same goal version; the choice itself is made in the UI.
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.NORESUME_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.NORESUME_OUT ?? join(root, '.private/f07z166/no-resume'));
const python = process.env.NORESUME_PYTHON ?? 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
mkdirSync(out, {recursive: true});
const runId = randomUUID();
const execute = promisify(execFile);
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

async function scoped(goal: string, script: string, mode?: string) {
  const args = ['-X', 'utf8', script, '--goal', goal, '--run-id', runId];
  if (mode) args.push('--mode', mode);
  if (script.includes('prep-web')) args.push('--watch', '150');
  const result = await execute(python, args, {cwd: root, windowsHide: true, timeout: 170000,
    env: {...process.env, PYTHONPATH: 'backend/src'}});
  return JSON.parse(result.stdout) as {status: string; session_id: string | null};
}

const primary = (page: Page) => page.locator('[data-testid="workbench-next-action"]');
async function expectVisible(page: Page, selector: string) {
  await page.locator(selector).waitFor({state: 'visible', timeout: 10000});
}


async function api<T>(page: Page, method: string, path: string, body?: unknown): Promise<T> {
  return page.evaluate(async ([verb, url, data]) => {
    const response = await fetch(url as string, {
      method: verb as string, credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: data === undefined ? undefined : JSON.stringify(data)});
    const text = await response.text();
    if (!response.ok) throw new Error(`${response.status} ${text.slice(0, 200)}`);
    return JSON.parse(text);
  }, [method, path, body]) as Promise<T>;
}

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  assert.ok((await fetch(base + '/', {signal: AbortSignal.timeout(4000)})).ok, 'vite_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(600);
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('先保存这份 JD，后面再准备训练。');
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.getByLabel('确认方向（可选）').fill('AI 应用开发');
  await page.locator('textarea[aria-label="职位说明 JD"]').fill(
    '岗位职责：负责 Agent 工具链与后端接口开发，熟悉 Python、FastAPI、PostgreSQL。');
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  const goalId = await page.evaluate(() => new URLSearchParams(location.search).get('goal'));
  assert.ok(goalId, 'goal_id_missing');
  await scoped(goalId!, 'scripts/career-web-support.py');

  const listing = await api<{material_revision: number}>(page, 'GET', `/api/career-goals/${goalId}/materials`);
  const source = await api<{source_id: string}>(page, 'POST', `/api/career-goals/${goalId}/materials/sources`,
    {expected_version: 1, expected_revision: listing.material_revision, name: '合成简历.md', format: 'markdown',
     text: '合成材料：我在课程项目里用 Python 写过一个问答服务，负责接口和错误处理，并写了测试。'});
  assert.ok(source.source_id);
  const afterSource = await api<{material_revision: number}>(page, 'GET', `/api/career-goals/${goalId}/materials`);
  await api(page, 'POST', `/api/career-goals/${goalId}/materials/rewrite`,
    {expected_version: 1, expected_revision: afterSource.material_revision});
  await scoped(goalId!, 'scripts/prep-web-support.py', 'drain');
  const withDraft = await api<{drafts: {id: string}[]}>(page, 'GET', `/api/career-goals/${goalId}/materials`);
  assert.ok(withDraft.drafts.length > 0, 'draft_missing');
  const draftId = withDraft.drafts[0].id;

  const job = await api<{snapshot_id: string}>(page, 'POST', `/api/career-goals/${goalId}/jobs/manual`,
    {expected_version: 1, company: '合成公司', title: 'AI 应用实习', cities: ['上海'],
     jd_text: '负责 Agent 工具链的 Python 服务开发，熟悉 FastAPI 与 PostgreSQL。'});
  const jobs = await api<{items: {snapshot_id: string; position_id: string}[]}>(page, 'GET', `/api/career-goals/${goalId}/jobs`);
  const positionId = jobs.items.find(item => item.snapshot_id === job.snapshot_id)?.position_id;
  assert.ok(positionId, 'position_missing');
  const binding = await api<{active: {resume_draft_id: string}}>(page,
    'GET', `/api/career-goals/${goalId}/positions/${positionId}/binding`);
  assert.equal(binding.active.resume_draft_id, draftId);
  mark('同一目标版本已有绑定简历（作为对照）', {draft: draftId, position: positionId});

  // The explicit choice is made in the UI; the quick path creates the preparation at this same
  // goal version, so the binding is genuinely available and must be skipped.
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-details') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.locator('[data-testid="no-resume-choice"]').check();
  await expectVisible(page, '[data-testid="no-resume-hint"]');
  await page.locator('[data-testid="pipeline-quick-prep"]').click();

  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.includes('正在准备') === true;
  }, undefined, {timeout: 60000});
  const planned = await scoped(goalId!, 'scripts/prep-web-support.py', 'plan');
  assert.equal(planned.status, 'WAITING_FOR_USER', JSON.stringify(planned));
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.trim() === '开始这场';
  }, undefined, {timeout: 60000});

  const prep = await api<{id: string; role: string; use_resume: boolean; draft_id: string | null;
    max_questions: number; duration_seconds: number;
    plan: {draft: unknown; use_resume: boolean}}>(page, 'GET',
    `/api/career-goals/${goalId}/interview-preparations/current`);
  assert.equal(prep.max_questions, 4);
  assert.equal(prep.duration_seconds, 1200);
  assert.equal(prep.use_resume, false);
  assert.equal(prep.draft_id, null);
  assert.equal(prep.plan.draft, null);
  assert.equal(prep.plan.use_resume, false);
  const planText = await page.locator('[data-testid="pipeline-plan-text"]').innerText();
  assert.match(planText, /不使用草稿/);
  await page.screenshot({path: join(out, '01-plan-no-resume.png'), fullPage: true});
  mark('明确不用简历：计划没有草稿，准备行记录 use_resume=false', {prep: prep.id, planText});

  await primary(page).click();
  const advanced = await scoped(goalId!, 'scripts/prep-web-support.py', 'advance');
  assert.equal(advanced.status, 'SUCCEEDED', JSON.stringify(advanced));
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 150000});
  const detail = await api<{draft_id: string | null; material_fact_ids: string[];
    resume_evidence: unknown[] | null}>(page, 'GET',
    `/api/career-goals/${goalId}/interviews/${advanced.session_id}`);
  assert.equal(detail.draft_id, null);
  assert.deepEqual(detail.material_fact_ids, []);
  assert.ok(!detail.resume_evidence || detail.resume_evidence.length === 0);
  await page.screenshot({path: join(out, '02-session-no-resume.png'), fullPage: true});
  mark('场次没有绑定稿及其材料证据', {session: advanced.session_id});

  assert.deepEqual(pageErrors, [], JSON.stringify(pageErrors.slice(0, 2)));
  writeFileSync(join(out, 'result.json'), JSON.stringify({goalId, draftId, positionId, cases, pageErrors}, null, 2));
  console.log(JSON.stringify({status: 'passed', cases: cases.length, out}));
} catch (error) {
  writeFileSync(join(out, 'result.json'), JSON.stringify({cases, pageErrors, error: String(error)}, null, 2));
  await browser?.close();
  console.error('FAIL', error);
  process.exit(1);
} finally {
  await browser?.close();
}
