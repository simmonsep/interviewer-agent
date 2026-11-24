import assert from 'node:assert/strict';
import {chromium,expect,type Page} from '@playwright/test';
import {execFile} from 'node:child_process';
import {existsSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..'),output=process.env.PY_WEB_OUTPUT!,runId=process.env.PY_WEB_RUN_ID!;
assert.ok(output&&runId&&process.env.PY_WEB_PYTHON,'run scripts/check-python-web.py');
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
const browser=await chromium.launch({executablePath:executable,headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1100}});
await context.addInitScript(()=>{
 const records:any[]=[];(window as any).__streams=records;
 const Native=window.EventSource;
 class Tracked extends Native{
  record:any;
  constructor(url:string|URL,options?:EventSourceInit){
   super(url,options);this.record={url:String(url),closed:false,opens:0,errors:0,events:[]};records.push(this.record);
   this.addEventListener('open',()=>this.record.opens++);
   this.addEventListener('error',()=>this.record.errors++);
   for(const name of ['operation','auth-expired','stream-end'])this.addEventListener(name,(event)=>this.record.events.push({name,data:(event as MessageEvent).data,id:(event as MessageEvent).lastEventId}));
  }
  close(){this.record.closed=true;super.close();}
 }
 window.EventSource=Tracked;
});
const page=await context.newPage(),errors:string[]=[],cases:{name:string;status:string}[]=[],workers=new Set<ReturnType<typeof execFile>>();
page.on('pageerror',error=>errors.push(error.message));
const mark=(name:string)=>cases.push({name,status:'passed'});
const answer='参数格式通过不等于获得读取权限。\n\n服务端应从会话取得身份，校验资源归属，并在工具入口再次检查。';
const account={email:`pyweb-${runId}-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID()};
async function signup(p:Page,email=account.email,password=account.password){
 await p.getByRole('button',{name:'第一次来？创建账号'}).click();
 await p.getByLabel('称呼').fill('合成测试学员');await p.getByLabel('邮箱').fill(email);await p.getByLabel('密码').fill(password);
 await p.getByRole('button',{name:'创建账号',exact:true}).click();
}
function support(action:'worker'|'inspect',id:string,assessment='scripted',delayMs=0):Promise<any>{
 return new Promise((resolveResult,reject)=>{
  const child=execFile(process.env.PY_WEB_PYTHON!,['scripts/python-web-support.py',action,'--session',id,'--run-id',runId,'--assessment',assessment,'--delay-ms',String(delayMs)],{cwd:root,windowsHide:true,timeout:30000},(error,stdout,stderr)=>{
   workers.delete(child);if(error)return reject(new Error(`test_worker_failed: ${stderr.slice(-1200)}`));
   try{resolveResult(JSON.parse(stdout));}catch(error){reject(error);}
  });workers.add(child);
 });
}
const sessionId=()=>new URL(page.url()).searchParams.get('session')!;
async function submit(text=answer){
 await page.getByLabel('你的回答',{exact:true}).fill(text);
 const response=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/answers'));
 await page.getByRole('button',{name:'提交并查看反馈 →'}).click();
 assert.equal((await response).status(),202);
 await page.getByText('你的回答 · 已保存',{exact:true}).waitFor();
}
async function start(){
 await page.getByRole('button',{name:'返回练习首页'}).click();
 await page.getByRole('button',{name:'开始这一题 →'}).click();
 await page.getByLabel('你的回答',{exact:true}).waitFor();return sessionId();
}
let passed=false;
try{
 await page.goto('http://127.0.0.1:5174/?shell=web');await signup(page);
 await page.getByRole('heading',{name:'今天，先练好一题。'}).waitFor();
 assert.equal(await page.locator('input[type=file]').count(),0);
 assert.equal((await context.request.post('http://127.0.0.1:5174/api/auth/sign-out',{headers:{Origin:'http://foreign.invalid'},data:{}})).status(),403);
 await page.screenshot({path:join(output,'home.png'),fullPage:true});mark('W01 actual browser signup, role-only entry and foreign-Origin rejection through Vite proxy');
 await page.getByRole('button',{name:'开始这一题 →'}).click();await page.getByLabel('你的回答',{exact:true}).fill(answer);
 const firstURL=page.url(),firstId=sessionId();
 await page.reload();await page.getByLabel('你的回答',{exact:true}).waitFor();
 assert.equal(await page.getByLabel('你的回答',{exact:true}).inputValue(),answer);mark('W02 refresh restores SQL question and user/session scoped draft');
 let submissions=0;page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/answers'))submissions++;});
 await page.getByRole('button',{name:'提交并查看反馈 →'}).dblclick();
 await page.getByText('你的回答 · 已保存',{exact:true}).waitFor();await page.reload();await page.getByText('你的回答 · 已保存',{exact:true}).waitFor();
 assert.equal(submissions,1);assert.equal((await support('inspect',firstId)).answers,1);mark('W03 double click sends one POST; submitted original answer survives page reload');
 await expect.poll(()=>page.evaluate(()=>(window as any).__streams.some((r:any)=>r.events.some((e:any)=>e.name==='operation')))).toBe(true);
 const opened=await page.evaluate(()=>(window as any).__streams.reduce((n:number,r:any)=>n+r.opens,0));
 const dropped=await context.request.post('http://127.0.0.1:5174/__test/drop-sse',{headers:{Origin:'http://127.0.0.1:5174'}});
 assert.ok((await dropped.json()).dropped>=1);
 await expect.poll(()=>page.evaluate(()=>(window as any).__streams.some((r:any)=>r.errors>0)),{timeout:6000}).toBe(true);
 await expect.poll(()=>page.evaluate(()=>(window as any).__streams.reduce((n:number,r:any)=>n+r.opens,0)),{timeout:10000}).toBeGreaterThan(opened);
 await support('worker',firstId,'scripted',500);
 await page.getByRole('heading',{name:'回看这一题'}).waitFor({timeout:10000});
 await expect.poll(()=>page.evaluate(()=>(window as any).__streams.some((r:any)=>r.events.some((e:any)=>e.name==='operation'&&JSON.parse(e.data).status==='SUCCEEDED')))).toBe(true);
 const done=await support('inspect',firstId);assert.equal(done.status,'COMPLETED');assert.equal(done.revisions,1);
 mark('W04 real EventSource disconnect/reconnect and committed SUCCEEDED event; real LangGraph scripted worker persists one revision');
 const feedback=await page.locator('.feedback').innerText();await page.reload();await page.getByRole('heading',{name:'回看这一题'}).waitFor();
 assert.equal(await page.locator('.feedback').innerText(),feedback);await expect(page.locator('.notice')).toContainText('脚本流程演示');
 await page.screenshot({path:join(output,'feedback.png'),fullPage:true});mark('W05 final feedback, exact quotes and explicit scripted label persist on refresh');
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await page.screenshot({path:join(output,'mobile.png'),fullPage:true});await page.setViewportSize({width:1440,height:1100});mark('W06 390px mobile viewport has no horizontal overflow');
 const other=await browser.newContext(),otherPage=await other.newPage();await otherPage.goto(firstURL);
 await signup(otherPage,`pyweb-${runId}-${randomUUID()}@example.com`);await otherPage.getByRole('alert').filter({hasText:'不可用'}).waitFor();
 assert.equal(await otherPage.getByRole('heading',{name:'回看这一题'}).count(),0);
 for(const path of [`/api/practice-sessions/${firstId}`,`/api/operations/${(await context.request.get(`http://127.0.0.1:5174/api/practice-sessions/${firstId}`).then(r=>r.json())).operation.id}/events`]){
  assert.equal((await other.request.get('http://127.0.0.1:5174'+path)).status(),404);
 }
 await other.close();mark('W07 copied session URL and SSE endpoint reject a different real browser account');
 await page.getByRole('button',{name:'返回练习首页'}).click();await page.getByLabel('选择练习问题').selectOption({index:2});
 const selected=await page.locator('.question-preview').textContent();
 await page.route(`**/api/practice-sessions/${firstId}`,async route=>{const response=await route.fetch();await delay(650);await route.fulfill({response});});
 await page.locator('.history-item').first().click();await page.getByRole('button',{name:'开始这一题 →'}).click();await page.getByLabel('你的回答',{exact:true}).waitFor();await delay(850);
 assert.equal(await page.locator('.question-title').textContent(),selected);assert.notEqual(sessionId(),firstId);await page.unroute(`**/api/practice-sessions/${firstId}`);
 mark('W08 delayed old-session HTTP response cannot overwrite a newly created practice');
 const cancelId=sessionId();await submit('这是一条验证取消路径的合成回答。');
 const pending=support('worker',cancelId,'scripted',1800);
 // The promise may reject while another assertion is awaiting; retain it for the
 // later await without letting failure cleanup turn into an unhandled rejection.
 void pending.catch(()=>{});
 await expect.poll(async()=>(await context.request.get(`http://127.0.0.1:5174/api/practice-sessions/${cancelId}`).then(r=>r.json())).operation.status,{timeout:15000}).toBe('RUNNING');
 await page.getByRole('button',{name:'取消分析',exact:true}).click();await page.getByRole('heading',{name:'已取消分析，原回答仍保留'}).waitFor();
 assert.equal((await pending).revisions,0);await page.reload();await page.getByRole('heading',{name:'已取消分析，原回答仍保留'}).waitFor();
 assert.equal(await page.getByRole('heading',{name:'回看这一题'}).count(),0);mark('W09 real in-flight worker cancellation rejects late scripted feedback; cancelled snapshot survives refresh');
 const waitingId=await start();await submit();await support('worker',waitingId,'disabled');
 await page.getByRole('heading',{name:'回答已保存，等待模型可用'}).waitFor();
 await page.getByRole('button',{name:'重试获取反馈',exact:true}).click();await support('worker',waitingId);
 await page.getByRole('heading',{name:'回看这一题'}).waitFor();mark('W10 disabled executor waits; explicit browser resume preserves answer and completes with scripted executor');
 const revokeId=await start();await submit();
 await expect.poll(()=>page.evaluate(()=>(window as any).__streams.some((r:any)=>!r.closed&&r.opens>0))).toBe(true);
 assert.equal((await context.request.post('http://127.0.0.1:5174/api/auth/sign-out',{headers:{Origin:'http://127.0.0.1:5174'},data:{}})).status(),200);
 await page.getByRole('heading',{name:'欢迎回来'}).waitFor();
 assert.equal(await page.evaluate(()=>(window as any).__streams.some((r:any)=>r.events.some((e:any)=>e.name==='auth-expired'))),true);
 assert.equal((await context.request.get(`http://127.0.0.1:5174/api/practice-sessions/${revokeId}`)).status(),401);
 mark('W11 server-side logout revokes an already open SSE stream and removes authenticated UI');
 await page.getByLabel('邮箱').fill(account.email);await page.getByLabel('密码').fill(account.password);await page.getByRole('button',{name:'登录',exact:true}).click();
 await page.getByText('你的回答 · 已保存',{exact:true}).waitFor();
 await expect.poll(()=>page.evaluate(()=>(window as any).__streams.some((r:any)=>!r.closed&&r.opens>0))).toBe(true);
 await page.getByRole('button',{name:'退出登录',exact:true}).click();await page.getByRole('heading',{name:'欢迎回来'}).waitFor();
 assert.equal(await page.evaluate(()=>(window as any).__streams.every((r:any)=>r.closed)),true);
 assert.equal(await page.evaluate(()=>Object.keys(sessionStorage).filter(k=>k.startsWith('py-practice:')).length),0);
 assert.deepEqual(errors,[]);mark('W12 normal UI logout closes every EventSource, clears scoped drafts and yields zero page JS errors');
 passed=true;
}catch(error){
 writeFileSync(join(output,'failure.txt'),error instanceof Error?error.stack??String(error):String(error));
 await page.screenshot({path:join(output,'failure.png'),fullPage:true}).catch(()=>{});
 console.error(error instanceof Error?error.message:'browser_flow_failed');
}finally{
 for(const worker of workers)worker.kill();
 await browser.close();
 writeFileSync(join(output,'receipt.json'),JSON.stringify({stage:'PY-MIG-07',status:passed?'passed':'failed',browser:executable,browserVersion:browser.version(),cases,pageErrors:errors,modelCalls:0,scope:'built React, real FastAPI/PG/Redis and scoped independent Python scripted worker processes; test-only proxy destroys actual SSE response for disconnect injection; no real model or production deployment'},null,2)+'\n');
 console.log(JSON.stringify({status:passed?'passed':'failed',cases:cases.length}));process.exitCode=passed?0:1;
}
