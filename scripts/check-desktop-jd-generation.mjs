// One packaged-desktop JD practice, with scripted defaults.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {createServer} from 'node:net';
import {join, resolve} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {chromium} from '@playwright/test';

const [candidateArg, outputArg, mode, seedArg] = process.argv.slice(2);
assert(candidateArg && outputArg && ['scripted', 'scripted-reference', 'scripted-wiki',
  'approved-cny-0.5', 'approved-reference-cny-0.5'].includes(mode),
  'pass candidate path, fresh private receipt path, and the intended mode');
const live = mode.startsWith('approved-');
const withReference = mode.includes('reference');
const withWiki = mode === 'scripted-wiki';
assert(!withWiki || seedArg, 'scripted-wiki requires a synthetic seed file');
const seed = withWiki ? JSON.parse(readFileSync(seedArg, 'utf8')) : null;
assert(!seed || /^f170-wiki-[a-f0-9]{32}@desktop\.example\.com$/.test(seed.email),
  'only the synthetic Wiki fixture is allowed');
const connector = '/api/runtime/connectors/preparation-reference';
const candidate = resolve(candidateArg);
const out = resolve(outputArg);
for (const file of ['api.json', 'worker.json']) {
  const config = JSON.parse(readFileSync(join(candidate, 'settings', file), 'utf8'));
  assert(config.mode === 'test' && config.database.dbname === 'py_interview_test',
    'the desktop candidate must use the dedicated test database');
}
mkdirSync(out);
const origin = 'http://127.0.0.1:3100';
const cdp = 'http://127.0.0.1:9368';
const identity = randomUUID();
const credentials = seed ? {email: seed.email, password: seed.password}
  : {email: `f168-${identity}@desktop.example.com`, password: `Synth-${randomUUID()}`};
const model = {provider: 'deepseek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash',
  api_key: '', prep_live_cap_cny: 0.2, live_cap_cny: 0.3, material_enabled: false};
const jd = withWiki ? '岗位职责：用 LangGraph 处理人工确认、中断与恢复。'
  : '岗位职责：用 Python 和 LangGraph 搭建多轮 Agent，负责会话检查点与恢复、写入工具的副作用控制、离线执行轨迹评测。';
const record = {mode, jd, steps: {}};
let child, browser, page, cookie, enabled, referenceEnabled;

function save(name, value) {
  record.steps[name] = value;
  writeFileSync(join(out, 'result.json'), JSON.stringify(record, null, 2), 'utf8');
  process.stdout.write(`${name}\n`);
}

async function api(method, path, body, status = 200) {
  const result = await fetch(origin + path, {
    method, signal: AbortSignal.timeout(12_000),
    headers: {Origin: origin, Cookie: cookie ?? '', 'Content-Type': 'application/json',
      'Idempotency-Key': randomUUID()},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await result.json();
  assert.equal(result.status, status, `${method} ${path}: ${data.code ?? result.status}`);
  const session = result.headers.getSetCookie().find(item => item.startsWith('py_interview_session='));
  if (session) cookie = session.split(';')[0];
  return data;
}

async function waitFor(get, done) {
  const until = Date.now() + 5 * 60_000;
  while (Date.now() < until) {
    const value = await get();
    if (done(value)) return value;
    await delay(500);
  }
  throw new Error('task_timeout');
}

async function launch() {
  for (const port of [3100, 9368]) {
    await new Promise((done, fail) => {
      const probe = createServer().once('error', fail);
      probe.listen(port, '127.0.0.1', () => probe.close(done));
    });
  }
  child = spawn(join(candidate, 'interview-desktop-probe.exe'), [], {cwd: candidate,
    env: {...process.env, INTERVIEW_DESKTOP_CDP_PORT: '9368',
      INTERVIEW_DESKTOP_CONFIG_DIR: join(candidate, 'settings'),
      INTERVIEW_DESKTOP_DATA_DIR: join(out, 'webview'),
      INTERVIEW_DESKTOP_RESOURCES: join(candidate, 'sidecar')}, windowsHide: true, stdio: 'ignore'});
  await waitFor(async () => {
    assert.equal(child.exitCode, null, 'desktop exited');
    try { return (await fetch(origin + '/health', {signal: AbortSignal.timeout(1000)})).ok; }
    catch { return false; }
  }, Boolean);
  browser = await chromium.connectOverCDP(cdp, {timeout: 15_000});
  [page] = browser.contexts()[0].pages();
  assert(page, 'desktop WebView not available');
}

async function closeDesktop() {
  if (page) await page.evaluate(() => globalThis.__TAURI__.core.invoke('quit_app')).catch(() => {});
  if (child) {
    for (let attempt = 0; child.exitCode === null && attempt < 24; attempt++) await delay(250);
    assert.notEqual(child.exitCode, null, 'desktop did not acknowledge quit');
  }
  if (browser) await browser.close().catch(() => {});
  child = browser = page = undefined;
}

async function costs() {
  const value = await api('GET', '/api/runtime/model-settings');
  return {
    preparation: {state: value.prep_state, spent: value.prep_spent_cny},
    interview: {state: value.live_state, spent: value.live_spent_cny},
  };
}

const latestQuestion = session => session.events.filter(event => event.kind === 'question').at(-1);
const pending = value => ['PENDING', 'RUNNING'].includes(value.status);
const prepTerminal = value => !pending(value);

try {
  await launch();
  const account = withWiki ? await api('POST', '/api/auth/sign-in/email', credentials)
    : await api('POST', '/api/auth/sign-up/email', {...credentials, name: 'F07Z168 synthetic'});
  save('account', account.user.id);
  const before = await api('GET', '/api/runtime/model-settings');
  assert(!before.enabled && !before.prep_enabled && !before.material_enabled, 'model switches must start off');
  if (withWiki) {
    const wikiPath = '/api/runtime/interview-wiki';
    const [draft] = (await api('GET', wikiPath)).items;
    assert.equal(draft.id, seed.version_id);
    assert.equal(draft.status, 'pending');
    await page.locator('[data-testid="nav-settings"]').waitFor({timeout: 30_000});
    await page.context().addCookies([{url: origin, name: 'py_interview_session',
      value: cookie.slice('py_interview_session='.length), httpOnly: true}]);
    await page.reload();
    await page.locator('[data-testid="nav-settings"]').click({timeout: 30_000});
    await page.locator('[data-testid="interview-wiki"] > summary').click();
    await page.getByRole('button', {name: '发布供下次练习'}).click({timeout: 30_000});
    await page.locator('[data-testid="wiki-entry-active"]').waitFor({timeout: 15_000});
    await page.screenshot({path: join(out, 'wiki-published.png')});
    save('wikiPublished', {firstSession: seed.first_session_id, version: draft.id});
    await closeDesktop();
    await launch();
    await api('POST', '/api/auth/sign-in/email', credentials);
    const [persisted] = (await api('GET', wikiPath)).items;
    assert.equal(persisted.id, draft.id);
    assert.equal(persisted.status, 'active');
    save('wikiAfterRestart', persisted.id);
  }
  if (withReference) {
    const setting = await api('PUT', connector, {enabled: true});
    referenceEnabled = setting.connectors.find(item => item.id === 'preparation-reference')?.enabled;
    assert(referenceEnabled, 'reference connector did not turn on');
  }
  if (live) {
    assert(before.key_masked, 'existing desktop key unavailable');
    const settings = await api('PUT', '/api/runtime/model-settings', {...model, enabled: true,
      prep_enabled: true, new_authorization: true, authorization_request_id: identity,
      prep_new_authorization: true, prep_authorization_request_id: identity});
    enabled = settings.enabled || settings.prep_enabled;
    assert(settings.enabled && settings.prep_enabled);
    assert.equal(settings.prep_live_cap_cny + settings.live_cap_cny, 0.5);
    save('authorizations', {preparation: settings.prep_live_authorization, interview: settings.live_authorization});
    await closeDesktop();
    await launch();
    await api('POST', '/api/auth/sign-in/email', credentials);
    const active = await api('GET', '/api/runtime/model-settings');
    assert(active.active && active.prep_enabled && !active.material_enabled);
  }

  const created = await api('POST', '/api/career-goals/', {
    message: '按这份 JD 做一场完整模拟。', request: 'save_jd', jd_text: jd,
    conditions: {direction: 'AI 应用开发'},
  }, 202);
  const root = `/api/career-goals/${created.id}`;
  save('goal', created.id);
  const goal = await waitFor(() => api('GET', root), value => !pending(value));
  const prep = await api('POST', root + '/interview-preparations',
    {expected_version: goal.version, profile: 'full', use_resume: false}, 202);
  const prepPath = root + '/interview-preparations/' + prep.id;
  const planned = await waitFor(() => api('GET', prepPath), prepTerminal);
  save('preparation', planned);
  if (live) save('costAfterPreparation', await costs());
  assert.equal(planned.status, 'WAITING_FOR_USER');
  assert(!('generated_questions' in planned.plan));
  assert.equal((await api('GET', root + '/interviews')).items.length, 0);
  if (live) {
    assert.equal(record.steps.costAfterPreparation.preparation.state, 'ACTIVE');
    assert(planned.plan.generated_question_count > 0, 'planner offered no accepted JD candidates');
    if (withReference) {
      assert.equal(planned.plan.public_reference_status, 'retrieved', 'official passage was not read');
      assert(planned.plan.public_reference_count > 0, 'no question cited a delivered passage');
    }
  } else {
    assert.equal(planned.plan.generated_question_count, 0);
  }
  if (withWiki) {
    assert.equal(planned.plan.wiki_question_count, 1);
    assert.equal(planned.plan.planner, 'wiki-reuse');
  }
  await api('POST', prepPath + '/confirm', {
    expected_version: planned.version, plan_sha256: planned.plan_sha256, decision: 'approve',
  });
  const opened = await waitFor(() => api('GET', prepPath), prepTerminal);
  save('opened', {status: opened.status, session: opened.session_id, error: opened.error_code});
  assert.equal(opened.status, 'SUCCEEDED');
  assert.equal((await api('GET', root + '/interviews')).items.length, 1);
  const sessionPath = root + '/interviews/' + opened.session_id;
  const first = await waitFor(() => api('GET', sessionPath), value => !pending(value));
  const question = latestQuestion(first);
  save('first', {status: first.status, run: first.run, question});
  assert.equal(first.status, 'WAITING_FOR_USER');
  assert(question);
  if (live) {
    save('costAfterFirst', await costs());
    assert.equal(record.steps.costAfterFirst.interview.state, 'ACTIVE');
    assert.equal(question.payload.source, 'jd_generated');
    assert.equal(question.payload.has_reference, false);
    assert(jd.includes(question.payload.generated.target_quote));
  }
  if (withWiki) {
    assert.equal(question.payload.source, 'wiki_reused');
    assert.equal(question.payload.wiki.id, seed.version_id);
    assert.equal(question.payload.text, seed.question);
  }

  const anchor = question.payload.generated?.target_quote || '这个问题';
  const firstAnswer = `我会围绕${anchor}写一个固定输入的场景，用真实存储或假工具注入失败，检查事件顺序和最终状态。对超时后的副作用和重复执行，还缺一个直接验证。`;
  await api('POST', sessionPath + '/answers',
    {expected_version: first.version, question_id: question.id, text: firstAnswer}, 202);
  const second = await waitFor(() => api('GET', sessionPath), value => !pending(value));
  const followup = latestQuestion(second);
  save('afterAnswer', {status: second.status, run: second.run, question: followup, answer: firstAnswer});
  if (live) save('costAfterAnswer', await costs());
  assert.equal(second.status, 'WAITING_FOR_USER');
  assert(followup?.id && followup.id !== question.id);
  const ended = await api('POST', sessionPath + '/stop', {expected_version: second.version});
  save('review', {status: ended.status, report: ended.report});
  assert.equal(ended.report.answers, 1);
  if (withWiki) {
    assert.equal(followup.payload.wiki.id, seed.version_id);
    assert.equal(followup.payload.parent_question_id, question.id);
    assert.equal(ended.report.wiki_questions, 2);
    assert.equal(ended.report.public_references.length, 1);
    assert.equal(ended.report.observations[0].label, 'inconclusive');
  }
  if (live) {
    assert.equal(record.steps.costAfterAnswer.interview.state, 'ACTIVE');
    assert.equal(ended.report.observations[0]?.label, 'inconclusive');
    const actualUse = ended.events.filter(event => event.kind === 'question' && event.payload.source === 'jd_generated');
    assert.equal(ended.report.generated_questions, actualUse.length);
    if (withReference) {
      assert(ended.report.public_references?.length, 'neither asked topic carried a frozen official passage');
    }
  }
  save('acceptance', 'protocol_passed');
} catch (error) {
  save('failure', error.message);
  process.exitCode = 1;
} finally {
  if (referenceEnabled) {
    try {
      const disabled = await api('PUT', connector, {enabled: false});
      save('referenceOff', !disabled.connectors.find(item => item.id === 'preparation-reference')?.enabled);
    } catch { save('referenceOff', false); }
  }
  if (enabled) {
    try { save('finalCost', await costs()); }
    catch { save('finalCost', 'read_failed'); }
    try {
      const disabled = await api('PUT', '/api/runtime/model-settings',
        {...model, enabled: false, prep_enabled: false});
      save('switchesOff', !disabled.enabled && !disabled.prep_enabled);
    } catch { save('switchesOff', false); }
  }
  await closeDesktop();
}
