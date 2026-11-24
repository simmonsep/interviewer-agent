// F07Z-152: fixing a self-authored JD keeps the same position; it does not create a new job card.
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.JOB_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.JOB_OUT ?? join(root, '.private/closeout152/job-edit'));
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

  await page.getByRole('button', {name: /岗位研究/}).first().click();
  const form = page.locator('[data-testid="manual-job-form"]');
  await form.waitFor({timeout: 30000});
  if (!(await form.getAttribute('open'))) await form.locator('summary').click();
  await page.locator('input[aria-label="自建岗位公司"]').fill('合成公司');
  await page.locator('input[aria-label="自建岗位职位"]').fill('Java实习');
  await page.locator('input[aria-label="自建岗位城市"]').fill('上海');
  await page.locator('textarea[aria-label="自建岗位JD"]').fill('负责Java服务开发，写接口与事务。');
  await page.locator('[data-testid="manual-job-save"]').click();

  const cards = page.locator('.job-card');
  await cards.first().waitFor({timeout: 60000});
  assert.equal(await cards.count(), 1);
  const before = (await cards.first().innerText()).replace(/\s+/g, ' ');
  assert.match(before, /负责Java服务开发/);
  mark('自建岗位保存后只有一张岗位卡');

  const positionsBefore = await page.evaluate(async () => {
    const goals = await (await fetch('/api/career-goals/', {credentials: 'same-origin'})).json();
    const view = await (await fetch(`/api/career-goals/${goals.items[0]}/jobs`, {credentials: 'same-origin'})).json();
    return {cards: view.items.filter((x: {source_id: string; current_snapshot_id: string | null; snapshot_id: string}) =>
      x.source_id === 'user-jd' && x.current_snapshot_id === x.snapshot_id).length,
      positions: new Set(view.items.filter((x: {position_id?: string}) => x.position_id)
        .map((x: {position_id: string}) => x.position_id)).size};
  });

  await page.getByRole('button', {name: '编辑 JD'}).first().click();
  await page.locator('[data-testid="job-edit-form"]').waitFor({timeout: 30000});
  await page.locator('textarea[aria-label="编辑岗位JD"]').fill('负责Java服务开发，写接口、事务与幂等。');
  await page.locator('[data-testid="job-edit-save"]').click();
  await page.waitForFunction(() => {
    const node = document.querySelector('.job-card');
    return !!node && node.textContent?.includes('幂等');
  }, undefined, {timeout: 60000});

  const after = await page.evaluate(async () => {
    const goals = await (await fetch('/api/career-goals/', {credentials: 'same-origin'})).json();
    const view = await (await fetch(`/api/career-goals/${goals.items[0]}/jobs`, {credentials: 'same-origin'})).json();
    return {positions: new Set(view.items.filter((x: {position_id?: string}) => x.position_id)
      .map((x: {position_id: string}) => x.position_id)).size,
      current: view.items.filter((x: {source_id: string; current_snapshot_id: string | null; snapshot_id: string}) =>
        x.source_id === 'user-jd' && x.current_snapshot_id === x.snapshot_id).length,
      versions: view.items.filter((x: {source_id: string; content_sha256: string}) => x.source_id === 'user-jd')
        .map((x: {content_sha256: string}) => x.content_sha256)};
  });
  assert.equal(await cards.count(), 1);
  assert.equal(after.positions, positionsBefore.positions);
  assert.equal(after.current, 1);
  assert.equal(after.versions.length, 2, 'old and new snapshot are both kept');
  await page.screenshot({path: join(out, '01-after-edit.png'), fullPage: true});
  mark('改 JD 后仍是同一岗位、一张卡，旧快照保留', after);

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
