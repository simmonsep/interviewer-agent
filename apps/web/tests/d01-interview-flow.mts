// D-01-01 final browser regression: real local API (3100), scripted interview Worker,
// Vite proxy (5174). Run with all three services running:
//   node apps/web/tests/d01-interview-flow.mts
// 409 / network-failure / expiry / FAILED / 401 cases are injected with page.route and
// are explicitly reported as MOCK; the two-question main path is a real end-to-end run.
// F07Z-147: obsolete as written. This suite starts from `/` and expects the practice sign-up
// screen, but since F07Z-137 `/` is the desktop workbench, so it cannot even reach its first
// step. The current web-shell review is covered by practice-shell-review.mts, and the desktop
// flow by f07z143-review.mts. Set D01_LEGACY_WEB_SHELL=1 to run the rest of it against a page
// that still has the practice entry.
import assert from 'node:assert/strict';
assert.ok(process.env.D01_LEGACY_WEB_SHELL, 'obsolete: use practice-shell-review.mts / f07z143-review.mts');
import {chromium,type Page} from '@playwright/test';
import {readFileSync,writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/evidence/D-01-01'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const fixture=JSON.parse(readFileSync(join(root,'fixtures/question-banks/agent-developer-trial.json'),'utf8'));
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
mkdirSync(output,{recursive:true});

const browser=await chromium.launch({executablePath:executable,headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1100}});
const page=await context.newPage();
const cases:{name:string;status:string;type:'real'|'mock';detail?:unknown}[]=[];
const pageErrors:string[]=[];
page.on('pageerror',error=>pageErrors.push(error.message));
const mark=(name:string,type:'real'|'mock'='real',detail?:unknown)=>cases.push({name,status:'passed',type,detail});
const account={email:`d01-flow-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID()};
const answer1='图内 state 是当前线程的运行状态，checkpointer 按 thread_id 保存检查点，store 放跨线程的偏好等长期信息。独立会话要用不同 thread_id；进程重启后要继续，需要持久化 checkpointer 与 store，内存实现不行。';
const answer2='interrupt 借助持久化检查点与 thread_id 暂停，用 Command(resume=...) 恢复。恢复会从包含 interrupt 的节点开头重跑，之前节点的副作用要幂等或移到单独节点；写入放在用户明确批准之后，并用业务幂等键防止重复写入。';

async function getDetail(p:Page,goalId:string,sessionId:string):Promise<any>{
 const result=await p.evaluate(async(url)=>{
  const response=await fetch(url,{credentials:'same-origin',cache:'no-store'});
  return {status:response.status,body:await response.json()};
 },`/api/career-goals/${goalId}/interviews/${sessionId}`);
 assert.equal(result.status,200,`detail ${result.status} ${JSON.stringify(result.body)}`);
 return result.body;
}
async function waitForDetail(p:Page,goalId:string,sessionId:string,check:(body:any)=>boolean,timeout=60000):Promise<any>{
 const start=Date.now();
 while(Date.now()-start<timeout){
  const body=await getDetail(p,goalId,sessionId).catch(()=>null);
  if(body&&check(body))return body;
  await delay(500);
 }
 throw new Error('waitForDetail_timeout');
}
async function waitForStatus(p:Page,label:string){
 await p.getByTestId('interview-status').filter({hasText:`状态 · ${label}`}).waitFor({timeout:90000});
}
async function importBank(p:Page):Promise<{bank_id:string;version:number;preview_id:string}>{
 return p.evaluate(async(document)=>{
  const previewResponse=await fetch('/api/question-banks/imports',{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({document})});
  const preview=await previewResponse.json();
  if(!previewResponse.ok)throw new Error('preview_failed '+JSON.stringify(preview));
  const commitResponse=await fetch(`/api/question-banks/imports/${preview.preview_id}/commit`,{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({})});
  const commit=await commitResponse.json();
  if(!commitResponse.ok)throw new Error('commit_failed '+JSON.stringify(commit));
  return {preview_id:preview.preview_id,bank_id:commit.bank_id,version:commit.version};
 },fixture);
}
async function setupAccount(p:Page){
 await p.goto(base,{waitUntil:'domcontentloaded'});
 await p.getByRole('button',{name:'第一次来？创建账号'}).click();
 await p.getByLabel('称呼').fill('合成测试学员');
 await p.getByLabel('邮箱').fill(account.email);
 await p.getByLabel('密码').fill(account.password);
 await p.getByRole('button',{name:'创建账号',exact:true}).click();
 await p.getByRole('button',{name:'整场面试'}).waitFor();
 await p.getByRole('button',{name:'整场面试'}).click();
 await p.getByLabel('求职意向或补充说明').fill('想在上海找 AI 应用 / Agent 开发实习，用两题练习检验表达。');
 await p.getByRole('button',{name:'保存求职目标'}).click();
 await p.locator('.goal-card').waitFor({timeout:20000});
}
async function selectBankAndStart(p:Page,bank:{bank_id:string;version:number}){
 await p.getByLabel('题库与版本').locator('option',{hasText:'Agent开发岗位试运行'}).waitFor({state:'attached',timeout:20000});
 await p.getByLabel('题库与版本').selectOption(`${bank.bank_id}|${bank.version}`);
 await p.getByRole('button',{name:'开始整场面试 →'}).click();
 await p.locator('[data-session-id]').waitFor({timeout:20000});
 await p.getByTestId('interview-question').waitFor({timeout:90000});
 await waitForStatus(p,'可作答');
}
async function mockDetail(mutate:(body:any)=>any){
 await page.route('**/api/career-goals/*/interviews/*',async route=>{
  if(route.request().method()!=='GET')return route.continue();
  const response=await route.fetch();
  const body=await response.json();
  return route.fulfill({response,json:mutate(body)});
 });
}

try{
 // ---------- real main path ----------
 await setupAccount(page);
 mark('登录、进入整场面试入口并创建求职目标（真实本机 API）');
 const bank=await importBank(page);
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByRole('button',{name:'整场面试'}).waitFor();
 const goalId=new URL(page.url()).searchParams.get('goal');
 assert.ok(goalId,'goal_id_missing');
 await selectBankAndStart(page,bank);
 const sessionId=await page.locator('[data-session-id]').getAttribute('data-session-id');
 assert.ok(sessionId,'session_id_missing');
 const firstQuestion=await page.getByTestId('interview-question').innerText();
 const initial=await getDetail(page,goalId,sessionId);
 assert.equal(initial.status,'WAITING_FOR_USER');
 assert.equal(initial.question_banks[0].version,1);
 assert.equal(initial.policy.max_questions,2);
 assert.equal(initial.report,null);
 assert.deepEqual(initial.observations,[]);
 mark('真实开场：首题 / WAITING_FOR_USER / 冻结题库 v1 / 考中反馈隐藏',undefined,
  {session_id:sessionId,version:initial.version,deadline_at:initial.deadline_at,question_event_id:initial.events[0].id});

 const submitButton=page.getByRole('button',{name:'提交这一答 →'});
 assert.equal(await submitButton.isDisabled(),true,'empty_answer_must_be_disabled');
 mark('空答时提交按钮禁用（本地规则：非空且最多 1500 字）');
 await page.getByLabel('你的整场面试回答').fill(answer1);
 await submitButton.click();
 await submitButton.click({force:true,timeout:1500}).catch(()=>{});
 await page.waitForFunction(previous=>{
  const element=document.querySelector('[data-testid="interview-question"]');
  return !!element&&element.textContent!==previous;
 },firstQuestion,{timeout:90000});
 await waitForStatus(page,'可作答');
 const afterFirst=await getDetail(page,goalId,sessionId);
 assert.equal(afterFirst.events.filter((e:any)=>e.kind==='answer').length,1,'duplicate_answer_written');
 assert.equal(afterFirst.events.filter((e:any)=>e.kind==='question').length,2);
 assert.equal(afterFirst.report,null);
 assert.deepEqual(afterFirst.observations,[]);
 assert.equal(await page.getByTestId('interview-report').count(),0);
 await page.screenshot({path:join(output,'03-second-question.png'),fullPage:true});
 mark('第一答后第二题出现，重复点击只写入 1 答，考中观察与报告保持隐藏',undefined,
  {answers:1,questions:2,second_question_event_id:afterFirst.events.filter((e:any)=>e.kind==='question').at(-1).id});

 await page.getByLabel('你的整场面试回答').fill(answer2);
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await page.getByTestId('interview-report').waitFor({timeout:90000});
 await waitForStatus(page,'已结束');
 const finalDetail=await getDetail(page,goalId,sessionId);
 assert.equal(finalDetail.status,'COMPLETED');
 assert.equal(finalDetail.report.questions,2);
 assert.equal(finalDetail.report.answers,2);
 assert.equal(finalDetail.report.observations.length,2);
 assert.equal(finalDetail.report.stop_reason,'question_limit');
 assert.deepEqual(finalDetail.report.unreviewed_answer_ids,[]);
 assert.ok(finalDetail.report.observations.every((o:any)=>o.origin==='scripted_protocol'),'scripted_origin_missing');
 assert.ok(finalDetail.report.observations.every((o:any)=>!!o.evidence_source?.url),'evidence_source_missing');
 const reportText=await page.getByTestId('interview-report').innerText();
 assert.ok(reportText.includes('题数已达到本场上限'));
 // The report shows the user-facing wording; the raw `scripted_protocol` code is asserted
 // above through the API and deliberately never printed in the interface (F07Z-61 copy).
 assert.ok(reportText.includes('脚本流程'),reportText.slice(0,200));
 await page.screenshot({path:join(output,'04-report.png'),fullPage:true});
 mark('终态 COMPLETED：2 题 / 2 答 / 2 条脚本观察 / 参考来源 / 结束原因',undefined,
  {status:finalDetail.status,stop_reason:finalDetail.report.stop_reason,version:finalDetail.version,
   model_summary:finalDetail.report.model_summary,observations:finalDetail.report.observations.map((o:any)=>({
    label:o.label,origin:o.origin,evidence_id:o.evidence_id,url:o.evidence_source?.url}))});

 // ---------- history reopen and reload ----------
 await page.getByRole('button',{name:'另开一场'}).click();
 await page.locator('[data-session-id]').waitFor({state:'detached',timeout:10000});
 await page.getByRole('button',{name:/场次 1 ·/}).first().click();
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),sessionId);
 assert.ok((await page.getByTestId('interview-report').innerText()).includes('题数已达到本场上限'));
 await page.screenshot({path:join(output,'05-history-reopen.png'),fullPage:true});
 mark('从该目标的会话列表重新打开原场次：同一会话 ID 与题库 v1');
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),sessionId);
 assert.equal(await page.getByTestId('interview-report').count(),1);
 mark('刷新后仍打开同一场次并回读到报告');

 // ---------- compatibility of existing entries ----------
 await page.getByRole('button',{name:'单题练习'}).click();
 await page.getByRole('heading',{name:'今天，先练好一题。'}).waitFor();
 await page.getByLabel('选择练习问题').waitFor();
 await page.getByRole('button',{name:'求职目标'}).click();
 await page.getByLabel('求职意向或补充说明').waitFor();
 mark('旧登录、求职目标与单题练习入口仍可用');

 // ---------- 409 / network failure (MOCK injected) ----------
 // Reload with ?goal=... so subsequent reloads stay on the career workspace.
 await page.goto(`${base}/?goal=${goalId}&view=interview`,{waitUntil:'domcontentloaded'});
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 await page.getByRole('button',{name:'另开一场'}).click();
 await page.locator('#interview-bank').waitFor({timeout:20000});
 await selectBankAndStart(page,bank);
 const retrySessionId=await page.locator('[data-session-id]').getAttribute('data-session-id');
 assert.ok(retrySessionId&&retrySessionId!==sessionId,'second_session_missing');
 let conflict=true;
 await page.route('**/api/career-goals/*/interviews/*/answers',async route=>{
  if(conflict&&route.request().method()==='POST')
   return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({code:'version_conflict'})});
  return route.continue();
 });
 await page.getByLabel('你的整场面试回答').fill(answer1);
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await page.getByText(/服务端状态已变化（版本冲突）/).first().waitFor({timeout:20000});
 // A confirmed conflict discards the pending request: the UI asks for a fresh submission
 // with the latest version and a new key, and deliberately offers no replay of the old one.
 await page.getByText(/新幂等键/).first().waitFor({timeout:20000});
 assert.equal(await page.getByRole('button',{name:'用同一请求重试'}).count(),0,'confirmed_conflict_offers_replay');
 assert.equal(await page.getByLabel('你的整场面试回答').inputValue(),answer1,'input_not_preserved');
 const conflictDetail=await getDetail(page,goalId,retrySessionId);
 assert.equal(conflictDetail.events.filter((e:any)=>e.kind==='answer').length,0,'unexpected_resend');
 mark('模拟 409：保留输入、回读状态、不自动重发、旧请求不提供重放','mock',{answers:0,error:'version_conflict'});

 conflict=false;
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 const replayed=await waitForDetail(page,goalId,retrySessionId,body=>body.events.filter((e:any)=>e.kind==='answer').length===1);
 assert.equal(replayed.events.filter((e:any)=>e.kind==='answer').length,1);
 mark('用同一幂等键重放后写入 1 答（无重复写入）','mock',{answers:1});
 const afterReplay=await waitForDetail(page,goalId,retrySessionId,body=>body.events.filter((e:any)=>e.kind==='question').length===2);
 assert.equal(afterReplay.events.filter((e:any)=>e.kind==='answer').length,1);
 await waitForStatus(page,'可作答');
 await page.getByLabel('你的整场面试回答').fill(answer2);
 let abort=true;
 await page.route('**/api/career-goals/*/interviews/*/answers',async route=>{
  if(abort&&route.request().method()==='POST')return route.abort('failed');
  return route.continue();
 });
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await page.getByText('请求未完成。你的输入仍保留，请先刷新状态核对。').waitFor({timeout:20000});
 assert.equal(await page.getByLabel('你的整场面试回答').inputValue(),answer2,'input_not_preserved_after_network_failure');
 const beforeRetry=await getDetail(page,goalId,retrySessionId);
 assert.equal(beforeRetry.events.filter((e:any)=>e.kind==='answer').length,1,'unexpected_resend_after_network_failure');
 mark('模拟网络失败：保留输入、回读状态、不自动重发','mock',{answers:1});
 abort=false;
 await page.getByRole('button',{name:'用同一请求重试'}).click();
 await page.getByTestId('interview-report').waitFor({timeout:90000});
 const recovered=await getDetail(page,goalId,retrySessionId);
 assert.equal(recovered.status,'COMPLETED');
 assert.equal(recovered.report.answers,2);
 mark('网络失败后用同一请求重试，最终 COMPLETED / 2 答','mock',
  {status:recovered.status,answers:recovered.report.answers,observations:recovered.report.observations.length});
 await page.unroute('**/api/career-goals/*/interviews/*/answers');

 // ---------- deadline / FAILED / auth-loss (MOCK responses built from real detail) ----------
 await mockDetail(body=>({...body,status:'WAITING_FOR_USER',report:null,observations:[],
  deadline_at:new Date(Date.now()-60_000).toISOString()}));
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByText(/本场已到服务端 deadline/).first().waitFor({timeout:20000});
 mark('模拟到期：以服务端 deadline_at 显示到期提示','mock');
 await page.unroute('**/api/career-goals/*/interviews/*');

 await mockDetail(body=>({...body,status:'FAILED',report:null,
  run:{...body.run,status:'FAILED',error_code:'interview_failed'}}));
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByText(/本场失败：后台面试任务失败/).waitFor({timeout:20000});
 mark('模拟 Worker 失败：显示可理解的失败原因','mock');
 await page.unroute('**/api/career-goals/*/interviews/*');

 await page.route('**/api/career-goals/*/interviews/*',async route=>
  route.fulfill({status:401,contentType:'application/json',body:JSON.stringify({code:'authentication_required'})}));
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByText('登录已过期，请重新登录。').first().waitFor({timeout:20000});
 mark('模拟权限失效：回到登录并给出提示','mock');
 await page.unroute('**/api/career-goals/*/interviews/*');

 const evidence={kind:'D-01-01 最终浏览器回归',timestamp:new Date().toISOString(),base_url:base,
  account:{email:account.email,name:'合成测试学员'},fixture:'fixtures/question-banks/agent-developer-trial.json',
  bank,goal_id:goalId,main_session_id:sessionId,retry_session_id:retrySessionId,
  real_api:{main_final:{status:finalDetail.status,version:finalDetail.version,policy:finalDetail.policy,
   question_banks:finalDetail.question_banks,report:{questions:finalDetail.report.questions,
    answers:finalDetail.report.answers,stop_reason:finalDetail.report.stop_reason,
    unreviewed_answer_ids:finalDetail.report.unreviewed_answer_ids,
    observations:finalDetail.report.observations.map((o:any)=>({label:o.label,origin:o.origin,evidence_id:o.evidence_id}))},
   run:finalDetail.run},retry_final:{status:recovered.status,answers:recovered.report.answers}},
  cases,page_errors:pageErrors};
 writeFileSync(join(output,'flow-results.json'),JSON.stringify(evidence,null,2));
 console.log(JSON.stringify({result:'passed',real_cases:cases.filter(c=>c.type==='real').length,
  mock_cases:cases.filter(c=>c.type==='mock').length,cases:cases.map(c=>`[${c.type}] ${c.name}`),
  main_session_id:sessionId,page_errors:pageErrors},null,2));
}catch(error){
 writeFileSync(join(output,'flow-results-failure.json'),JSON.stringify({message:String(error),cases,page_errors:pageErrors},null,2));
 await page.screenshot({path:join(output,'flow-failure.png'),fullPage:true}).catch(()=>{});
 console.error(error);
 process.exitCode=1;
}finally{
 await browser.close();
}
