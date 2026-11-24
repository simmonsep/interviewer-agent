// F07Z-151 acceptance (spec §12): a real position -> its original JD is kept -> the brief is
// derived from it -> every requirement keeps its source -> the workbench shows the selected job
// -> materials and the interview read the SAME brief. Scripted stack, no model call.
//
//   node --experimental-strip-types apps/web/tests/position-brief.mts
import assert from 'node:assert/strict';
import {chromium, type Browser} from '@playwright/test';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.D151_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.D151_OUT ?? join(root, '.private/f07z151/brief'));
mkdirSync(out, {recursive: true});

const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };
const targetName = 'Java 后端开发实习';
const jd = [
  '岗位职责：负责订单查询接口的开发和维护，参与慢查询治理。',
  '任职要求：熟悉 Java、Spring Boot、MySQL；了解 Redis 者优先；有分布式系统经验者优先。',
  '工作地点：上海。',
].join('\n');

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  // ---------------------------------------------------------------- goal + a real (user-typed) job
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
  const goalVersion = await page.evaluate(async (id: string) =>
    (await (await fetch(`/api/career-goals/${id}`, {credentials: 'same-origin'})).json()).version as number, goalId);
  const manual = await page.evaluate(async ([id, version, text, title]: [string, number, string, string]) => {
    const response = await fetch(`/api/career-goals/${id}/jobs/manual`, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: version, company: '合成制造', title, cities: ['上海'],
        recruitment_label: '实习', url: 'https://example.com/jobs/java-1', jd_text: text})});
    return {status: response.status, body: await response.json()};
  }, [goalId, goalVersion, jd, targetName] as [string, number, string, string]);
  assert.ok([200, 201].includes(manual.status), `manual_job_failed ${manual.status} ${JSON.stringify(manual.body)}`);

  // ---------------------------------------------------------------- select it (a user action, as always)
  await page.locator('.d-nav-item[title="岗位研究"]').click();
  // The pane was mounted before the job existed; re-read the list like a user would.
  await page.locator('button', {hasText: '刷新岗位列表'}).first().click();
  const card = page.locator('.job-card').filter({hasText: targetName}).first();
  await card.waitFor({timeout: 60000});
  const ack = card.locator('input[type="checkbox"]');
  if (await ack.count()) await ack.first().check().catch(() => {});
  await card.getByRole('button', {name: '选择该岗位'}).click();
  await page.locator('[data-testid="position-brief"]').waitFor({timeout: 60000});

  // ---------------------------------------------------------------- the brief itself
  const brief = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="position-brief"]') as HTMLElement;
    return {
      text: root.innerText,
      facts: (root.querySelector('.d-brief-facts') as HTMLElement).innerText,
      chips: [...root.querySelectorAll('[data-testid="brief-skills"] .tag')].map(node => node.textContent?.trim()),
      chipTitles: [...root.querySelectorAll('[data-testid="brief-skills"] .tag')].map(node => node.getAttribute('title') ?? ''),
      unknowns: (root.querySelector('[data-testid="brief-unknowns"]') as HTMLElement).innerText,
      sources: (root.querySelector('[data-testid="brief-sources"]') as HTMLElement).innerText,
      rawOpen: (root.querySelector('[data-testid="brief-raw-jd"]') as HTMLDetailsElement).open,
      rawText: (root.querySelector('[data-testid="brief-raw-jd"]') as HTMLElement | null)?.textContent ?? '',
    };
  });
  assert.match(brief.facts, /合成制造/);
  assert.match(brief.facts, new RegExp(targetName));
  assert.match(brief.facts, /上海/);
  for (const skill of ['Java', 'Spring Boot', 'MySQL', 'Redis']) {
    assert.ok(brief.chips.includes(skill), `brief_missing_skill ${skill} in ${brief.chips.join(',')}`);
  }
  assert.ok(brief.chipTitles.some(title => title.includes('Spring Boot')), 'skill_chip_needs_its_quote');
  assert.match(brief.unknowns, /在招状态未确认/);
  assert.match(brief.unknowns, /未做官网核验/);
  assert.match(brief.sources, /用户输入/);
  assert.match(brief.sources, /[0-9a-f]{12}/i, 'brief_must_show_a_snapshot_fingerprint');
  assert.equal(brief.rawOpen, false, 'raw_jd_starts_collapsed');
  // The raw JD lives inside a closed <details>: textContent sees it, innerText does not.
  assert.ok(brief.rawText.includes('岗位职责'), 'brief_must_keep_the_raw_jd_available');
  mark('岗位简报：事实 / 技能词（带原文出处）/ 待确认 / 来源与版本都在，JD 原文默认折叠', {
    chips: brief.chips, unknowns: brief.unknowns.replace(/\n/g, ' / ').slice(0, 120)});
  await page.locator('[data-testid="position-brief"]').scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  await page.screenshot({path: join(out, '01-brief-1440.png'), fullPage: true});

  // ---------------------------------------------------------------- materials reads the same brief
  await page.locator('.d-nav-item[title="材料与简历"]').click();
  await page.locator('[data-testid="materials-head"]').waitFor({timeout: 60000});
  const materials = await page.evaluate(() => ({
    goal: document.querySelector('[data-testid="materials-goal"]')?.textContent ?? '',
    points: document.querySelector('[data-testid="materials-job-points"]')?.textContent ?? '',
  }));
  assert.match(materials.goal, new RegExp(targetName));
  assert.match(materials.points, /岗位重点/);
  for (const skill of ['Java', 'Spring Boot', 'MySQL']) {
    assert.ok(materials.points.includes(skill), `materials_points_missing ${skill}: ${materials.points}`);
  }
  mark('材料页读取同一个简报：目标岗位 + 岗位重点（来自岗位原文）', {
    points: materials.points.replace(/\s+/g, ' ').trim()});
  await page.screenshot({path: join(out, '02-materials-points.png'), fullPage: true});

  // ---------------------------------------------------------------- the interview setup reads it too
  await page.locator('.d-nav-item[title="面试"]').click();
  await page.locator('[data-testid="interview-job-points"]').waitFor({timeout: 60000});
  const interview = await page.locator('[data-testid="interview-job-points"]').innerText();
  // 收尾 02-3: the opening must not imply the bank was chosen by the position's keywords.
assert.match(interview, /已选岗位的原文重点/);
assert.match(interview, /只作背景参考/);
assert.doesNotMatch(interview, /本场按已选岗位准备/);
const source = await page.locator('[data-testid="interview-question-source"]').innerText();
assert.match(source, /本场题目来自/);
assert.match(source, /不会用来硬筛题目/);
  assert.match(interview, new RegExp(targetName));
  assert.ok(interview.includes('Java'), `interview_points_missing_skill ${interview}`);
  mark('面试开始页读取同一个简报，并如实区分「岗位原文重点」与「题目来自题库」', {
  line: interview.replace(/\s+/g, ' '), source: source.replace(/\s+/g, ' '),
});
  await page.screenshot({path: join(out, '03-interview-points.png'), fullPage: true});

  // ---------------------------------------------------------------- 390
  await page.locator('.d-nav-item[title="岗位研究"]').click();
  await page.locator('[data-testid="position-brief"]').waitFor({timeout: 30000});
  await page.setViewportSize({width: 390, height: 844});
  await page.waitForTimeout(500);
  const narrow = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="position-brief"]') as HTMLElement;
    const overflows = [root, ...root.querySelectorAll<HTMLElement>('*')]
      .filter(node => node.scrollWidth > node.clientWidth + 1).length;
    return {scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth, overflows};
  });
  assert.equal(narrow.scrollWidth, narrow.innerWidth, `brief_narrow_overflow ${JSON.stringify(narrow)}`);
  assert.equal(narrow.overflows, 0, 'brief_children_must_not_overflow');
  mark('390：岗位简报没有横向溢出', narrow);
  await page.screenshot({path: join(out, '04-brief-390.png'), fullPage: true});
  await page.setViewportSize({width: 1440, height: 1000});

  assert.equal(pageErrors.length, 0, `page_errors ${JSON.stringify(pageErrors.slice(0, 3))}`);
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: new Date().toISOString(), base, goal_id: goalId, snapshot_id: manual.body.snapshot_id,
    cases, page_errors: pageErrors, note: '真实本地栈；手工岗位由用户输入，0 模型调用。',
  }, null, 1), 'utf8');
  console.log(`${cases.length}/${cases.length} passed`);
} catch (error) {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({message: String(error), cases, page_errors: pageErrors}, null, 1), 'utf8');
  console.error(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
}
