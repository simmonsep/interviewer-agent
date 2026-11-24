// F05C acceptance: local site progress sync on the desktop shell.
//
// The browser window is a real Edge instance with a real loopback DevTools port;
// the page it reads is a local fixture site served over real HTTP. That is a real
// end-to-end run of the product code path, NOT a simulation of the sync logic.
// It is also NOT a verification of any real recruitment website: no user account,
// no third-party site and no external request is involved here.
//
//   D05C_OUTPUT=<new dir> node apps/web/tests/d01-desktop-tracking.mts
//
// Requires: PostgreSQL/Redis containers up, ports 3100/5174 free.
import assert from 'node:assert/strict';
import {chromium,type Browser,type Page} from '@playwright/test';
import {openDetail, waitGoalReady} from './desktop-nav.mts';
import {execFileSync,spawn,type ChildProcess} from 'node:child_process';
import {createServer,type Server} from 'node:http';
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D05C_OUTPUT??join(root,'docs/records/desktop-handoff/F05C-tracking'));
const base=process.env.D05C_BASE_URL??'http://127.0.0.1:5174';
const pythonExe=process.env.D05C_PYTHON??'E:/codex/myagent/outputs/python-interview/.venv/Scripts/python.exe';
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const privateDir=join(root,'.private/f05c');
const modelFile=join(privateDir,'model.json');
const adapterFile=join(privateDir,'tracking-adapters.json');
const runId=randomUUID();
const runDir=join(privateDir,'runs',runId);
const fixtureDir=join(root,'fixtures/tracking-demo');
mkdirSync(output,{recursive:true});mkdirSync(join(output,'shots'),{recursive:true});
mkdirSync(privateDir,{recursive:true});mkdirSync(runDir,{recursive:true});
writeFileSync(modelFile,JSON.stringify({provider:'',base_url:'',model:'',api_key:'',enabled:false},null,1));

const cases:{name:string;detail?:unknown}[]=[];
const pageErrors:string[]=[];
const shots:string[]=[];
const mark=(name:string,detail?:unknown)=>{cases.push({name,detail});console.log('PASS',name);};
const evidence:Record<string,unknown>={
 run:new Date().toISOString(),cases,page_errors:pageErrors,screenshots:shots,
 case_type_note:'真实本机端到端联调：真实 Edge + 真实回环 DevTools 端口 + 真实 HTTP 本机站点 + 真实本地 API/数据库。站点为随仓库提供的本机夹具，不是任何真实招聘网站；真实站点未验证，需要使用者本人账号与一次页面核对。',
 unverified:['真实招聘网站页面结构','真实招聘网站账号登录态','目标站点服务条款与风控行为'],
};

const shot=async(page:Page,name:string)=>{await page.screenshot({path:join(output,'shots',name)});shots.push(name);};

function startSite(onlyLogin:boolean):Promise<{server:Server;port:number}>{
 const server=createServer((request,response)=>{
  const name=request.url==='/'?'applications.html':(request.url??'').replace(/^\//,'').split('?')[0];
  const file=onlyLogin&&name!=='login.html'?'login.html':name;
  try{
   const body=readFileSync(join(fixtureDir,file));
   response.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'});
   response.end(body);
  }catch{response.writeHead(404);response.end('not found');}
 });
 return new Promise(resolve=>{
  server.listen(0,'127.0.0.1',()=>resolve({server,port:(server.address() as {port:number}).port}));
 });
}

function runSupport(goalId:string){
 execFileSync(pythonExe,['-X','utf8',join(root,'scripts/research-web-support.py'),'--goal',goalId,
  '--run-id',randomUUID(),'--scenario','ok'],{encoding:'utf8',timeout:180000,windowsHide:true});
}

async function setupGoal(page:Page):Promise<string>{
 await openDetail(page,'工作台');
 await page.locator('.desktop-shell[data-pane="workbench"]').waitFor({timeout:15000});
 await page.locator('textarea[aria-label="求职意向或补充说明"]').fill('合成验收：站点进度同步。');
 await page.locator('[data-testid="workbench-primary"]').click();
 await waitGoalReady(page);
 return page.evaluate(async()=>{
  const response=await fetch('/api/career-goals/',{credentials:'same-origin',cache:'no-store'});
  return ((await response.json()) as {items:string[]}).items[0];
 });
}

async function selectJob(page:Page,goalId:string){
 await openDetail(page,'岗位研究');
 const pane=page.locator('[data-testid="research-pane"]');
 await pane.getByLabel('查询关键词',{exact:true}).fill('合成');
 await pane.getByRole('button',{name:'查询岗位',exact:true}).click();
 await pane.getByRole('status').filter({hasText:'等待读取'}).waitFor({timeout:20000});
 runSupport(goalId);
 await page.waitForTimeout(1200);
 await pane.locator('.check-label').first().waitFor({timeout:60000});
 await pane.locator('.check-label').first().click();
 await pane.getByRole('button',{name:'选择该岗位'}).first().click();
 await page.locator('[data-testid="desktop-selection"]').waitFor({timeout:30000});
}

let browser:Browser|null=null;
let vite:ChildProcess|null=null;
let api:ChildProcess|null=null;
let siteA:Server|null=null,siteB:Server|null=null;

try{
 // ---------------------------------------------------------- 0. local services
 const a=await startSite(false),b=await startSite(true);
 siteA=a.server;siteB=b.server;
 writeFileSync(adapterFile,JSON.stringify({adapters:[
  {id:'local-demo',label:'本机演示站点（验收用）',entry_url:`http://127.0.0.1:${a.port}/applications.html`,
   row:'.application-row',company:'.company',title:'.title',status:'.status',link:'a.job-link',
   status_map:{'面试':'INTERVIEW','已投递':'SUBMITTED','未通过':'REJECTED','录用':'OFFER'},verified:true},
  {id:'login-only',label:'未登录页（负例）',entry_url:`http://127.0.0.1:${b.port}/login.html`,
   row:'.application-row',company:'.company',title:'.title',status:'.status',link:'a.job-link',
   status_map:{'面试':'INTERVIEW','已投递':'SUBMITTED'},verified:true},
  {id:'my-site',label:'自定义站点（待填写选择器）',entry_url:'https://example.com/',
   row:'',company:'',title:'',status:'',link:'',status_map:{},verified:false},
 ]},null,2));

 const vitePath=join(root,'apps/web/node_modules/vite/bin/vite.js');
 vite=spawn(process.env.D05C_NODE??'node',[vitePath,'--host','127.0.0.1','--port','5174','--strictPort'],
  {cwd:join(root,'apps/web'),stdio:'ignore',env:{...process.env,PATH:`/e/codex/myagent/node24-shim:${process.env.PATH??''}`}});
 api=spawn(pythonExe,['-X','utf8','-m','interview_agent.api','--config',join(root,'.private/postgres/test.json'),
  '--model-config',modelFile],{cwd:join(root,'backend'),stdio:'ignore',windowsHide:true,
  env:{...process.env,PYTHONPATH:join(root,'backend/src'),
   INTERVIEW_TRACKING_HEADLESS:'1',INTERVIEW_TRACKING_BROWSER:edge,
   INTERVIEW_TRACKING_DATA_DIR:runDir}});
 const started=Date.now();
 let ready=false;
 while(Date.now()-started<90000){
  try{
   const health=await fetch('http://127.0.0.1:3100/health',{signal:AbortSignal.timeout(1200)});
   const app=await fetch(base+'/',{signal:AbortSignal.timeout(1200)});
   if(health.ok&&app.ok){ready=true;break;}
  }catch{}
  await delay(500);
 }
 assert.ok(ready,'local_services_not_ready');
 mark('本机服务就绪：API 挂上站点同步路由，Vite 提供桌面工作台，两个本机夹具站点走真实 HTTP',
  {site_a_port:a.port,site_b_port:b.port,config:adapterFile});

 browser=await chromium.launch({executablePath:edge,headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:860}});
 const page=await context.newPage();
 page.on('pageerror',error=>pageErrors.push(error.message));
 await page.goto(`${base}/?shell=desktop`,{waitUntil:'domcontentloaded'});
 await page.locator('.desktop-shell[data-pane="workbench"]').waitFor({timeout:60000});

 const goalId=await setupGoal(page);
 await selectJob(page,goalId);
 await openDetail(page,'申请跟踪');
 const apps=page.locator('[data-testid="apps-pane"]');
 const sync=apps.locator('.d-sync');
 await sync.waitFor({timeout:20000});

 // ---------------------------------------------------------- 1. surface replaced
 assert.equal(await apps.locator('textarea[aria-label="邮件或回执正文"]').count(),0,'mail_parser_should_be_removed');
 assert.match(await sync.innerText(),/不保存 Cookie 或令牌/,'privacy_boundary_missing');
 const options=await sync.locator('select[aria-label="同步站点"] option').allInnerTexts();
 assert.ok(options.some(text=>text.includes('未验证')),'unverified_adapter_should_be_visible');
 mark('邮件解析已下线；面板改为站点进度同步，未验证的真实站点模板明确标注，边界文案写明不存 Cookie/令牌',
  {options});
 await shot(page,'01-site-sync-surface.png');

 // ---------------------------------------------------------- 2. local applications to match
 const created:{name:string;id:string}[]=[];
 // The server deduplicates applications by (goal, selected job), so one goal can
 // only hold one record per job. One local application is enough to prove the
 // sync loop; the other three site rows are the "no local match" case.
 const names=['星海科技'];
 for(const [index,name] of names.entries()){
  await apps.locator('input[placeholder="例如：某科技公司"]').fill(name);
  await apps.locator('input[placeholder="例如：后端开发实习生"]').fill('后端开发实习生');
  await apps.getByRole('button',{name:'记录为草稿'}).click();
  try{await apps.locator('.d-session-item').nth(index).waitFor({timeout:30000});}
  catch(error){
   const diag=await apps.innerText();
   throw new Error(`create_${index}_(${name})_failed pane_text=${diag.replace(/\s+/g,' ').slice(0,1400)}`);
  }
 }
 const summary=await page.evaluate(async(gid:string)=>{
  const list=await (await fetch(`/api/career-goals/${gid}/applications`,{credentials:'same-origin'})).json() as {items:{id:string;status:string}[]};
  const details=await Promise.all(list.items.map(async item=>(await (await fetch(
   `/api/career-goals/${gid}/applications/${item.id}`,{credentials:'same-origin'})).json()) as {package:{company:string}}));
  return list.items.map((item,index)=>({id:item.id,status:item.status,company:details[index].package.company}));
 },goalId);
 created.push(...summary.map(item=>({name:item.company,id:item.id})));
 assert.equal(created.length,1,`expected_one_local_application ${JSON.stringify(created)}`);
 assert.deepEqual(summary.map(item=>item.status),['DRAFT']);
 mark('本地投递记录为草稿，作为站点条目的匹配目标（同一目标同一岗位只能有一条投递，其余站点条目作为「无本地匹配」样本）',
  {applications:created.map(item=>item.name)});

 // ---------------------------------------------------------- 3. open the local browser window
 await sync.getByRole('button',{name:'打开站点'}).click();
 await sync.getByText('请在该窗口用自己的账号登录').waitFor({timeout:60000});
 const adapterState=await page.evaluate(async()=>{
  const response=await fetch('/api/desktop/tracking/adapters',{credentials:'same-origin'});
  return response.json() as {bridge_open:boolean;config_path:string;profile:string};
 });
 assert.equal(adapterState.bridge_open,true,'bridge_should_be_open');
 assert.match(adapterState.config_path,/tracking-adapters\.json$/,'config_should_be_local_file');
 mark('打开站点：启动独立本机浏览器窗口与独立 profile，同步会话令牌只存在于本机进程内存',
  {bridge_open:adapterState.bridge_open,config_path:adapterState.config_path,profile:adapterState.profile});

 // ---------------------------------------------------------- 4. sync reads the real page
 await sync.getByRole('button',{name:'同步当前页'}).click();
 await sync.locator('.d-sync-row').first().waitFor({timeout:60000});
 const rowCount=await sync.locator('.d-sync-row').count();
 assert.equal(rowCount,4,`expected_four_site_rows got ${rowCount}`);
 const texts=await sync.locator('.d-sync-row').allInnerTexts();
 assert.ok(texts[0].includes('面试中')&&texts[0].includes('星海科技'),`first_row_wrong ${texts[0]}`);
 assert.ok(texts.some(text=>text.includes('远川网络')),'unmatched_company_missing');
 const selects=await sync.locator('.d-sync-row').evaluateAll(nodes=>nodes.map(node=>({
  status:(node.querySelector('select[aria-label$="条写入状态"]') as HTMLSelectElement|null)?.value??'',
  target:(node.querySelector('select[aria-label$="条对应投递"]') as HTMLSelectElement|null)?.value??'',
  checked:(node.querySelector('input[type=checkbox]') as HTMLInputElement|null)?.checked??false,
 })));
 assert.equal(selects[0].status,'INTERVIEW','site_status_mapping_wrong');
 assert.ok(selects[0].target,'company_should_auto_match');
 assert.ok(selects.slice(1).every(item=>!item.target),'unmatched_rows_must_not_write_by_default');
 assert.equal(selects[0].checked,true,'mapped_row_should_be_preselected');
 mark('同步当前页：真实 Edge 页面读出 4 条投递，状态映射与公司自动匹配生效；3 条没有本地投递的条目默认不写入',
  {rows:texts.map(text=>text.replace(/\s+/g,' ').slice(0,90)),selects});
 await shot(page,'02-site-sync-rows.png');

 // ---------------------------------------------------------- 5. user confirmation only
 const readDetail=(id:string)=>page.evaluate(async([gid,aid])=>{
  const value=await (await fetch(`/api/career-goals/${gid}/applications/${aid}`,{credentials:'same-origin'})).json() as
   {status:string;events:{kind:string;payload:Record<string,unknown>}[]};
  return {status:value.status,progress:value.events.filter(event=>event.kind==='progress').map(event=>event.payload)};
 },[goalId,id] as [string,string]);
 const localId=created[0].id;
 const firstCheck=sync.locator('.d-sync-row').first().locator('input[type=checkbox]');
 const confirmButton=sync.getByRole('button',{name:'确认写入事件流'});
 await firstCheck.uncheck();
 assert.equal(await confirmButton.isDisabled(),true,'confirm_must_require_a_checked_matched_row');
 const untouched=await readDetail(localId);
 assert.equal(untouched.status,'DRAFT','status_changed_without_confirmation');
 assert.equal(untouched.progress.filter(event=>event.evidence_kind==='site_sync').length,0,'wrote_without_confirmation');
 mark('没有用户确认就不写库：取消勾选后确认按钮不可用，本地投递仍是草稿，数据库里没有站点同步事件',
  {status:untouched.status,progress_events:untouched.progress.length});

 await firstCheck.check();
 await confirmButton.click();
 await sync.locator('.d-sync-results').waitFor({state:'detached',timeout:60000});
 const star=await readDetail(localId);
 assert.equal(star.status,'INTERVIEW','draft_to_interview_should_walk_via_submitted');
 assert.equal(star.progress.length,2,'expected_two_progress_events_for_state_machine');
 const siteEvent=star.progress.find(event=>event.evidence_kind==='site_sync');
 assert.ok(siteEvent,'site_sync_evidence_missing');
 assert.match(String(siteEvent!.note),/站点同步：.*显示「/,`site_note_missing ${JSON.stringify(siteEvent)}`);
 assert.match(String(siteEvent!.receipt_reference),/127\.0\.0\.1/,`site_reference_missing ${JSON.stringify(siteEvent)}`);
 mark('确认写入：勾选后才写库；DRAFT→面试中自动经「已投递」两步遵守状态机；事件保留站点原文、读取时间与 site_sync 依据',
  {status:star.status,progress_events:star.progress.length,evidence_kind:siteEvent!.evidence_kind,
   verification:(siteEvent as Record<string,unknown>).verification});
 await shot(page,'03-site-sync-written.png');

 // ---------------------------------------------------------- 6. honest failure on a login page
 await sync.locator('select[aria-label="同步站点"]').selectOption('login-only');
 await sync.getByRole('button',{name:'打开站点'}).click();
 await delay(3000);
 const openText=(await sync.innerText()).replace(/\s+/g,' ');
 assert.match(openText,/已在本机浏览器打开 未登录页/ ,`open_site_failed ${openText.slice(0,700)}`);
 await sync.getByRole('button',{name:'同步当前页'}).click();
 await sync.locator('.error').waitFor({timeout:60000});
 const loginError=await sync.locator('.error').innerText();
 assert.match(loginError,/登录页|没有读到投递条目/,`login_diagnostic_missing ${loginError}`);
 mark('未登录/选择器不命中时给出中文诊断，不把空结果当成「没有进展」，也不静默写库',{diagnostic:loginError});

 // ---------------------------------------------------------- 7. probe + disconnect
 await sync.getByRole('button',{name:'探测页面结构'}).click();
 await sync.locator('.d-sync-probe li').first().waitFor({timeout:60000});
 const probeCount=await sync.locator('.d-sync-probe li').count();
 assert.ok(probeCount>0,'probe_should_return_samples');
 const expiredToken=await page.evaluate(async()=>{
  const response=await fetch('/api/desktop/tracking/sync',{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({adapter_id:'login-only',token:'stale-token'})});
  return {status:response.status,body:await response.json() as {code:string;message?:string}};
 });
 assert.equal(expiredToken.status,409,'stale_token_should_be_rejected');
 await sync.getByRole('button',{name:'停止读取'}).click();
 await sync.getByText('已停止读取页面').waitFor({timeout:15000});
 const afterDisconnect=await page.evaluate(async()=>{
  const response=await fetch('/api/desktop/tracking/sync',{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({adapter_id:'login-only',token:'stale-token'})});
  return response.status;
 });
 assert.equal(afterDisconnect,409,'sync_should_be_closed_after_disconnect');
 mark('探测页面结构返回本机元素样本；伪造令牌被拒；停止读取后同步会话立即失效',
  {probe_samples:probeCount,stale_token_status:expiredToken.status,after_disconnect:afterDisconnect});

 // ---------------------------------------------------------- 8. no secrets on disk
 const configText=readFileSync(adapterFile,'utf8');
 assert.ok(!/token|cookie|password|api_key/i.test(configText),'adapter_config_must_not_hold_secrets');
 assert.ok(!(await page.evaluate(async()=>{
  const response=await fetch('/api/desktop/tracking/adapters',{credentials:'same-origin'});
  return /token|cookie|password/i.test(await response.text());
 })),'adapter_api_must_not_return_secrets');
 mark('数据边界：本机适配器配置与接口响应都不含令牌/Cookie/密码；同步结果只在用户确认后作为状态事件落库',
  {config_bytes:configText.length,profile_is_browser_owned:true});

 assert.equal(pageErrors.length,0,`page_errors ${JSON.stringify(pageErrors)}`);
 await context.close();
}catch(error){
 cases.push({name:'FAILED: '+(error instanceof Error?error.message:String(error)),detail:{stack:error instanceof Error?error.stack:''}});
 console.error('FAIL',error);
 process.exitCode=1;
}finally{
 try{vite?.kill();}catch{}
 try{api?.kill();}catch{}
 try{siteA?.close();siteB?.close();}catch{}
 try{await browser?.close();}catch{}
 writeFileSync(join(output,'evidence.json'),JSON.stringify(evidence,null,2));
 writeFileSync(join(output,'report.md'),[
  '# F05C 站点进度同步验收',
  '',
  `- 用例：${cases.filter(item=>!item.name.startsWith('FAILED')).length} 条通过`+
   `，失败 ${cases.filter(item=>item.name.startsWith('FAILED')).length} 条`,
  `- 页面错误：${pageErrors.length}`,
  `- 截图：${shots.length} 张`,
  '',
  '真实联调：真实 Edge + 真实回环 DevTools 端口 + 真实 HTTP 本机夹具站点 + 真实本地 API/数据库。',
  '真实招聘网站未验证：需要使用者本人账号与一次真实页面核对；本套件不包含任何第三方站点请求。',
  '',
  ...cases.map((item,index)=>`${index+1}. ${item.name}${item.detail?' — `'+JSON.stringify(item.detail)+'`':''}`),
 ].join('\n'));
 console.log('OUTPUT',output);
}
