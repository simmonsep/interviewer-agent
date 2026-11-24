// F07Z-151 acceptance for the arbiter's C: a deep link that names a goal must never render the
// create form while the goal is loading, and a failed read must not silently degrade into it.
// Real stack; the delay/404 are injected and labelled.
//
//   node --experimental-strip-types apps/web/tests/goal-load-state.mts
import assert from 'node:assert/strict';
import {chromium, type Browser, type Route} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.D151B_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.D151B_OUT ?? join(root, '.private/f07z151/goal-load'));
mkdirSync(out, {recursive: true});

const cases: {name: string; type: 'real' | 'mock'; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, type: 'real' | 'mock' = 'real', detail?: unknown) => {
  cases.push({name, type, detail}); console.log(`PASS [${type}] ${name}`);
};
const targetName = 'Java 后端开发实习';

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  // ---------------------------------------------------------------- a real goal to link to
  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(700);
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('想在上海找 Java 后端实习。');
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.getByLabel('确认方向（可选）').fill(targetName);
  await page.locator('[data-testid="workbench-primary"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  const goalId = await page.evaluate(async () => (await (await fetch('/api/career-goals/', {credentials: 'same-origin'})).json()).items[0]);
  const deepLink = `${base}/?shell=desktop&goal=${goalId}`;
  mark('对照：目标创建成功、进度行出现', 'real', {goal_id: goalId});

  // ---------------------------------------------------------------- 1) slow read: loading, no create form
  const detailPattern = `**/api/career-goals/${goalId}`;
  const requested: string[] = [];
  page.on('request', request => {
    if (request.method() !== 'GET') return;
    const match = request.url().match(/\/api\/career-goals\/[0-9a-f-]{36}$/);
    if (match) requested.push(request.url().slice(-36));
  });
  // The page can navigate/cancel while a reply is in flight; Playwright then throws
  // "Route is already handled". That is noise for this contract - every assertion is on the DOM.
  const mockDetail = (mode: 'slow' | '404') => async (route: Route) => {
    if (route.request().method() !== 'GET') return route.continue();
    try {
      if (mode === '404') {
        return await route.fulfill({status: 404, contentType: 'application/json',
          body: JSON.stringify({code: 'resource_unavailable'})});
      }
      const response = await route.fetch();
      await delay(1800);
      return await route.fulfill({response});
    } catch {
      return undefined;
    }
  };
  await page.route(detailPattern, mockDetail('slow'));
  await page.goto(deepLink, {waitUntil: 'domcontentloaded'});
  await page.locator('[data-testid="goal-loading"]').waitFor({timeout: 30000});
  const during = await page.evaluate(() => ({
    loading: Boolean(document.querySelector('[data-testid="goal-loading"]')),
    createForm: document.querySelectorAll('[data-testid="empty-create-goal"]').length,
    form: document.querySelectorAll('#desktop-goal-form').length,
    title: document.querySelector('[data-testid="workbench-goal"]')?.textContent?.trim() ?? '',
    crumb: document.querySelector('[data-testid="desktop-goal"]')?.textContent?.trim() ?? '',
  }));
  assert.equal(during.loading, true, 'slow_read_must_show_a_loading_surface');
  assert.equal(during.createForm, 0, 'slow_read_must_not_render_the_create_button');
  assert.equal(during.form, 0, 'slow_read_must_not_render_the_create_form');
  assert.equal(during.title, '正在读取目标…');
  assert.equal(during.crumb, '正在读取目标', 'the header crumb must not claim the goal does not exist');
  await page.screenshot({path: join(out, '01-goal-loading.png'), fullPage: true});
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 30000});
  assert.equal(await page.locator('[data-testid="goal-loading"]').count(), 0, 'loading_surface_must_disappear');
  assert.equal((await page.locator('[data-testid="workbench-goal"]').innerText()).trim(), targetName,
    'goal_must_be_the_real_title_after_the_read');
  mark('深链加载中：显示「正在读取目标…」，不渲染创建表单；读完显示真实目标', 'mock', during);
  await page.unroute(detailPattern);

  // ---------------------------------------------------------------- 2) 404: route error + retry works
  await page.route(detailPattern, mockDetail('404'));
  await page.goto(deepLink, {waitUntil: 'domcontentloaded'});
  await page.locator('[data-testid="goal-load-error"]').waitFor({timeout: 60000});
  const failed = await page.evaluate(() => ({
    text: document.querySelector('[data-testid="goal-load-error"]')?.textContent ?? '',
    createForm: document.querySelectorAll('[data-testid="empty-create-goal"]').length,
    form: document.querySelectorAll('#desktop-goal-form').length,
    crumb: document.querySelector('[data-testid="desktop-goal"]')?.textContent?.trim() ?? '',
  }));
  assert.match(failed.text, /目标没有读出来/);
  assert.match(failed.text, /不存在|不属于当前账号/);
  assert.doesNotMatch(failed.text, /404|resource_unavailable/, 'route_error_must_not_print_a_code');
  assert.equal(failed.createForm, 0, 'failed_read_must_not_degrade_into_the_create_form');
  assert.equal(failed.form, 0);
  assert.equal(failed.crumb, '目标没有读出来', 'the header crumb must report the failed read, not an empty state');
  await page.screenshot({path: join(out, '02-goal-load-error.png'), fullPage: true});
  mark('目标读取失败：明确的错误面 + 重试/改为新建，不静默退化成创建表单', 'mock', failed);

  // 收尾 02-1: the locator moved (another goal id in the URL). Retry must still ask for the id
  // that failed, and the error surface must stay instead of revealing the create form.
  const other = '00000000-0000-4000-8000-000000000000';
  await page.evaluate(id => window.history.replaceState(null, '', `/?shell=desktop&goal=${id}`), other);
  requested.length = 0;
  await page.locator('[data-testid="goal-load-retry"]').click();
  await page.waitForTimeout(1500);
  assert.ok(requested.includes(goalId), `retry must reuse the failed goal id: ${JSON.stringify(requested)}`);
  assert.equal(requested.includes(other), false, 'retry must not switch to another goal');
  assert.equal(await page.locator('[data-testid="goal-load-error"]').count(), 1, 'error must stay after a failed retry');
  assert.equal(await page.locator('[data-testid="empty-create-goal"]').count(), 0, 'retry must not reveal the create form');
  mark('重试读取：沿用当次失败的 id，不跟随 URL 切到别的目标，也不退化成创建表单', 'mock', requested.slice(-3));

  await page.unroute(detailPattern);
  await page.locator('[data-testid="goal-load-retry"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  assert.equal((await page.locator('[data-testid="workbench-goal"]').innerText()).trim(), targetName);
  mark('重试读取：恢复真实目标', 'mock');

  // ---------------------------------------------------------------- 3) 404 + "create instead"
  await page.route(detailPattern, mockDetail('404'));
  await page.goto(deepLink, {waitUntil: 'domcontentloaded'});
  await page.locator('[data-testid="goal-load-create"]').waitFor({timeout: 60000});
  await page.locator('[data-testid="goal-load-create"]').click();
  await page.locator('[data-testid="empty-create-goal"]').waitFor({timeout: 30000});
  assert.equal(await page.locator('[data-testid="goal-load-error"]').count(), 0, 'create_choice_must_clear_the_error');
  mark('用户主动「改为新建目标」：才显示创建表单', 'mock');
  await page.unroute(detailPattern);

  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, goal_id: goalId, cases, page_errors: pageErrors,
    note: '真实本地栈 + 注入慢读/404；scripted，无模型调用。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
