// 新增业务 1 · A2: updating a bank to v2 must not rewrite the session that was opened on v1.
// Real stack; no model call.
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.BVP_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.BVP_OUT ?? join(root, '.private/closeout157/version-pin'));
mkdirSync(out, {recursive: true});
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

const bankV1 = {
  schema_version: 1, name: '版本钉住对照库',
  items: [
    {item_id: 'q-1', kind: 'knowledge', roles: ['ai-application'], title: '第一题', question: 'state 与 checkpointer 各负责什么？', reference: '', tags: []},
    {item_id: 'q-2', kind: 'knowledge', roles: ['ai-application'], title: '第二题', question: '工具调用超时后怎样安全重试？', reference: '', tags: []},
  ],
};
const bankV2 = {
  ...bankV1,
  items: [...bankV1.items, {item_id: 'q-3', kind: 'knowledge', roles: ['ai-application'],
    title: '第三题', question: '进程重启后，哪些状态必须持久化？', reference: '', tags: []}],
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

  const result = await page.evaluate(async ({v1, v2}) => {
    const importBank = async (document: unknown) => {
      const preview = await (await fetch('/api/question-banks/imports', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
        body: JSON.stringify({document})})).json();
      return preview;
    };
    const commit = async (previewId: string, body: unknown) =>
      (await (await fetch(`/api/question-banks/imports/${previewId}/commit`, {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
        body: JSON.stringify(body)})).json());

    const goals = await (await fetch('/api/career-goals/', {credentials: 'same-origin'})).json();
    const goalId = goals.items[0];
    const goal = await (await fetch(`/api/career-goals/${goalId}`, {credentials: 'same-origin'})).json();

    const created = await commit((await importBank(v1)).preview_id, {bank_id: null, expected_version: 0});
    const session = await (await fetch(`/api/career-goals/${goalId}/interviews`, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: goal.version, role: 'ai-application', max_questions: 2,
        question_banks: [{bank_id: created.bank_id, version: created.version}]})})).json();
    const before = await (await fetch(`/api/career-goals/${goalId}/interviews/${session.id}`, {credentials: 'same-origin'})).json();

    // Same bank, new version.
    const v2commit = await commit((await importBank(v2)).preview_id,
      {bank_id: created.bank_id, expected_version: created.version});
    const after = await (await fetch(`/api/career-goals/${goalId}/interviews/${session.id}`, {credentials: 'same-origin'})).json();
    const listed = await (await fetch('/api/question-banks', {credentials: 'same-origin'})).json();
    const listedBank = (listed.banks ?? []).find((item: {bank_id: string}) => item.bank_id === created.bank_id);
    return {
      sessionId: session.id, created, v2commit,
      beforePack: before.pack_version, afterPack: after.pack_version,
      beforeBanks: before.question_banks, afterBanks: after.question_banks,
      listedVersion: listedBank?.version, listedCount: listedBank?.item_count,
    };
  }, {v1: bankV1, v2: bankV2});

  assert.equal(result.v2commit.version, 2, `uppercase import must be v2: ${JSON.stringify(result.v2commit)}`);
  assert.equal(result.afterPack, result.beforePack, 'the old session must keep its frozen pack');
  assert.deepEqual(result.afterBanks, result.beforeBanks, 'the old session must keep its bank version');
  assert.equal(result.listedVersion, 2);
  assert.equal(result.listedCount, 3);
  mark('旧场次钉住 v1：升级到 v2 后 pack 与题库版本都不变', {
    pack: result.beforePack, banks: result.beforeBanks, listed: [result.listedVersion, result.listedCount],
  });

  assert.equal(pageErrors.length, 0, JSON.stringify(pageErrors.slice(0, 2)));
  writeFileSync(join(out, 'result.json'), JSON.stringify({run: new Date().toISOString(), base, result, cases}, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
