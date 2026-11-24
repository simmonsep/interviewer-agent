import {waitGoalReady} from './desktop-nav.mts';
// F07Z-141 acceptance (doc §71-73): one primary action, one target surface, readable goal copy.
// Runs against the local stack (API 3100 + Vite 5174 + test DB). No model call: the goal is
// created through the same form a user fills, and the session is scripted.
//
//   D141_BASE=http://127.0.0.1:5174 node --experimental-strip-types apps/web/tests/workbench-next-action.mts
import assert from 'node:assert/strict';
import {chromium,type Browser} from '@playwright/test';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.D141_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.D141_OUT ?? join(root, '.private/f07z141/evidence'));
mkdirSync(out, {recursive: true});

const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

let browser: Browser | null = null;
try {
  const health = await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)});
  assert.ok(health.ok, 'local_stack_not_ready');

  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(800);

  // ---------------------------------------------------------------- 1. empty: one surface, one primary
  const empty = await page.evaluate(() => {
    const pane = document.querySelector('.desktop-pane') as HTMLElement;
    const text = pane.innerText;
    return {
      primaryActions: pane.querySelectorAll('[data-workbench-primary-action]').length,
      primaryIsSubmit: (pane.querySelector('[data-workbench-primary-action]') as HTMLElement | null)?.getAttribute('type'),
      accentButtons: [...pane.querySelectorAll('button.primary')].filter(button => !(button as HTMLButtonElement).disabled).length,
      createCard: pane.querySelectorAll('[data-testid="empty-create-cta"]').length,
      createHeadings: [...pane.querySelectorAll('h1,h2,h3')].filter(node => (node.textContent ?? '').trim() === '创建求职目标').length,
      createWords: (text.match(/创建求职目标/g) ?? []).length,
      separateCtaButton: pane.querySelectorAll('[data-testid="empty-create-goal"]').length,
      textareaVisible: Boolean((pane.querySelector('textarea[aria-label="求职意向或补充说明"]') as HTMLElement | null)?.offsetParent),
    };
  });
  assert.equal(empty.primaryActions, 1, 'empty_must_have_exactly_one_primary_action');
  assert.equal(empty.primaryIsSubmit, 'submit', 'empty_primary_should_be_the_goal_submit');
  // Nothing to submit yet, so the single primary is correctly disabled: zero enabled accents.
  assert.ok(empty.accentButtons <= 1, `empty_must_not_show_two_accent_buttons ${empty.accentButtons}`);
  assert.equal(empty.createCard, 1, 'empty_surface_missing');
  assert.equal(empty.createHeadings, 1, `create_heading_should_appear_once ${empty.createHeadings}`);
  assert.equal(empty.separateCtaButton, 1, 'the label node inside the submit should stay addressable');
  assert.equal(empty.textareaVisible, true, 'the one sentence field must be on the first screen');
  mark('无目标：只有一个创建 surface、一个 Primary（表单提交），不再有独立的 CTA 卡', empty);
  await page.screenshot({path: join(out, '01-empty-desktop.png'), fullPage: true});

  // ---------------------------------------------------------------- 2. create the goal (scripted, 0 cost)
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在上海找 AI 应用 / Agent 开发实习。');
  const typed = await page.evaluate(() => {
    const pane = document.querySelector('.desktop-pane') as HTMLElement;
    return [...pane.querySelectorAll('button.primary')].filter(button => !(button as HTMLButtonElement).disabled).length;
  });
  assert.equal(typed, 1, `typing_must_enable_exactly_one_accent_button ${typed}`);
  await page.locator('[data-testid="workbench-primary"]').click();
  await waitGoalReady(page);
  await page.waitForTimeout(2500);

  const withGoal = await page.evaluate(() => {
    const pane = document.querySelector('.desktop-pane') as HTMLElement;
    const goalCard = pane.querySelector('[aria-label="当前求职目标"]') as HTMLElement;
    const goalButton = pane.querySelector('[data-testid="workbench-primary"]') as HTMLElement;
    const next = pane.querySelector('[data-workbench-primary-action]') as HTMLElement | null;
    return {
      primaryActions: pane.querySelectorAll('[data-workbench-primary-action]').length,
      primaryTestId: next?.dataset.testid,
      nextLabel: next?.textContent?.trim(),
      goalButtonClass: goalButton.className,
      goalButtonDisabled: (goalButton as HTMLButtonElement).disabled,
      goalCardText: goalCard.innerText,
      pipelineHeading: (pane.querySelector('[data-testid="agent-pipeline"] h2') as HTMLElement | null)?.textContent,
      pipelineLede: (pane.querySelector('.d-pipeline-lede') as HTMLElement | null)?.textContent,
      pipelineRunKept: pane.querySelectorAll('[data-testid="pipeline-run"]').length,
      stepTitles: [...pane.querySelectorAll('[data-testid^="pipeline-step-"] strong')].map(node => node.textContent),
      dockHeading: (pane.querySelector('[data-testid="agent-dock"] h2') as HTMLElement | null)?.textContent,
      stages: [...pane.querySelectorAll('[data-testid="career-progress"] .d-progress-step')].map(node => ({
        key: (node as HTMLElement).dataset.stage,
        state: (node as HTMLElement).dataset.state,
        label: node.textContent?.replace(/[✓●○!\s]/g, ''),
      })),
    };
  });
  assert.equal(withGoal.primaryActions, 1, 'goal_state_must_keep_one_primary_action');
  assert.equal(withGoal.primaryTestId, 'workbench-next-action', 'the state-driven next action owns the primary');
  assert.equal(withGoal.nextLabel, '添加真实材料', `next_action_label ${withGoal.nextLabel}`);
  assert.equal(withGoal.goalButtonClass, 'secondary', 'the goal card action must stay secondary');
  assert.equal(withGoal.goalButtonDisabled, false, 'the secondary action still has to work');
  assert.equal(withGoal.pipelineHeading, '下一步', `next_action_heading_wrong ${withGoal.pipelineHeading}`);
  assert.match(withGoal.pipelineLede ?? '', /添加真实材料/, 'the card must name the current step');
  assert.equal(withGoal.pipelineRunKept, 1, 'the one-click pipeline must survive as a secondary entry');
  assert.deepEqual(withGoal.stepTitles, ['完善简历', '模拟面试', '复盘记录'], `step_titles ${JSON.stringify(withGoal.stepTitles)}`);
  assert.equal(withGoal.dockHeading, '专属 Agent', `dock_heading ${withGoal.dockHeading}`);
  assert.deepEqual(withGoal.stages.map(stage => stage.key), ['goal', 'materials', 'resume', 'interview', 'review', 'application']);
  assert.deepEqual(withGoal.stages.map(stage => stage.state), ['completed', 'current', 'upcoming', 'upcoming', 'upcoming', 'upcoming']);
  assert.deepEqual(withGoal.stages.map(stage => stage.label), ['目标', '材料', '简历', '面试', '复盘', '投递']);
  mark('有目标：唯一 Primary 落在状态化的「下一步」，目标卡退为 Secondary；进度行指出当前阶段', withGoal);
  // The coordinator may legitimately put the cursor back in the goal form (ask_user), which
  // opens 调整目标条件. The review shot shows the card as a default-policy user sees it.
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-details') as HTMLDetailsElement | null;
    if (details) details.open = false;
  });
  await page.waitForTimeout(400);
  await page.screenshot({path: join(out, '02-goal-desktop.png'), fullPage: true});

  // §15: a created goal must say what happens next, and the CTA must land on that page.
  const notice = await page.locator('[data-testid="goal-created-next"]').innerText().catch(() => '');
  assert.match(notice, /目标已创建/, 'created_goal_must_state_the_next_step');
  await page.locator('[data-testid="workbench-next-action"]').click();
  await page.locator('[data-testid="materials-pane"]').waitFor({timeout: 20000});
  assert.equal(await page.locator('.desktop-shell').getAttribute('data-pane'), 'materials');
  mark('目标创建后有明确下一步；CTA 落到真实目的页（添加材料 → 材料与简历）', {notice});
  await page.locator('.d-nav-item').filter({hasText: '工作台'}).first().click();
  await page.waitForTimeout(700);

  // ---------------------------------------------------------------- 3. internal state values stay off the product surface (doc §73)
  const surface = await page.evaluate(() => {
    const pane = document.querySelector('.desktop-pane') as HTMLElement;
    const card = pane.querySelector('[aria-label="当前求职目标"]') as HTMLElement;
    return {card: card.innerText, pane: pane.innerText};
  });
  for (const technical of ['等待后台协调', '目标状态', 'WAITING_FOR', '输入已保存；']) {
    assert.equal(surface.card.includes(technical), false, `goal_card_leaks_internal_state ${technical}`);
  }
  assert.match(surface.card, /(正在整理目标|还有信息需要确认|目标已确认|这次整理没完成|求职方向正在确认中)/,
    'goal_card_must_say_what_is_happening_in_user_words');
  mark('目标卡不暴露内部状态值，改成可读状态；技术值仍可在运行详情里查到', {card: surface.card.slice(0, 160)});

  // ---------------------------------------------------------------- 4. narrow: one surface, optional fields folded
  const phone = await browser.newContext({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  const narrow = await phone.newPage();
  narrow.on('pageerror', error => pageErrors.push(error.message));
  await narrow.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await narrow.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 60000}).catch(() => null);
  await narrow.waitForTimeout(1500);
  const mobile = await narrow.evaluate(() => {
    const pane = document.querySelector('.desktop-pane') as HTMLElement;
    const more = pane.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    return {
      horizontal: document.documentElement.scrollWidth > window.innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      surfaces: pane.querySelectorAll(':scope > section.card').length,
      moreOpen: more?.open ?? null,
      moreVisible: Boolean(more?.offsetParent),
      textareaVisible: Boolean((pane.querySelector('textarea[aria-label="求职意向或补充说明"]') as HTMLElement | null)?.offsetParent),
      primaryActions: pane.querySelectorAll('[data-workbench-primary-action]').length,
    };
  });
  assert.equal(mobile.horizontal, false, `narrow_horizontal_scroll ${mobile.scrollWidth}`);
  assert.equal(mobile.moreOpen, false, 'optional_conditions_should_start_collapsed_on_phone');
  assert.equal(mobile.moreVisible, true, 'the disclosure itself must be reachable');
  assert.equal(mobile.textareaVisible, true, 'the one sentence field stays on the first screen');
  assert.equal(mobile.primaryActions, 1, 'narrow_state_must_keep_one_primary_action');
  mark('390px：无横向滚动、可选条件默认折叠、核心字段与唯一 Primary 都在首屏', mobile);
  await narrow.screenshot({path: join(out, '03-empty-narrow.png'), fullPage: true});
  await phone.close();

  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(),
    cases,
    page_errors: pageErrors,
    note: '真实本地栈；目标由表单创建，未调用模型（scripted）。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} finally {
  await browser?.close();
}
