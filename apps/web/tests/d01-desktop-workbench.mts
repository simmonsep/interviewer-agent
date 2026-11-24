// D-01-03 node B: desktop workbench acceptance in a real browser against the real
// local stack (Vite 5174 -> FastAPI 3100 -> PG/Redis + free scripted Worker).
// No page.route injection, no synthetic API, no model calls.
//
//   D01_OUTPUT=<new dir> node apps/web/tests/d01-desktop-workbench.mts
//
// Covered: explicit desktop entry + identity kept on refresh / navigation, web entry
// untouched, style scoping, workbench -> focus answering -> review, single polling and
// submit path (one InterviewPanel instance), draft restore, keyboard focus, 880x600
// first-screen reachability, history reopen, logout back to the desktop login.
import assert from 'node:assert/strict';
import {chromium,type Browser,type BrowserContext,type Page} from '@playwright/test';
import {openDetail} from './desktop-nav.mts';
import {readFileSync,writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/evidence/D-01-03/desktop'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const fixture=JSON.parse(readFileSync(join(root,'fixtures/question-banks/agent-developer-trial.json'),'utf8'));
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
// F07Z-142: superseded. This suite covers the pre-guest desktop flow (register -> sign in ->
// desktop auth card -> logout back to it). F07B replaced that with direct guest entry, so the
// selectors it starts from no longer exist; the current desktop entry is covered by
// d01-desktop-form.mts and workbench-next-action.mts. Set D01_LEGACY_AUTH=1 to run it anyway.
assert.ok(process.env.D01_LEGACY_AUTH, 'use d01-desktop-form.mts / workbench-next-action.mts');
mkdirSync(join(output,'shots'),{recursive:true});

const cases:{name:string;type:'real'|'mock';detail?:unknown}[]=[];
const mark=(name:string,detail?:unknown)=>cases.push({name,type:'real',detail});
const pageErrors:string[]=[];
const webErrors:string[]=[];
const account={email:`d0103-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID(),name:'桌面验收学员'};
const webAccount={email:`d0103-web-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID()};
const answer1='图里 state 是线程运行状态，checkpointer 按 thread_id 存检查点，store 放跨线程偏好。独立会话要用不同 thread_id；进程重启后继续需要持久化 checkpointer 与 store，内存实现不行。';
const answer2='interrupt 借助持久化检查点和 thread_id 暂停，用 Command(resume=...) 恢复；恢复会从含 interrupt 的节点开头重跑，副作用要幂等或移到单独节点，写入放在用户确认之后并用业务幂等键防重复。';

let browser:Browser;
async function newContext():Promise<BrowserContext>{
 return browser.newContext({viewport:{width:1280,height:860}});
}
function watch(page:Page,sink:string[]){
 page.on('pageerror',error=>sink.push(error.message));
}
async function shot(page:Page,name:string){
 await page.screenshot({path:join(output,'shots',name)});
}
async function counts(page:Page,needle:string):Promise<number>{
 return page.evaluate(text=>{
  const body=document.body.innerText;
  return body.split(text).length-1;
 },needle);
}
async function box(page:Page,selector:string):Promise<{x:number;y:number;w:number;h:number}>{
 const value=await page.locator(selector).first().evaluate((el:Element)=>{
  const rect=el.getBoundingClientRect();
  return {x:rect.x,y:rect.y,w:rect.width,h:rect.height};
 });
 assert.ok(value.w>0&&value.h>0,`${selector}_not_rendered`);
 return value;
}
async function noHorizontalOverflow(page:Page,label:string){
 const value=await page.evaluate(()=>({inner:window.innerWidth,doc:document.documentElement.scrollWidth,body:document.body.scrollWidth}));
 assert.ok(value.doc<=value.inner+2,`${label}_doc_overflow ${JSON.stringify(value)}`);
 assert.ok(value.body<=value.inner+2,`${label}_body_overflow ${JSON.stringify(value)}`);
 return value;
}
async function detail(page:Page,goalId:string,sessionId:string):Promise<any>{
 const result=await page.evaluate(async(url)=>{
  const response=await fetch(url,{credentials:'same-origin',cache:'no-store'});
  return {status:response.status,body:await response.json()};
 },`/api/career-goals/${goalId}/interviews/${sessionId}`);
 assert.equal(result.status,200,`detail ${result.status} ${JSON.stringify(result.body)}`);
 return result.body;
}
async function importBank(page:Page):Promise<{bank_id:string;version:number}>{
 return page.evaluate(async(document)=>{
  const previewResponse=await fetch('/api/question-banks/imports',{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({document})});
  const preview=await previewResponse.json();
  if(!previewResponse.ok)throw new Error('preview_failed '+JSON.stringify(preview));
  const commitResponse=await fetch(`/api/question-banks/imports/${preview.preview_id}/commit`,{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({})});
  const commit=await commitResponse.json();
  if(!commitResponse.ok)throw new Error('commit_failed '+JSON.stringify(commit));
  return {bank_id:commit.bank_id,version:commit.version};
 },fixture);
}
async function register(page:Page){
 await page.getByRole('button',{name:'第一次来？创建账号'}).click();
 await page.locator('input[name="name"]').fill(account.name);
 await page.locator('input[name="email"]').fill(account.email);
 await page.locator('input[name="password"]').fill(account.password);
 await page.locator('.desktop-auth-card form .primary').click();
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
}

browser=await chromium.launch({executablePath:executable,headless:true});
const evidence:any={kind:'D-01-03 node B 桌面工作台验收',timestamp:new Date().toISOString(),
 base_url:base,shell_entry:base+'/?shell=desktop',
 api_mode:'real-local-stack: Vite 5174 proxy -> FastAPI 3100 -> PG/Redis + free scripted Worker',
 case_type_note:'全部用例为真实本地服务联调，未使用 page.route 注入或合成 API，未启用真实模型调用。',
 account:{email:account.email,name:account.name},cases,page_errors:pageErrors,web_entry_page_errors:webErrors,screenshots:[]};

try{
 // ---------------------------------------------------------------- 1. web entry stays as it was
 {
  const context=await newContext();
  const page=await context.newPage();
  watch(page,webErrors);
  await page.goto(base,{waitUntil:'domcontentloaded'});
  await page.locator('.auth-layout').waitFor({timeout:30000});
  assert.equal(await page.locator('.desktop-shell').count(),0,'desktop_shell_leaked_into_web_entry');
  assert.equal(await page.locator('.desktop-rail').count(),0);
  assert.equal(await page.locator('.desktop-context').count(),0);
  assert.equal(await page.locator('.site-header').count(),1,'web_header_missing');
  await shot(page,'00-web-entry.png');
  mark('普通浏览器打开原地址：仍是 Web 入口（站点页头 + auth-layout），桌面外壳与桌面根节点样式不出现',
   {desktop_nodes:await page.locator('.desktop-shell,.desktop-rail,.desktop-context').count()});
  // Web entry smoke: it must still register, reach the career workspace and keep its own layout.
  await page.getByRole('button',{name:'第一次来？创建账号'}).click();
  await page.locator('input[name="name"]').fill('Web 入口学员');
  await page.locator('input[name="email"]').fill(webAccount.email);
  await page.locator('input[name="password"]').fill(webAccount.password);
  await page.getByRole('button',{name:'创建账号',exact:true}).click();
  await page.getByRole('button',{name:'求职目标'}).waitFor({timeout:30000});
  await page.getByRole('button',{name:'求职目标'}).click();
  await page.locator('.career-layout').waitFor({timeout:30000});
  assert.equal(await page.locator('.desktop-shell').count(),0);
  assert.equal(await page.locator('.desktop-context').count(),0);
  mark('Web 入口主链未受影响：注册后仍进入原有求职目标页（career-layout），不渲染桌面外壳', {});
  await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
  await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
  assert.equal(await page.locator('.career-layout').count(),0,'web_layout_leaked_into_desktop_entry');
  assert.equal(await page.locator('.site-header').count(),0,'web_header_leaked_into_desktop_entry');
  assert.equal(await page.locator('.desktop-shell[data-pane]').count(),1);
  await shot(page,'01-web-to-desktop.png');
  mark('已登录会话切换到 ?shell=desktop：同一账号直接进入桌面外壳，Web 版页头与侧栏不出现',{});
  await context.close();
 }

 const context=await newContext();
 const page=await context.newPage();
 watch(page,pageErrors);

 // ---------------------------------------------------------------- 2. login + entry identity
 // F05B 免登直入后，登录页改为“一次性标志”显式到达（本套件要测登录页本身）。
 await page.goto(base,{waitUntil:'domcontentloaded'});
 await page.evaluate(()=>{try{localStorage.setItem('py-desktop-login','1');}catch{}});
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-auth-card form').waitFor({timeout:30000});
 await shot(page,'02-desktop-login.png');
 await page.locator('input[name="email"]').fill('nobody-'+randomUUID()+'@example.com');
 await page.locator('input[name="password"]').fill('wrong-'+randomUUID());
 await page.locator('.desktop-auth-card form .primary').click();
 await page.locator('.desktop-auth-card form [role="alert"]').waitFor({timeout:30000});
 mark('桌面登录页的失败提示走现有鉴权错误通道（role=alert），不静默失败',{text:(await page.locator('.desktop-auth-card form [role="alert"]').innerText()).slice(0,80)});
 await register(page);
 await page.locator('[data-testid="desktop-goal"]').waitFor({timeout:30000});
 mark('桌面入口注册后直接进入工作台（同一份 AuthPanel 逻辑，仅展示不同）',{});

 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 assert.equal(await page.locator('.career-layout').count(),0);
 mark('刷新后仍停在桌面外壳（入口身份保存在会话内，不依赖地址栏参数）',{url:page.url()});

 await page.goto(base,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 mark('同一会话去掉 ?shell 直接访问根地址：仍是桌面外壳（入口身份保持）',{url:page.url()});

 await page.goto(`${base}/?shell=web`,{waitUntil:'domcontentloaded'});
 await page.locator('.site-header').waitFor({timeout:30000});
 assert.equal(await page.locator('.desktop-shell').count(),0,'shell_identity_not_switched_back_to_web');
 await page.getByRole('button',{name:'求职目标'}).click();
 await page.locator('.career-layout').waitFor({timeout:30000});
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.site-header').waitFor({timeout:30000});
 assert.equal(await page.locator('.desktop-shell').count(),0,'reload_lost_web_identity');
 mark('显式 ?shell=web 时回到 Web 入口（站点页头 + 原求职目标页），刷新后身份同样保持',{url:page.url()});

 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 await shot(page,'03-workbench-empty.png');

 // ---------------------------------------------------------------- 3. workbench: goal + primary action
 assert.equal(await page.locator('[data-testid="workbench-primary"]').count(),1,'primary_action_duplicated');
 assert.equal(await page.locator('[data-testid="desktop-goal"]').innerText(),'尚未创建求职目标');
 mark('工作台首屏只有一个主操作（未创建目标时是创建目标的表单提交）',{label:await page.locator('[data-testid="workbench-primary"]').innerText()});
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在上海找 AI 应用 / Agent 开发实习。');
 await page.locator('[data-testid="workbench-primary"]').click();
 await page.locator('[data-testid="desktop-goal"]').filter({hasNotText:'尚未创建'}).waitFor({timeout:60000});
 await page.getByTestId('workbench-goal').waitFor({timeout:30000});
 mark('填写意向并提交后目标保存成功，上下文栏与目标卡同步（复用现有目标状态机与幂等命令）',
  {goal:await page.locator('[data-testid="desktop-goal"]').innerText()});
 await shot(page,'04-workbench-goal.png');
 assert.equal(await page.getByTestId('workbench-primary').innerText(),'开始面试');
 await page.getByTestId('workbench-primary').click();
 await page.locator('#interview-bank').waitFor({timeout:30000});
 assert.equal(await page.locator('[data-testid="desktop-page"]').innerText(),'面试训练');
 mark('主操作跳转到面试训练页，上下文栏页面名同步',{line:await page.locator('[data-testid="desktop-session"]').innerText()});
 await shot(page,'05-interview-setup.png');

 // ---------------------------------------------------------------- 4. focus answering (single polling/submit path)
 const bank=await importBank(page);
 await page.getByRole('button',{name:'刷新题库'}).click();
 await page.locator('#interview-bank option',{hasText:'Agent开发岗位试运行'}).waitFor({state:'attached',timeout:30000});
 await page.getByLabel('题库与版本').selectOption(`${bank.bank_id}|${bank.version}`);
 await page.getByRole('button',{name:'开始整场面试 →'}).click();
 await page.getByTestId('interview-question').waitFor({timeout:90000});
 await page.locator('.desktop-shell[data-answering="true"]').waitFor({timeout:30000});
 await delay(600);
 assert.equal(await page.locator('#interview-workspace').count(),1,'interview_workspace_duplicated');
 assert.equal(await page.locator('#interview-answer').count(),1,'answer_form_duplicated');
 assert.equal(await page.locator('[data-testid="answer-submit"]').count(),1,'submit_duplicated');
 assert.equal(await page.locator('[data-testid="interview-question"]').count(),1,'question_duplicated');
 const line1=await page.locator('[data-testid="desktop-session"]').innerText();
 assert.match(line1,/第 1\/2 题/);
 assert.match(line1,/可作答/);
 assert.equal(await counts(page,'剩余'),1,'countdown_shown_more_than_once');
 assert.equal(await counts(page,'可作答'),1,'status_shown_more_than_once');
 assert.equal(await counts(page,'场次'),1,'session_shown_more_than_once');
 mark('专注答题：同一场次只有一份面试面板、一个作答框、一条进度行（进度/倒计时/状态各出现一次）',
  {line:line1,workspace:1,answer_forms:1,submits:1});
 const sessionId=await page.locator('[data-session-id]').getAttribute('data-session-id');
 assert.ok(sessionId,'session_id_missing');
 const goalId=await page.evaluate(async()=>{
  const response=await fetch('/api/career-goals/',{credentials:'same-origin',cache:'no-store'});
  const body=await response.json() as {items:string[]};
  return body.items[0]??null;
 });
 assert.ok(goalId,'goal_id_missing');
 const initial=await detail(page,goalId!,sessionId!);
 assert.equal(initial.status,'WAITING_FOR_USER');
 assert.equal(initial.policy.max_questions,2);
 mark('开场成功：首题、WAITING_FOR_USER、2 题上限、题库版本冻结',{session_id:sessionId,version:initial.version});

 await shot(page,'06-answering.png');

 // ---------------------------------------------------------------- 5. keyboard focus + IME-ish input + draft
 await page.locator('#interview-answer').fill(answer1);
 await page.locator('#interview-answer').focus();
 const focused=await page.evaluate(()=>document.activeElement?.id);
 assert.equal(focused,'interview-answer','textarea_not_focusable');
 await page.keyboard.press('Tab');
 const afterTab=await page.evaluate(()=>{
  const el=document.activeElement as HTMLElement|null;
  return {testid:el?.getAttribute('data-testid')??'',tag:el?.tagName??''};
 });
 assert.equal(afterTab.testid,'answer-submit',`tab_from_answer_goes_to_${JSON.stringify(afterTab)}`);
 await page.keyboard.press('Shift+Tab');
 assert.equal(await page.evaluate(()=>document.activeElement?.id),'interview-answer');
 mark('键盘：作答框可获得焦点，Tab 直达提交按钮，Shift+Tab 返回',{after_tab:afterTab});

 const draft=await page.evaluate(()=>({saved:Object.keys(sessionStorage).filter(key=>key.includes(':draft:')),
  value:Object.keys(sessionStorage).filter(key=>key.includes(':draft:')).map(key=>sessionStorage.getItem(key))}));
 assert.equal(draft.saved.length,1,'draft_key_missing');
 assert.ok((draft.value[0]??'').includes('checkpointer'),'draft_value_not_written');
 mark('中文输入写入草稿（用户+场次+题目事件维度），提交前刷新不会丢',{keys:draft.saved});

 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:30000});
 assert.equal(await page.locator('[data-testid="desktop-page"]').innerText(),'工作台');
 assert.equal(await page.locator('[data-testid="desktop-session"]').count(),0,'context_line_leaked_into_workbench');
 await page.locator('.d-session-item').first().waitFor({timeout:30000});
 mark('刷新后外壳身份保持并回到工作台，场次列表由现有 currentSession / history 逻辑回读',
  {sessions:await page.locator('.d-session-item').count()});
 await openDetail(page,'面试训练');
 await page.getByTestId('interview-question').waitFor({timeout:60000});
 await page.locator('.desktop-shell[data-answering="true"]').waitFor({timeout:30000});
 assert.equal(await page.locator('#interview-answer').inputValue(),answer1,'draft_not_restored');
 mark('切回面试训练：同一场次原题恢复，草稿自动回填',{});

 // ---------------------------------------------------------------- 6. narrow window first screen
 await page.setViewportSize({width:880,height:600});
 await delay(500);
 const overflow=await noHorizontalOverflow(page,'880x600_answering');
 const questionBox=await box(page,'[data-testid="interview-question"]');
 const answerBox=await box(page,'#interview-answer');
 const submitBox=await box(page,'[data-testid="answer-submit"]');
 const viewport=await page.evaluate(()=>({w:window.innerWidth,h:window.innerHeight}));
 assert.ok(questionBox.y>=0&&questionBox.y+questionBox.h<=viewport.h,`question_not_in_first_screen ${JSON.stringify(questionBox)}`);
 assert.ok(answerBox.y+answerBox.h<=viewport.h,`answer_outside_first_screen ${JSON.stringify(answerBox)}`);
 assert.ok(submitBox.y+submitBox.h<=viewport.h,`submit_outside_first_screen ${JSON.stringify(submitBox)}`);
 mark('880×600：题干、作答框、提交按钮均在首屏内，无横向溢出（题目区独立滚动）',
  {viewport,question:questionBox,answer:answerBox,submit:submitBox,overflow});
 await shot(page,'07-answering-880x600.png');
 await page.setViewportSize({width:1280,height:860});
 await delay(300);

 // ---------------------------------------------------------------- 7. submit -> second question -> review
 await page.locator('[data-testid="answer-submit"]').click();
 await page.waitForFunction(()=>{
  const el=document.querySelector('[data-testid="desktop-session"]');
  return !!el&&/第 2\/2/.test(el.textContent??'');
 },{timeout:90000});
 await delay(400);
 const line2=await page.locator('[data-testid="desktop-session"]').innerText();
 assert.match(line2,/第 2\/2 题/);
 await shot(page,'08-second-question.png');
 mark('提交第一答后进度行推进到第 2/2 题，题目与作答框仍是同一套',{line:line2});

 await page.locator('#interview-answer').fill(answer2);
 await page.locator('[data-testid="answer-submit"]').click();
 await page.locator('[data-testid="interview-report"]').waitFor({timeout:90000});
 await delay(500);
 const final=await detail(page,goalId!,sessionId!);
 assert.equal(final.status,'COMPLETED');
 assert.equal(final.events.filter((e:any)=>e.kind==='answer').length,2,'duplicate_answer_written');
 assert.equal(final.events.filter((e:any)=>e.kind==='question').length,2);
 assert.equal(final.report.questions,2);
 assert.equal(final.report.answers,2);
 assert.equal(final.report.stop_reason,'question_limit');
 const line3=await page.locator('[data-testid="desktop-session"]').innerText();
 assert.match(line3,/第 2\/2 题 · 已结束 · 场次/);
 assert.equal((line3.match(/已结束/g)??[]).length,1,'terminal_state_repeated_in_context_line');
 const contextText=await page.locator('.desktop-context').innerText();
 assert.equal((contextText.match(/场次/g)??[]).length,1,'session_identity_repeated_in_context_bar');
 mark('第二答后进入复盘：服务端 2 题 / 2 答 / question_limit，答案未重复写入（一套提交逻辑）',
  {line:line3,answers:final.report.answers,questions:final.report.questions,stop:final.report.stop_reason});
 assert.equal(await page.locator('[data-testid="interview-report"]').count(),1);
 await shot(page,'09-report.png');
 await page.setViewportSize({width:880,height:600});
 await delay(400);
 const reportOverflow=await noHorizontalOverflow(page,'880x600_report');
 await shot(page,'10-report-880x600.png');
 mark('复盘在 880×600 下同样无横向溢出，报告与观察可读',{overflow:reportOverflow});
 await page.setViewportSize({width:1280,height:860});
 await delay(300);

 // ---------------------------------------------------------------- 8. history reopen from workbench
 await openDetail(page,'工作台');
 await page.locator('.d-session-item').first().waitFor({timeout:30000});
 const itemText=await page.locator('.d-session-item').first().innerText();
 assert.match(itemText,/已结束/);
 await shot(page,'11-workbench-history.png');
 await page.locator('.d-session-item').first().click();
 await page.getByTestId('interview-report').waitFor({timeout:60000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),sessionId,'history_open_changed_session');
 mark('从工作台历史场次打开原场次：同一 session_id、原报告回读（不新建场次）',{item:itemText.replace(/\n/g,' / ')});
 await shot(page,'12-history-opened.png');

 // ---------------------------------------------------------------- 8b. dark appearance on the same real data
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('[data-testid="settings-pane"]').waitFor({timeout:15000});
 await page.locator('[data-testid="theme-dark"]').click();
 await page.locator('.desktop-shell[data-theme="dark"]').waitFor({timeout:10000});
 await page.locator('.d-nav-item').first().click();
 await page.locator('.desktop-shell[data-pane="workbench"]').waitFor({timeout:15000});
 assert.equal(await page.locator('.d-session-item').count()>0,true,'dark_workbench_lost_sessions');
 await shot(page,'12b-dark-workbench.png');
 mark('深色下的工作台：目标、场次列表、折叠区与状态栏复用同一份真实数据，只换配色',{sessions:await page.locator('.d-session-item').count()});
 await page.getByRole('button',{name:'打开面试训练'}).click();
 await page.locator('[data-testid="interview-report"]').waitFor({timeout:30000});
 const darkReport=await page.evaluate(()=>{
  const shell=document.querySelector('.desktop-shell') as HTMLElement;
  const grid=document.querySelector('.interview-report-grid') as HTMLElement|null;
  const cells=grid?[...grid.children].map(child=>({w:Math.round(child.getBoundingClientRect().width),h:Math.round(child.getBoundingClientRect().height)})):[];
  return {theme:shell.dataset.theme,bg:getComputedStyle(shell).backgroundColor,cells};
 });
 assert.equal(darkReport.theme,'dark');
 assert.equal(new Set(darkReport.cells.map(cell=>cell.h)).size,1,`report_cells_uneven ${JSON.stringify(darkReport.cells)}`);
 await shot(page,'12c-dark-report.png');
 mark('深色下的复盘：报告四格等高，不因长文案折行而错位',darkReport);
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('[data-testid="theme-light"]').click();
 await page.waitForTimeout(200);

 // ---------------------------------------------------------------- 9. logout returns to the desktop login
 await page.locator('.d-account>summary').click();
 await page.getByRole('button',{name:'退出登录'}).click();
 await page.locator('.desktop-shell.is-login').waitFor({timeout:30000});
 mark('退出登录回到桌面登录页，外壳身份不变',{url:page.url()});
 await shot(page,'13-logged-out.png');

 assert.deepEqual(pageErrors,[],'page_errors_present');
 await context.close();
}catch(error){
 evidence.failure={message:(error as Error).message};
 evidence.cases=cases;
 const pages=browser.contexts().flatMap(item=>item.pages());
 if(pages[0]){
  evidence.failure.url=pages[0].url();
  evidence.failure.shell=await pages[0].evaluate(()=>({
   shell:document.querySelector('.desktop-shell')?.className??null,
   has_career:!!document.querySelector('.career-layout'),
   has_auth:!!document.querySelector('.auth-layout'),
   header:document.querySelector('.site-header')?.textContent?.slice(0,60)??null,
   body:document.body.innerText.slice(0,300)
  })).catch(()=>({}));
 }
 writeFileSync(join(output,'result.json'),JSON.stringify(evidence,null,2));
 await browser.close();
 console.error('FAILED:',(error as Error).message,JSON.stringify(evidence.failure,null,2));
 process.exit(1);
}

evidence.cases=cases;
evidence.screenshots=(await import('node:fs')).readdirSync(join(output,'shots')).sort();
evidence.summary={total:cases.length,real:cases.filter(item=>item.type==='real').length,mock:0};
writeFileSync(join(output,'result.json'),JSON.stringify(evidence,null,2));
await browser.close();
console.log(`PASS ${cases.length}/${cases.length}`);
for(const item of cases)console.log(' ✓',item.name);
console.log('evidence:',join(output,'result.json'));
