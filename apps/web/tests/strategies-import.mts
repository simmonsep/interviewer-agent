// F07Z-164: a declarative strategy JSON is previewed, imported, disabled, re-enabled and removed
// from the settings pane. Real stack; no model call.
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.STRAT_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.STRAT_OUT ?? join(root, '.private/closeout164/strategies'));
mkdirSync(out, {recursive: true});
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

const document = {
  schema_version: 1, id: 'my-project-probes', name: '我的项目追问', role: 'java-backend',
  cards: [{id: 'my-lock-probe', title: '锁的失效边界', applies_to: ['Redis', '锁'],
           trigger: ['锁', '过期'], dimensions: ['verification', 'failure'],
           example_question: '锁过期后旧执行者还在跑，你会怎样验证不会覆盖新持有者？'}],
};
const file = join(out, 'strategy.json');
writeFileSync(file, JSON.stringify(document, null, 2), 'utf-8');

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
  await page.evaluate(async () => {
    await fetch('/api/strategies/my-project-probes', {method: 'DELETE', credentials: 'same-origin'});
  });
  await page.locator('[data-testid="nav-settings"]').click();
  const section = page.locator('[data-testid="strategies-section"]');
  await section.waitFor({timeout: 30000});
  await page.screenshot({path: join(out, '01-empty.png'), fullPage: true});
  mark('设置页出现追问策略分区');

  await page.locator('[data-testid="strategies-input"]').setInputFiles(file);
  const preview = page.locator('[data-testid="strategies-preview"]');
  await preview.waitFor({timeout: 30000});
  const text = (await preview.innerText()).replace(/\s+/g, ' ');
  assert.match(text, /我的项目追问/);
  assert.match(text, /锁的失效边界/);
  assert.match(text, /1 张/);
  mark('预览显示名称、角色与卡片', {text: text.slice(0, 160)});

  await page.locator('[data-testid="strategies-commit"]').click();
  const card = page.locator('[data-testid="strategy-my-project-probes"]');
  await card.waitFor({timeout: 30000});
  assert.match((await card.innerText()).replace(/\s+/g, ' '), /启用中/);
  mark('导入后可看到启用中的策略');

  await page.locator('[data-testid="strategy-toggle-my-project-probes"]').click();
  await page.waitForFunction(() => {
    const node = document.querySelector('[data-testid="strategy-my-project-probes"]');
    return !!node && node.textContent?.includes('已停用');
  }, undefined, {timeout: 30000});
  mark('停用生效');

  await page.locator('[data-testid="strategy-toggle-my-project-probes"]').click();
  await page.waitForFunction(() => {
    const node = document.querySelector('[data-testid="strategy-my-project-probes"]');
    return !!node && node.textContent?.includes('启用中');
  }, undefined, {timeout: 30000});
  mark('可重新启用');

  await page.locator('[data-testid="strategy-remove-my-project-probes"]').click();
  await page.locator('[data-testid="strategies-empty"]').waitFor({timeout: 30000});
  mark('卸载后回到空状态');

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
