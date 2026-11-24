import assert from "node:assert/strict";
import {test,after} from "node:test";
import {readFileSync,writeFileSync,mkdirSync} from "node:fs";
import {join,resolve} from "node:path";
import {randomUUID} from "node:crypto";
import {setTimeout as delay} from "node:timers/promises";
import {Pool} from "pg";
import {Queue,type Worker} from "bullmq";
import {Redis} from "ioredis";
import {migrate} from "../../../packages/storage/src/migrate.ts";
import {TargetRepository} from "@pi-interview/storage";
import {OperationRepository,type QueueMessage,type Claim} from "@pi-interview/storage/operations";
import {loadLocalConnection,createLocalPool} from "../../api/src/local-config.ts";
import {buildDevServer} from "../../api/src/server.ts";
import {connectRedis,OutboxDispatcher,createTaskWorker,parseMessage,type RedisConfig} from "../src/queue.ts";

const root=process.cwd(),c=loadLocalConnection(root);
const output=resolve(process.env.PI08_OUTPUT??join(root,".private/pi108",randomUUID()));mkdirSync(output,{recursive:true});
const rc=JSON.parse(readFileSync(join(root,".private/redis-PI-08/connection.json"),"utf8")) as RedisConfig;
const db="pi08_"+randomUUID().replaceAll("-","");
const admin=new Pool({host:c.host,port:c.port,user:c.adminUser,password:c.adminPassword,database:"postgres",max:1});
await admin.query(`CREATE DATABASE ${db} OWNER pi_app_owner`);
const pool=createLocalPool(c,db),authPool=createLocalPool(c,db,true);
await migrate(pool,join(root,"packages/storage/migrations"));
const config={mode:"test",host:"127.0.0.1",port:3000,baseURL:"http://127.0.0.1:3000",secret:c.authSecret};
let app=buildDevServer(pool,authPool,config);await app.ready();
const repository=new OperationRepository(pool),targets=new TargetRepository(pool);
const publish=connectRedis(rc),queueName="pi08-test-"+randomUUID();
publish.on("error",()=>{});
const queue=new Queue(queueName,{connection:publish}),dispatcher=new OutboxDispatcher(repository,queue);
const workers:Worker[]=[],connections:Redis[]=[];
const cases:{name:string;status:string}[]=[];
let userA="",userB="",cookieA="",cookieB="";
for(const who of ['a','b']){
 const r=await app.inject({method:"POST",url:"/api/auth/sign-up/email",headers:{origin:config.baseURL},payload:{email:who+"@example.invalid",name:"Synthetic "+who,password:"Synthetic-password-"+randomUUID()}});
 assert.equal(r.statusCode,200,r.body);
 const set=r.headers["set-cookie"],cookie=(Array.isArray(set)?set:[set]).map(v=>String(v).split(';')[0]).join('; ');
 if(who==='a'){userA=r.json().user.id;cookieA=cookie;}else{userB=r.json().user.id;cookieB=cookie;}
}
function check(name:string,fn:()=>Promise<void>){test(name,{timeout:20000},async()=>{await fn();cases.push({name,status:"passed"});});}
async function target(){return (await targets.create({userId:userA},randomUUID(),{role_package_id:"agent-intern",level:"intern",focus:"before"})).target;}
async function operation(focus="after"){
 const t=await target();const o=(await repository.enqueue({userId:userA},t.id,randomUUID(),1,focus)).operation;
 return {t,o,message:await message(o.id)};
}
async function message(id:string):Promise<QueueMessage>{
 const r=await pool.query('SELECT j.id AS "jobId",o.id AS "operationId",o.expected_version AS "resourceVersion" FROM app.operations o JOIN app.background_jobs j ON j.operation_id=o.id WHERE o.id=$1',[id]);return r.rows[0];
}
function start(prepare?:(c:Claim,s:AbortSignal)=>Promise<void>){
 const connection=connectRedis(rc,true);connection.on("error",()=>{});connections.push(connection);
 const worker=createTaskWorker(repository,connection,queueName,{prepare});workers.push(worker);return worker;
}
async function stopWorkers(){for(const w of workers.splice(0))await w.close();for(const r of connections.splice(0))await r.quit();}
async function until(fn:()=>Promise<boolean>){const end=Date.now()+10000;while(Date.now()<end){if(await fn())return;await delay(20);}throw Error("condition_timeout");}
async function terminal(id:string){await until(async()=>['SUCCEEDED','FAILED','CANCELLED'].includes((await repository.get({userId:userA},id)).status));return repository.get({userId:userA},id);}
const headers=(cookie=cookieA,key?:string)=>({cookie,origin:config.baseURL,...(key?{'idempotency-key':key}:{})});
after(async()=>{
 writeFileSync(join(output,"cases.json"),JSON.stringify(cases,null,2)+'\n');
 await stopWorkers();
 // Only this random test queue is removed. Never FLUSHDB/FLUSHALL or touch the development queue.
 await queue.obliterate({force:true});await queue.close();await publish.quit();
 await app.close();await authPool.end();await pool.end();
 if(!/^pi08_[a-f0-9]{32}$/.test(db))throw Error("invalid_test_database");
 await admin.query(`DROP DATABASE ${db}`);await admin.end();
});

check("J01 authenticated real Redis and noeviction policy",async()=>{
 assert.equal(await publish.ping(),'PONG');
 assert.deepEqual(await publish.config('GET','maxmemory-policy'),['maxmemory-policy','noeviction']);
 const unauth=new Redis({host:rc.host,port:rc.port,maxRetriesPerRequest:1,retryStrategy:()=>null,lazyConnect:true,enableReadyCheck:false});
 unauth.on('error',()=>{});await unauth.connect();await assert.rejects(unauth.ping(),/NOAUTH/);unauth.disconnect();
 assert.equal(parseMessage({jobId:randomUUID(),operationId:randomUUID(),resourceVersion:1,apiKey:'forbidden'}),null);
});
check("J02 five concurrent commands produce one operation job and outbox with payload conflict",async()=>{
 const t=await target(),key=randomUUID();
 const results=await Promise.all(Array.from({length:5},()=>repository.enqueue({userId:userA},t.id,key,1,'once')));
 assert.equal(new Set(results.map(r=>r.operation.id)).size,1);assert.equal(results.filter(r=>r.created).length,1);
 const id=results[0].operation.id;
 assert.equal((await pool.query('SELECT count(*)::int n FROM app.background_jobs j JOIN app.outbox_events e ON e.job_id=j.id WHERE j.operation_id=$1',[id])).rows[0].n,1);
 assert.equal((await targets.get({userId:userA},t.id)).version,1);
 await assert.rejects(repository.enqueue({userId:userA},t.id,key,1,'different'),/idempotency_conflict/);
 await repository.cancel({userId:userA},id);
});
check("J03 failed outbox insertion rolls back operation and job in real SQL",async()=>{
 const t=await target(),key=randomUUID();
 await pool.query("CREATE FUNCTION app.reject_test_outbox() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected_outbox_failure'; END; $$; CREATE TRIGGER reject_test_outbox BEFORE INSERT ON app.outbox_events FOR EACH ROW EXECUTE FUNCTION app.reject_test_outbox()");
 try{await assert.rejects(repository.enqueue({userId:userA},t.id,key,1,'never'),/injected_outbox_failure/);}
 finally{await pool.query('DROP TRIGGER reject_test_outbox ON app.outbox_events; DROP FUNCTION app.reject_test_outbox()');}
 assert.equal((await pool.query('SELECT count(*)::int n FROM app.operations WHERE request_id=$1',[key])).rows[0].n,0);
 assert.equal((await pool.query('SELECT count(*)::int n FROM app.background_jobs j LEFT JOIN app.operations o ON o.id=j.operation_id WHERE o.id IS NULL')).rows[0].n,0);
});
check("J04 async HTTP commands enforce auth owner schema origin and deleted state",async()=>{
 const {t,o}=await operation();
 assert.equal((await app.inject({method:'GET',url:`/api/operations/${o.id}`})).statusCode,401);
 for(const method of ['GET','POST'] as const){
  const path=`/api/operations/${o.id}`+(method==='POST'?'/cancel':'');
  const r=await app.inject({method,url:path,headers:headers(cookieB),...(method==='POST'?{payload:{}}:{})});assert.equal(r.statusCode,404);
 }
 const bad=await app.inject({method:'POST',url:`/api/training-targets/${t.id}/focus-operations`,headers:headers(cookieA,randomUUID()),payload:{expected_version:1,focus:'x',user_id:userB}});assert.equal(bad.statusCode,400);
 const origin=await app.inject({method:'POST',url:`/api/operations/${o.id}/cancel`,headers:{cookie:cookieA,origin:'https://example.invalid'},payload:{}});assert.equal(origin.statusCode,403);
 await pool.query('UPDATE app.training_targets SET deleted_at=clock_timestamp() WHERE id=$1',[t.id]);
 await assert.rejects(repository.get({userId:userA},o.id),/resource_unavailable/);
 await assert.rejects(repository.enqueue({userId:userA},t.id,randomUUID(),1,'x'),/resource_unavailable/);
 assert.equal(await repository.claim(await message(o.id)),null);
});
check("J05 actual BullMQ publisher failure keeps SQL pending for recovery",async()=>{
 const {o}=await operation();
 const brokenConnection=connectRedis(rc);brokenConnection.on('error',()=>{});
 const broken=new Queue(queueName,{connection:brokenConnection});broken.on('error',()=>{});await broken.waitUntilReady();
 brokenConnection.disconnect(false);await until(async()=>brokenConnection.status==='end');
 try{await assert.rejects(new OutboxDispatcher(repository,broken).dispatch(0),/Connection is closed/);}
 finally{await broken.close();}
 const r=await pool.query('SELECT e.dispatched_at,j.status FROM app.outbox_events e JOIN app.background_jobs j ON j.id=e.job_id WHERE j.operation_id=$1',[o.id]);
 assert.equal(r.rows[0].dispatched_at,null);assert.equal(r.rows[0].status,'PENDING');
 await repository.cancel({userId:userA},o.id);
});
check("J06 crash after real publish redelivers only IDs; two Workers commit once",async()=>{
 const {t,o}=await operation('duplicate-safe');
 class CrashAfterPublish extends OperationRepository {override async markDispatched(){throw Error('injected_after_publish');}}
 await assert.rejects(new OutboxDispatcher(new CrashAfterPublish(pool),queue).dispatch(0),/injected_after_publish/);
 await dispatcher.dispatch(0);
 const waiting=await queue.getWaiting();assert.ok(waiting.length>=2);
 assert.ok(waiting.every(j=>Object.keys(j.data).sort().join(',')==='jobId,operationId,resourceVersion'));
 const a=start(),b=start();await Promise.all([a.waitUntilReady(),b.waitUntilReady()]);
 assert.equal((await terminal(o.id)).status,'SUCCEEDED');
 await until(async()=>await queue.getActiveCount()===0);await stopWorkers();
 const result=await targets.get({userId:userA},t.id);assert.equal(result.version,2);assert.equal(result.focus,'duplicate-safe');
 assert.equal((await pool.query('SELECT attempt FROM app.background_jobs WHERE operation_id=$1',[o.id])).rows[0].attempt,1);
});
check("J07 lost Redis queue entries rebuild from already-dispatched SQL",async()=>{
 const {t,o}=await operation('rebuilt');await dispatcher.dispatch(0);
 await queue.drain(true);assert.equal(await queue.getWaitingCount(),0);
 assert.ok((await pool.query('SELECT dispatched_at FROM app.outbox_events e JOIN app.background_jobs j ON j.id=e.job_id WHERE j.operation_id=$1',[o.id])).rows[0].dispatched_at);
 await dispatcher.dispatch(0);start();assert.equal((await terminal(o.id)).status,'SUCCEEDED');await stopWorkers();
 assert.equal((await targets.get({userId:userA},t.id)).version,2);
});
check("J08 expired attempt reclaimed; old fencing token and mismatched payload rejected",async()=>{
 const {o,message:m}=await operation();
 assert.equal(await repository.claim({...m,resourceVersion:2}),null);
 const first=await repository.claim(m);assert.ok(first);
 assert.equal(await repository.claim(m),null);
 await pool.query("UPDATE app.background_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1",[m.jobId]);
 const second=await repository.claim(m);assert.ok(second);assert.equal(second.attempt,2);assert.ok(second.fencingToken>first.fencingToken);
 assert.equal(await repository.complete(first),false);assert.equal(await repository.complete({...second,userId:userB}),false);
 assert.equal(await repository.complete(second),true);assert.equal(await repository.complete(second),false);
 assert.equal((await repository.get({userId:userA},o.id)).status,'SUCCEEDED');
});
check("J09 cancellation while Worker runs rejects late result and repeated delivery",async()=>{
 const {t,o,message:m}=await operation();
 let entered!:()=>void,release!:()=>void;
 const enteredPromise=new Promise<void>(r=>entered=r),gate=new Promise<void>(r=>release=r);
 const worker=start(async()=>{entered();await gate;});await worker.waitUntilReady();
 await dispatcher.dispatch(0);await enteredPromise;
 const r=await app.inject({method:'POST',url:`/api/operations/${o.id}/cancel`,headers:headers(),payload:{}});assert.equal(r.statusCode,200);assert.equal(r.json().status,'CANCELLED');
 release();await until(async()=>await queue.getActiveCount()===0);await stopWorkers();
 assert.equal(await repository.claim(m),null);assert.equal((await repository.cancel({userId:userA},o.id)).status,'CANCELLED');
 assert.equal((await targets.get({userId:userA},t.id)).version,1);
});
check("J10 changed or deleted resource invalidates an already-running claim",async()=>{
 for(const deleted of [false,true]){
  const {t,o,message:m}=await operation();const claim=await repository.claim(m);assert.ok(claim);
  if(deleted)await pool.query('UPDATE app.training_targets SET deleted_at=clock_timestamp() WHERE id=$1',[t.id]);
  else await targets.updateFocus({userId:userA},t.id,1,'newer');
  assert.equal(await repository.heartbeat(claim),false);assert.equal(await repository.complete(claim),false);
  assert.equal((await pool.query('SELECT status,error_code FROM app.operations WHERE id=$1',[o.id])).rows[0].error_code,'resource_changed');
 }
});
check("J11 heartbeat renews live lease but cannot resurrect expired attempt",async()=>{
 const {o,message:m}=await operation();const claim=await repository.claim(m,1000);assert.ok(claim);
 assert.equal(await repository.heartbeat(claim,3000),true);
 assert.equal((await pool.query("SELECT lease_until>clock_timestamp()+interval '2 seconds' AS renewed FROM app.background_jobs WHERE id=$1",[m.jobId])).rows[0].renewed,true);
 await pool.query("UPDATE app.background_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1",[m.jobId]);
 assert.equal(await repository.heartbeat(claim),false);assert.equal(await repository.complete(claim),false);
 await repository.cancel({userId:userA},o.id);
});
check("J12 lease rechecked after waiting for business row lock",async()=>{
 const {t,o,message:m}=await operation();const claim=await repository.claim(m,200);assert.ok(claim);
 const blocker=await pool.connect();
 try{
  await blocker.query('BEGIN');await blocker.query('SELECT id FROM app.training_targets WHERE id=$1 FOR UPDATE',[t.id]);
  const completion=repository.complete(claim);
  await blocker.query('SELECT pg_sleep(0.4)');await blocker.query('COMMIT');
  assert.equal(await completion,false);
 }finally{await blocker.query('ROLLBACK');blocker.release();}
 assert.equal((await targets.get({userId:userA},t.id)).version,1);await repository.cancel({userId:userA},o.id);
});
check("J13 failed work retries through SQL and stops after three attempts",async()=>{
 const {t,o}=await operation();const w=start(async()=>{throw Error('injected_work_failure');});await w.waitUntilReady();
 for(let attempt=1;attempt<=3;attempt++){
  await dispatcher.dispatch(0);
  await until(async()=>{const r=await pool.query('SELECT attempt,status FROM app.background_jobs WHERE operation_id=$1',[o.id]);return r.rows[0].attempt===attempt&&r.rows[0].status===(attempt===3?'FAILED':'PENDING');});
 }
 await stopWorkers();assert.equal((await repository.get({userId:userA},o.id)).status,'FAILED');assert.equal((await targets.get({userId:userA},t.id)).version,1);
});
check("J14 different commands for one resource version allow one business effect",async()=>{
 const t=await target();
 const one=await repository.enqueue({userId:userA},t.id,randomUUID(),1,'one'),two=await repository.enqueue({userId:userA},t.id,randomUUID(),1,'two');
 start();start();await dispatcher.dispatch(0);
 const results=await Promise.all([terminal(one.operation.id),terminal(two.operation.id)]);await stopWorkers();
 assert.deepEqual(results.map(r=>r.status).sort(),['FAILED','SUCCEEDED']);assert.equal((await targets.get({userId:userA},t.id)).version,2);
});
check("J15 HTTP 202 result replay persists after API recreation without model calls",async()=>{
 const t=await target(),key=randomUUID(),url=`/api/training-targets/${t.id}/focus-operations`,payload={expected_version:1,focus:'persisted'};
 const first=await app.inject({method:'POST',url,headers:headers(cookieA,key),payload});assert.equal(first.statusCode,202,first.body);const id=first.json().id;
 start();await dispatcher.dispatch(0);assert.equal((await terminal(id)).status,'SUCCEEDED');await stopWorkers();
 await app.close();app=buildDevServer(pool,authPool,config);await app.ready();
 const replay=await app.inject({method:'POST',url,headers:headers(cookieA,key),payload});assert.equal(replay.statusCode,202,replay.body);assert.equal(replay.json().id,id);assert.equal(replay.json().status,'SUCCEEDED');
 const read=await app.inject({method:'GET',url:`/api/operations/${id}`,headers:headers()});assert.equal(read.json().result.version,2);
 assert.equal((await targets.get({userId:userA},t.id)).version,2);
});
