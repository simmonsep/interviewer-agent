// Packaged-artifact acceptance probe (收尾 03).
//
// Runs the shipped build (not the dev server) in its real WebView2 window and walks one scripted
// journey: goal -> manual job -> selected position -> position brief -> two questions with a
// mid-session reload -> review -> retest suggestion -> application entry -> practice shell.
//
//   $env:PROBE_PACKAGE = 'E:\path\to\interview-workbench'
//   $env:PROBE_OUT     = '.'            # optional, defaults to .private/desktop-probe/<stamp>
//   node --experimental-strip-types scripts/desktop-acceptance-probe.mts
//
// Safety rules (deliberate):
//   * refuses to start when 3100 is already in use - it never takes over or stops another process;
//   * only accepts a test-mode settings directory (./settings or PROBE_SETTINGS);
//   * starts and stops only its own child; a failure still writes screenshots and exits non-zero.
import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
import {spawn} from 'node:child_process';
import {existsSync, mkdirSync, readFileSync, writeFileSync, rmSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

const root = process.cwd();
const pkg = process.env.PROBE_PACKAGE;
if (!pkg) {
  console.error('PROBE_PACKAGE is required (the built interview-workbench directory)');
  process.exit(2);
}
const artifact = resolve(pkg);
const exe = join(artifact, 'interview-desktop-probe.exe');
const settings = resolve(process.env.PROBE_SETTINGS ?? join(artifact, 'settings'));
const stamp = new Date().toISOString().replace(/[:.]/g, '-');
const out = resolve(process.env.PROBE_OUT ?? join(root, '.private/desktop-probe', stamp));
const port = Number(process.env.PROBE_CDP_PORT ?? 9334);
const fixturePath = resolve(process.env.PROBE_FIXTURE ?? join(root, 'fixtures/question-banks/agent-developer-trial.json'));

const cases: {name: string; detail?: unknown}[] = [];
const mark = (name: string, detail?: unknown) => {
  cases.push({name, detail});
  console.log('PROBE PASS', name);
};
const fail = (message: string, detail?: unknown) => {
  writeFileSync(join(out, 'result-failure.json'), JSON.stringify({
    run: stamp, package: artifact, cases, message, detail,
  }, null, 1), 'utf8');
  console.error('PROBE FAIL', message, detail ?? '');
};

/** The probe must never adopt a port that another program already owns. */
async function portFree() {
  try {
    const response = await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(1500)});
    return !response.ok;
  } catch {
    return true;
  }
}

let child: ReturnType<typeof spawn> | null = null;
let browser: Awaited<ReturnType<typeof chromium.connectOverCDP>> | undefined;
const problems: string[] = [];
const startedAt = Date.now();

try {
  assert.ok(existsSync(exe), `missing executable: ${exe}`);
  assert.ok(existsSync(settings), `missing settings directory: ${settings}`);
  const api = JSON.parse(readFileSync(join(settings, 'api.json'), 'utf8'));
  const worker = JSON.parse(readFileSync(join(settings, 'worker.json'), 'utf8'));
  assert.equal(api.mode, 'test', 'probe only accepts a test-database settings directory');
  assert.equal(worker.mode, 'test', 'probe only accepts a test-database worker settings directory');
  assert.ok(await portFree(), 'port 3100 is already in use; stop that process yourself and re-run');
  mark('preflight: package + test settings + 3100 free', {package: artifact, settings});

  mkdirSync(out, {recursive: true});
  const dataDir = join(out, 'data');
  rmSync(dataDir, {recursive: true, force: true});
  mkdirSync(dataDir, {recursive: true});
  const fixture = JSON.parse(readFileSync(fixturePath, 'utf8'));

  child = spawn(exe, [], {
    cwd: artifact,
    env: {...process.env,
      INTERVIEW_DESKTOP_DATA_DIR: dataDir,
      INTERVIEW_PROBE_OUTPUT: join(out, 'run'),
      INTERVIEW_DESKTOP_CDP_PORT: String(port)},
    stdio: 'ignore',
    windowsHide: false,
  });

  let page: any = null;
  for (let attempt = 0; attempt < 60 && !page; attempt += 1) {
    await delay(1000);
    try {
      browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
      page = browser.contexts()[0]?.pages()[0] ?? null;
    } catch { /* still starting */ }
  }
  assert.ok(page, 'cdp_attach_failed');
  page.on('pageerror', (error: Error) => problems.push(error.message));
  await page.waitForSelector('.desktop-shell', {timeout: 180000});
  await page.waitForSelector('textarea[aria-label="求职意向或补充说明"]', {timeout: 120000});

  const shell = await page.evaluate(() => ({
    devBadge: /版本\s*B/.test(document.body.innerText),
    url: location.href,
  }));
  assert.equal(shell.devBadge, false, 'packaged build shows a development badge');
  mark('window renders the workbench without a development badge');

  // ---------------------------------------------------------------- goal
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.fill('textarea[aria-label="求职意向或补充说明"]', '想在上海找 AI 应用 / Agent 开发实习。');
  await page.fill('input[maxlength="80"]', 'AI 应用开发实习生');
  await page.click('[data-testid="workbench-primary"]');
  await page.waitForSelector('[data-testid="career-progress"]', {timeout: 180000});
  const goalId = await page.evaluate(async () => {
    const response = await fetch('/api/career-goals/', {credentials: 'same-origin'});
    return (await response.json()).items[0];
  });
  const bank = await page.evaluate(async (document: unknown) => {
    const preview = await (await fetch('/api/question-banks/imports', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({document})})).json();
    const commit = await (await fetch(`/api/question-banks/imports/${preview.preview_id}/commit`, {method: 'POST',
      credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({})})).json();
    return {bank_id: commit.bank_id, version: commit.version};
  }, fixture);

  // ---------------------------------------------------------------- deep link + brief
  await page.goto(`http://127.0.0.1:3100/?shell=desktop&goal=${goalId}`);
  await page.waitForSelector('[data-goal-state="ready"]', {timeout: 180000});
  const heading = await page.textContent('[data-testid="workbench-goal"]');
  assert.equal((heading ?? '').trim(), 'AI 应用开发实习生', 'deep link did not load the goal title');

  const goalVersion = await page.evaluate(async (id: string) =>
    (await (await fetch(`/api/career-goals/${id}`, {credentials: 'same-origin'})).json()).version as number, goalId);
  const manual = await page.evaluate(async ([id, version]: [string, number]) => {
    const created = await fetch(`/api/career-goals/${id}/jobs/manual`, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      body: JSON.stringify({expected_version: version, company: '合成制造', title: 'AI 应用开发实习生',
        cities: ['上海'], recruitment_label: '实习', url: 'https://example.com/jobs/ai-1',
        jd_text: '任职要求：熟悉 Python 与 LangGraph；了解 Redis 者优先。'})});
    const body = await created.json();
    const listing = await (await fetch(`/api/career-goals/${id}/jobs`, {credentials: 'same-origin'})).json();
    const item = (listing.items ?? []).find((row: {snapshot_id: string}) => row.snapshot_id === body.snapshot_id);
    const done = await fetch(`/api/career-goals/${id}/selection`, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({expected_version: version,
        run_id: item?.run_id, snapshot_id: body.snapshot_id, acknowledge_unknown: true})});
    return {created: created.status, selected: done.status};
  }, [goalId, goalVersion] as [string, number]);
  assert.equal(manual.selected, 200, `selection failed ${JSON.stringify(manual)}`);
  await page.click('.d-nav-item[title="岗位研究"]');
  // The research pane can mount before the API-created job exists in its list; a real user has
  // the same "刷新岗位列表" button, so the probe uses it instead of assuming a remount.
  try {
    await page.waitForSelector('[data-testid="position-brief"]', {timeout: 20000});
  } catch {
    await page.click('button:has-text("刷新岗位列表")');
    await page.waitForSelector('[data-testid="position-brief"]', {timeout: 90000});
  }
  const brief = await page.evaluate(() => {
    const root = document.querySelector('[data-testid="position-brief"]') as HTMLElement;
    return {
      title: (root.querySelector('h3') as HTMLElement)?.textContent ?? '',
      chips: [...root.querySelectorAll('[data-testid="brief-skills"] .tag')].map(node => node.textContent?.trim()),
      unknowns: (root.querySelector('[data-testid="brief-unknowns"]') as HTMLElement)?.innerText ?? '',
    };
  });
  assert.equal(brief.title.trim(), 'AI 应用开发实习生', 'brief title wrong');
  assert.ok(brief.chips.includes('Python') && brief.chips.includes('LangGraph'), `brief chips ${brief.chips}`);
  assert.match(brief.unknowns, /在招状态未确认/);
  await page.screenshot({path: join(out, '01-brief.png'), fullPage: true});
  mark('deep link -> selected position -> position brief', brief);

  // ---------------------------------------------------------------- two questions, mid reload
  await page.click('.d-nav-item[title="面试"]');
  await page.waitForFunction(() => document.querySelector('.desktop-shell')?.getAttribute('data-pane') === 'interview');
  await page.waitForSelector('#interview-bank', {state: 'visible', timeout: 90000});
  // The journey needs a bank whose topics the authored pack can resolve a retest question for;
  // the imported fixture above already proved the import path, but its items have no retest.
  const builtin = await page.evaluate(() => {
    const select = document.querySelector('#interview-bank') as HTMLSelectElement | null;
    const option = select ? [...select.options].find(item => /ai-application 默认/.test(item.textContent ?? '')) : null;
    return option ? option.value : null;
  });
  assert.ok(builtin, 'builtin bank for the goal role is missing from the picker');
  await page.selectOption('#interview-bank', builtin!);
  await page.click('button:has-text("开始整场面试 →")');
  await page.waitForSelector('[data-testid="interview-question"]', {timeout: 180000});
  const first = await page.textContent('[data-testid="interview-question"]');
  const sessionId = await page.getAttribute('[data-session-id]', 'data-session-id');
  assert.ok((await page.evaluate(() => location.search)).includes(`interview=${sessionId}`), 'URL lost the session locator');
  await page.reload();
  await page.waitForSelector('[data-session-id]', {timeout: 180000});
  assert.equal(await page.getAttribute('[data-session-id]', 'data-session-id'), sessionId, 'reload lost the session');
  await page.waitForSelector('[data-testid="interview-question"]', {timeout: 180000});
  assert.equal(await page.textContent('[data-testid="interview-question"]'), first, 'reload lost the question');
  // The first answer is deliberately weak so the scripted protocol records needs_probe and the
  // review must resolve an authored retest question for it.
  await page.fill('#interview-answer', '不知道，这块我没做过。');
  await page.click('[data-testid="answer-submit"]');
  await page.waitForFunction((previous: string | null) => {
    const node = document.querySelector('[data-testid="interview-question"]');
    return !!node && node.textContent !== previous;
  }, first, {timeout: 180000});
  await page.fill('#interview-answer', '用 Command(resume=...) 恢复，副作用要幂等。');
  await page.click('[data-testid="answer-submit"]');
  await page.waitForSelector('[data-testid="interview-review"]', {timeout: 240000});

  const review = await page.evaluate(() => {
    const node = document.querySelector('[data-testid="interview-review"]') as HTMLElement;
    const drills = [...node.querySelectorAll('[data-testid^="review-retest-"]')] as HTMLElement[];
    return {
      status: (node.querySelector('[data-testid="review-status"]') as HTMLElement)?.innerText,
      primaries: [...node.querySelectorAll('button.primary')].map(button => button.textContent?.trim()),
      entry: (node.querySelector('[data-application-entry]') as HTMLElement | null)?.dataset.applicationEntry ?? null,
      answers: node.querySelectorAll('[data-testid="review-answers"] .d-review-answer').length,
      hasGoalTitle: Boolean(node.querySelector('.d-review-title h2')?.textContent?.includes('AI 应用开发实习生')),
      drills: drills.map(block => ({
        question: (block.querySelector('.d-review-retest-question') as HTMLElement | null)?.innerText ?? '',
        text: block.innerText,
      })),
      devBadge: /版本\s*B/.test(document.body.innerText),
    };
  });
  assert.equal(review.devBadge, false, 'review shows a development badge');
  assert.equal(review.status, '复盘可查看');
  assert.deepEqual(review.primaries, ['记录一次投递']);
  assert.equal(review.entry, 'record-application');
  assert.equal(review.answers, 2);
  assert.equal(review.hasGoalTitle, true);
  assert.ok(review.drills.length >= 1, 'a needs_probe answer must yield a retest suggestion');
  assert.ok(review.drills[0].question.length > 8, 'the suggestion must carry a question');
  assert.match(review.drills[0].text, /题库版次/);
  await page.screenshot({path: join(out, '02-review-retest.png'), fullPage: true});
  mark('two questions + mid-session reload + review with a retest suggestion', {
    drills: review.drills.length, entry: review.entry,
  });

  // ---------------------------------------------------------------- entry + practice shell
  await page.click('[data-testid="review-to-applications"]');
  await page.waitForFunction(() => document.querySelector('.desktop-shell')?.getAttribute('data-pane') === 'apps',
    undefined, {timeout: 60000});
  const routed = await page.locator('.desktop-shell').getAttribute('data-pane');
  await page.screenshot({path: join(out, '03-applications.png'), fullPage: true});

  await page.goto(`${new URL(page.url()).origin}/?shell=web`);
  await page.waitForSelector('.site-header', {timeout: 120000});
  const practice = await page.evaluate(() => document.body.innerText.slice(0, 60));
  await page.screenshot({path: join(out, '04-practice.png'), fullPage: true});
  mark('application entry routes by precondition; practice shell loads', {routed, practice: practice.replace(/\s+/g, ' ')});

  assert.equal(problems.length, 0, `page errors: ${JSON.stringify(problems.slice(0, 3))}`);
  const git = process.env.PROBE_GIT ?? '';
  writeFileSync(join(out, 'result.json'), JSON.stringify({
    run: stamp, package: artifact, git, settings, port,
    seconds: Math.round((Date.now() - startedAt) / 1000),
    shell, goalId, bank, brief, review, routed,
    cases, page_errors: problems,
    note: 'scripted only: four Workers use the free protocol; no model request is made.',
  }, null, 1), 'utf8');
  console.log('PROBE RESULT', JSON.stringify({cases: cases.length, out, seconds: Math.round((Date.now() - startedAt) / 1000)}));
  await page.evaluate(() => (window as any).__TAURI__?.core?.invoke('quit_app')).catch(() => {});
} catch (error) {
  fail(String(error), {page_errors: problems.slice(0, 3)});
  process.exitCode = 1;
} finally {
  await delay(1500);
  try { child?.kill(); } catch { /* already gone */ }
  await browser?.close().catch(() => {});
}
