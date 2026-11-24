// F07Z-166: a Java direction must prepare, plan, open and ask from the Java role pack, not from
// the AI-application default.
import assert from 'node:assert/strict';
import {chromium, type Browser, type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {mkdirSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root = resolve(import.meta.dirname, '../../..');
const base = process.env.JAVA_BASE ?? 'http://127.0.0.1:5174';
const out = resolve(process.env.JAVA_OUT ?? join(root, '.private/f07z166/java-role'));
const python = process.env.JAVA_PYTHON ?? 'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
mkdirSync(out, {recursive: true});
const runId = randomUUID();
const execute = promisify(execFile);
const cases: {name: string; detail?: unknown}[] = [];
const pageErrors: string[] = [];
const mark = (name: string, detail?: unknown) => { cases.push({name, detail}); console.log('PASS', name); };

// The role's own topic ids; the shared ones are listed too so a pack update is visible here.
const JAVA_TOPICS = ['java-concurrency', 'sql-isolation', 'redis-lock', 'http-idempotency'];

async function scoped(goal: string, script: string, mode?: string) {
  const args = ['-X', 'utf8', script, '--goal', goal, '--run-id', runId];
  if (mode) args.push('--mode', mode);
  if (script.includes('prep-web')) args.push('--watch', '120');
  const result = await execute(python, args, {cwd: root, windowsHide: true, timeout: 170000,
    env: {...process.env, PYTHONPATH: 'backend/src'}});
  return JSON.parse(result.stdout) as {status: string; session_id: string | null};
}

const primary = (page: Page) => page.locator('[data-testid="workbench-next-action"]');

let browser: Browser | null = null;
try {
  assert.ok((await fetch('http://127.0.0.1:3100/health', {signal: AbortSignal.timeout(4000)})).ok, 'local_stack_not_ready');
  assert.ok((await fetch(base + '/', {signal: AbortSignal.timeout(4000)})).ok, 'vite_not_ready');
  browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true});
  const page = await (await browser.newContext({viewport: {width: 1440, height: 1000}})).newPage();
  page.on('pageerror', error => pageErrors.push(error.message));

  await page.goto(`${base}/?shell=desktop`, {waitUntil: 'domcontentloaded'});
  await page.locator('nav[aria-label="主导航"]').first().waitFor({timeout: 90000});
  await page.waitForTimeout(600);
  await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('按这份 JD 练一场 Java 后端面试。');
  await page.evaluate(() => {
    const details = document.querySelector('details.d-goal-more') as HTMLDetailsElement | null;
    if (details) details.open = true;
  });
  await page.getByLabel('确认方向（可选）').fill('Java 后端');
  await page.locator('textarea[aria-label="职位说明 JD"]').fill(
    '岗位职责：负责 Java 服务端开发，使用 Spring Boot、PostgreSQL 与 Redis，要求理解事务隔离与并发。');
  await page.locator('[data-testid="workbench-train"]').click();
  await page.locator('[data-testid="career-progress"]').waitFor({timeout: 60000});
  const goalId = await page.evaluate(() => new URLSearchParams(location.search).get('goal'));
  assert.ok(goalId, 'goal_id_missing');
  await scoped(goalId!, 'scripts/career-web-support.py');

  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.includes('正在准备') === true;
  }, undefined, {timeout: 60000});
  const planned = await scoped(goalId!, 'scripts/prep-web-support.py', 'plan');
  assert.equal(planned.status, 'WAITING_FOR_USER', JSON.stringify(planned));
  await page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="workbench-next-action"]') as HTMLButtonElement | null;
    return button?.textContent?.trim() === '开始这场';
  }, undefined, {timeout: 60000});

  const planText = await page.locator('[data-testid="pipeline-plan-text"]').innerText();
  assert.match(planText, /Java 后端/, planText);
  const prep = await page.evaluate(async id =>
    (await (await fetch(`/api/career-goals/${id}/interview-preparations/current`, {credentials: 'same-origin'})).json()), goalId);
  assert.equal(prep.role, 'java-backend');
  await page.screenshot({path: join(out, '01-java-plan.png'), fullPage: true});
  mark('Java 方向：准备与计划都是 java-backend', {planText, role: prep.role});

  await primary(page).click();
  const advanced = await scoped(goalId!, 'scripts/prep-web-support.py', 'advance');
  assert.equal(advanced.status, 'SUCCEEDED', JSON.stringify(advanced));
  await page.locator('[data-testid="interview-question"]').waitFor({timeout: 150000});

  const detail = await page.evaluate(async ([id, session]) =>
    (await (await fetch(`/api/career-goals/${id}/interviews/${session}`, {credentials: 'same-origin'})).json()),
    [goalId, advanced.session_id]);
  assert.equal(detail.policy.role, 'java-backend');
  const question = detail.events.find((event: {kind: string}) => event.kind === 'question');
  assert.ok(question, 'first_question_missing');
  assert.ok(JAVA_TOPICS.includes(question.payload.topic_id), `topic_not_in_java_pack ${question.payload.topic_id}`);
  assert.notEqual(question.payload.topic_id, 'agent-tools');
  await page.screenshot({path: join(out, '02-java-question.png'), fullPage: true});
  mark('首题来自 Java 角色题源，场次策略与题源一致', {
    session: advanced.session_id, role: detail.policy.role, pack: detail.pack_version,
    topic: question.payload.topic_id,
  });

  assert.deepEqual(pageErrors, [], JSON.stringify(pageErrors.slice(0, 2)));
  writeFileSync(join(out, 'result.json'), JSON.stringify({goalId, cases, pageErrors}, null, 2));
  console.log(JSON.stringify({status: 'passed', cases: cases.length, out}));
} catch (error) {
  writeFileSync(join(out, 'result.json'), JSON.stringify({cases, pageErrors, error: String(error)}, null, 2));
  await browser?.close();
  console.error('FAIL', error);
  process.exit(1);
} finally {
  await browser?.close();
}
