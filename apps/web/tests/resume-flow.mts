// F07Z-152: import from the materials page, then bind and rebind the position's resume version.
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.RF_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.RF_OUT ?? join(root, '.private/closeout152/resume-flow'));
mkdirSync(out, {recursive: true});
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('dialog', dialog => void dialog.accept());

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

  await page.getByRole('button', {name: /材料与简历/}).first().click();
  await page.locator('[data-testid="resume-import"]').click();
  await page.getByText(/已把上传的原文导入为简历/).waitFor({timeout: 60000});
  await page.locator('[data-testid="draft-complete"]').waitFor({timeout: 60000});
  const draftHead = (await page.locator('[data-testid="draft-complete"]').innerText()).replace(/\s+/g, ' ');
  assert.match(draftHead, /已按原文导入为简历/);
  const scope = await page.locator('[data-testid="draft-verbatim-scope"]').innerText();
  assert.match(scope, /前 6 条/);
  assert.match(scope, /你选用的简历/);
  mark('材料页按钮导入为简历 v1', {draftHead});

  await page.getByRole('button', {name: /岗位研究/}).first().click();
  const form = page.locator('[data-testid="manual-job-form"]');
  await form.waitFor({timeout: 30000});
  if (!(await form.getAttribute('open'))) await form.locator('summary').click();
  await page.locator('input[aria-label="自建岗位公司"]').fill('合成公司');
  await page.locator('input[aria-label="自建岗位职位"]').fill('Java实习');
  await page.locator('input[aria-label="自建岗位城市"]').fill('上海');
  await page.locator('textarea[aria-label="自建岗位JD"]').fill('负责Java服务开发，写接口与事务。');
  await page.locator('[data-testid="manual-job-save"]').click();
  await page.locator('.job-card').first().waitFor({timeout: 60000});

  const binding = page.locator('[data-testid="job-binding"]');
  await binding.waitFor({timeout: 30000});
  if (!(await binding.getAttribute('open'))) await binding.locator('summary').click();
  await page.waitForFunction(() =>
    document.querySelector('[data-testid="job-binding"]')?.textContent?.includes('当前绑定 v1'), undefined, {timeout: 30000});
  mark('岗位保存后自动绑定最新简历 v1');

  const second = await page.evaluate(async (goalId) => {
    const response = await fetch(`/api/career-goals/${goalId}/materials/resume-imports`, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: 1, expected_revision: 1})});
    return {status: response.status, body: await response.json()};
  }, setup.goalId);
  assert.equal(second.status, 201, JSON.stringify(second).slice(0, 200));

  // Refresh candidates, then rebind to v2.
  await page.locator('[data-testid="job-binding-refresh"]').click();
  await page.waitForFunction(() =>
    [...document.querySelectorAll('[data-testid="job-binding-select"] option')].some(option => option.textContent?.startsWith('v2')),
    undefined, {timeout: 30000});
  const v2Option = page.locator('[data-testid="job-binding-select"] option').filter({hasText: /^v2/}).first();
  const v2Value = await v2Option.getAttribute('value');
  await page.locator('[data-testid="job-binding-select"]').selectOption(v2Value!);
  await page.locator('[data-testid="job-binding-save"]').click();
  await page.waitForFunction(() =>
    document.querySelector('[data-testid="job-binding"]')?.textContent?.includes('当前绑定 v2'), undefined, {timeout: 30000});
  const notice = await page.locator('[data-testid="job-binding-notice"]').innerText();
  assert.match(notice, /已换绑/);
  assert.match(notice, /重新生成计划/);
  await page.screenshot({path: join(out, '01-bound-v2.png'), fullPage: true});
  mark('换绑到 v2 生效', {notice});

  // Version history: restore v1 as a new version; both older versions stay.
  await page.getByRole('button', {name: /材料与简历/}).first().click();
  const history = page.locator('[data-testid="draft-history"]');
  await history.waitFor({timeout: 60000});
  if (!(await history.getAttribute('open'))) await history.locator('summary').click();
  await page.locator('[data-testid="restore-draft-1"]').click();
  await page.waitForFunction(() =>
    document.body.textContent?.includes('第 3 版'), undefined, {timeout: 60000});
  const versions = await page.locator('[data-testid^="draft-version-"]').count();
  assert.equal(versions, 3);
  mark('恢复旧版生成新版本，历史保留', {versions});

  // Older versions collapse after four rows; the rest stay one click away.
  await page.locator('[data-testid="resume-import"]').click();
  await page.waitForFunction(() =>
    document.body.textContent?.includes('第 4 版'), undefined, {timeout: 60000});
  await page.locator('[data-testid="resume-import"]').click();
  await page.waitForFunction(() =>
    document.body.textContent?.includes('第 5 版'), undefined, {timeout: 60000});
  await history.evaluate(element => { (element as HTMLDetailsElement).open = true; });
  await page.waitForTimeout(200);
  const shown = await page.locator('[data-testid^="draft-version-"]').count();
  assert.equal(shown, 4);
  const more = page.locator('[data-testid="draft-history-more"]');
  assert.match(await more.textContent(), /显示全部 5 版/);
  await more.click();
  assert.equal(await page.locator('[data-testid^="draft-version-"]').count(), 5);
  mark('历史版本先显示最近四条，其余一键展开', {before: shown, after: 5});

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
