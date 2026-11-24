// 新增业务 1: the session header reports what it can draw from (bank + role base), and the
// numbers match the frozen policy. Real stack, no model call.
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.BSRC_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.BSRC_OUT ?? join(root, '.private/closeout157/bank-source'));
mkdirSync(out, {recursive: true});
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

const document = {
  schema_version: 1, name: '来源显示对照库',
  items: [
    {item_id: 'q-1', kind: 'knowledge', roles: ['ai-application'], title: 'LangGraph 的 state',
     question: 'state 与 checkpointer 各负责什么？', reference: '', tags: ['LangGraph']},
    {item_id: 'q-2', kind: 'knowledge', roles: ['ai-application'], title: '工具调用重试',
     question: '工具调用超时后怎样安全重试？', reference: '', tags: ['工具调用']},
  ],
};

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(600);
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('想在上海找 AI 应用开发实习。');
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});

  const created = await page.evaluate(async (bank) => {
    const goals = await (await fetch('/api/career-goals/', {credentials: 'same-origin'})).json();
    const goalId = goals.items[0];
    const preview = await (await fetch('/api/question-banks/imports', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({document: bank})})).json();
    const commit = await (await fetch(`/api/question-banks/imports/${preview.preview_id}/commit`, {method: 'POST',
      credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({bank_id: null, expected_version: 0})})).json();
    const detail = await (await fetch(`/api/career-goals/${goalId}`, {credentials: 'same-origin'})).json();
    const session = await fetch(`/api/career-goals/${goalId}/interviews`, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: detail.version, role: 'ai-application', max_questions: 2,
        question_banks: [{bank_id: commit.bank_id, version: commit.version}]})});
    const body = await session.json();
    return {goalId, status: session.status, sessionId: body.id, policy: body.policy};
  }, document);
  assert.ok(created.sessionId, `session not created: ${JSON.stringify(created).slice(0, 300)}`);
  assert.equal(created.policy?.bank_source?.bank_items, 2);

  await page.goto(`${base}/?shell=desktop&goal=${created.goalId}&interview=${created.sessionId}`, {waitUntil: 'domcontentloaded'});
  const line = page.locator('[data-testid="interview-bank-source"]');
  await line.waitFor({timeout: 60000});
  const text = (await line.innerText()).replace(/\s+/g, ' ');
  assert.match(text, /你的题库 2 题/);
  assert.match(text, /角色基础 4 题/);
  assert.match(text, /由面试官判断/);
  await page.screenshot({path: join(out, '01-bank-source.png'), fullPage: true});
  mark('场次页显示可用来源，数字与冻结 policy 一致', {text});

  assert.equal(pageErrors.length, 0, JSON.stringify(pageErrors.slice(0, 2)));
  writeFileSync(join(out, 'result.json'), JSON.stringify({run: new Date().toISOString(), base, created, text, cases}, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
