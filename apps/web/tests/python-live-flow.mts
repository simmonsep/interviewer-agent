import assert from 'node:assert/strict';
import {chromium,expect} from '@playwright/test';
import {execFile} from 'node:child_process';
import {existsSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..'),output=process.env.PY_WEB_OUTPUT!,runId=process.env.PY_WEB_RUN_ID!;
assert.ok(output&&runId&&process.env.PY_WEB_PYTHON,'use scripts/check-python-live.py');
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
const browser=await chromium.launch({executablePath:executable,headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage();
const errors:string[]=[],events:any[]=[],cases:string[]=[];let child:ReturnType<typeof execFile>|undefined;
page.on('pageerror',e=>errors.push(e.message));
await context.addInitScript(()=>{
 (window as any).__liveEvents=[];
 const Original=window.EventSource;
 window.EventSource=class extends Original{
  constructor(url:string|URL,options?:EventSourceInit){super(url,options);this.addEventListener('operation',event=>(window as any).__liveEvents.push(JSON.parse((event as MessageEvent).data)));}
 };
});
let passed=false,billing:any=null;
try{
 await page.goto('http://127.0.0.1:5174/?shell=web');await page.getByRole('button',{name:'第一次来？创建账号'}).click();
 await page.getByLabel('称呼').fill('真实模型合成测试');await page.getByLabel('邮箱').fill(`pyweb-${runId}-${randomUUID()}@example.com`);
 await page.getByLabel('密码').fill('Synthetic-password-'+randomUUID());await page.getByRole('button',{name:'创建账号',exact:true}).click();
 await page.getByRole('heading',{name:'今天，先练好一题。'}).waitFor();await expect(page.locator('.notice')).toContainText('AI反馈预览');
 await page.getByRole('button',{name:'开始这一题 →'}).click();
 await page.getByLabel('你的回答',{exact:true}).waitFor();
 const id=new URL(page.url()).searchParams.get('session')!;
 const answer='参数通过JSON Schema，只说明参数形状符合约定，不表示当前用户获得了文档读取权限。\n我会先从可信登录会话确认用户身份，再查文档是否属于该用户或允许其访问，校验文档版本与请求范围。\n工具入口还要使用服务端绑定的用户和白名单，不接受模型自报身份。返回内容也要限制范围，并记录操作结果。';
 await page.getByLabel('你的回答',{exact:true}).fill(answer);await page.getByRole('button',{name:'提交并查看反馈 →'}).click();
 await page.getByText('你的回答 · 已保存',{exact:true}).waitFor();cases.push('real browser signup and original answer persisted before model request');
 billing=await new Promise((resolveResult,reject)=>{
  child=execFile(process.env.PY_WEB_PYTHON!,['scripts/python-web-support.py','worker','--session',id,'--run-id',runId,'--live'],
   {cwd:root,windowsHide:true,timeout:205000},(error,stdout,stderr)=>{
    child=undefined;
    if(error)return reject(new Error('scoped_live_worker_failed: '+stderr.slice(-1200)));
    try{resolveResult(JSON.parse(stdout));}catch(error){reject(error);}
   });
 });
 writeFileSync(join(output,'billing-operation.json'),JSON.stringify(billing,null,2)+'\n');
 assert.equal(billing.status,'COMPLETED');assert.equal(billing.revisions,1);assert.equal(billing.billing.usage_status,'known');
 assert.ok(billing.billing.http_attempts>=2&&billing.billing.http_attempts<=6);
 assert.ok(billing.billing.accounted_upper_nano>0&&billing.billing.accounted_upper_nano<=2_000_000_000);
 await page.getByRole('heading',{name:'回看这一题'}).waitFor({timeout:10000});
 await expect.poll(()=>page.evaluate(()=>(window as any).__liveEvents.some((e:any)=>e.status==='SUCCEEDED'))).toBe(true);
 const saved=await context.request.get(`http://127.0.0.1:5174/api/practice-sessions/${id}`).then(r=>r.json());
 assert.equal(saved.answer.text,answer);assert.equal(saved.feedback.origin,'real_model');assert.equal(saved.feedback.provider,'deepseek-flash');
 assert.equal(saved.feedback.semantic_scoring_eligible,false);assert.equal(saved.feedback.citations.some((c:any)=>c.kind==='answer'),true);
 assert.equal(saved.feedback.citations.some((c:any)=>c.kind==='evidence'),true);
 cases.push('real DeepSeek through real SDK/LangGraph, delivered-window citation Gate and SQL final feedback; no formal semantic score');
 writeFileSync(join(output,'feedback.json'),JSON.stringify(saved,null,2)+'\n');
 await page.screenshot({path:join(output,'feedback.png'),fullPage:true});
 await page.reload();await page.getByRole('heading',{name:'回看这一题'}).waitFor();
 const restored=await context.request.get(`http://127.0.0.1:5174/api/practice-sessions/${id}`).then(r=>r.json());
 assert.deepEqual(restored.feedback,saved.feedback);assert.equal(restored.answer.text,answer);
 assert.deepEqual(errors,[]);cases.push('browser reload restores identical SQL feedback and original answer; zero page errors');
 events.push(...await page.evaluate(()=>(window as any).__liveEvents));
 await page.getByRole('button',{name:'退出登录',exact:true}).click();await page.getByRole('heading',{name:'欢迎回来'}).waitFor();
 passed=true;
}catch(error){
 writeFileSync(join(output,'failure.txt'),error instanceof Error?error.stack??error.message:String(error));
 await page.screenshot({path:join(output,'failure.png'),fullPage:true}).catch(()=>{});
 console.error(error instanceof Error?error.message:'live_browser_failed');
}finally{
 child?.kill();await browser.close();
 writeFileSync(join(output,'receipt.json'),JSON.stringify({stage:'PY-MIG-08-live',status:passed?'passed':'failed',browser:executable,
  browserVersion:browser.version(),cases,pageErrors:errors,operationBilling:billing?.billing??null,
  scope:'one synthetic real browser practice with authorized real DeepSeek; no benchmark or semantic-quality validation'},null,2)+'\n');
 console.log(JSON.stringify({status:passed?'passed':'failed',cases:cases.length}));process.exitCode=passed?0:1;
}
