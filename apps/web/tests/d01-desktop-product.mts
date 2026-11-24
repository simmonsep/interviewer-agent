import {waitGoalReady} from './desktop-nav.mts';
// F05 product acceptance. Drives the REAL portable desktop build, started from a copy
// outside the source tree, with no Vite dev server running:
//
//   host (Win32 window) -> frozen sidecar -> FastAPI (3100) serving the built frontend
//   -> PG/Redis + the free scripted interview/materials Workers.
//
// Everything here is a real local service. No page.route injection, no synthetic API,
// no model calls. CDP is used only to inspect and drive the window, and the port is
// opened by this test, not by the shipped default.
//
//   D05_OUTPUT=<new dir> node apps/web/tests/d01-desktop-product.mts
//
// Requires: a portable directory built by scripts/build-desktop-product.ps1 with
// settings/api.json + settings/worker.json, PG/Redis containers up, and port 3100 free.
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {execFileSync,spawn,type ChildProcess} from 'node:child_process';
import {readFileSync,writeFileSync,mkdirSync,existsSync,readdirSync,statSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {createServer,type Server} from 'node:net';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D05_OUTPUT??join(root,'docs/records/desktop-handoff/F05-product'));
const cdpPort=Number(process.env.D05_CDP_PORT??9263);
const winScript=join(root,'scripts/d01-desktop-window.ps1');
const fixture=JSON.parse(readFileSync(join(root,'fixtures/question-banks/agent-developer-trial.json'),'utf8'));
mkdirSync(output,{recursive:true});

function portableDir():string{
 if(process.env.D05_PORTABLE)return resolve(process.env.D05_PORTABLE);
 const base=join(root,'.private/f05/portable');
 const dirs=readdirSync(base).map(name=>join(base,name))
  .filter(dir=>existsSync(join(dir,'interview-desktop-probe.exe'))&&existsSync(join(dir,'settings/api.json')))
  .sort((a,b)=>statSync(b).mtimeMs-statSync(a).mtimeMs);
 if(!dirs.length)throw new Error('portable_build_missing: run scripts/build-desktop-product.ps1 first');
 return dirs[0];
}
const appDir=portableDir();
// A first launch looks like an empty WebView2 profile. The same profile is reused across
// the reopen case, so the cookie and the goal pointer have to survive on their own. It
// lives under .private because it holds a real session token and must never reach git.
const profileDir=resolve(root,'.private/f05/profiles',String(Date.now()));
mkdirSync(profileDir,{recursive:true});
const hostExe=join(appDir,'interview-desktop-probe.exe');
const account={email:`f05-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID(),name:'F05 产品验收'};
const answer1='图里 state 是线程运行状态，checkpointer 按 thread_id 存检查点，store 放跨线程偏好。独立会话要用不同 thread_id。';
const answer2='interrupt 借助持久化检查点和 thread_id 暂停，用 Command(resume=...) 恢复；恢复会从含 interrupt 的节点开头重跑，副作用要幂等。';

const cases:{name:string;detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,detail});console.log('PASS',name);};
const evidence:Record<string,unknown>={run:new Date().toISOString(),app_dir:appDir,host_exe:hostExe,cases,page_errors:pageErrors,
 screenshots:[],case_type_note:'全部用例在源码目录之外的便携构建上运行：宿主启动冻结 sidecar、真实 PG/Redis 与免费脚本 Worker，未使用注入或合成 API，未调用模型。'};

function ps(command:string):string{
 return execFileSync('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-Command',command],{encoding:'utf8',timeout:60000,windowsHide:true}).trim();
}
function win(action:'Info'|'Capture'|'Resize'|'Close',pid:number,args:string[]=[]):any{
 const stdout=execFileSync('powershell.exe',['-NoProfile','-ExecutionPolicy','Bypass','-File',winScript,
  '-Action',action,'-ProcessId',String(pid),...args],{encoding:'utf8',timeout:45000,windowsHide:true});
 const text=stdout.trim();
 const start=text.indexOf('{');
 if(start<0)throw new Error('window_helper_no_json: '+text.slice(0,300));
 return JSON.parse(text.slice(start));
}
function sidecarProcesses():number{
 const out=ps("@(Get-Process | Where-Object { $_.ProcessName -like 'interview-sidecar*' }).Count");
 return Number(out||0);
}
async function portOpen(port:number):Promise<boolean>{
 try{const response=await fetch(`http://127.0.0.1:${port}/health`,{signal:AbortSignal.timeout(1200)});return response.ok;}
 catch{return false;}
}
async function waitClosed(port:number,timeout=20000){
 const started=Date.now();
 while(Date.now()-started<timeout){if(!(await portOpen(port)))return true;await delay(300);}
 return false;
}
function launch(extra:Record<string,string>={}):ChildProcess{
 // A fresh WebView2 profile is what a first launch looks like; the same profile is reused
 // across the reopen case so the cookie and the goal pointer have to survive on their own.
 const env={...process.env,INTERVIEW_DESKTOP_CDP_PORT:String(cdpPort),INTERVIEW_DESKTOP_DATA_DIR:profileDir,...extra};
 // A previous run must never inherit probe mode through the environment.
 delete (env as any).INTERVIEW_DESKTOP_PREVIEW;
 delete (env as any).INTERVIEW_DESKTOP_PROBE;
 return spawn(hostExe,[],{cwd:appDir,env,stdio:['ignore','ignore','ignore'],windowsHide:false});
}
async function waitForCdp(timeout=90000){
 const started=Date.now();
 while(Date.now()-started<timeout){
  try{const r=await fetch(`http://127.0.0.1:${cdpPort}/json/version`);if(r.ok)return await r.json();}catch{}
  await delay(400);
 }
 throw new Error('webview_cdp_not_ready');
}
async function attach(match:(url:string)=>boolean,timeout=90000):Promise<{browser:Browser;page:Page}>{
 await waitForCdp(timeout);
 const browser=await chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`,{timeout:40000});
 for(let i=0;i<120;i++){
  for(const ctx of browser.contexts())for(const p of ctx.pages()){
   if(match(p.url())){
    p.on('pageerror',error=>pageErrors.push(error.message));
    return {browser,page:p};
   }
  }
  await delay(300);
 }
 const seen=browser.contexts().flatMap(c=>c.pages().map(p=>p.url()));
 throw new Error('webview_page_not_found: '+JSON.stringify(seen));
}
async function shot(page:Page,name:string,pid?:number){
 const file=join(output,'shots',name);
 mkdirSync(join(output,'shots'),{recursive:true});
 if(pid){
  try{win('Capture',pid,['-OutFile',file]);}catch{await page.screenshot({path:file});}
 }else{
  await page.screenshot({path:file});
 }
 (evidence.screenshots as string[]).push(file);
}
async function register(page:Page){
 await page.getByRole('button',{name:'第一次来？创建账号'}).click();
 await page.locator('input[name="name"]').fill(account.name);
 await page.locator('input[name="email"]').fill(account.email);
 await page.locator('input[name="password"]').fill(account.password);
 await page.getByRole('button',{name:'创建账号',exact:true}).click();
}
async function importBank(page:Page):Promise<{bank_id:string;version:number}>{
 return page.evaluate(async(document)=>{
  const previewResponse=await fetch('/api/question-banks/imports',{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({document})});
  const preview=await previewResponse.json() as any;
  if(!previewResponse.ok)throw new Error('preview_failed '+JSON.stringify(preview));
  const commitResponse=await fetch(`/api/question-banks/imports/${preview.preview_id}/commit`,{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({})});
  const commit=await commitResponse.json() as any;
  if(!commitResponse.ok)throw new Error('commit_failed '+JSON.stringify(commit));
  return {bank_id:commit.bank_id,version:commit.version};
 },fixture);
}

let browser:Browser|null=null;
let child:ChildProcess|null=null;
let blocker:Server|null=null;

try{
 // Leftover sidecars from an interrupted earlier run would make every child count below
 // lie, so they are removed first and the run starts from a clean process table.
 const strays=sidecarProcesses();
 if(strays>0)ps("@(Get-Process | Where-Object { $_.ProcessName -like 'interview-sidecar*' }) | ForEach-Object { Stop-Process -Id $_.Id -Force }");
 assert.equal(sidecarProcesses(),0,'stray_sidecar_processes_at_start');
 (evidence as Record<string,unknown>).strays_before_run=strays;

 // --------------------------------------------------------------- 0. the point of F05: no dev server
 assert.equal(await portOpen(3100),false,'port_3100_already_busy_before_launch');
 let viteReachable=true;
 try{await fetch('http://127.0.0.1:5174/',{signal:AbortSignal.timeout(1200)});}catch{viteReachable=false;}
 assert.equal(viteReachable,false,'vite_is_running_this_test_needs_it_off');
 mark('起点：Vite 未运行（5174 无监听），3100 空闲；界面只能来自构建产物',{vite_reachable:viteReachable});

 // --------------------------------------------------------------- 1. host starts the stack itself
 child=launch();
 const attached=await attach(url=>url.includes('127.0.0.1:3100'));
 browser=attached.browser;
 const page=attached.page;
 // F05B 免登直入：全新配置档下窗口直接落在工作台（本机身份自动创建）；
 // 登录页只应出现在“切换账号”之后，不再是首次启动的第一屏。
 await page.locator('.desktop-shell[data-pane="workbench"]').waitFor({timeout:90000});
 assert.equal(await page.locator('.desktop-auth-card').count(),0,'first_run_should_skip_login');
 const guestEmail=await page.evaluate(()=>{try{return (JSON.parse(localStorage.getItem('py-local-identity')??'null') as {email:string}|null)?.email??'';}catch{return '';}});
 assert.match(guestEmail,/^local-[0-9a-f-]{36}@desktop\.example\.com$/,'guest_identity_missing');
 const startup=JSON.parse(readFileSync(join(appDir,'run-data/startup.json'),'utf8'));
 assert.equal(startup.ready,true,`startup_not_ready ${JSON.stringify(startup)}`);
 const health=await (await fetch('http://127.0.0.1:3100/health')).json() as any;
 assert.equal(health.status,'ok');
 const origin=await page.evaluate(()=>location.origin);
 assert.equal(origin,'http://127.0.0.1:3100','page_not_served_by_the_local_api');
 const served=await page.evaluate(async()=>{
  const html=await (await fetch('/',{cache:'no-store'})).text();
  const asset=html.match(/\/assets\/[^"]+\.js/)?.[0]??'';
  const assetResponse=asset?await fetch(asset,{cache:'no-store'}):null;
  return {hasRoot:html.includes('id="root"'),asset,assetStatus:assetResponse?.status??0,assetType:assetResponse?.headers.get('content-type')??''};
 });
 assert.equal(served.hasRoot,true,'index_html_not_served');
 assert.equal(served.assetStatus,200,`asset_not_served ${served.asset}`);
 await shot(page,'01-product-login.png',child.pid!);
 mark('宿主自启整套本地栈：冻结 sidecar 提供 API 与构建前端，窗口直接落在 http://127.0.0.1:3100（同源），无 Vite',
  {startup_code:startup.code,health,origin,built_asset:served.asset,asset_type:served.assetType});

 // --------------------------------------------------------------- 2. same-origin auth + writes
 // 会话来自免登的本机身份（HttpOnly Cookie 已由静默登录建立）。
 await page.locator('.desktop-shell[data-pane]').waitFor({timeout:60000});
 // The session cookie is HttpOnly, so the honest check is that the page's own request
 // is authenticated without any header the app would have to add by hand.
 const session=await page.evaluate(async()=>{
  const response=await fetch('/api/auth/get-session',{credentials:'same-origin',cache:'no-store'});
  return {status:response.status,body:await response.json() as any,scriptReadable:document.cookie.includes('py-interview')};
 });
 assert.equal(session.status,200,`session_${session.status}`);
 assert.equal(session.body?.user?.email,guestEmail,'session_cookie_not_sent_on_this_origin');
 assert.equal(session.scriptReadable,false,'session_cookie_should_stay_http_only');
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('研二，想在上海找 AI 应用 / Agent 开发实习。');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);
 mark('同源写入：注册拿到的会话 Cookie 直接用于 /api 写请求（无需代理改写 Origin，未放宽校验）',
  {session_user:session.body.user.email,http_only:!session.scriptReadable,goal:await page.locator('[data-testid="desktop-goal"]').innerText()});
 await shot(page,'02-product-workbench.png',child.pid!);

 // --------------------------------------------------------------- 3. main chain on the product build
 await page.getByTestId('workbench-primary').click();
 await page.locator('#interview-bank').waitFor({timeout:30000});
 const bank=await importBank(page);
 await page.getByRole('button',{name:'刷新题库'}).click();
 try{
  await page.locator('#interview-bank option',{hasText:'Agent开发岗位试运行'}).waitFor({state:'attached',timeout:30000});
 }catch(error){
  const options=await page.locator('#interview-bank option').allInnerTexts();
  throw new Error(`bank_option_missing imported=${JSON.stringify(bank)} options=${JSON.stringify(options)}`);
 }
 await page.getByLabel('题库与版本').selectOption(`${bank.bank_id}|${bank.version}`);
 await page.getByRole('button',{name:'开始整场面试 →'}).click();
 await page.getByTestId('interview-question').waitFor({timeout:120000});
 await page.locator('.desktop-shell[data-answering="true"]').waitFor({timeout:30000});
 await shot(page,'03-product-answering.png',child.pid!);
 await page.locator('#interview-answer').fill(answer1);
 await page.locator('[data-testid="answer-submit"]').click();
 await page.getByTestId('interview-question').waitFor({timeout:120000});
 await page.locator('#interview-answer').waitFor({state:'visible',timeout:60000});
 await page.locator('#interview-answer').fill(answer2);
 await page.locator('[data-testid="answer-submit"]').click();
 await page.locator('[data-testid="interview-report"]').waitFor({timeout:180000});
 const reportText=await page.locator('[data-testid="interview-report"]').innerText();
 assert.ok(reportText.length>20,'report_empty');
 await shot(page,'04-product-report.png',child.pid!);
 mark('产品构建跑通主链：建目标 → 选库开场 → 两答 → 复盘（评价由免费脚本 Worker 产出）',
  {report_excerpt:reportText.replace(/\n+/g,' / ').slice(0,120)});

 // --------------------------------------------------------------- 4. window close recycles only our children
 const before=sidecarProcesses();
 assert.ok(before>=1,`sidecar_not_running_before_close ${before}`);
 win('Close',child.pid!);
 const exited=await new Promise<boolean>(async resolveExit=>{
  const started=Date.now();
  while(Date.now()-started<25000){if(child!.exitCode!==null||child!.signalCode!==null)return resolveExit(true);await delay(300);}
  resolveExit(false);
 });
 assert.equal(exited,true,'host_did_not_exit_on_window_close');
 assert.equal(await waitClosed(3100,15000),true,'api_still_listening_after_close');
 await delay(1200);
 assert.equal(sidecarProcesses(),0,'sidecar_or_workers_left_running');
 mark('关闭窗口即回收：宿主退出、3100 释放、sidecar 与两个 Worker 全部结束（只回收自己启动的进程）',
  {sidecar_before_close:before,sidecar_after_close:0});

 // --------------------------------------------------------------- 5. reopen restores account, goal and sessions
 browser=null;
 child=launch();
 const again=await attach(url=>url.includes('127.0.0.1:3100'));
 browser=again.browser;
 const page2=again.page;
 await page2.locator('.desktop-shell[data-pane]').waitFor({timeout:90000});
 // No login form: the account session survived the restart in the WebView profile.
 assert.equal(await page2.locator('.desktop-auth-card').count(),0,'login_page_shown_after_reopen');
 await waitGoalReady(page2);
 // The session list is published by the (hidden) interview pane, so give it the same
 // window the workbench itself gets; it reads the same history the report came from.
 await page2.locator('.d-session-item').first().waitFor({timeout:60000});
 const sessions=await page2.locator('.d-session-item').count();
 assert.ok(sessions>=1,`history_empty_after_reopen ${sessions}`);
 const goalLine=await page2.locator('[data-testid="desktop-goal"]').innerText();
 await shot(page2,'05-product-reopen.png',child.pid!);
 mark('关窗重开：账号会话、上次目标与历史场次都从本机服务回读，界面不需要重新登录或重选目标',
  {sessions_in_list:sessions,goal:goalLine});

 // --------------------------------------------------------------- 6. even a hard kill cannot orphan children
 // TerminateProcess runs no destructors and no exit handler; only the job object can
 // clean up after it, which is the case a crashed host behaves like.
 child!.kill();
 await new Promise<void>(resolveExit=>{child!.once('exit',()=>resolveExit());setTimeout(()=>resolveExit(),15000);});
 assert.equal(await waitClosed(3100,15000),true,'api_survived_hard_kill');
 await delay(1200);
 assert.equal(sidecarProcesses(),0,'sidecar_or_workers_survived_hard_kill');
 mark('硬杀宿主（TerminateProcess，无清理钩子可跑）：作业对象仍让系统终止 sidecar 与 Worker，不留孤儿进程',
  {sidecar_processes:0});

 // --------------------------------------------------------------- 7. port occupied is reported, not adopted
 // The host was hard-killed above, so the port is already free for the blocker.
 blocker=createServer();
 await new Promise<void>((ok,fail)=>blocker!.once('error',fail).listen(3100,'127.0.0.1',()=>ok()));
 child=launch();
 const blocked=await attach(url=>url.includes('startup.html'),60000);
 browser=blocked.browser;
 await blocked.page.locator('#actions:not([hidden])').waitFor({timeout:60000});
 const detail=await blocked.page.locator('#detail').innerText();
 const message=await blocked.page.locator('#text').innerText();
 assert.match(detail,/port_3100_in_use/);
 await shot(blocked.page,'06-startup-port-in-use.png',child.pid!);
 mark('端口被占用：启动页给出明确原因与诊断码，不接管、不静默失败',{detail,message});
 await blocked.browser.close();
 browser=null;
 child.kill();
 await new Promise<void>(resolveExit=>{child!.once('exit',()=>resolveExit());setTimeout(()=>resolveExit(),15000);});
 await new Promise<void>(ok=>blocker!.close(()=>ok()));
 blocker=null;
 await delay(800);

 // --------------------------------------------------------------- 8. missing configuration is reported
 const emptyConfig=join(output,'empty-config');
 mkdirSync(emptyConfig,{recursive:true});
 child=launch({INTERVIEW_DESKTOP_CONFIG_DIR:emptyConfig});
 const broken=await attach(url=>url.includes('startup.html'),60000);
 browser=broken.browser;
 await broken.page.locator('#actions:not([hidden])').waitFor({timeout:60000});
 const missing=await broken.page.locator('#detail').innerText();
 assert.match(missing,/configuration_missing/);
 await shot(broken.page,'07-startup-config-missing.png',child.pid!);
 mark('缺少配置：启动页说明需要在外部配置目录提供 api.json / worker.json，诊断码可追',{detail:missing});
 await broken.browser.close();
 browser=null;
 child.kill();
 await new Promise<void>(resolveExit=>{child!.once('exit',()=>resolveExit());setTimeout(()=>resolveExit(),15000);});
 await delay(600);

 evidence.cases=cases;
 evidence.sidecar_processes_after_all=sidecarProcesses();
 writeFileSync(join(output,'result.json'),JSON.stringify(evidence,null,2));
 console.log(`\n${cases.length}/${cases.length} passed`);
 process.exit(0);
}catch(error){
 const failure=(error as Error).message;
 evidence.failure=failure;
 try{evidence.diagnostic={port_3100:await portOpen(3100),sidecars:sidecarProcesses()};}catch{}
 evidence.cases=cases;
 writeFileSync(join(output,'result.json'),JSON.stringify(evidence,null,2));
 if(browser){try{await browser.close();}catch{}}
 if(child&&child.exitCode===null)child.kill();
 if(blocker)blocker.close();
 console.error('FAILED:',failure);
 process.exit(1);
}
