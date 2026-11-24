// D-01-01 checkpoint one: real browser + paired local test API + scripted interview Worker.
// Run with the API (3100), interview Worker (scripted) and Vite (5174) already running:
//   node apps/web/tests/d01-checkpoint-one.mts
// Evidence is written to D01_OUTPUT (default docs/records/evidence/D-01-01).
import assert from 'node:assert/strict';
import {chromium,type Page} from '@playwright/test';
import {readFileSync,writeFileSync,mkdirSync,existsSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const root=resolve(import.meta.dirname,'../../..');
const output=resolve(process.env.D01_OUTPUT??join(root,'docs/records/evidence/D-01-01'));
const base=process.env.D01_BASE_URL??'http://127.0.0.1:5174';
const fixture=JSON.parse(readFileSync(join(root,'fixtures/question-banks/agent-developer-trial.json'),'utf8'));
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const chrome='C:/Program Files/Google/Chrome/Application/chrome.exe';
const executable=[edge,chrome].find(existsSync);
assert.ok(executable,'installed_browser_required');
mkdirSync(output,{recursive:true});

const browser=await chromium.launch({executablePath:executable,headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1100}});
const page=await context.newPage();
const cases:{name:string;status:string;detail?:unknown}[]=[];
const errors:string[]=[];
page.on('pageerror',error=>errors.push(error.message));
const mark=(name:string,detail?:unknown)=>cases.push({name,status:'passed',detail});
const account={email:`d01-check1-${randomUUID()}@example.com`,password:'Synthetic-password-'+randomUUID()};

async function api<T>(p:Page,url:string,init?:RequestInit):Promise<T>{
 const result=await p.evaluate(async({url,init})=>{
  const response=await fetch(url,{...init,credentials:'same-origin',cache:'no-store',
   headers:{'Content-Type':'application/json',...(init?.headers??{})}});
  const body=await response.json().catch(()=>({code:'invalid_response'}));
  return {status:response.status,body};
 },{url,init});
 assert.ok(result.status>=200&&result.status<300,`api ${url} -> ${result.status} ${JSON.stringify(result.body)}`);
 return result.body as T;
}

try{
 await page.goto(base,{waitUntil:'domcontentloaded'});
 await page.getByRole('button',{name:'第一次来？创建账号'}).click();
 await page.getByLabel('称呼').fill('合成测试学员');
 await page.getByLabel('邮箱').fill(account.email);
 await page.getByLabel('密码').fill(account.password);
 await page.getByRole('button',{name:'创建账号',exact:true}).click();
 await page.getByRole('button',{name:'整场面试'}).waitFor();
 mark('注册合成账号并进入整场面试入口');

 await page.getByRole('button',{name:'整场面试'}).click();
 await page.getByLabel('求职意向或补充说明').fill('想在上海找 AI 应用 / Agent 开发实习，用两题练习检验表达。');
 await page.getByRole('button',{name:'保存求职目标'}).click();
 await page.locator('.goal-card').waitFor({timeout:20000});
 mark('创建求职目标');

 // Import and confirm the two-question fixture, reusing the existing question-bank HTTP API.
 const imported=await page.evaluate(async(document)=>{
  const previewResponse=await fetch('/api/question-banks/imports',{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({document})});
  const preview=await previewResponse.json();
  if(!previewResponse.ok)throw new Error('preview_failed '+JSON.stringify(preview));
  const commitResponse=await fetch(`/api/question-banks/imports/${preview.preview_id}/commit`,{method:'POST',credentials:'same-origin',
   headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({})});
  const commit=await commitResponse.json();
  if(!commitResponse.ok)throw new Error('commit_failed '+JSON.stringify(commit));
  return {preview_id:preview.preview_id,item_count:preview.document.items.length,
   items_without_reference:preview.items_without_reference,bank_id:commit.bank_id,version:commit.version};
 },fixture);
 assert.equal(imported.item_count,2);
 assert.equal(imported.version,1);
 mark('导入并确认两题私有题库 v1',imported);

 // Reload so the panel picks up the newly committed bank through GET /api/question-banks.
 await page.reload({waitUntil:'domcontentloaded'});
 await page.getByRole('button',{name:'整场面试'}).waitFor();
 await page.getByLabel('题库与版本').waitFor({timeout:20000});
 await page.getByLabel('题库与版本').locator('option',{hasText:'Agent开发岗位试运行'}).waitFor({state:'attached',timeout:20000});
 const options=await page.getByLabel('题库与版本').locator('option').allTextContents();
 assert.ok(options.some(text=>text.includes('Agent开发岗位试运行（无简历）')&&text.includes('v1')),'bank_option_missing');
 await page.getByLabel('题库与版本').selectOption(`${imported.bank_id}|${imported.version}`);
 await page.screenshot({path:join(output,'01-bank-selection.png'),fullPage:true});
 mark('题库下拉列出已启用 v1 并选中',options);

 await page.getByRole('button',{name:'开始整场面试 →'}).click();
 await page.locator('[data-session-id]').waitFor({timeout:20000});
 const sessionId=await page.locator('[data-session-id]').getAttribute('data-session-id');
 assert.ok(sessionId,'session_id_missing');
 const goalId=new URL(page.url()).searchParams.get('goal');
 assert.ok(goalId,'goal_id_missing');
 await page.locator('[data-testid="interview-question"]').waitFor({timeout:60000});
 await page.getByText('状态 · 可作答').waitFor({timeout:60000});
 const question=await page.locator('[data-testid="interview-question"]').innerText();
 assert.ok(question.includes('LangGraph'),'unexpected_first_question');
 await page.screenshot({path:join(output,'02-first-question.png'),fullPage:true});

 const detail=await api<Record<string,unknown>>(page,`/api/career-goals/${goalId}/interviews/${sessionId}`);
 const events=detail.events as {id:string;kind:string;payload:Record<string,unknown>}[];
 const questions=events.filter(e=>e.kind==='question');
 assert.equal(detail.status,'WAITING_FOR_USER');
 assert.equal(questions.length,1);
 assert.equal((detail.question_banks as {version:number}[])[0].version,1);
 assert.equal((detail.policy as {max_questions:number}).max_questions,2);
 assert.equal((detail.policy as {role:string}).role,'ai-application');
 assert.equal(detail.report,null);
 assert.deepEqual(detail.observations,[]);
 mark('真实 API 回读：WAITING_FOR_USER / 1 题 / 冻结 v1 / 考中反馈隐藏',{
  session_id:detail.id,version:detail.version,status:detail.status,goal_version:detail.goal_version,
  question_event_id:questions[0].id,question_id_in_payload:questions[0].payload.question_id,
  policy:detail.policy,deadline_at:detail.deadline_at,question_banks:detail.question_banks,
  run:detail.run,report:detail.report,observations:detail.observations
 });

 const evidence={kind:'D-01-01 检查点一',timestamp:new Date().toISOString(),
  base_url:base,account:{email:account.email,name:'合成测试学员'},
  fixture:'fixtures/question-banks/agent-developer-trial.json',imported,
  goal_id:goalId,session_id:sessionId,first_question_text:question,
  api_detail:{id:detail.id,version:detail.version,status:detail.status,goal_version:detail.goal_version,
   policy:detail.policy,pack_version:detail.pack_version,question_banks:detail.question_banks,
   deadline_at:detail.deadline_at,events:events.map(e=>({id:e.id,seq:e.seq,kind:e.kind})),
   observations:detail.observations,report:detail.report,run:detail.run},
  cases,page_errors:errors};
 writeFileSync(join(output,'checkpoint-one.json'),JSON.stringify(evidence,null,2));
 console.log(JSON.stringify({result:'passed',cases:cases.map(c=>c.name),session_id:sessionId,page_errors:errors},null,2));
}catch(error){
 writeFileSync(join(output,'checkpoint-one-failure.json'),JSON.stringify({message:String(error),cases,page_errors:errors},null,2));
 await page.screenshot({path:join(output,'checkpoint-one-failure.png'),fullPage:true}).catch(()=>{});
 console.error(error);
 process.exitCode=1;
}finally{
 await browser.close();
}
