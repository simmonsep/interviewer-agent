// F07Z-152: the materials timeline shows a resume version and opens its first bullet's evidence.
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.TL_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.TL_OUT ?? join(root, '.private/closeout152/timeline'));
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

  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(600);
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('想在上海找 Java 后端实习。');
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});

  const setup = await page.evaluate(async () => {
    const goals = await (await fetch('/api/career-goals/', {credentials: 'same-origin'})).json();
    const goalId = goals.items[0];
    const headers = {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()};
    const source = await fetch(`/api/career-goals/${goalId}/materials/sources`, {
      method: 'POST', credentials: 'same-origin',
      headers: {...headers, 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: 1, expected_revision: 0, name: '项目.md',
        format: 'markdown', text: '项目经历\n负责订单接口开发，实现幂等请求记录。\n优化查询，P99 从 800ms 降到 200ms。'})});
    if (!source.ok) return {error: await source.text()};
    const imported = await fetch(`/api/career-goals/${goalId}/materials/resume-imports`, {
      method: 'POST', credentials: 'same-origin', headers, body: JSON.stringify({expected_version: 1, expected_revision: 1})});
    return {goalId, imported: imported.status, body: await imported.json()};
  });
  assert.equal(setup.imported, 201, JSON.stringify(setup).slice(0, 300));

  await page.getByRole('button', {name: /材料与简历/}).first().click();
  const timeline = page.locator('[data-testid="resume-timeline"]');
  await timeline.waitFor({timeout: 60000});
  if (!(await timeline.getAttribute('open'))) await timeline.locator('summary').click();
  await page.locator('[data-testid="timeline-open-resume_version"]').first().click();
  const detail = page.locator('[data-testid="timeline-detail"]');
  await detail.waitFor({timeout: 30000});
  const text = (await detail.innerText()).replace(/\s+/g, ' ');
  assert.match(text, /负责订单接口开发/);
  assert.match(text, /引用原文/);
  assert.match(text, /归档|uploaded|按原文/);
  await page.screenshot({path: join(out, '01-timeline-detail.png'), fullPage: true});
  mark('时间线可打开简历条目的出处详情', {text: text.slice(0, 200)});

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
