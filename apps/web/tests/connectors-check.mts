// F07Z-162: the connectors section is off by default, explains what leaves the device, and only
// then reveals the five official references with a check button. No outbound call in the test.
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.CONN_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.CONN_OUT ?? join(root, '.private/closeout162/connectors'));
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
  // The test owns the starting state: a previous run may have left it on.
  await page.evaluate(async () => {
    await fetch('/api/runtime/connectors/reference-check', {
      method: 'PUT', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled: false})});
  });
  await page.locator('[data-testid="nav-settings"]').click();

  const section = page.locator('[data-testid="connectors-section"]');
  await section.waitFor({timeout: 30000});
  const connector = page.locator('[data-testid="connector-reference-check"]');
  await connector.waitFor({timeout: 30000});
  // The test owns the starting state: a previous run may have left it on.
  if ((await connector.innerText()).includes('已启用')) {
    await page.locator('[data-testid="connector-toggle-reference-check"]').click();
    await page.waitForFunction(() =>
      document.querySelector('[data-testid="connector-reference-check"]')?.textContent?.includes('未启用'),
      undefined, {timeout: 30000});
  }
  const initial = (await connector.innerText()).replace(/\s+/g, ' ');
  assert.match(initial, /未启用/);
  assert.match(initial, /不含简历、回答、题库正文和账号信息/);
  assert.equal(await page.locator('[data-testid="connector-items"]').count(), 0);
  await page.screenshot({path: join(out, '01-off.png'), fullPage: true});
  mark('默认关闭，并说明外发范围', {text: initial.slice(0, 200)});

  await page.locator('[data-testid="connector-toggle-reference-check"]').click();
  await page.locator('[data-testid="connector-items"]').waitFor({timeout: 30000});
  const items = await page.locator('[data-testid="connector-items"] .d-set-row').count();
  assert.equal(items, 5);
  const text = (await connector.innerText()).replace(/\s+/g, ' ');
  assert.match(text, /已启用/);
  assert.match(text, /redis\.io/);
  await page.screenshot({path: join(out, '02-on.png'), fullPage: true});
  mark('启用后列出 5 条官方来源与白名单域名', {items});

  const listing = await page.evaluate(async () => await (await fetch('/api/runtime/connectors', {credentials: 'same-origin'})).json());
  assert.equal(listing.connectors[0].enabled, true);
  mark('服务端开关与界面一致');

  // Turning it off again hides the list but keeps the choice stored.
  await page.locator('[data-testid="connector-toggle-reference-check"]').click();
  await page.waitForFunction(() => !document.querySelector('[data-testid="connector-items"]'), undefined, {timeout: 30000});
  mark('关闭后隐藏核验入口');

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
