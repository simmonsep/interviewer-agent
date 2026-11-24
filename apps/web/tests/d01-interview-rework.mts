// D-01-01 rework regression: real local API (3100) + scripted interview Worker +
// Vite proxy (5174). All output goes to D01_OUTPUT (a NEW directory); the first-round
// report, test script and evidence directory are left untouched.
//
//   D01_OUTPUT=<new dir> node apps/web/tests/d01-interview-rework.mts
//
// [real] = real local services, [mock] = page.route injected state. Conflicts are
// injected with a READ that actually returns a changed version / question, so the
// second POST must carry the new version and a NEW idempotency key.
import assert from 'node:assert/strict';
import {chromium,type Page,type Route} from '@playwright/test';
import {readFileSync,writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/evidence/D-01-01-rework'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const fixture=JSON.parse(readFileSync(join(root,'fixtures/question-banks/agent-developer-trial.json'),'utf8'));
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
 'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(existsSync);
assert.ok(executable,'installed_browser_required');
mkdirSync(output,{recursive:true});

const DETAIL='**/api/career-goals/*/interviews/*';
const ANSWERS='**/api/career-goals/*/interviews/*/answers';
const CREATE='**/api/career-goals/*/interviews';
const browser=await chromium.launch({executablePath:executable,headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1100}});
const page=await context.newPage();
const cases:{name:string;status:string;type:'real'|'mock';detail?:unknown}[]=[];
const pageErrors:string[]=[];
page.on('pageerror',error=>pageErrors.push(error.message));
const mark=(name:string,type:'real'|'mock'='real',detail?:unknown)=>cases.push({name,status:'passed',type,detail});
const account={email:`d01-rework-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID()};
const answer1='图内 state 是当前线程的运行状态，checkpointer 按 thread_id 保存检查点，store 放跨线程的偏好等长期信息。独立会话要用不同 thread_id；进程重启后要继续，需要持久化 checkpointer 与 store，内存实现不行。';
const answer2='interrupt 借助持久化检查点与 thread_id 暂停，用 Command(resume=...) 恢复。恢复会从包含 interrupt 的节点开头重跑，之前节点的副作用要幂等或移到单独节点；写入放在用户明确批准之后，并用业务幂等键防止重复写入。';
const draftText='这是尚未提交的草稿正文，退出登录后不应恢复。';
interface Captured {url:string;key:string;body:any}

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
// The REAL backend allows only one ACTIVE interview per goal (interview_in_progress),
// and caps sessions at 20. The synthetic API has neither limit. Before a case opens a
// fresh session, close leftover ACTIVE sessions through the public stop endpoint, so
// the case really starts from a NEW session instead of silently reusing the previous
// one (which would shift the question event ids and invalidate the draft assertions).
// This is test-environment preparation only; it changes no production code.
async function closeActiveSessions(p:Page,goalId:string){
 if(process.env.D01_MOCK_API)return;
 await p.evaluate(async(gid)=>{
  const response=await fetch(`/api/career-goals/${gid}/interviews`,{credentials:'same-origin',cache:'no-store'});
  if(!response.ok)return;
  const data=await response.json();
  for(const item of (data.items??[]) as {id:string;version:number;status:string}[]){
   if(['COMPLETED','FAILED','CANCELLED','EXPIRED'].includes(item.status))continue;
   await fetch(`/api/career-goals/${gid}/interviews/${item.id}/stop`,{
    method:'POST',credentials:'same-origin',
    headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},
    body:JSON.stringify({expected_version:item.version})}).catch(()=>{});
  }
 },goalId);
}
async function startFreshSession(p:Page,goalId:string,bank:{bank_id:string;version:number}):Promise<string>{
 await closeActiveSessions(p,goalId);
 await p.evaluate(()=>{
  for(const key of Object.keys(sessionStorage))
   if(key.startsWith('py-interview:')&&(key.endsWith(':current')||key.endsWith(':pending')))sessionStorage.removeItem(key);
 });
 await p.goto(`${base}/?goal=${goalId}&view=interview`,{waitUntil:'domcontentloaded'});
 await p.locator('#interview-bank').waitFor({timeout:20000});
 await selectBankAndStart(p,bank);
 const id=await p.locator('[data-session-id]').getAttribute('data-session-id');
 assert.ok(id,'session_id_missing');
 return id;
}
async function getUserId(p:Page):Promise<string>{
 const id=await p.evaluate(async()=>{
  const response=await fetch('/api/auth/get-session',{credentials:'same-origin'});
  return (await response.json())?.user?.id??'';
 });
 assert.ok(id,'user_id_missing');
 return id;
}
async function storageKeys(p:Page,prefixes:string[]):Promise<string[]>{
 return p.evaluate(list=>Object.keys(sessionStorage).filter(key=>list.some(prefix=>key.startsWith(prefix))),prefixes);
}
async function clearMockRoutes(p:Page){
 await p.unroute(DETAIL);
 await p.unroute(ANSWERS);
}
async function mockDetail(p:Page,mutate:(body:any)=>any){
 await p.route(DETAIL,async route=>{
  if(route.request().method()!=='GET')return route.continue();
  const response=await route.fetch();
  const body=await response.json();
  return route.fulfill({response,json:mutate(body)});
 });
}
function capturedPost(route:Route):Captured{
 const request=route.request();
 return {url:request.url(),key:request.headers()['idempotency-key']??'',body:JSON.parse(request.postData()??'{}')};
}
// Close-out helpers: return to the setup form with a clean pointer, keeping the goal
// at v1 so a start conflict can be simulated as a real v1 -> v2 read-back.
async function goToSetup(p:Page,goalId:string,bank:{bank_id:string;version:number}){
 await p.evaluate(()=>{
  for(const key of Object.keys(sessionStorage))
   if(key.startsWith('py-interview:')&&(key.endsWith(':current')||key.endsWith(':pending')))sessionStorage.removeItem(key);
 });
 await p.goto(`${base}/?goal=${goalId}&view=interview`,{waitUntil:'domcontentloaded'});
 await p.locator('#interview-bank').waitFor({timeout:20000});
 await p.getByLabel('题库与版本').locator('option',{hasText:'Agent开发岗位试运行'}).waitFor({state:'attached',timeout:20000});
 const choice=`${bank.bank_id}|${bank.version}`;
 await p.getByLabel('题库与版本').selectOption(choice);
 return choice;
}
async function answerSecondQuestion(p:Page,goalId:string,sessionId:string){
 await waitForDetail(p,goalId,sessionId,body=>body.events.filter((e:any)=>e.kind==='question').length===2);
 await waitForStatus(p,'可作答');
 await p.getByLabel('你的整场面试回答').fill(answer2);
 await p.getByRole('button',{name:'提交这一答 →'}).click();
 await p.getByTestId('interview-report').waitFor({timeout:90000});
 return getDetail(p,goalId,sessionId);
}

try{
 if(process.env.D01_MOCK_API){
  const reset=await fetch('http://127.0.0.1:3100/__reset',{method:'POST'});
  assert.equal(reset.status,200,'mock_api_reset_failed');
 }
 // ================= 1. REAL two-question main path =================
 await setupAccount(page);
 const bank=await importBank(page);
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByRole('button',{name:'整场面试'}).waitFor();
 const goalId=new URL(page.url()).searchParams.get('goal');
 assert.ok(goalId,'goal_id_missing');
 const userId=await getUserId(page);
 await selectBankAndStart(page,bank);
 const mainId=await page.locator('[data-session-id]').getAttribute('data-session-id');
 assert.ok(mainId,'main_session_id_missing');
 const firstQuestion=await page.getByTestId('interview-question').innerText();
 const initial=await getDetail(page,goalId,mainId);
 assert.equal(initial.status,'WAITING_FOR_USER');
 assert.equal(initial.question_banks[0].version,1);
 assert.equal(initial.policy.max_questions,2);
 assert.equal(initial.report,null);
 assert.deepEqual(initial.observations,[]);
 mark('真实开场：首题 / WAITING_FOR_USER / 冻结题库 v1 / 考中反馈隐藏','real',
  {session_id:mainId,version:initial.version,deadline_at:initial.deadline_at});
 await page.screenshot({path:join(output,'01-first-question.png'),fullPage:true});

 const submitButton=page.getByRole('button',{name:'提交这一答 →'});
 assert.equal(await submitButton.isDisabled(),true,'empty_answer_must_be_disabled');
 mark('空答时提交按钮禁用；未到 deadline 时输入后可提交','real');
 await page.getByLabel('你的整场面试回答').fill(answer1);
 await submitButton.click();
 await page.waitForFunction(previous=>{
  const element=document.querySelector('[data-testid="interview-question"]');
  return !!element&&element.textContent!==previous;
 },firstQuestion,{timeout:90000});
 await waitForStatus(page,'可作答');
 const afterFirst=await getDetail(page,goalId,mainId);
 assert.equal(afterFirst.events.filter((e:any)=>e.kind==='answer').length,1,'duplicate_answer_written');
 assert.equal(afterFirst.events.filter((e:any)=>e.kind==='question').length,2);
 assert.equal(afterFirst.report,null);
 assert.equal(await page.getByTestId('interview-report').count(),0);
 await page.screenshot({path:join(output,'02-second-question.png'),fullPage:true});
 mark('第一答后第二题出现、考中观察与报告保持隐藏','real',
  {answers:1,questions:2,version:afterFirst.version});

 const finalDetail=await answerSecondQuestion(page,goalId,mainId);
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
 await page.screenshot({path:join(output,'03-report.png'),fullPage:true});
 mark('终态 COMPLETED：2 题 / 2 答 / 2 条脚本观察 / 参考来源 / 0 未评阅','real',
  {status:finalDetail.status,version:finalDetail.version,stop_reason:finalDetail.report.stop_reason,
   observations:finalDetail.report.observations.map((o:any)=>({label:o.label,origin:o.origin,url:o.evidence_source?.url}))});

 await page.getByRole('button',{name:'另开一场'}).click();
 await page.locator('[data-session-id]').waitFor({state:'detached',timeout:10000});
 await page.getByRole('button',{name:/场次 1 ·/}).first().click();
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),mainId);
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),mainId);
 assert.equal(await page.getByTestId('interview-report').count(),1);
 mark('历史回读与刷新仍打开同一场次并回读报告','real');
 await page.getByRole('button',{name:'单题练习'}).click();
 await page.getByRole('heading',{name:'今天，先练好一题。'}).waitFor();
 await page.getByLabel('选择练习问题').waitFor();
 mark('旧登录、求职目标与单题练习入口仍可用','real');

 // ================= 2. UNKNOWN outcome: same URL/body/key replay =================
 await page.goto(`${base}/?goal=${goalId}&view=interview`,{waitUntil:'domcontentloaded'});
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 await page.getByRole('button',{name:'另开一场'}).click();
 await page.locator('#interview-bank').waitFor({timeout:20000});
 await selectBankAndStart(page,bank);
 const networkId=await page.locator('[data-session-id]').getAttribute('data-session-id');
 assert.ok(networkId&&networkId!==mainId,'network_session_missing');
 const networkPosts:Captured[]=[];
 let abort=true;
 await page.route(ANSWERS,async route=>{
  if(route.request().method()!=='POST')return route.continue();
  const post=capturedPost(route);networkPosts.push(post);
  if(abort&&networkPosts.length===1)return route.abort('failed');
  return route.continue();
 });
 await page.getByLabel('你的整场面试回答').fill(answer1);
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await page.getByTestId('unknown-retry').waitFor({timeout:20000});
 assert.equal(await page.getByLabel('你的整场面试回答').inputValue(),answer1,'input_not_preserved');
 const beforeReplay=await getDetail(page,goalId,networkId);
 assert.equal(beforeReplay.events.filter((e:any)=>e.kind==='answer').length,0,'unexpected_resend');
 assert.equal(networkPosts.length,1,'automatic_resend');
 mark('模拟网络失败：保留输入、回读状态、不自动重发 POST','mock',{posts:1,answers:0});
 abort=false;
 await page.getByRole('button',{name:'用同一请求重试'}).click();
 await waitForDetail(page,goalId,networkId,body=>body.events.filter((e:any)=>e.kind==='answer').length===1);
 assert.equal(networkPosts.length,2,'replay_not_sent');
 assert.equal(networkPosts[1].url,networkPosts[0].url,'replay_url_changed');
 assert.equal(networkPosts[1].key,networkPosts[0].key,'replay_key_changed');
 assert.deepEqual(networkPosts[1].body,networkPosts[0].body,'replay_body_changed');
 mark('结果未知才允许手动重放：URL / body / 幂等键完全一致','mock',
  {key_prefix:networkPosts[0].key.slice(0,8),expected_version:networkPosts[0].body.expected_version});
 const networkFinal=await answerSecondQuestion(page,goalId,networkId);
 assert.equal(networkFinal.status,'COMPLETED');
 assert.equal(networkFinal.report.answers,2);
 mark('重放后真实 Worker 继续完成本场：COMPLETED / 2 答','real');
 await clearMockRoutes(page);

 // ================= 3. VERSION CONFLICT: read returns a CHANGED version =================
 const conflictId=await startFreshSession(page,goalId,bank);
 const conflictDetail=await getDetail(page,goalId,conflictId);
 const conflictQuestionId=conflictDetail.events.find((e:any)=>e.kind==='question').id;
 const conflictVersion=conflictDetail.version;
 const conflictPosts:Captured[]=[];
 await mockDetail(page,body=>({...body,version:body.version+1}));
 await page.route(ANSWERS,async route=>{
  if(route.request().method()!=='POST')return route.continue();
  const post=capturedPost(route);conflictPosts.push(post);
  if(conflictPosts.length===1)
   return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({code:'version_conflict'})});
  return route.fulfill({status:202,contentType:'application/json',json:{...conflictDetail,version:conflictVersion+1}});
 });
 await page.getByLabel('你的整场面试回答').fill(answer1);
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await page.getByText(/会话版本已更新到 v\d+/).waitFor({timeout:20000});
 assert.equal(await page.getByLabel('你的整场面试回答').inputValue(),answer1,'conflict_input_not_preserved');
 assert.equal(await page.getByTestId('unknown-retry').count(),0,'conflict_must_not_offer_same_key_replay');
 assert.equal(conflictPosts.length,1,'conflict_automatic_resend');
 assert.ok((await page.getByTestId('conflict-hint').innerText()).includes('旧幂等键'));
 mark('版本冲突（读接口返回 v+1）：保留原文、废弃旧幂等键、不自动重发','mock',
  {read_version:conflictVersion+1,first_key:conflictPosts[0].key.slice(0,8),posts:1});
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await delay(800);
 assert.equal(conflictPosts.length,2,'second_post_missing');
 assert.equal(conflictPosts[1].body.expected_version,conflictVersion+1,'second_post_used_stale_version');
 assert.notEqual(conflictPosts[1].key,conflictPosts[0].key,'second_post_reused_idempotency_key');
 assert.equal(conflictPosts[1].body.question_id,conflictQuestionId);
 assert.equal(conflictPosts[1].body.text,answer1);
 mark('用户重新提交：携带最新会话版本与新幂等键（非旧请求重放）','mock',
  {old_version:conflictPosts[0].body.expected_version,new_version:conflictPosts[1].body.expected_version,
   old_key:conflictPosts[0].key.slice(0,8),new_key:conflictPosts[1].key.slice(0,8)});
 await clearMockRoutes(page);

 // ================= 3A. START 409: goal v1 -> v2, explicit new key =================
 const goalDetailMatch=(url:URL)=>url.pathname===`/api/career-goals/${goalId}`;
 const startBankChoice=await goToSetup(page,goalId,bank);
 const realGoal=await page.evaluate(async(gid)=>{
  const response=await fetch(`/api/career-goals/${gid}`,{credentials:'same-origin',cache:'no-store'});
  return await response.json();
 },goalId);
 assert.equal(realGoal.version,1,'goal_not_v1');
 // The READ after the conflict really returns v2; every other goal field is preserved.
 await page.route(goalDetailMatch,async route=>{
  if(route.request().method()!=='GET')return route.continue();
  const response=await route.fetch();
  const body=await response.json();
  return route.fulfill({response,json:{...body,version:2}});
 });
 const startSessionBody=await getDetail(page,goalId,mainId);
 const newStartId=randomUUID();
 const startPosts:Captured[]=[];
 await page.route(CREATE,async route=>{
  if(route.request().method()!=='POST')return route.continue();
  const post=capturedPost(route);startPosts.push(post);
  if(post.body.expected_version!==2)
   return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({code:'version_conflict'})});
  // Only v2 is accepted; return a session so the UI enters the new session.
  return route.fulfill({status:202,contentType:'application/json',json:{...startSessionBody,id:newStartId}});
 });
 await page.getByRole('button',{name:'开始整场面试 →'}).click();
 await page.getByTestId('interview-notice').filter({hasText:'目标版本已更新到 v2'}).waitFor({timeout:20000});
 assert.equal(startPosts.length,1,'start_conflict_automatic_resend');
 assert.equal(startPosts[0].body.expected_version,1,'first_start_not_v1');
 assert.equal(await page.getByTestId('unknown-retry').count(),0,'start_conflict_offers_old_replay');
 assert.equal(await page.getByLabel('题库与版本').inputValue(),startBankChoice,'bank_choice_lost');
 assert.ok((await page.getByTestId('conflict-hint').innerText()).includes('旧幂等键'));
 await page.screenshot({path:join(output,'07-start-conflict.png'),fullPage:true});
 mark('开场 409：回读目标收到 v2、题库及版本仍选中、旧重放入口消失、用户操作前无第二次开场 POST','mock',
  {first_version:1,read_version:2,first_key:startPosts[0].key.slice(0,8),posts:startPosts.length});
 await page.getByRole('button',{name:'开始整场面试 →'}).click();
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),newStartId,'new_session_not_opened');
 assert.equal(startPosts.length,2,'second_start_missing');
 assert.equal(startPosts[1].body.expected_version,2,'second_start_stale_version');
 assert.notEqual(startPosts[1].key,startPosts[0].key,'second_start_reused_key');
 assert.deepEqual(startPosts[1].body.question_banks,startPosts[0].body.question_banks,'bank_changed_between_attempts');
 mark('用户再次开场：携带 v2、不同幂等键、原题库及版本；服务端只接受 v2 并进入新场次','mock',
  {old_version:1,new_version:2,old_key:startPosts[0].key.slice(0,8),new_key:startPosts[1].key.slice(0,8),
   question_banks:startPosts[1].body.question_banks});
 await page.unroute(CREATE);
 await page.unroute(goalDetailMatch);
 await clearMockRoutes(page);

 // ================= 3B. CROSS-SESSION EXPIRY: replay is bound to session A =================
 const crossId=await startFreshSession(page,goalId,bank);
 const crossDetail=await getDetail(page,goalId,crossId);
 const crossQuestionId=crossDetail.events.find((e:any)=>e.kind==='question').id;
 const crossDraftKey=`py-interview:${userId}:draft:${crossId}:${crossQuestionId}`;
 const crossPosts:Captured[]=[];
 let crossAbort=true;
 await page.route(ANSWERS,async route=>{
  if(route.request().method()!=='POST')return route.continue();
  const post=capturedPost(route);crossPosts.push(post);
  if(crossAbort){crossAbort=false;return route.abort('failed');}
  return route.continue();
 });
 await page.getByLabel('你的整场面试回答').fill(draftText);
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await page.getByTestId('unknown-retry').waitFor({timeout:20000});
 assert.equal(crossPosts.length,1,'cross_pending_not_created');
 const pendingCommand=await page.evaluate(prefix=>{
  const entry=Object.keys(sessionStorage).find(k=>k.startsWith(prefix)&&k.endsWith(':pending'));
  return entry?JSON.parse(sessionStorage.getItem(entry)!):null;
 },`py-interview:${userId}:`);
 assert.equal(pendingCommand.sessionId,crossId,'pending_not_pinned_to_A');
 mark('制造 A 的待重放答题请求：命令固定指向原场次 A，草稿保留','mock',
  {session_id:crossId,key_prefix:crossPosts[0].key.slice(0,8)});

 const past=new Date(Date.now()-60_000).toISOString();
 const future=new Date(Date.now()+3_600_000).toISOString();
 const otherId=mainId;
 const mutateCross=(body:any)=>{
  if(body.id===crossId)return {...body,status:'WAITING_FOR_USER',report:null,observations:[],deadline_at:past};
  if(body.id===otherId)return {...body,status:'WAITING_FOR_USER',report:null,observations:[],deadline_at:future};
  return body;
 };
 await mockDetail(page,mutateCross);
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByTestId('unknown-retry').waitFor({timeout:20000});
 await page.getByTestId('deadline-notice').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),crossId,'A_not_opened');
 assert.equal(await page.getByRole('button',{name:'用同一请求重试'}).isDisabled(),true,'A_expired_replay_enabled');
 mark('A 已到期且原场次打开：重放入口停用，截止时间取自 A 的详情而非当前场次','mock',
  {A:crossId,deadline_at:past});
 // Switch to B whose deadline is in the future: the old bug used B's deadline for A's POST.
 await page.getByRole('button',{name:new RegExp(otherId.slice(0,8))}).first().click();
 await page.waitForFunction(id=>document.querySelector('[data-session-id]')?.getAttribute('data-session-id')===id,
  otherId,{timeout:20000});
 assert.equal(await page.getByTestId('unknown-retry').count(),1,'pending_lost_on_switch');
 assert.equal(await page.getByRole('button',{name:'用同一请求重试'}).isDisabled(),true,'cross_session_replay_enabled');
 const blockedText=await page.getByTestId('replay-blocked').innerText();
 assert.ok(blockedText.includes('其他场次')||blockedText.includes('详情尚未加载'),'missing_cross_session_hint');
 const kept=await page.evaluate(([prefix,draft])=>({
  pending:Object.keys(sessionStorage).some(k=>k.startsWith(prefix)&&k.endsWith(':pending')),
  draft:sessionStorage.getItem(draft)
 }),[`py-interview:${userId}:`,crossDraftKey] as [string,string]);
 assert.equal(kept.pending,true,'pending_lost_on_switch');
 assert.equal(kept.draft,draftText,'draft_lost_on_switch');
 await page.getByTestId('unknown-retry').evaluate(container=>{
  const button=container.querySelector('button') as HTMLButtonElement|null;
  if(!button)return;
  const wasDisabled=button.disabled;
  button.removeAttribute('disabled');button.click();
  // Restore the DOM attribute: React will not rewrite a controlled prop whose value
  // did not change, so a later isDisabled() assertion must not see this bypass.
  if(wasDisabled)button.setAttribute('disabled','');
 });
 await delay(800);
 assert.equal(crossPosts.length,1,'cross_session_sent_answer_post');
 await page.screenshot({path:join(output,'08-cross-session-expiry.png'),fullPage:true});
 mark('切到未到期场次 B：重放入口不可用、绕过按钮也没有向 A 发 POST，请求与草稿均保留','mock',
  {A:crossId,B:otherId,B_deadline_at:future,posts:crossPosts.length});

 // ---- Control (terminal-session fix): A's command is STILL unresolved. Opening or
 // clicking "另开一场" from a TERMINAL B must never clear A's pending command/draft. ----
 await page.unroute(DETAIL);
 const terminalBReport={stop_reason:'question_limit',model_summary:null,questions:2,answers:2,
  assisted_answers:0,observations:[],semantic_scoring_eligible:false,
  unreviewed_answer_ids:[],next_actions:[]};
 await mockDetail(page,body=>{
  if(body.id===crossId)return {...body,status:'WAITING_FOR_USER',report:null,observations:[],deadline_at:past};
  if(body.id===otherId)return {...body,status:'COMPLETED',deadline_at:future,report:terminalBReport};
  return body;
 });
 await page.reload({waitUntil:'domcontentloaded'});
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),otherId,'terminal_B_not_opened');
 await page.getByRole('button',{name:'另开一场'}).waitFor({timeout:20000});
 await page.getByRole('button',{name:'另开一场'}).click();
 await page.locator('#interview-bank').waitFor({timeout:20000});
 const afterTerminalB=await page.evaluate(([prefix,draft])=>({
  pending:Object.keys(sessionStorage).some(k=>k.startsWith(prefix)&&k.endsWith(':pending')),
  draft:sessionStorage.getItem(draft),
  retry:!!document.querySelector('[data-testid="unknown-retry"]'),
  session:!!document.querySelector('[data-session-id]')
 }),[`py-interview:${userId}:`,crossDraftKey] as [string,string]);
 assert.equal(afterTerminalB.pending,true,'terminal_B_cleared_A_pending');
 assert.equal(afterTerminalB.draft,draftText,'terminal_B_cleared_A_draft');
 assert.equal(afterTerminalB.retry,true,'terminal_B_hid_A_replay');
 assert.equal(crossPosts.length,1,'terminal_B_sent_answer_post_to_A');
 await page.screenshot({path:join(output,'09-terminal-b-keeps-a.png'),fullPage:true});
 mark('对照：A 的命令仍未解决时，从终态 B 点「另开一场」，A 的待重放命令、重放入口与原题草稿均保留','mock',
  {A:crossId,terminal_B:otherId,posts:crossPosts.length});
 // The later "A detail held" case re-opens A from the current pointer; restore it
 // explicitly because reset() removed the pointer.
 await page.evaluate(([key,sid])=>sessionStorage.setItem(key,sid),
  [`py-interview:${userId}:${goalId}:current`,crossId] as [string,string]);

 // Refresh while A's detail is deliberately held: session === null, so no valid
 // deadline can be confirmed for the target session.
 await page.evaluate(([prefix,sid])=>{
  const entry=Object.keys(sessionStorage).find(k=>k.startsWith(prefix)&&k.endsWith(':current'));
  if(entry)sessionStorage.setItem(entry,sid);
 },[`py-interview:${userId}:`,crossId] as [string,string]);
 await page.unroute(DETAIL);
 let holdDetail=true;
 const heldDetail=async(route:Route)=>{
  if(route.request().method()!=='GET')return route.continue();
  if(holdDetail)await delay(2000);
  const response=await route.fetch();
  const body=await response.json();
  return route.fulfill({response,json:mutateCross(body)});
 };
 await page.route(DETAIL,heldDetail);
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByTestId('unknown-retry').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-session-id]').count(),0,'A_detail_loaded_too_early');
 assert.equal(await page.getByRole('button',{name:'用同一请求重试'}).isDisabled(),true,'unloaded_detail_replay_enabled');
 await page.getByTestId('unknown-retry').evaluate(container=>{
  const button=container.querySelector('button') as HTMLButtonElement|null;
  if(!button)return;
  const wasDisabled=button.disabled;
  button.removeAttribute('disabled');button.click();
  // Restore the DOM attribute: React will not rewrite a controlled prop whose value
  // did not change, so a later isDisabled() assertion must not see this bypass.
  if(wasDisabled)button.setAttribute('disabled','');
 });
 await delay(800);
 assert.equal(crossPosts.length,1,'unloaded_detail_sent_answer_post');
 mark('刷新后 A 详情尚未加载：截止时间无法确认，重放入口不可用且不发 POST','mock',{posts:crossPosts.length});
 holdDetail=false;
 await page.getByTestId('deadline-notice').waitFor({timeout:25000});
 await delay(400);
 const afterLoad=await page.evaluate(prefix=>{
  const container=document.querySelector('[data-testid="unknown-retry"]');
  const button=container?.querySelector('button') as HTMLButtonElement|null;
  const pendingKey=Object.keys(sessionStorage).find(key=>key.startsWith(prefix)&&key.endsWith(':pending'));
  const pending=pendingKey?JSON.parse(sessionStorage.getItem(pendingKey)!):null;
  return {retryVisible:!!container,disabled:button?.disabled??null,
   sessionId:document.querySelector('[data-session-id]')?.getAttribute('data-session-id')??null,
   pending:{kind:pending?.kind,sessionId:pending?.sessionId,url:pending?.url},
   notice:document.querySelector('[data-testid="deadline-notice"]')?.textContent??null,
   blocked:document.querySelector('[data-testid="replay-blocked"]')?.textContent??null,
   phase:document.querySelector('.phase')?.textContent??null,
   status:document.querySelector('[data-testid="interview-status"]')?.textContent??null};
 },`py-interview:${userId}:`);
 assert.ok(afterLoad.retryVisible&&afterLoad.disabled===true,
  'A_expired_after_load '+JSON.stringify(afterLoad));
 await page.unroute(DETAIL);

 // Control: A itself is open and unexpired -> the SAME key/URL/body replay is allowed.
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByTestId('unknown-retry').waitFor({timeout:20000});
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),crossId,'A_not_reopened');
 const replayButton=page.getByRole('button',{name:'用同一请求重试'});
 await replayButton.waitFor({timeout:20000});
 assert.equal(await replayButton.isDisabled(),false,'unexpired_A_replay_disabled');
 await replayButton.click();
 await delay(1200);
 assert.equal(crossPosts.length,2,'control_replay_not_sent');
 assert.equal(crossPosts[1].url,crossPosts[0].url,'control_url_changed');
 assert.equal(crossPosts[1].key,crossPosts[0].key,'control_key_changed');
 assert.deepEqual(crossPosts[1].body,crossPosts[0].body,'control_body_changed');
 assert.equal(await page.evaluate(key=>sessionStorage.getItem(key),crossDraftKey),null,'A_draft_not_cleared');
 mark('对照：A 未到期且原场次已打开时，同 URL / body / 幂等键重放可用，成功后清理 A 原题草稿','real',
  {session_id:crossId,posts:crossPosts.length,key_prefix:crossPosts[0].key.slice(0,8)});
 await clearMockRoutes(page);

 // ================= 3C. TERMINAL A: "另开一场" clears ONLY A's stale replay =================
 const termId=await startFreshSession(page,goalId,bank);
 const termDetail=await getDetail(page,goalId,termId);
 const termQuestionId=termDetail.events.find((e:any)=>e.kind==='question').id;
 const termDraftKey=`py-interview:${userId}:draft:${termId}:${termQuestionId}`;
 const termPosts:Captured[]=[];
 let termAbort=true;
 await page.route(ANSWERS,async route=>{
  if(route.request().method()!=='POST')return route.continue();
  const post=capturedPost(route);termPosts.push(post);
  if(termAbort){termAbort=false;return route.abort('failed');}
  return route.continue();
 });
 await page.getByLabel('你的整场面试回答').fill(draftText);
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await page.getByTestId('unknown-retry').waitFor({timeout:20000});
 assert.equal(termPosts.length,1,'term_pending_not_created');
 const termPendingCommand=await page.evaluate(prefix=>{
  const entry=Object.keys(sessionStorage).find(k=>k.startsWith(prefix)&&k.endsWith(':pending'));
  return entry?JSON.parse(sessionStorage.getItem(entry)!):null;
 },`py-interview:${userId}:`);
 assert.equal(termPendingCommand.sessionId,termId,'term_pending_not_pinned_to_A');
 assert.equal(await page.evaluate(key=>sessionStorage.getItem(key),termDraftKey),draftText,'term_draft_missing');
 mark('终态场景准备：A 的答题请求结果未知，待重放命令固定指向 A，原题草稿保留','mock',
  {A:termId,posts:termPosts.length});

 // Negative control 1: a LOCAL countdown of zero with the server still saying
 // WAITING_FOR_USER must not offer "另开一场" or clear A's command/draft.
 const countdownPast=new Date(Date.now()-60_000).toISOString();
 await mockDetail(page,body=>body.id===termId
  ?{...body,status:'WAITING_FOR_USER',deadline_at:countdownPast,report:null,observations:[]}
  :body);
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByTestId('deadline-notice').waitFor({timeout:20000});
 assert.equal(await page.getByRole('button',{name:'另开一场'}).count(),0,'countdown_offered_start_another');
 const afterCountdown=await page.evaluate(([prefix,draftKey])=>({
  pending:Object.keys(sessionStorage).some(k=>k.startsWith(prefix)&&k.endsWith(':pending')),
  draft:sessionStorage.getItem(draftKey),
  retry:!!document.querySelector('[data-testid="unknown-retry"]')
 }),[`py-interview:${userId}:`,termDraftKey] as [string,string]);
 assert.equal(afterCountdown.pending,true,'countdown_cleared_A_pending');
 assert.equal(afterCountdown.draft,draftText,'countdown_cleared_A_draft');
 assert.equal(afterCountdown.retry,true,'countdown_hid_A_replay');
 assert.equal(termPosts.length,1,'countdown_sent_answer_post');
 mark('否定对照：仅本地倒计时到零（服务端仍 WAITING_FOR_USER）不提供「另开一场」，A 的待重放命令与草稿保留','mock',
  {A:termId,deadline_at:countdownPast,server_status:'WAITING_FOR_USER'});

 // Negative control 2: a FAILED detail read leaves A's detail unloaded; the terminal
 // button must not appear and A's command/draft must stay.
 await page.unroute(DETAIL);
 await page.route(DETAIL,async route=>route.request().method()!=='GET'
  ?route.continue()
  :route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({code:'resource_unavailable'})}));
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByTestId('unknown-retry').waitFor({timeout:20000});
 await delay(900);
 assert.equal(await page.locator('[data-session-id]').count(),0,'failed_read_loaded_A_detail');
 assert.equal(await page.getByRole('button',{name:'另开一场'}).count(),0,'failed_read_offered_start_another');
 const afterFailedRead=await page.evaluate(([prefix,draftKey])=>({
  pending:Object.keys(sessionStorage).some(k=>k.startsWith(prefix)&&k.endsWith(':pending')),
  draft:sessionStorage.getItem(draftKey)
 }),[`py-interview:${userId}:`,termDraftKey] as [string,string]);
 assert.equal(afterFailedRead.pending,true,'failed_read_cleared_A_pending');
 assert.equal(afterFailedRead.draft,draftText,'failed_read_cleared_A_draft');
 assert.equal(termPosts.length,1,'failed_read_sent_answer_post');
 mark('否定对照：A 详情读取失败/未加载时不提供「另开一场」，A 的待重放命令与草稿保留','mock',
  {A:termId,detail_status:500});
 await page.unroute(DETAIL);

 // Inject A's EXPIRED detail together with a SERVER-CONFIRMED terminal state.
 const termPast=new Date(Date.now()-60_000).toISOString();
 const termReport={stop_reason:'deadline',model_summary:null,questions:1,answers:0,
  assisted_answers:0,observations:[],semantic_scoring_eligible:false,
  unreviewed_answer_ids:[],next_actions:[]};
 await mockDetail(page,body=>body.id===termId
  ?{...body,status:'COMPLETED',deadline_at:termPast,report:termReport,observations:[]}
  :body);
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByTestId('deadline-end').waitFor({timeout:20000});
 assert.equal(await page.locator('[data-session-id]').getAttribute('data-session-id'),termId,'term_A_not_opened');
 assert.equal(await page.getByRole('button',{name:'用同一请求重试'}).isDisabled(),true,'term_A_replay_enabled');
 assert.ok((await page.getByTestId('deadline-end').innerText()).includes('本场已到期'),'term_A_end_reason_missing');
 await page.screenshot({path:join(output,'10-terminal-a.png'),fullPage:true});
 mark('注入 A 的到期详情与服务端终态：重放禁用，按 stop_reason=deadline 展示结束原因','mock',
  {A:termId,deadline_at:termPast,status:'COMPLETED'});

 await page.getByRole('button',{name:'另开一场'}).click();
 await page.locator('#interview-bank').waitFor({timeout:20000});
 const afterTermReset=await page.evaluate(([prefix,draftKey])=>({
  pending:Object.keys(sessionStorage).some(k=>k.startsWith(prefix)&&k.endsWith(':pending')),
  draft:sessionStorage.getItem(draftKey),
  retry:!!document.querySelector('[data-testid="unknown-retry"]'),
  session:!!document.querySelector('[data-session-id]'),
  startDisabled:(document.querySelector('.interview-setup button.primary') as HTMLButtonElement|null)?.disabled??null,
  notice:document.querySelector('[data-testid="interview-notice"]')?.textContent??null
 }),[`py-interview:${userId}:`,termDraftKey] as [string,string]);
 assert.equal(afterTermReset.pending,false,'term_pending_not_cleared');
 assert.equal(afterTermReset.retry,false,'term_retry_still_visible');
 assert.equal(afterTermReset.session,false,'term_auto_started_session');
 assert.equal(afterTermReset.draft,draftText,'term_A_draft_lost');
 assert.equal(termPosts.length,1,'term_sent_answer_post_to_A');
 assert.equal(afterTermReset.startDisabled,false,'term_start_still_blocked');
 assert.ok(afterTermReset.notice?.includes('原场次已由服务端结束'),'term_clear_notice_missing');
 await page.screenshot({path:join(output,'11-terminal-a-reset.png'),fullPage:true});
 mark('从终态 A 点「另开一场」：只清 A 的待重放命令及缓存、保留原题草稿、不向 A 发答题 POST、不自动开场且开场按钮解锁','mock',
  {A:termId,posts:termPosts.length,notice:afterTermReset.notice});

 // The USER explicitly starts a new session. Remove the injected detail first, then
 // stop the real A through the public stop endpoint so the real backend permits a
 // genuine create (one ACTIVE session per goal). No POST is issued before this click.
 await page.unroute(DETAIL);
 await page.unroute(ANSWERS);
 await closeActiveSessions(page,goalId);
 const termCreatePosts:Captured[]=[];
 await page.route(CREATE,async route=>{
  if(route.request().method()!=='POST')return route.continue();
  termCreatePosts.push(capturedPost(route));
  return route.continue();
 });
 await page.getByRole('button',{name:'开始整场面试 →'}).click();
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 const termNewId=await page.locator('[data-session-id]').getAttribute('data-session-id');
 assert.ok(termNewId&&termNewId!==termId,'term_new_session_same_as_A');
 assert.equal(termCreatePosts.length,1,'term_create_not_sent');
 assert.equal(termPosts.length,1,'term_answer_post_after_new_start');
 mark('用户显式点击开场：真实新开场请求成功，进入 ID 不同的新场次（无自动开场、无向终态 A 的答题 POST）','real',
  {A:termId,new_session:termNewId,create_posts:termCreatePosts.length});
 await page.unroute(CREATE);
 await clearMockRoutes(page);

 // ================= 4. QUESTION_CHANGED: old draft kept under the ORIGINAL event id =================
 const changedId=await startFreshSession(page,goalId,bank);
 const changedDetail=await getDetail(page,goalId,changedId);
 const oldQuestionId=changedDetail.events.find((e:any)=>e.kind==='question').id;
 const newQuestionId=randomUUID();
 const changedPosts:Captured[]=[];
 await mockDetail(page,body=>({...body,version:body.version+1,
  events:body.events.map((event:any)=>event.kind==='question'
   ?{...event,id:newQuestionId,payload:{...event.payload,text:'【模拟新题】请说明新题目的作答依据。'}}:event)}));
 await page.route(ANSWERS,async route=>{
  if(route.request().method()!=='POST')return route.continue();
  changedPosts.push(capturedPost(route));
  return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({code:'question_changed'})});
 });
 await page.getByLabel('你的整场面试回答').fill(answer1);
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 // Wait for the conflict handler (not the early error banner) to finish.
 await page.getByTestId('interview-question').filter({hasText:'模拟新题'}).waitFor({timeout:20000});
 await page.getByTestId('interview-notice').filter({hasText:'题目已变化：'}).waitFor({timeout:20000});
 await page.waitForFunction(()=>
  (document.querySelector('#interview-answer') as HTMLTextAreaElement|null)?.value==='',null,{timeout:10000});
 const savedDraft=await page.evaluate(key=>sessionStorage.getItem(key),`py-interview:${userId}:draft:${changedId}:${oldQuestionId}`);
 assert.equal(savedDraft,answer1,'old_draft_not_saved_under_original_question_id');
 assert.equal(await page.getByLabel('你的整场面试回答').inputValue(),'','new_question_inherited_old_answer');
 assert.ok((await page.getByTestId('interview-question').innerText()).includes('模拟新题'));
 assert.equal(changedPosts.length,1,'question_changed_automatic_resend');
 const notice=await page.getByTestId('interview-notice').innerText();
 assert.ok(!notice.includes('本次提交已生效'),'false_claim_of_success');
 await page.screenshot({path:join(output,'04-question-changed.png'),fullPage:true});
 mark('题目变化：旧草稿按原题事件 ID 保存、新题输入框为空、不误报已生效','mock',
  {old_question_id:oldQuestionId,new_question_id:newQuestionId,draft_key:`py-interview:${userId}:draft:${changedId}:${oldQuestionId}`});
 await clearMockRoutes(page);

 // ================= 5. Saved answer: evidence INSUFFICIENT vs SUFFICIENT =================
 const savedId=await startFreshSession(page,goalId,bank);
 const savedDetail=await getDetail(page,goalId,savedId);
 const savedQuestionId=savedDetail.events.find((e:any)=>e.kind==='question').id;
 const maxSeq=Math.max(...savedDetail.events.map((e:any)=>e.seq));
 const withAnswer=(text:string)=>(body:any)=>({...body,version:body.version+1,
  events:[...body.events,{id:randomUUID(),seq:maxSeq+1,kind:'answer',
   payload:{text,question_id:savedQuestionId,topic_id:'langgraph-checkpointer',assisted:false}}]});
 await mockDetail(page,withAnswer('来自其它请求的回答，不是本次提交。'));
 const savedPosts:Captured[]=[];
 await page.route(ANSWERS,async route=>{
  if(route.request().method()!=='POST')return route.continue();
  savedPosts.push(capturedPost(route));
  return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({code:'version_conflict'})});
 });
 await page.getByLabel('你的整场面试回答').fill(answer1);
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await page.getByTestId('saved-answer-hint').waitFor({timeout:20000});
 assert.equal(await page.getByRole('button',{name:'提交这一答 →'}).isDisabled(),true,'submit_not_disabled');
 const weakNotice=await page.getByTestId('interview-notice').innerText();
 assert.ok(weakNotice.includes('证据不足'),'missing_evidence_warning');
 assert.ok(!weakNotice.includes('本次提交已生效'),'false_claim_of_success');
 assert.equal(savedPosts.length,1,'automatic_resend');
 mark('该题已有已保存回答但证据不足：只展示当前状态、禁用提交、不宣称已生效','mock');
 await clearMockRoutes(page);

 // Same session, but the read-back NOW contains a NEW answer event whose text matches
 // this submission exactly -> enough evidence to say "本次提交已生效".
 await page.evaluate(()=>{
  for(const key of Object.keys(sessionStorage))if(key.startsWith('py-interview:')&&key.endsWith(':pending'))sessionStorage.removeItem(key);
 });
 await page.goto(`${base}/?goal=${goalId}&view=interview`,{waitUntil:'domcontentloaded'});
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 await mockDetail(page,withAnswer(answer1));
 const proofPosts:Captured[]=[];
 await page.route(ANSWERS,async route=>{
  if(route.request().method()!=='POST')return route.continue();
  proofPosts.push(capturedPost(route));
  return route.abort('failed');
 });
 await page.getByLabel('你的整场面试回答').fill(answer1);
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await page.getByText(/已回读确认：本次提交已生效/).waitFor({timeout:20000});
 assert.equal(await page.getByTestId('unknown-retry').count(),0,'retry_should_be_cleared');
 assert.equal(proofPosts.length,1,'automatic_resend');
 const draftAfter=await page.evaluate(key=>sessionStorage.getItem(key),`py-interview:${userId}:draft:${savedId}:${savedQuestionId}`);
 assert.equal(draftAfter,null,'draft_not_cleared');
 mark('证据充分（新答案事件逐字匹配）：才提示「本次提交已生效」并清理草稿','mock');
 await clearMockRoutes(page);

 // ================= 6. EXPIRED: no answer POST, handlers re-check the clock =================
 const expiryId=await startFreshSession(page,goalId,bank);
 const expiryDetail=await getDetail(page,goalId,expiryId);
 const expiryQuestionId=expiryDetail.events.find((e:any)=>e.kind==='question').id;
 const answerPosts:Captured[]=[];
 await page.route(ANSWERS,async route=>{
  if(route.request().method()!=='POST')return route.continue();
  answerPosts.push(capturedPost(route));
  return route.continue();
 });
 // Before mocking: a live, unexpired session accepts input and enables submit.
 await page.getByLabel('你的整场面试回答').fill(draftText);
 assert.equal(await page.getByTestId('answer-submit').isDisabled(),false,'unexpired_submit_must_be_enabled');
 mark('未到期场次：填写草稿后提交按钮可用（真实服务状态）','real',{session_id:expiryId});
 const expiredDeadline=new Date(Date.now()-60_000).toISOString();
 await mockDetail(page,body=>({...body,status:'WAITING_FOR_USER',report:null,observations:[],
  deadline_at:expiredDeadline}));
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByTestId('deadline-notice').waitFor({timeout:20000});
 await page.getByLabel('你的整场面试回答').fill(draftText);
 assert.equal(await page.getByTestId('answer-submit').isDisabled(),true,'expired_submit_not_disabled');
 assert.equal(await page.getByLabel('你的整场面试回答').inputValue(),draftText,'draft_lost');
 // Bypass the disabled button to prove the handler itself re-checks deadline_at.
 await page.locator('form.interview-answer').evaluate(form=>
  form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
 await delay(800);
 assert.equal(answerPosts.length,0,'expired_handler_sent_answer_post');
 mark('到期（服务端 deadline_at 已过、仍显示 WAITING_FOR_USER）：按钮禁用，处理函数二次核时也不发答题 POST','mock',
  {deadline_at:expiredDeadline,posts:answerPosts.length,draft_preserved:true});
 await page.screenshot({path:join(output,'05-expired.png'),fullPage:true});

 // Pending answer replay is disabled too, and its handler refuses to send.
 await page.evaluate(payload=>{
  for(const key of Object.keys(sessionStorage))if(key.startsWith('py-interview:')&&key.endsWith(':pending'))sessionStorage.removeItem(key);
  sessionStorage.setItem(payload.key,JSON.stringify(payload.command));
 },{key:`py-interview:${userId}:${goalId}:pending`,command:{kind:'answer',
  url:`/api/career-goals/${goalId}/interviews/${expiryId}/answers`,key:randomUUID(),
  body:{expected_version:expiryDetail.version,question_id:expiryQuestionId,text:draftText}}});
 await page.reload({waitUntil:'domcontentloaded'});
 // Wait until the expired session has been read back; only then is `expired` true.
 await page.getByTestId('deadline-notice').waitFor({timeout:20000});
 const retryButton=page.getByRole('button',{name:'用同一请求重试'});
 await retryButton.waitFor({timeout:20000});
 assert.equal(await retryButton.isDisabled(),true,'expired_replay_not_disabled');
 await page.getByTestId('unknown-retry').evaluate(container=>{
  const button=container.querySelector('button') as HTMLButtonElement|null;
  if(!button)return;
  const wasDisabled=button.disabled;
  button.removeAttribute('disabled');button.click();
  // Restore the DOM attribute: React will not rewrite a controlled prop whose value
  // did not change, so a later isDisabled() assertion must not see this bypass.
  if(wasDisabled)button.setAttribute('disabled','');
 });
 await delay(800);
 assert.equal(answerPosts.length,0,'expired_replay_sent_answer_post');
 mark('到期后待重放答题请求停用：按钮禁用，绕过按钮点击仍不发 POST','mock',{posts:answerPosts.length});
 await page.unroute(DETAIL);
 await mockDetail(page,body=>({...body,status:'COMPLETED',deadline_at:expiredDeadline,
  report:{stop_reason:'deadline',model_summary:null,questions:1,answers:0,assisted_answers:0,
   observations:[],semantic_scoring_eligible:false,unreviewed_answer_ids:[],next_actions:[]}}));
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByTestId('deadline-end').waitFor({timeout:20000});
 assert.ok((await page.getByTestId('deadline-end').innerText()).includes('本场已到期'));
 mark('按服务端返回展示结束原因：stop_reason=deadline 映射为「本场已到期」','mock');
 await clearMockRoutes(page);

 // ================= 7. SIGN-OUT clears interview cache, but not logged-in reload =================
 // The expiry case left a pending replay behind; drop it so the fresh answer form is usable.
 await page.evaluate(()=>{
  for(const key of Object.keys(sessionStorage))
   if(key.startsWith('py-interview:')&&key.endsWith(':pending'))sessionStorage.removeItem(key);
 });
 await page.goto(`${base}/?goal=${goalId}&view=interview`,{waitUntil:'domcontentloaded'});
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 await page.getByLabel('你的整场面试回答').fill(draftText);
 await page.evaluate(([sid,qid])=>sessionStorage.setItem(`py-interview-draft:${sid}:${qid}`,'遗留旧前缀草稿'),
  [expiryId,expiryQuestionId] as [string,string]);
 let signOutAbort=true;
 await page.route(ANSWERS,async route=>{
  if(route.request().method()!=='POST')return route.continue();
  if(signOutAbort){signOutAbort=false;return route.abort('failed');}
  return route.continue();
 });
 await page.getByRole('button',{name:'提交这一答 →'}).click();
 await page.getByTestId('unknown-retry').waitFor({timeout:20000});
 const beforeLogout=await storageKeys(page,[`py-interview:${userId}:`,'py-interview-draft:']);
 assert.ok(beforeLogout.some(k=>k.endsWith(':current')),'current_pointer_missing');
 assert.ok(beforeLogout.some(k=>k.endsWith(':pending')),'pending_replay_missing');
 assert.ok(beforeLogout.some(k=>k.includes(':draft:')),'new_draft_missing');
 assert.ok(beforeLogout.some(k=>k.startsWith('py-interview-draft:')),'legacy_draft_missing');
 const pendingValue=await page.evaluate(prefix=>{
  const key=Object.keys(sessionStorage).find(k=>k.startsWith(prefix)&&k.endsWith(':pending'));
  return key?sessionStorage.getItem(key):null;
 },`py-interview:${userId}:`);
 assert.ok(pendingValue&&pendingValue.includes(draftText),'pending_replay_must_contain_answer_text');
 // Logged-in reload still restores the same session's draft and pending request.
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByTestId('unknown-retry').waitFor({timeout:20000});
 // The textarea mounts with an empty value; the draft-restoring effect runs right
 // after, so wait for the restored value instead of sampling the first render.
 await page.waitForFunction(expected=>{
  const element=document.querySelector('#interview-answer') as HTMLTextAreaElement|null;
  return !!element&&element.value===expected;
 },draftText,{timeout:15000});
 assert.equal(await page.getByLabel('你的整场面试回答').inputValue(),draftText,'draft_not_restored_while_logged_in');
 mark('已登录刷新：同一场次草稿与待重放请求仍可恢复（能力保留）','real',{keys:beforeLogout.length});
 await page.getByRole('button',{name:'退出登录'}).click();
 await page.getByRole('button',{name:'登录',exact:true}).waitFor({timeout:20000});
 const afterLogout=await storageKeys(page,[`py-interview:${userId}:`,'py-interview-draft:']);
 assert.deepEqual(afterLogout,[],'interview_cache_not_cleared: '+afterLogout.join(','));
 assert.equal(await page.getByTestId('unknown-retry').count(),0,'retry_still_visible');
 mark('退出登录：当前账号场次指针 / 待重放请求（含回答正文）/ 新草稿 / 旧前缀遗留键全部清理','real',
  {cleared_keys:beforeLogout.length,remaining:afterLogout.length});
 await page.screenshot({path:join(output,'06-after-logout.png'),fullPage:true});
 await page.goto(`${base}/?goal=${goalId}&view=interview`,{waitUntil:'domcontentloaded'});
 await page.getByLabel('邮箱').fill(account.email);
 await page.getByLabel('密码').fill(account.password);
 await page.getByRole('button',{name:'登录',exact:true}).click();
 await page.getByRole('button',{name:'退出登录'}).waitFor({timeout:20000});
 await delay(1200);
 assert.equal(await page.getByTestId('unknown-retry').count(),0,'pending_restored_after_login');
 assert.equal(await page.locator('[data-session-id]').count(),0,'session_pointer_restored_after_login');
 await page.getByRole('button',{name:new RegExp(expiryId.slice(0,8))}).first().click();
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 assert.equal(await page.getByLabel('你的整场面试回答').inputValue(),'','old_draft_restored_after_login');
 mark('重新登录：旧草稿未恢复、场次指针未恢复、无待重放请求','real');
 await clearMockRoutes(page);

 // ================= 8. Auth-loss branch remains intact =================
 await page.route(DETAIL,async route=>route.fulfill({status:401,contentType:'application/json',
  body:JSON.stringify({code:'authentication_required'})}));
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByText('登录已过期，请重新登录。').first().waitFor({timeout:20000});
 mark('模拟权限失效：回到登录页并给出提示','mock');
 await clearMockRoutes(page);

 const apiMode=process.env.D01_MOCK_API
  ?'synthetic-http-mock: 3100 内存状态机，无 PG / Redis / scripted Worker，全程模拟'
  :'real-local-stack: 3100 测试 API + scripted Worker + PG / Redis';
 const evidence={kind:'D-01-01 终态开场修复与复验',timestamp:new Date().toISOString(),base_url:base,
  api_mode:apiMode,
  real_stack_note:process.env.D01_MOCK_API
   ?'未使用真实测试栈。'
   :'真实后端每目标只允许一个进行中场次；startFreshSession 通过公开 stop 端点结束遗留 ACTIVE 场次后再开新场（仅测试环境准备，不改生产代码、不调用模型）。',
  case_type_note:process.env.D01_MOCK_API
   ?'case.type=real 仅表示用例原本按真实联调设计；本轮后端为合成服务，因此所有用例均为模拟，不等同于真实 PG/Redis/Worker 联调。'
   :'case.type 区分真实本机联调（真实 API + scripted Worker + PG/Redis）与 page.route 注入的模拟分支；到期/终态/冲突状态由读接口注入，不发付费模型请求。',
  output_dir:output,account:{email:account.email,name:'合成测试学员'},
  fixture:'fixtures/question-banks/agent-developer-trial.json',bank,goal_id:goalId,user_id:userId,
  real_api:{main_session_id:mainId,main_final:{status:finalDetail.status,version:finalDetail.version,
   policy:finalDetail.policy,question_banks:finalDetail.question_banks,
   report:{questions:finalDetail.report.questions,answers:finalDetail.report.answers,
    stop_reason:finalDetail.report.stop_reason,unreviewed_answer_ids:finalDetail.report.unreviewed_answer_ids,
    observations:finalDetail.report.observations.map((o:any)=>({label:o.label,origin:o.origin,evidence_id:o.evidence_id,url:o.evidence_source?.url}))},
   run:finalDetail.run},network_replay_final:{status:networkFinal.status,answers:networkFinal.report.answers}},
  cases,page_errors:pageErrors};
 writeFileSync(join(output,'rework-results.json'),JSON.stringify(evidence,null,2));
 console.log(JSON.stringify({result:'passed',real_cases:cases.filter(c=>c.type==='real').length,
  mock_cases:cases.filter(c=>c.type==='mock').length,cases:cases.map(c=>`[${c.type}] ${c.name}`),
  output_dir:output,page_errors:pageErrors},null,2));
}catch(error){
 writeFileSync(join(output,'rework-results-failure.json'),JSON.stringify({message:String(error),cases,page_errors:pageErrors},null,2));
 await page.screenshot({path:join(output,'rework-failure.png'),fullPage:true}).catch(()=>{});
 console.error(error);
 process.exitCode=1;
}finally{
 await browser.close();
}
