// F07Z-161: a user-installed text skill is previewed, installed, disabled, re-enabled and removed
// from the settings pane. Real stack; no model call.
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, mkdtempSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.SKILL_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.SKILL_OUT ?? join(root, '.private/closeout161/skills'));
mkdirSync(out, {recursive: true});
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

const folder = mkdtempSync(join(tmpdir(), 'skill-'));
const skillDir = join(folder, 'slow-query');
mkdirSync(join(skillDir, 'references'), {recursive: true});
writeFileSync(join(skillDir, 'SKILL.md'), `---
id: slow-query
name: 慢查询排查
description: 面试时先定位慢查询的观察点与验证方式。
allowed-tools: Read
---
只问一个考点：先用执行计划说清慢在哪里，再问验证方式。
`, 'utf-8');
writeFileSync(join(skillDir, 'references', 'notes.md'), '复盘：先看执行计划。\n', 'utf-8');

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

  await page.locator('[data-testid="nav-settings"]').click();
  const section = page.locator('[data-testid="skills-section"]');
  await section.waitFor({timeout: 30000});
  await page.screenshot({path: join(out, '01-empty.png'), fullPage: true});
  mark('设置页出现技能分区');

  await page.locator('[data-testid="skills-input"]').setInputFiles(skillDir);
  const preview = page.locator('[data-testid="skills-preview"]');
  await preview.waitFor({timeout: 30000});
  const previewText = (await preview.innerText()).replace(/\s+/g, ' ');
  assert.match(previewText, /慢查询排查/);
  assert.match(previewText, /slow-query/);
  assert.match(previewText, /Read/);
  assert.match(previewText, /仅作为元数据识别/);
  await page.screenshot({path: join(out, '02-preview.png'), fullPage: true});
  mark('预览显示名称、id 与"工具只作元数据"', {text: previewText.slice(0, 200)});

  await page.locator('[data-testid="skills-commit"]').click();
  const card = page.locator('[data-testid="skill-slow-query"]');
  await card.waitFor({timeout: 30000});
  assert.match((await card.innerText()).replace(/\s+/g, ' '), /启用中/);
  mark('确认安装后可看到启用中的技能');

  await page.locator('[data-testid="skill-toggle-slow-query"]').click();
  await page.waitForFunction(() => {
    const node = document.querySelector('[data-testid="skill-slow-query"]');
    return !!node && node.textContent?.includes('已停用');
  }, undefined, {timeout: 30000});
  mark('停用后状态变为已停用');

  await page.locator('[data-testid="skill-toggle-slow-query"]').click();
  await page.waitForFunction(() => {
    const node = document.querySelector('[data-testid="skill-slow-query"]');
    return !!node && node.textContent?.includes('启用中');
  }, undefined, {timeout: 30000});
  mark('可重新启用');

  await page.locator('[data-testid="skill-remove-slow-query"]').click();
  await page.locator('[data-testid="skills-empty"]').waitFor({timeout: 30000});
  await page.screenshot({path: join(out, '03-removed.png'), fullPage: true});
  mark('卸载后回到空状态');

  const listing = await page.evaluate(async () => (await (await fetch('/api/skills', {credentials: 'same-origin'})).json()));
  assert.deepEqual(listing.skills, []);
  mark('服务端列表与界面一致');

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
