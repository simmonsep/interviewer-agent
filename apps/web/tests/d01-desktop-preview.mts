// D-01-02 native Tauri preview acceptance. Drives the REAL WebView2 window over
// the WebView2 DevTools protocol, while the normal React app talks over HTTP to
// the real local stack: Vite (5174) -> FastAPI (3100) -> PG/Redis + free scripted
// Worker. No page.route injection, no synthetic API, no model calls.
//
//   D01_OUTPUT=<new dir> node apps/web/tests/d01-desktop-preview.mts
//
// Requires: built host (.private/d-01-02/tauri-target/debug/interview-desktop-probe.exe),
// running 3100/5174 services, and a free CDP port (default 9222).
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {execFileSync} from 'node:child_process';
import {spawn,type ChildProcess} from 'node:child_process';
import {readFileSync,writeFileSync,mkdirSync,existsSync,openSync,closeSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/evidence/D-01-02'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const hostExe=resolve(process.env.D01_HOST_EXE??join(root,'.private/d-01-02/tauri-target/debug/interview-desktop-probe.exe'));
const cdpPort=Number(process.env.D01_CDP_PORT??9222);
const winScript=join(root,'scripts/d01-desktop-window.ps1');
const fixture=JSON.parse(readFileSync(join(root,'fixtures/question-banks/agent-developer-trial.json'),'utf8'));
mkdirSync(output,{recursive:true});
const logPath=join(output,'native-host.log');
const runDir=join(output,'native-run');
mkdirSync(runDir,{recursive:true});

const cases:{name:string;status:string;type:'native'|'check';detail?:unknown}[]=[];
const pageErrors:string[]=[];
const mark=(name:string,type:'native'|'check'='native',detail?:unknown)=>cases.push({name,status:'passed',type,detail});
const account={email:`d01-desktop-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID()};
const answer1='图里 state 是线程运行状态，checkpointer 按 thread_id 存检查点，store 放跨线程偏好。独立会话要用不同 thread_id；进程重启后继续需要持久化 checkpointer 与 store，内存实现不行。';
const answer2='interrupt 靠持久化检查点与 thread_id 暂停，用 Command(resume=...) 恢复；恢复会从含 interrupt 的节点开头重跑，副作用要幂等或移到单独节点，写入放在用户明确批准之后并用业务幂等键防重复。';

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
 child.on('exit',(code,signal)=>{closeSync(fd);console.log(`[host pid=${child.pid}] exit=${code} signal=${signal}`);});
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
async function waitForStatus(p:Page,label:string){
 await p.getByTestId('interview-status').filter({hasText:`状态 · ${label}`}).waitFor({timeout:90000});
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
async function setupAccount(p:Page):Promise<string>{
 await p.goto(base,{waitUntil:'domcontentloaded'});
 // WebView2 keeps the previous run's Cookie profile. Always start from the login
 // page so each acceptance run creates its own synthetic account.
 const existing=await p.evaluate(async()=>{
  const r=await fetch('/api/auth/get-session',{credentials:'same-origin',cache:'no-store'});
  return r.ok?await r.json():null;
 });
 if(existing?.user){
  await p.getByRole('button',{name:'退出登录'}).click();
  await p.getByRole('button',{name:'第一次来？创建账号'}).waitFor({timeout:30000});
  await p.goto(base,{waitUntil:'domcontentloaded'});
 }
 await p.getByRole('button',{name:'第一次来？创建账号'}).click();
 await p.getByLabel('称呼').fill('合成测试学员');
 await p.getByLabel('邮箱').fill(account.email);
 await p.getByLabel('密码').fill(account.password);
 await p.getByRole('button',{name:'创建账号',exact:true}).click();
 await p.getByRole('button',{name:'整场面试'}).waitFor({timeout:30000});
 await p.getByRole('button',{name:'整场面试'}).click();
 await p.getByLabel('求职意向或补充说明').fill('想在上海找 AI 应用 / Agent 开发实习，用两题练习检验表达。');
 await p.getByRole('button',{name:'保存求职目标'}).click();
 await p.locator('.goal-card').waitFor({timeout:30000});
 const id=new URL(p.url()).searchParams.get('goal');
 assert.ok(id,'goal_id_missing');
 return id;
}
async function clickable(p:Page,locator:any,label:string){
 await locator.scrollIntoViewIfNeeded();
 const box=await locator.boundingBox();
 assert.ok(box&&box.width>0&&box.height>0,`${label}_not_rendered`);
 const vp=await p.evaluate(()=>({w:window.innerWidth,h:window.innerHeight}));
 assert.ok(box.x>=-2&&box.x+box.width<=vp.w+2,`${label}_outside_viewport_x=${box.x}_w=${box.width}_vp=${vp.w}`);
 return {box,vp};
}
async function layout(p:Page,label:string){
 const value=await p.evaluate(()=>({inner_width:window.innerWidth,inner_height:window.innerHeight,
  document_scroll_width:document.documentElement.scrollWidth,body_scroll_width:document.body.scrollWidth,
  device_pixel_ratio:window.devicePixelRatio}));
 assert.ok(value.document_scroll_width<=value.inner_width+2,
  `${label}_horizontal_overflow_${JSON.stringify(value)}`);
 assert.ok(value.body_scroll_width<=value.inner_width+2,
  `${label}_body_horizontal_overflow_${JSON.stringify(value)}`);
 return value;
}

const evidence:any={kind:'D-01-02 桌面预览验收',timestamp:new Date().toISOString(),
 base_url:base,host_exe:hostExe,cdp_port:cdpPort,
 api_mode:'real-local-stack: Vite 5174 proxy -> FastAPI 3100 -> PG/Redis + free scripted Worker',
 case_type_note:'所有用例都在真实 Tauri WebView2 原生窗口内完成，未使用 page.route 注入或合成 API；仅为验收启用 WebView2 远程调试端口。',
 account:{email:account.email,name:'合成测试学员'},cases,page_errors:pageErrors,screenshots:[]};
const children:ChildProcess[]=[];
let firstBrowser:Browser|null=null;
try{
 assert.ok(existsSync(hostExe),'built_host_missing');
 evidence.preflight=await preflight();
 mark('启动前确认 3100 测试 API 与 5174 Vite 均由开发者预先启动','check',evidence.preflight);

 // ---------------- Window 1: native Tauri entry + full two-question chain ----------------
 let child=launchHost();children.push(child);
 const cdp=await waitForCdp();
 evidence.cdp={browser:cdp.Browser,protocol:cdp['Protocol-Version']};
 const first=await attach();firstBrowser=first.browser;
 const page=first.page;
 page.on('pageerror',error=>pageErrors.push(error.message));
 const info1=win('Info',child.pid!);
 evidence.native_window=info1;
 assert.match(info1.process,/interview-desktop-probe/i,'not_our_host_process');
 assert.match(info1.title,/Tauri/,'native_window_title_missing');
 mark('实际构建并打开原生 Tauri 窗口（进程名与标题可区分普通浏览器）','native',info1);
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'00-native-window.png')]));

 const goalId=await setupAccount(page);
 evidence.goal_id=goalId;
 const bank=await importBank(page);
 evidence.bank=bank;
 mark('原生窗口内合成账号注册、创建求职目标、经 Vite 代理导入并启用私有题库 v1','native',{goal_id:goalId,bank});

 await page.goto(`${base}/?goal=${goalId}&view=interview`,{waitUntil:'domcontentloaded'});
 await page.locator('#interview-bank').waitFor({timeout:30000});
 await page.getByLabel('题库与版本').locator('option',{hasText:'Agent开发岗位试运行'}).waitFor({state:'attached',timeout:30000});
 await page.getByLabel('题库与版本').selectOption(`${bank.bank_id}|${bank.version}`);
 await page.getByRole('button',{name:'开始整场面试 →'}).click();
 await page.locator('[data-session-id]').waitFor({timeout:30000});
 await page.getByTestId('interview-question').waitFor({timeout:90000});
 await waitForStatus(page,'可作答');
 const sessionId=await page.locator('[data-session-id]').getAttribute('data-session-id');
 assert.ok(sessionId,'session_id_missing');
 evidence.session_id=sessionId;
 const firstQuestion=await page.getByTestId('interview-question').innerText();
 const initial=await getDetail(page,goalId,sessionId);
 assert.equal(initial.status,'WAITING_FOR_USER');
 assert.equal(initial.question_banks[0].version,1);
 assert.equal(initial.policy.max_questions,2);
 assert.equal(initial.report,null);
 mark('开场成功：首题 / WAITING_FOR_USER / 冻结私有题库 v1 / 2 题上限','native',{session_id:sessionId,version:initial.version});
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'01-first-question.png')]));

 const submitButton=page.getByRole('button',{name:'提交这一答 →'});
 assert.equal(await submitButton.isDisabled(),true,'empty_answer_must_be_disabled');
 mark('空答时提交按钮禁用','native');
 await page.getByLabel('你的整场面试回答').fill(answer1);
 await submitButton.click();
 await page.waitForFunction(previous=>{
  const element=document.querySelector('[data-testid="interview-question"]');
  return !!element&&element.textContent!==previous;
 },firstQuestion,{timeout:90000});
 await waitForStatus(page,'可作答');
 const afterFirst=await getDetail(page,goalId,sessionId);
 assert.equal(afterFirst.events.filter((e:any)=>e.kind==='answer').length,1,'duplicate_answer_written');
 assert.equal(afterFirst.events.filter((e:any)=>e.kind==='question').length,2);
 assert.equal(afterFirst.report,null,'report_shown_before_terminal');
 mark('第一答后第二题出现，报告仍隐藏，未重复写入答案','native');
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'02-second-question.png')]));

 // ---- Real Win32 window resizes: buttons remain reachable at common desktop sizes ----
 const textarea=page.getByLabel('你的整场面试回答');
 for(const [w,h,name] of [[1024,768,'04-window-1024.png'],[900,700,'05-window-900.png']] as [number,number,string][]){
  const resized=win('Resize',child.pid!,['-Width',String(w),'-Height',String(h)]);
  await delay(500);
  const a=await clickable(page,textarea,'答题框');
  const b=await clickable(page,submitButton,'提交按钮');
  const c=await clickable(page,page.getByRole('button',{name:'整场面试'}),'整场面试入口');
  const layoutInfo=await layout(page,`${w}x${h}`);
  // Show the answer area (not the header) in the narrow-window screenshot.
  await textarea.scrollIntoViewIfNeeded();
  await delay(200);
  evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,name)]));
  mark(`${w}×${h} 原生窗口：答题框、提交按钮、顶部入口均在视口内，无横向溢出`,'native',
   {resize:resized,layout:layoutInfo,viewport:a.vp,answer:a.box,submit:b.box,nav:c.box});
 }
 win('Resize',child.pid!,['-Width','1280','-Height','860']);
 await delay(400);

 await page.getByLabel('你的整场面试回答').fill(answer2);
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await page.getByTestId('interview-report').waitFor({timeout:90000});
 const reportLayout=await layout(page,'report-1280');
 await clickable(page,page.getByTestId('interview-report'),'整场面试报告');
 const final=await getDetail(page,goalId,sessionId);
 assert.equal(final.status,'COMPLETED');
 assert.equal(final.report.questions,2);
 assert.equal(final.report.answers,2);
 assert.equal(final.report.observations.length,2);
 assert.equal(final.report.observations.every((o:any)=>o.origin==='scripted_protocol'),true);
 assert.deepEqual(final.report.unreviewed_answer_ids,[]);
 assert.equal(final.report.stop_reason,'question_limit');
 assert.equal(final.question_banks[0].version,1);
 evidence.main_chain={session_id:sessionId,status:final.status,version:final.version,
  questions:final.report.questions,answers:final.report.answers,observations:final.report.observations.length,
  unreviewed_answer_ids:final.report.unreviewed_answer_ids,stop_reason:final.report.stop_reason,
  bank_version:final.question_banks[0].version,run_origin:final.run?.origin??null,layout:reportLayout};
 mark('两题主链完成：COMPLETED / 2 题 / 2 答 / 2 条脚本观察 / 0 未评阅 / question_limit','native',evidence.main_chain);
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'03-report.png')]));

 // ---- Narrow window: report and start form remain reachable ----
 win('Resize',child.pid!,['-Width','900','-Height','700']);
 await delay(400);
 const reportBox=await clickable(page,page.getByTestId('interview-report'),'整场面试报告-900');
 const resetBox=await clickable(page,page.getByRole('button',{name:'另开一场'}),'另开一场-900');
 const reportLayout900=await layout(page,'report-900');
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'08-report-900.png')]));
 mark('900×700 原生窗口：报告区域与「另开一场」按钮可滚动可达、无横向溢出','native',
  {layout:reportLayout900,report:reportBox.box,reset:resetBox.box});
 await page.getByRole('button',{name:'另开一场'}).click();
 const bankBox=await clickable(page,page.getByLabel('题库与版本'),'题库下拉-900');
 const startBox=await clickable(page,page.getByRole('button',{name:'开始整场面试 →'}),'开始按钮-900');
 const setupLayout900=await layout(page,'setup-900');
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'09-setup-900.png')]));
 mark('900×700 原生窗口：题库下拉与「开始整场面试」按钮可点击、无横向溢出','native',
  {layout:setupLayout900,bank:bankBox.box,start:startBox.box});
 win('Resize',child.pid!,['-Width','1280','-Height','860']);
 await delay(400);

 // ---- History read-back in the SAME window ----
 await page.locator('.interview-history .history-item').first().click();
 await page.locator('[data-session-id]').waitFor({timeout:30000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),sessionId,'history_session_id_changed');
 await page.getByTestId('interview-report').waitFor({timeout:30000});
 const historyText=await page.getByTestId('interview-report').innerText();
 assert.match(historyText,/2/,'report_counts_missing');
 mark('同一原生窗口从历史列表重新打开原场次：ID 与报告一致','native',{session_id:sessionId});
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'06-history-same-window.png')]));

 // ---- Close the window; external services must stay alive ----
 child.kill();
 await new Promise<void>(resolve=>{child.once('exit',()=>resolve());setTimeout(resolve,15000);});
 const afterClose=await preflight();
 assert.equal(pageErrors.length,0,'page_errors_present');
 mark('关闭预览窗口只退出自己的进程；3100 / 5174 服务不受影响','check',afterClose);

 // ---------------- Window 2: restart, re-login if needed, history read-back ----------------
 await delay(1200);
 child=launchHost();children.push(child);
 await waitForCdp();
 const second=await attach();
 const page2=second.page;
 page2.on('pageerror',error=>pageErrors.push(error.message));
 const info2=win('Info',child.pid!);
 evidence.native_window_restarted=info2;
 assert.match(info2.title,/Tauri/,'restarted_native_window_title_missing');
 await page2.goto(`${base}/?goal=${goalId}&view=interview`,{waitUntil:'domcontentloaded'});
 const session=await page2.evaluate(async()=>{
  const r=await fetch('/api/auth/get-session',{credentials:'same-origin',cache:'no-store'});
  return r.ok?await r.json():null;
 });
 evidence.session_after_restart=session?.user?{id:session.user.id,email:session.user.email}:null;
 if(!session?.user){
  await page2.getByLabel('邮箱').fill(account.email);
  await page2.getByLabel('密码').fill(account.password);
  await page2.getByRole('button',{name:'登录',exact:true}).click();
  await page2.getByRole('button',{name:'整场面试'}).waitFor({timeout:30000});
  await page2.goto(`${base}/?goal=${goalId}&view=interview`,{waitUntil:'domcontentloaded'});
  mark('关窗重开后 Cookie 失效，用同一合成账号重新登录','native');
 }else{
  mark('关窗重开后 WebView2 保留登录态，无需重新登录','native',evidence.session_after_restart);
 }
 await page2.locator('.interview-history .history-item').first().waitFor({timeout:30000});
 await page2.locator('.interview-history .history-item').first().click();
 await page2.locator('[data-session-id]').waitFor({timeout:30000});
 assert.equal(await page2.locator('[data-session-id]').getAttribute('data-session-id'),sessionId,'reopened_session_id_changed');
 const final2=await getDetail(page2,goalId,sessionId);
 assert.equal(final2.status,'COMPLETED');
 assert.equal(final2.question_banks[0].version,1);
 assert.equal(final2.report.questions,2);
 assert.equal(final2.report.answers,2);
 assert.equal(final2.report.observations.length,2);
 await page2.getByTestId('interview-report').waitFor({timeout:30000});
 mark('重开窗口并从历史打开原场次：相同 ID / 题库 v1 / COMPLETED 报告一致','native',
  {session_id:sessionId,status:final2.status,bank_version:final2.question_banks[0].version,
   questions:final2.report.questions,answers:final2.report.answers,observations:final2.report.observations.length});
 evidence.screenshots.push(win('Capture',child.pid!,['-OutFile',join(output,'07-reopened-after-restart.png')]));

 child.kill();
 await new Promise<void>(resolve=>{child.once('exit',()=>resolve());setTimeout(resolve,15000);});
 const afterAll=await preflight();
 mark('第二次关闭窗口后外部服务仍正常，页面无 JS 异常','check',{...afterAll,page_errors:pageErrors.length});

 // ---------------- Default D-00 probe entry must stay separate ----------------
 const d00Dir=join(output,'d00-run');mkdirSync(d00Dir,{recursive:true});
 const d00Env:any={...process.env,INTERVIEW_PROBE_OUTPUT:d00Dir};
 delete d00Env.INTERVIEW_DESKTOP_PREVIEW;
 delete d00Env.INTERVIEW_DESKTOP_PREVIEW_CDP_PORT;
 delete d00Env.INTERVIEW_PROBE_AUTORUN;
 const d00Fd=openSync(join(output,'d00-host.log'),'a');
 const d00Child=spawn(hostExe,[],{cwd:d00Dir,env:d00Env,stdio:['ignore',d00Fd,d00Fd]});
 children.push(d00Child);
 d00Child.on('exit',()=>{try{closeSync(d00Fd);}catch{}});
 let d00Info:any=null;
 for(let i=0;i<40;i++){
  try{d00Info=win('Info',d00Child.pid!);if(String(d00Info.title).includes('桌面接缝验证'))break;}catch{}
  await delay(400);
 }
 assert.ok(d00Info&&String(d00Info.title).includes('桌面接缝验证'),'d00_probe_window_missing');
 evidence.d00_probe_window=d00Info;
 evidence.screenshots.push(win('Capture',d00Child.pid!,['-OutFile',join(output,'10-d00-probe.png')]));
 mark('同一个 exe 不带预览变量时仍打开 D-00 技术探针窗口（默认入口未改变）','check',d00Info);
 d00Child.kill();
 await new Promise<void>(resolve=>{d00Child.once('exit',()=>resolve());setTimeout(resolve,15000);});

 evidence.result='passed';
 evidence.model={declared:'claude-opus-6',provider_calls:0,run_origin:'scripted_protocol',
  credits_balance:'unknown',credits_consumed:'unknown'};
 writeFileSync(join(output,'desktop-results.json'),JSON.stringify(evidence,null,2));
 console.log(JSON.stringify({result:'passed',cases:cases.length,session_id:sessionId,
  main_chain:evidence.main_chain,native_window:info1.title,screenshots:evidence.screenshots.length},null,2));
}catch(error){
 evidence.result='failed';
 evidence.error=String((error as Error)?.stack??error);
 writeFileSync(join(output,'desktop-results-failure.json'),JSON.stringify(evidence,null,2));
 for(const c of children){try{win('Capture',c.pid!,['-OutFile',join(output,`desktop-failure-${c.pid}.png`)]);}catch{}}
 console.error('D-01-02 preview acceptance FAILED:',(error as Error).message);
 process.exitCode=1;
}finally{
 for(const c of children)if(c.exitCode===null&&!c.killed)try{c.kill();}catch{}
}
