// D-01-03 node B native acceptance. Drives the REAL Tauri window (WebView2) over the
// WebView2 DevTools protocol; the page talks over HTTP to the real local stack:
// Vite (5174) -> FastAPI (3100) -> PG/Redis + free scripted Worker. No page.route
// injection, no synthetic API, no model calls.
//
//   D01_OUTPUT=<new dir> node apps/web/tests/d01-desktop-native.mts
//
// Requires: a host built from the current main.rs (PREVIEW_URL .../?shell=desktop),
// running 3100/5174 services, and a free CDP port (default 9223).
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {openDetail, waitGoalReady} from './desktop-nav.mts';
import {execFileSync,spawn,type ChildProcess} from 'node:child_process';
import {readFileSync,writeFileSync,mkdirSync,existsSync,openSync,closeSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/evidence/D-01-03/native'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const hostExe=resolve(process.env.D01_HOST_EXE??join(root,'.private/d-01-02/tauri-target/debug/interview-desktop-probe.exe'));
const cdpPort=Number(process.env.D01_CDP_PORT??9223);
const winScript=join(root,'scripts/d01-desktop-window.ps1');
const fixture=JSON.parse(readFileSync(join(root,'fixtures/question-banks/agent-developer-trial.json'),'utf8'));
mkdirSync(output,{recursive:true});
const logPath=join(output,'native-host.log');
const runDir=join(output,'native-run');
mkdirSync(runDir,{recursive:true});

const cases:{name:string;type:'native'|'check';detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,type:'native'|'check'='native',detail?:unknown)=>cases.push({name,type,detail});
const account={email:`d0103-native-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID(),name:'桌面原生学员'};
const answer1='图里 state 是线程运行状态，checkpointer 按 thread_id 存检查点，store 放跨线程偏好。独立会话要用不同 thread_id；进程重启后继续需要持久化 checkpointer 与 store。';
const answer2='interrupt 借助持久化检查点和 thread_id 暂停，用 Command(resume=...) 恢复；恢复会从含 interrupt 的节点开头重跑，副作用要幂等或移到单独节点，写入放在用户确认之后并用业务幂等键防重复。';

function win(action:'Info'|'Capture'|'Resize',pid:number,args:string[]=[]):any{
 const stdout=execFileSync('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',winScript,
  '-Action',action,'-ProcessId',String(pid),...args],{encoding:'utf8',timeout:45000,windowsHide:true});
 const text=stdout.trim();
 const start=text.indexOf('{');
 if(start<0)throw new Error('window_helper_no_json: '+text.slice(0,300));
 return JSON.parse(text.slice(start));
}
async function preflight(){
 const api=await fetch('http://127.0.0.1:3100/health');
 assert.equal(api.status,200,'api_not_ready');
 const vite=await fetch(base+'/');
 assert.equal(vite.status,200,'vite_not_ready');
 return {api:await api.text(),vite_status:vite.status};
}
function launchHost():ChildProcess{
 const env={...process.env,INTERVIEW_DESKTOP_PREVIEW:'1',INTERVIEW_PROBE_OUTPUT:runDir,
  INTERVIEW_DESKTOP_PREVIEW_CDP_PORT:String(cdpPort)};
 delete (env as any).INTERVIEW_PROBE_AUTORUN;
 const fd=openSync(logPath,'a');
 const child=spawn(hostExe,[],{cwd:runDir,env,stdio:['ignore',fd,fd],windowsHide:false});
 child.on('exit',(code,signal)=>{try{closeSync(fd);}catch{}console.log(`[host pid=${child.pid}] exit=${code} signal=${signal}`);});
 return child;
}
async function waitForCdp(timeout=60000){
 const started=Date.now();
 while(Date.now()-started<timeout){
  try{const r=await fetch(`http://127.0.0.1:${cdpPort}/json/version`);if(r.ok)return await r.json();}catch{}
  await delay(400);
 }
 throw new Error('webview_cdp_not_ready');
}
async function attach():Promise<{browser:Browser;page:Page}>{
 const browser=await chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`,{timeout:40000});
 for(let i=0;i<60;i++){
  for(const ctx of browser.contexts())for(const p of ctx.pages()){
   if(p.url().includes('127.0.0.1:5174'))return {browser,page:p};
  }
  await delay(400);
 }
 const seen=browser.contexts().flatMap(c=>c.pages().map(p=>p.url()));
 throw new Error('webview_page_not_found: '+JSON.stringify(seen));
}
async function getDetail(p:Page,goalId:string,sessionId:string):Promise<any>{
 const result=await p.evaluate(async(url)=>{
  const response=await fetch(url,{credentials:'same-origin',cache:'no-store'});
  return {status:response.status,body:await response.json()};
 },`/api/career-goals/${goalId}/interviews/${sessionId}`);
 assert.equal(result.status,200,`detail_${result.status}_${JSON.stringify(result.body)}`);
 return result.body;
}
async function importBank(p:Page):Promise<{bank_id:string;version:number}>{
 return p.evaluate(async(document)=>{
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
async function layout(p:Page,label:string){
 const value=await p.evaluate(()=>({inner_width:window.innerWidth,inner_height:window.innerHeight,
  document_scroll_width:document.documentElement.scrollWidth,body_scroll_width:document.body.scrollWidth,
  device_pixel_ratio:window.devicePixelRatio}));
 assert.ok(value.document_scroll_width<=value.inner_width+2,`${label}_horizontal_overflow_${JSON.stringify(value)}`);
 assert.ok(value.body_scroll_width<=value.inner_width+2,`${label}_body_horizontal_overflow_${JSON.stringify(value)}`);
 return value;
}
async function rect(p:Page,selector:string){
 const value=await p.locator(selector).first().evaluate((el:Element)=>{
  const r=el.getBoundingClientRect();
  return {x:r.x,y:r.y,w:r.width,h:r.height,bottom:r.bottom};
 });
 assert.ok(value.w>0&&value.h>0,`${selector}_not_rendered`);
 return value;
}
// The desktop runs at 150% scaling, so a Win32 window of W×H gives a CSS viewport of
// roughly W/1.5 × (H-titlebar)/1.5. Drive the native window until the CSS viewport
// actually matches the size we want to evidence, and report the achieved numbers.
async function resizeToCss(p:Page,child:ChildProcess,cssW:number,cssH:number){
 const dpr=await p.evaluate(()=>window.devicePixelRatio);
 let w=Math.round(cssW*dpr),h=Math.round(cssH*dpr),achieved={w:0,h:0};
 for(let attempt=0;attempt<5;attempt++){
  win('Resize',child.pid!,['-Width',String(w),'-Height',String(h)]);
  await delay(500);
  achieved=await p.evaluate(()=>({w:window.innerWidth,h:window.innerHeight}));
  if(Math.abs(achieved.w-cssW)<=8&&Math.abs(achieved.h-cssH)<=8)return {dpr,window:{w,h},css:achieved};
  w+=Math.round((cssW-achieved.w)*dpr);
  h+=Math.round((cssH-achieved.h)*dpr);
 }
 throw new Error(`resize_to_css_failed css=${JSON.stringify(achieved)} target=${cssW}x${cssH}`);
}
async function inViewport(p:Page,label:string,selectors:[string,string][]){
 const vp=await p.evaluate(()=>({w:window.innerWidth,h:window.innerHeight}));
 const boxes:Record<string,unknown>={};
 for(const [selector,name] of selectors){
  const value=await rect(p,selector);
  assert.ok(value.y>=-2&&value.bottom<=vp.h+2,`${label}_${name}_outside_viewport_${JSON.stringify(value)}_vp=${vp.h}`);
  assert.ok(value.x>=-2&&value.x+value.w<=vp.w+2,`${label}_${name}_outside_viewport_x`);
  boxes[name]=value;
 }
 return {viewport:vp,boxes};
}

const evidence:any={kind:'D-01-03 node B 原生窗口验收',timestamp:new Date().toISOString(),
 base_url:base,host_exe:hostExe,cdp_port:cdpPort,
 api_mode:'real-local-stack: Vite 5174 proxy -> FastAPI 3100 -> PG/Redis + free scripted Worker',
 case_type_note:'所有用例都在真实 Tauri WebView2 原生窗口内完成，未使用 page.route 注入或合成 API；仅为验收启用 WebView2 远程调试端口。',
 account:{email:account.email,name:account.name},cases,page_errors:pageErrors,screenshots:[]};
const children:ChildProcess[]=[];
let attached:Browser|null=null;
try{
 assert.ok(existsSync(hostExe),'built_host_missing');
 evidence.preflight=await preflight();
 mark('构建前确认 3100 测试 API 与 5174 Vite 均在运行','check',evidence.preflight);

 // ---------------- window 1 ----------------
 let child=launchHost();children.push(child);
 const cdp=await waitForCdp();
 evidence.cdp={browser:cdp.Browser,protocol:cdp['Protocol-Version']};
 const first=await attach();attached=first.browser;
 const page=first.page;
 page.on('pageerror',error=>pageErrors.push(error.message));
 const info1=win('Info',child.pid!);
 evidence.native_window=info1;
 assert.match(info1.process,/interview-desktop-probe/i,'not_our_host_process');
 assert.match(info1.title,/Tauri/,'native_window_title_missing');
 assert.match(info1.title,/桌面工作台/,'desktop_workbench_title_missing');
 await page.locator('.desktop-shell').waitFor({timeout:40000});
 evidence.entry_url=page.url();
 // The host opens ?shell=desktop once; the shell then keeps the identity in sessionStorage
 // and may rewrite the URL when it reopens the last goal, so only the rendered shell matters.
 const shellIdentity=await page.evaluate(()=>({stored:sessionStorage.getItem('py-shell'),shell:!!document.querySelector('.desktop-shell')}));
 assert.equal(shellIdentity.shell,true,'native_shell_missing');
 assert.equal(shellIdentity.stored,'desktop','native_shell_identity_not_kept');
 mark('原生窗口按显式桌面入口打开：进程名、窗口标题与 ?shell=desktop 均可区分普通浏览器','native',
  {window:info1,url:page.url()});
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'00-native-login.png')]));

 // WebView2 keeps the previous run's cookie profile. Always start from the desktop
 // login page so each acceptance run creates its own synthetic account.
 if(await page.locator('.desktop-shell[data-pane]').count()>0){
  await page.locator('.d-account>summary').click();
  await page.getByRole('button',{name:'退出登录'}).click();
  await page.locator('.desktop-shell.is-login').waitFor({timeout:40000});
  mark('上一次运行留下的登录态已退出，回到桌面登录页','check');
 }
 await page.getByRole('button',{name:'第一次来？创建账号'}).click();
 await page.locator('input[name="name"]').fill(account.name);
 await page.locator('input[name="email"]').fill(account.email);
 await page.locator('input[name="password"]').fill(account.password);
 await page.locator('.desktop-auth-card form .primary').click();
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:40000});
 assert.equal(await page.locator('.career-layout').count(),0,'web_layout_in_native_window');
 mark('原生窗口内注册并进入桌面工作台（与原 Web 入口共用同一鉴权逻辑）','native');

 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在上海找 AI 应用 / Agent 开发实习。');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);
 const goalId=await page.evaluate(async()=>{
  const response=await fetch('/api/career-goals/',{credentials:'same-origin',cache:'no-store'});
  const body=await response.json() as {items:string[]};
  return body.items[0]??null;
 });
 assert.ok(goalId,'goal_id_missing');
 evidence.goal_id=goalId;
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'01-native-workbench.png')]));
 mark('原生工作台：填写意向 → 保存目标 → 上下文栏同步','native',{goal_id:goalId});

 const bank=await importBank(page);
 evidence.bank=bank;
 await page.getByTestId('workbench-primary').click();
 await page.locator('#interview-bank').waitFor({timeout:40000});
 await page.getByRole('button',{name:'刷新题库'}).click();
 await page.locator('#interview-bank option',{hasText:'Agent开发岗位试运行'}).waitFor({state:'attached',timeout:40000});
 await page.getByLabel('题库与版本').selectOption(`${bank.bank_id}|${bank.version}`);
 await page.getByRole('button',{name:'开始整场面试 →'}).click();
 await page.locator('[data-session-id]').waitFor({timeout:60000});
 await page.getByTestId('interview-question').waitFor({timeout:90000});
 await page.locator('.desktop-shell[data-answering="true"]').waitFor({timeout:40000});
 await delay(600);
 const sessionId=await page.locator('[data-session-id]').getAttribute('data-session-id');
 assert.ok(sessionId,'session_id_missing');
 evidence.session_id=sessionId;
 const initial=await getDetail(page,goalId!,sessionId!);
 assert.equal(initial.status,'WAITING_FOR_USER');
 assert.equal(initial.policy.max_questions,2);
 const line1=await page.locator('[data-testid="desktop-session"]').innerText();
 assert.match(line1,/第 1\/2 题/);
 mark('原生窗口开场成功：首题 / WAITING_FOR_USER / 2 题上限 / 冻结私有题库 v1','native',
  {session_id:sessionId,line:line1,bank_version:initial.question_banks[0].version});

 // 1280x860 CSS viewport in a real native window
 const wideResize=await resizeToCss(page,child,1280,860);
 const wide=await inViewport(page,'1280x860',[['[data-testid="interview-question"]','question'],['#interview-answer','answer'],['[data-testid="answer-submit"]','submit'],['.desktop-context','context']]);
 const wideLayout=await layout(page,'1280x860_answering');
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'02-native-answering-1280x860.png')]));
 mark('宽窗（原生窗口 CSS 视口 1280×860）：题干、作答框、提交按钮与上下文栏都在视口内，无横向溢出','native',
  {resize:wideResize,...wide,layout:wideLayout});

 // 880x600 CSS viewport in a real native window
 const narrowResize=await resizeToCss(page,child,880,600);
 const narrow=await inViewport(page,'880x600',[['[data-testid="interview-question"]','question'],['#interview-answer','answer'],['[data-testid="answer-submit"]','submit'],['.desktop-context','context']]);
 const narrowLayout=await layout(page,'880x600_answering');
 const railWidth=await page.evaluate(()=>document.querySelector('.desktop-rail')?.getBoundingClientRect().width??0);
 assert.ok(railWidth<=80,`rail_not_collapsed_${railWidth}`);
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'03-native-answering-880x600.png')]));
 mark('窄窗（原生窗口 CSS 视口 880×600）：侧栏收成图标条，题干/作答框/提交按钮仍在首屏内，无横向溢出','native',
  {resize:narrowResize,rail_width:railWidth,...narrow,layout:narrowLayout});
 await resizeToCss(page,child,1280,860);

 // two answers -> report
 await page.locator('#interview-answer').fill(answer1);
 await page.locator('[data-testid="answer-submit"]').click();
 await page.waitForFunction(()=>{
  const el=document.querySelector('[data-testid="desktop-session"]');
  return !!el&&/第 2\/2/.test(el.textContent??'');
 },{timeout:90000});
 await delay(400);
 const afterFirst=await getDetail(page,goalId!,sessionId!);
 assert.equal(afterFirst.events.filter((e:any)=>e.kind==='answer').length,1,'duplicate_answer_written');
 assert.equal(afterFirst.report,null,'report_before_terminal');
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'04-native-second-question.png')]));
 mark('第一答后进入第 2/2 题，报告仍隐藏，答案未重复写入','native');

 await page.locator('#interview-answer').fill(answer2);
 await page.locator('[data-testid="answer-submit"]').click();
 await page.getByTestId('interview-report').waitFor({timeout:90000});
 await delay(600);
 const final=await getDetail(page,goalId!,sessionId!);
 assert.equal(final.status,'COMPLETED');
 assert.equal(final.report.questions,2);
 assert.equal(final.report.answers,2);
 assert.equal(final.report.observations.length,2);
 assert.equal(final.report.observations.every((o:any)=>o.origin==='scripted_protocol'),true);
 assert.deepEqual(final.report.unreviewed_answer_ids,[]);
 assert.equal(final.report.stop_reason,'question_limit');
 const line3=await page.locator('[data-testid="desktop-session"]').innerText();
 assert.match(line3,/第 2\/2 题 · 已结束 · 场次/);
 evidence.main_chain={session_id:sessionId,status:final.status,questions:final.report.questions,answers:final.report.answers,
  observations:final.report.observations.length,unreviewed:final.report.unreviewed_answer_ids.length,
  stop_reason:final.report.stop_reason,bank_version:final.question_banks[0].version,context_line:line3};
 mark('原生两题主链完成：COMPLETED / 2 题 / 2 答 / 2 条脚本观察 / question_limit，上下文栏只出现一条终态'
  ,'native',evidence.main_chain);
 await inViewport(page,'1280x860_report',[['.desktop-context','context']]);
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'05-native-report-1280x860.png')]));

 const reportNarrow=await resizeToCss(page,child,880,600);
 const reportLayout=await layout(page,'880x600_report');
 await inViewport(page,'880x600_report',[['.desktop-context','context']]);
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'06-native-report-880x600.png')]));
 mark('窄窗复盘页（原生 CSS 视口 880×600）：报告可读、无横向溢出','native',{resize:reportNarrow,layout:reportLayout});
 await resizeToCss(page,child,1280,860);

 // appearance: the same native window in dark, so the theme is proven under WebView2
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('[data-testid="settings-pane"]').waitFor({timeout:20000});
 await page.locator('[data-testid="theme-dark"]').click();
 await page.locator('.desktop-shell[data-theme="dark"]').waitFor({timeout:10000});
 // The attribute flips before the palette is painted; wait for the painted colour
 // so the navigation below cannot race the theme switch.
 await page.waitForFunction(
  ()=>getComputedStyle(document.querySelector('.desktop-shell') as HTMLElement).backgroundColor==='rgb(6, 6, 8)',
  null,{timeout:10000});
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'07-native-dark-settings.png')]));
 await page.locator('.d-nav-item').first().click();
 await page.locator('.desktop-shell[data-pane="workbench"]').waitFor({timeout:20000});
 const darkBg=await page.evaluate(()=>getComputedStyle(document.querySelector('.desktop-shell') as HTMLElement).backgroundColor);
 assert.equal(darkBg,'rgb(6, 6, 8)',`native_dark_bg_unexpected ${darkBg}`);
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'08-native-dark-workbench.png')]));
 mark('原生窗口切深色：背景 #060608，设置页与工作台都跟随，WebView2 下主题变量生效','native',{background:darkBg});
 await page.locator('[data-testid="nav-settings"]').click();
 await page.locator('[data-testid="theme-light"]').click();
 await page.waitForTimeout(300);

 // history reopen inside the same window
 await openDetail(page,'工作台');
 await page.locator('.d-session-item').first().waitFor({timeout:40000});
 const itemText=await page.locator('.d-session-item').first().innerText();
 assert.match(itemText,/已结束/);
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'07-native-workbench-history.png')]));
 await page.locator('.d-session-item').first().click();
 await page.getByTestId('interview-report').waitFor({timeout:60000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),sessionId,'same_window_history_changed_session');
 mark('同一原生窗口从工作台历史打开原场次：同一 session_id 与原报告','native',{item:itemText.replace(/\n/g,' / ')});

 // ---------------- close window ----------------
 child.kill();
 await new Promise<void>(resolve=>{child.once('exit',()=>resolve());setTimeout(resolve,15000);});
 const afterClose=await preflight();
 mark('关闭原生窗口只退出自己的进程，3100 / 5174 服务不受影响','check',afterClose);

 // ---------------- window 2: restart + history read-back ----------------
 await delay(1200);
 child=launchHost();children.push(child);
 await waitForCdp();
 const second=await attach();
 const page2=second.page;
 page2.on('pageerror',error=>pageErrors.push(error.message));
 const info2=win('Info',child.pid!);
 evidence.native_window_restarted=info2;
 assert.match(info2.title,/Tauri/,'restarted_native_window_title_missing');
 await page2.locator('.desktop-shell').waitFor({timeout:40000});
 const session=await page2.evaluate(async()=>{
  const r=await fetch('/api/auth/get-session',{credentials:'same-origin',cache:'no-store'});
  return r.ok?await r.json():null;
 });
 evidence.session_after_restart=session?.user?{id:session.user.id,email:session.user.email}:null;
 if(!session?.user){
  await page2.getByLabel('邮箱').fill(account.email);
  await page2.getByLabel('密码').fill(account.password);
  await page2.getByRole('button',{name:'登录',exact:true}).click();
  mark('关窗重开后 Cookie 失效：用同一合成账号重新登录，仍落在桌面外壳','native');
 }else{
  mark('关窗重开后 WebView2 保留登录态，无需重新登录，入口仍是桌面外壳','native',evidence.session_after_restart);
 }
 await page2.locator('.desktop-shell[data-pane]').waitFor({timeout:60000});
 assert.match(page2.url(),/shell=desktop|goal=/,'restarted_entry_url_unexpected');
 await page2.locator('.d-session-item').first().waitFor({timeout:60000});
 assert.match(await page2.locator('.d-session-item').first().innerText(),/已结束/);
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'08-native-reopened-workbench.png')]));
 await page2.locator('.d-session-item').first().click();
 await page2.getByTestId('interview-report').waitFor({timeout:60000});
 assert.equal(await page2.locator('[data-session-id]').getAttribute('data-session-id'),sessionId,'reopened_session_id_changed');
 const final2=await getDetail(page2,goalId!,sessionId!);
 assert.equal(final2.status,'COMPLETED');
 assert.equal(final2.report.questions,2);
 assert.equal(final2.report.answers,2);
 assert.equal(final2.report.observations.length,2);
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'09-native-reopened-report.png')]));
 mark('重开原生窗口：工作台直接列出上一场的已结束记录，打开后同 ID / 同报告（关窗重开历史回读）','native',
  {session_id:sessionId,status:final2.status,questions:final2.report.questions,answers:final2.report.answers,observations:final2.report.observations.length});

 assert.deepEqual(pageErrors,[],'page_errors_present');
 mark('整个原生链路无页面 JS 异常','check',{page_errors:pageErrors.length});

 evidence.result='passed';
 evidence.model={declared:'claude-opus-6',provider_calls:0,run_origin:'scripted_protocol',
  credits_balance:'unknown',credits_consumed:'unknown'};
 writeFileSync(join(output,'native-results.json'),JSON.stringify(evidence,null,2));
 console.log(JSON.stringify({result:'passed',cases:cases.length,session_id:sessionId,main_chain:evidence.main_chain,
  native_window:info1.title,screenshots:evidence.screenshots.length},null,2));
}catch(error){
 evidence.result='failed';
 evidence.error=String((error as Error)?.stack??error);
 evidence.cases=cases;
 writeFileSync(join(output,'native-results-failure.json'),JSON.stringify(evidence,null,2));
 for(const c of children){try{win('Capture',c.pid!,['-OutFile',join(output,`native-failure-${c.pid}.png`)]);}catch{}}
 console.error('D-01-03 native acceptance FAILED:',(error as Error).message);
 process.exitCode=1;
}finally{
 try{await attached?.close();}catch{}
 for(const c of children)if(c.exitCode===null&&!c.killed)try{c.kill();}catch{}
}
