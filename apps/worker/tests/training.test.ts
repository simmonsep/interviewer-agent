import assert from 'node:assert/strict';
import {test,after} from 'node:test';
import {randomUUID} from 'node:crypto';
import {mkdirSync,writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {createTrainingHarness,scriptedAssessment,waitUntil} from './training-support.ts';
import {SpendLedger} from '@pi-interview/runtime-pi/deepseek';
import {createPI09AssessmentExecutor,loadPI09Config} from '../src/assessment.ts';
import {spawn,type ChildProcess} from 'node:child_process';

const root=process.cwd(),output=process.env.PI09_OUTPUT??join(root,'.private/pi109',randomUUID());mkdirSync(output,{recursive:true});
const h=await createTrainingHarness(root),a=await h.account('a'),b=await h.account('b');
const headers=(cookie=a.cookie,key?:string)=>({cookie,origin:h.config.baseURL,...(key?{'idempotency-key':key}:{})});
const cases:{name:string;status:string}[]=[];
function check(name:string,fn:()=>Promise<void>){test(name,{timeout:20000},async()=>{await fn();cases.push({name,status:'passed'});});}
async function session(key=randomUUID()){
 const target=await h.app.inject({method:'POST',url:'/api/training-targets/',headers:headers(a.cookie,randomUUID()),payload:{role_package_id:'agent-intern',level:'intern'}});assert.equal(target.statusCode,201,target.body);
 const result=await h.app.inject({method:'POST',url:'/api/practice-sessions',headers:headers(a.cookie,key),payload:{target_id:target.json().id,question_id:'q-tool-use-1'}});assert.equal(result.statusCode,201,result.body);return result.json();
}
async function submit(s:{id:string;version:number},text='Schema通过仍需服务端按身份和资源归属检查权限。',key=randomUUID()){
 const result=await h.app.inject({method:'POST',url:`/api/practice-sessions/${s.id}/answers`,headers:headers(a.cookie,key),payload:{expected_version:s.version,text}});assert.equal(result.statusCode,202,result.body);return {operation:result.json(),text,key};
}
async function message(operationId:string){return (await h.pool.query('SELECT j.id AS "jobId",o.id AS "operationId",o.expected_version AS "resourceVersion" FROM app.operations o JOIN app.background_jobs j ON j.operation_id=o.id WHERE o.id=$1',[operationId])).rows[0];}
after(async()=>{writeFileSync(join(output,'cases.json'),JSON.stringify(cases,null,2)+'\n');await h.close();});
let completedSession:any,completedSubmission:any;
check('T01 authenticated role catalog exposes candidate questions without labels or rubric',async()=>{
 assert.equal((await h.app.inject({method:'GET',url:'/api/catalog/questions'})).statusCode,401);
 const r=await h.app.inject({method:'GET',url:'/api/catalog/questions',headers:headers()});assert.equal(r.statusCode,200);assert.ok(r.json().items.length>0);
 assert.ok(r.json().items.every((q:any)=>q.semantic_scoring_eligible===false));assert.ok(!/rubric|acceptable_answers|misconceptions|expected_kind/.test(r.body));
});
check('T02 role_only sessions freeze the question and create idempotently without materials',async()=>{
 const s=await session(),key=randomUUID(),payload={target_id:s.target_id,question_id:s.question.id};
 const results=await Promise.all(Array.from({length:5},()=>h.app.inject({method:'POST',url:'/api/practice-sessions',headers:headers(a.cookie,key),payload})));
 assert.equal(new Set(results.map(r=>r.json().id)).size,1);assert.equal(results.filter(r=>r.statusCode===201).length,1);
 assert.equal(s.status,'WAITING_FOR_ANSWER');assert.equal(s.context_mode,'role_only');assert.equal(s.mode,'practice');assert.equal(s.answer,null);assert.equal(s.feedback,null);
 assert.ok(!JSON.stringify(s).includes('rubric'));
});
check('T03 cross-user resource and operation access consistently denied; extra identity rejected',async()=>{
 const s=await session(),created=await submit(s);
 for(const url of [`/api/practice-sessions/${s.id}`,`/api/operations/${created.operation.id}`,`/api/operations/${created.operation.id}/events`])assert.equal((await h.app.inject({method:'GET',url,headers:headers(b.cookie)})).statusCode,404);
 for(const action of ['cancel','resume'])assert.equal((await h.app.inject({method:'POST',url:`/api/operations/${created.operation.id}/${action}`,headers:headers(b.cookie),payload:{}})).statusCode,404);
 const wrong=await h.app.inject({method:'POST',url:`/api/practice-sessions/${s.id}/answers`,headers:headers(b.cookie,randomUUID()),payload:{expected_version:1,text:'forged'}});assert.equal(wrong.statusCode,404);
 const extra=await h.app.inject({method:'POST',url:`/api/practice-sessions/${s.id}/answers`,headers:headers(a.cookie,randomUUID()),payload:{expected_version:1,text:'x',user_id:b.userId}});assert.equal(extra.statusCode,400);
 await h.repository.cancel(a,created.operation.id);
});
check('T04 concurrent answer submission atomically stores one original answer and one job/outbox',async()=>{
 const s=await session(),key=randomUUID(),payload={expected_version:1,text:'Schema只是形状校验。\n\n授权应依据服务端身份和资源归属。'};
 const results=await Promise.all(Array.from({length:5},()=>h.app.inject({method:'POST',url:`/api/practice-sessions/${s.id}/answers`,headers:headers(a.cookie,key),payload})));
 assert.ok(results.every(r=>r.statusCode===202));assert.equal(new Set(results.map(r=>r.json().id)).size,1);
 const count=await h.pool.query('SELECT count(*)::int n FROM app.answer_attempts a JOIN app.operations o ON o.session_id=a.session_id JOIN app.background_jobs j ON j.operation_id=o.id JOIN app.outbox_events e ON e.job_id=j.id WHERE a.session_id=$1',[s.id]);assert.equal(count.rows[0].n,1);
 const saved=await h.training.get(a,s.id);assert.equal(saved.answer?.text,payload.text);assert.equal(saved.answer?.origin,'user_submitted');assert.equal(saved.status,'EVALUATING');assert.equal(saved.feedback,null);
 completedSession=s;completedSubmission={operation:results[0].json(),text:payload.text,key};
});
check('T05 real Pi tool protocol gates and commits feedback with exact answer/source citations',async()=>{
 assert.equal((await h.execute(a,completedSubmission.operation.id)).status,'SUCCEEDED');
 const saved=await h.training.get(a,completedSession.id);assert.equal(saved.status,'COMPLETED');assert.ok(saved.feedback);assert.equal(saved.feedback.semantic_scoring_eligible,false);assert.equal(saved.feedback.origin,'scripted_test');
 assert.ok(!('verdict' in saved.feedback));assert.ok(saved.feedback.citations.some(c=>c.kind==='answer'&&completedSubmission.text.includes(c.quote)));assert.ok(saved.feedback.citations.some(c=>c.kind==='evidence'&&c.source?.url.startsWith('https://')));
 const row=await h.pool.query('SELECT count(*)::int n FROM app.assessment_revisions WHERE session_id=$1',[saved.id]);assert.equal(row.rows[0].n,1);
});
check('T06 answer is immutable; replay after completion returns same operation rather than reevaluation',async()=>{
 const before=h.controls.calls;
 const replay=await submit(completedSession,completedSubmission.text,completedSubmission.key);assert.equal(replay.operation.id,completedSubmission.operation.id);assert.equal(replay.operation.status,'SUCCEEDED');
 const conflict=await h.app.inject({method:'POST',url:`/api/practice-sessions/${completedSession.id}/answers`,headers:headers(a.cookie,completedSubmission.key),payload:{expected_version:1,text:'changed'}});assert.equal(conflict.statusCode,409);assert.equal(h.controls.calls,before);
});
check('T07 invalid citations fail the gate and never create user-visible feedback',async()=>{
 const s=await session(),r=await submit(s);h.controls.invalid=true;try{assert.equal((await h.execute(a,r.operation.id)).status,'FAILED');}finally{h.controls.invalid=false;}
 const saved=await h.training.get(a,s.id);assert.equal(saved.feedback,null);assert.ok(saved.answer);assert.equal((await h.pool.query('SELECT count(*)::int n FROM app.assessment_revisions WHERE session_id=$1',[s.id])).rows[0].n,0);
});
check('T08 unavailable model preserves answer and resumes same operation after explicit retry',async()=>{
 const s=await session(),r=await submit(s);h.controls.unavailable=true;
 try{assert.equal((await h.execute(a,r.operation.id)).status,'WAITING_CREDENTIALS');}finally{h.controls.unavailable=false;}
 assert.ok((await h.training.get(a,s.id)).answer);assert.equal((await h.training.get(a,s.id)).feedback,null);
 const resumed=await h.app.inject({method:'POST',url:`/api/operations/${r.operation.id}/resume`,headers:headers(),payload:{}});assert.equal(resumed.statusCode,200);assert.equal(resumed.json().id,r.operation.id);
 assert.equal((await h.execute(a,r.operation.id)).status,'SUCCEEDED');
});
check('T09 persisted candidate reused after expired claim without another model/executor call',async()=>{
 const s=await session(),r=await submit(s),m=await message(r.operation.id),first=await h.repository.claim(m);assert.ok(first);
 const context=await h.repository.assessmentContext(first),output=await scriptedAssessment(context);assert.equal(await h.repository.saveAssessment(first,output),true);
 const before=h.controls.calls;await h.pool.query("UPDATE app.background_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1",[m.jobId]);
 assert.equal((await h.execute(a,r.operation.id)).status,'SUCCEEDED');assert.equal(h.controls.calls,before);assert.equal(await h.repository.complete(first),false);
});
check('T10 cancellation while evaluation runs preserves answer and refuses late candidate',async()=>{
 const s=await session(),r=await submit(s);h.controls.delayMs=500;
 await h.dispatcher.dispatch(0);await waitUntil(async()=>(await h.repository.get(a,r.operation.id)).status==='RUNNING');
 await h.repository.cancel(a,r.operation.id);await waitUntil(async()=>await h.queue.getActiveCount()===0);h.controls.delayMs=0;
 const saved=await h.training.get(a,s.id);assert.equal(saved.status,'CANCELLED');assert.ok(saved.answer);assert.equal(saved.feedback,null);
});
check('T11 session version or deletion invalidates an already-saved candidate',async()=>{
 for(const deleted of [false,true]){
  const s=await session(),r=await submit(s),claim=await h.repository.claim(await message(r.operation.id));assert.ok(claim);
  await h.repository.saveAssessment(claim,await scriptedAssessment(await h.repository.assessmentContext(claim)));
  await h.pool.query(deleted?'UPDATE app.practice_sessions SET deleted_at=clock_timestamp() WHERE id=$1':"UPDATE app.practice_sessions SET version=version+1,status='CANCELLED' WHERE id=$1",[s.id]);
  assert.equal(await h.repository.heartbeat(claim),false);assert.equal(await h.repository.complete(claim),false);
  assert.equal((await h.pool.query('SELECT count(*)::int n FROM app.assessment_revisions WHERE session_id=$1',[s.id])).rows[0].n,0);
  if(deleted)await assert.rejects(h.training.get(a,s.id),/resource_unavailable/);
 }
});
check('T12 SQL business-event SSE replays with Last-Event-ID and excludes private model data',async()=>{
 const s=await session(),r=await submit(s,'SSE-PRIVATE-MARKER 仍须检查权限。');await h.execute(a,r.operation.id);
 const events=await h.training.events(a,r.operation.id,'0');assert.deepEqual(events.map(e=>e.status),['PENDING','RUNNING','SUCCEEDED']);
 const response=await fetch(`${h.config.baseURL}/api/operations/${r.operation.id}/events?after=0`,{headers:{cookie:a.cookie,'Last-Event-ID':events[0].event_id},signal:AbortSignal.timeout(5000)});
 assert.equal(response.status,200);assert.match(response.headers.get('content-type')??'',/text\/event-stream/);const text=await response.text();
 assert.ok(!text.includes(`id: ${events[0].event_id}\n`));assert.ok(text.includes(`id: ${events[1].event_id}\n`));assert.ok(text.includes('SUCCEEDED'));assert.ok(!/SSE-PRIVATE-MARKER|thinking|rubric|candidate|apiKey|password/.test(text));
});
check('T13 an open SSE connection stops after logout instead of retaining stale authorization',async()=>{
 const s=await session(),r=await submit(s),response=await fetch(`${h.config.baseURL}/api/operations/${r.operation.id}/events`,{headers:{cookie:a.cookie},signal:AbortSignal.timeout(5000)});const reader=response.body!.getReader();await reader.read();
 const logout=await h.app.inject({method:'POST',url:'/api/auth/sign-out',headers:headers(),payload:{}});assert.equal(logout.statusCode,200);
 while(!(await reader.read()).done){}await reader.cancel();
 await h.repository.cancel(a,r.operation.id);
 const a2=await h.account('replacement');a.userId=a2.userId;a.cookie=a2.cookie;
});
check('T14 recreated API reads persisted answer feedback and events; old cookies cannot read another user',async()=>{
 await h.recreate();
 const response=await h.app.inject({method:'GET',url:`/api/practice-sessions/${completedSession.id}`,headers:headers()});assert.equal(response.statusCode,404);
 const row=await h.pool.query('SELECT status FROM app.practice_sessions WHERE id=$1',[completedSession.id]);assert.equal(row.rows[0].status,'COMPLETED');
 const own=await session(),submitted=await submit(own);await h.execute(a,submitted.operation.id);await h.recreate();
 const restored=await h.app.inject({method:'GET',url:`/api/practice-sessions/${own.id}`,headers:headers()});assert.equal(restored.statusCode,200);assert.equal(restored.json().feedback.semantic_scoring_eligible,false);assert.equal(restored.json().answer.text,submitted.text);
});
check('T15 separate local authorization caps reservations below CNY2 without reading a key',async()=>{
 const config=loadPI09Config(root),ledger=new SpendLedger(join(output,'synthetic-spend-ledger.json'),config);
 for(let i=0;i<12;i++)ledger.reserve();assert.ok(ledger.snapshot().reserved_cny<=2);assert.throws(()=>ledger.reserve(),/spend_budget_exhausted/);
 await assert.rejects(createPI09AssessmentExecutor(root,false)(h.controls.contexts[0],new AbortController().signal),/model_not_enabled/);
});
check('T16 actual API process restarts restore pending answer and committed feedback',async()=>{
 let child:ChildProcess|null=null;
 async function start(){
  const p=spawn(process.execPath,['apps/worker/tests/api-process.ts',h.database],{cwd:root,windowsHide:true,stdio:['ignore','ignore','pipe','ipc']});child=p;
  await new Promise<void>((resolve,reject)=>{const timer=setTimeout(()=>{p.kill();reject(Error('api_child_start_timeout'));},8000);p.once('message',()=>{clearTimeout(timer);resolve();});p.once('exit',code=>{clearTimeout(timer);if(code!==0)reject(Error('api_child_start_failed'));});});
 }
 async function stop(){const p=child;if(!p)return;child=null;await new Promise<void>((resolve,reject)=>{const timer=setTimeout(()=>{p.kill();reject(Error('api_child_stop_timeout'));},8000);p.once('exit',()=>{clearTimeout(timer);resolve();});p.send('stop');});}
 const base='http://127.0.0.1:3379',s=await session(),text='API进程重启后仍应保留的合成回答：Schema不代替授权。';
 try{
  await start();
  const accepted=await fetch(`${base}/api/practice-sessions/${s.id}/answers`,{method:'POST',headers:{cookie:a.cookie,origin:base,'content-type':'application/json','idempotency-key':randomUUID()},body:JSON.stringify({expected_version:1,text})});assert.equal(accepted.status,202);const op=await accepted.json() as {id:string};
  await stop();await start();
  const pending=await (await fetch(`${base}/api/practice-sessions/${s.id}`,{headers:{cookie:a.cookie}})).json() as any;assert.equal(pending.answer.text,text);assert.equal(pending.status,'EVALUATING');
  assert.equal((await h.execute(a,op.id)).status,'SUCCEEDED');
  await stop();await start();
  const restored=await (await fetch(`${base}/api/practice-sessions/${s.id}`,{headers:{cookie:a.cookie}})).json() as any;assert.equal(restored.status,'COMPLETED');assert.equal(restored.answer.text,text);assert.equal(restored.feedback.semantic_scoring_eligible,false);
 }finally{await stop();}
});
