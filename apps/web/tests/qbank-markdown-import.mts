// 新增业务 1 · A1 acceptance: the Markdown template goes through the product's own import path.
// Real stack; no model, no fixture injection beyond the document the user would paste.
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.QBT_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.QBT_OUT ?? join(root, '.private/closeout157/qbank'));
mkdirSync(out, {recursive: true});

const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => {
  cases.push({name, detail});
  console.log('PASS', name);
};

const bankName = `标记导入-${Date.now()}`;
const markdown = `# ${bankName}

## LangGraph 的 state 与 checkpointer
题干：同一个用户的多个独立会话，state、checkpointer 和 store 各自负责什么？
参考：state 描述当前线程；checkpointer 按 thread_id 保存；store 放跨线程信息。
标签：Python, LangGraph

## 工具调用重试的判断
题干：你怎样判断一次工具调用应该重试？
`;

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(800);
  // A goal is needed before the settings/banks surface accepts work in the desktop shell.
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('想在上海找 AI 应用开发实习。');
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});

  await page.locator('.d-nav-item').filter({hasText: '题库'}).first().click();
  await page.waitForFunction(() => document.querySelector('.desktop-shell')?.getAttribute('data-pane') === 'banks',
    undefined, {timeout: 20000});

  // 导入题库 card -> 展开 -> Markdown 输入
  await page.locator('section[aria-label="导入题库"] button', {hasText: '展开'}).first().click();
  await page.locator('[data-testid="md-input"]').waitFor({timeout: 20000});
  // The template button exists and is a real download affordance.
  assert.equal(await page.locator('[data-testid="md-download-template"]').count(), 1, 'template button missing');
  await page.fill('[data-testid="md-input"]', markdown);
  await page.locator('[data-testid="md-preview"]').click();

  // Converter output first...
  await page.locator('[data-testid="md-converted"]').waitFor({timeout: 20000});
  const converted = await page.locator('[data-testid="md-converted"]').innerText();
  assert.match(converted, /已转换 2 题/);
  assert.match(converted, /1 题没有参考/);
  // ...then the server's own preview.
  try {
    await page.locator('[data-testid="bank-preview"]').waitFor({timeout: 30000});
  } catch (error) {
    const shown = await page.locator('section[aria-label="导入题库"]').innerText();
    throw new Error('server_preview_missing\n' + shown.replace(/\s+/g, ' ').slice(0, 600));
  }
  const preview = await page.locator('[data-testid="bank-preview"]').innerText();
  assert.match(preview, new RegExp(bankName));
  mark('Markdown → 转换 → 服务端预览：两题与缺参考提示都对得上', {converted});

  await page.locator('[data-testid="bank-preview"] button', {hasText: '确认导入'}).click();
  await page.locator('.notice', {hasText: '已导入'}).first().waitFor({timeout: 30000});
  const notice = await page.locator('.notice', {hasText: '已导入'}).first().innerText();
  assert.match(notice, /共 2 题/);

  // And the bank really exists in the list with both items.
  const bank = await page.evaluate(async name => {
    const body = await (await fetch('/api/question-banks', {credentials: 'same-origin'})).json();
    const items = body.banks ?? [];
    const found = items.find((item: {name: string}) => item.name === name);
    return found ? {name: found.name, version: found.version, count: found.item_count, origin: found.origin} : null;
  }, bankName);
  assert.ok(bank, 'imported bank not listed');
  assert.equal(bank!.count, 2);
  assert.equal(bank!.origin, 'private');
  mark('确认导入后题库出现在列表里，2 题、来源为用户', bank);

  // 新增业务 1 · A2: the same document can be committed as a NEW VERSION of that bank.
  await page.locator('section[aria-label="导入题库"] button', {hasText: '展开'}).first().click();
  await page.locator('[data-testid="md-input"]').waitFor({timeout: 20000});
  const bumped = markdown.replace('## 工具调用重试的判断', '## 记忆与恢复的边界\n题干：进程重启后，哪些状态必须持久化？\n\n## 工具调用重试的判断');
  await page.fill('[data-testid="md-input"]', bumped);
  await page.locator('[data-testid="md-preview"]').click();
  await page.locator('[data-testid="bank-preview"]').waitFor({timeout: 30000});
  const bankId = await page.evaluate(async name => {
    const body = await (await fetch('/api/question-banks', {credentials: 'same-origin'})).json();
    return ((body.banks ?? []) as {name: string; bank_id: string}[]).find(item => item.name === name)?.bank_id ?? null;
  }, bankName);
  assert.ok(bankId, 'bank id missing before the version update');
  await page.selectOption('[data-testid="bank-target"]', `${bankId}|1`);
  await page.locator('[data-testid="bank-preview"] button', {hasText: '确认导入'}).click();
  await page.locator('.notice', {hasText: '新版本'}).first().waitFor({timeout: 30000});
  const versioned = await page.evaluate(async name => {
    const body = await (await fetch('/api/question-banks', {credentials: 'same-origin'})).json();
    const found = ((body.banks ?? []) as {name: string; version: number; item_count: number}[])
      .find(item => item.name === name);
    return found ? {version: found.version, count: found.item_count} : null;
  }, bankName);
  assert.deepEqual(versioned, {version: 2, count: 3}, `expected v2 with 3 items, got ${JSON.stringify(versioned)}`);
  mark('同一题库提交新版本：v1 → v2，旧版本仍保留在库里', versioned);

  await page.screenshot({path: join(out, '01-markdown-bank.png'), fullPage: true});
  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, bank: bankName, cases, page_errors: pageErrors,
    note: '真实本地栈；0 元；转换在前端，预览/提交走既有 import API。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
