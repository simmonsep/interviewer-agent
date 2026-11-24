import {Pool} from 'pg';
import {Queue} from 'bullmq';
import {randomUUID} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {loadLocalConnection,createLocalPool} from '../../api/src/local-config.ts';
import {buildDevServer} from '../../api/src/server.ts';
import {migrate} from '../../../packages/storage/src/migrate.ts';
import {OperationRepository} from '@pi-interview/storage/operations';
import {TrainingRepository} from '@pi-interview/storage/training';
import {buildSqlAssessment,AssessmentUnavailable,type AssessmentContext,type AssessmentOutput} from '@pi-interview/domain';
import {PiRuntimeAdapter} from '@pi-interview/runtime-pi';
import {ScopedAssessmentStore} from '@pi-interview/runtime-pi/store';
import {createScriptedStream} from '../../../packages/runtime-pi/test-support/scripted-stream.ts';
import {connectRedis,createTaskWorker,OutboxDispatcher,type RedisConfig} from '../src/queue.ts';
import type {AssessmentExecutor} from '../src/assessment.ts';

export async function scriptedAssessment(context:AssessmentContext):Promise<AssessmentOutput>{
 const {input,snapshot}=buildSqlAssessment(context,'pm003-scripted');
 const source=context.question.sources[0],lines=context.answerText.split(/\r?\n/),line=lines.findIndex(x=>x.trim())+1;
 const candidate={question_id:context.question.id,answer_id:context.answerId,verdict:'partial' as const,summary:'离线协议联调反馈',feedback:'这是脚本驱动的协议测试反馈，不是模型对回答内容的实际评价。',citations:[{kind:'answer' as const,source_id:context.answerId,version:'v1',start_line:line,end_line:line,quote:lines[line-1]},{kind:'evidence' as const,source_id:source.id,version:source.version,start_line:1,end_line:1,quote:source.lines[0]}]};
 const script=createScriptedStream([
  {calls:[{id:'read-question',name:'read_question',arguments:{question_id:context.question.id}},{id:'read-answer',name:'read_current_answer',arguments:{}},{id:'read-evidence',name:'read_evidence',arguments:{source_id:source.id,version:source.version,start_line:1,end_line:1}}]},
  {calls:[{id:'submit',name:'submit_assessment',arguments:candidate}]},
 ],input.allowed_tools);
 const result=await new PiRuntimeAdapter(new ScopedAssessmentStore(snapshot)).run(input,script.streamFn);
 if(result.envelope.termination!=='candidate_ready'||!result.envelope.candidate||!result.cleanup_complete||script.failures.length)throw Error('scripted_assessment_failed');
 return {candidate:result.envelope.candidate,metadata:{origin:'scripted_test',provider:null,model_calls:0,pi_tool_executions:result.envelope.counts.tool_executions}};
}
export async function waitUntil(fn:()=>Promise<boolean>,timeout=10000){const end=Date.now()+timeout;while(Date.now()<end){if(await fn())return;await delay(25);}throw Error('condition_timeout');}
export async function createTrainingHarness(root:string,options:{port?:number;serveWeb?:boolean;assess?:AssessmentExecutor;autoDispatch?:boolean}={}){
 const c=loadLocalConnection(root),db='pi09_'+randomUUID().replaceAll('-','');
 const admin=new Pool({host:c.host,port:c.port,user:c.adminUser,password:c.adminPassword,database:'postgres',max:1});
 await admin.query(`CREATE DATABASE ${db} OWNER pi_app_owner`);
 const pool=createLocalPool(c,db),authPool=createLocalPool(c,db,true);await migrate(pool,join(root,'packages/storage/migrations'));
 const port=options.port??3319,config={mode:'test',host:'127.0.0.1',port,baseURL:`http://127.0.0.1:${port}`,secret:c.authSecret};
 let app=buildDevServer(pool,authPool,config,{root,serveWeb:options.serveWeb});await app.listen({host:'127.0.0.1',port});
 const rc=JSON.parse(readFileSync(join(root,'.private/redis-PI-08/connection.json'),'utf8')) as RedisConfig;
 const publish=connectRedis(rc),consume=connectRedis(rc,true);publish.on('error',()=>{});consume.on('error',()=>{});
 const queue=new Queue('pi09-test-'+randomUUID(),{connection:publish}),repository=new OperationRepository(pool),training=new TrainingRepository(pool),dispatcher=new OutboxDispatcher(repository,queue);
 const controls={calls:0,delayMs:0,unavailable:false,invalid:false,contexts:[] as AssessmentContext[]};
 const assess:AssessmentExecutor=options.assess??(async(context,signal)=>{
  controls.calls++;controls.contexts.push(context);
  if(controls.unavailable)throw new AssessmentUnavailable('model_not_enabled');
  if(controls.delayMs)await delay(controls.delayMs,undefined,{signal});
  const output=await scriptedAssessment(context);if(controls.invalid)output.candidate.citations[0].quote='injected_not_in_answer';return output;
 });
 const worker=createTaskWorker(repository,consume,queue.name,{leaseMs:options.assess?30000:3000,concurrency:2,assess});await worker.waitUntilReady();
 let dispatching:Promise<unknown>|null=null,dispatchError:unknown=null;
 const timer=options.autoDispatch?setInterval(()=>{if(!dispatching)dispatching=dispatcher.dispatch(0).catch(e=>{dispatchError=e;}).finally(()=>{dispatching=null;});},100):null;
 async function account(which:string){
  const response=await app.inject({method:'POST',url:'/api/auth/sign-up/email',headers:{origin:config.baseURL},payload:{name:'Synthetic '+which,email:which+'@example.invalid',password:'Synthetic-password-'+randomUUID()}});
  if(response.statusCode!==200)throw Error('test_signup_failed');
  const set=response.headers['set-cookie'],cookie=(Array.isArray(set)?set:[set]).map(v=>String(v).split(';')[0]).join('; ');
  return {userId:response.json().user.id as string,cookie};
 }
 async function execute(p:{userId:string},id:string){await dispatcher.dispatch(0);await waitUntil(async()=>['SUCCEEDED','FAILED','CANCELLED','WAITING_CREDENTIALS'].includes((await repository.get(p,id)).status),options.assess?130000:10000);return repository.get(p,id);}
 async function close(){
  if(timer)clearInterval(timer);await dispatching;await worker.close();await queue.obliterate({force:true});await queue.close();await publish.quit();await consume.quit();await app.close();await authPool.end();await pool.end();
  if(!/^pi09_[a-f0-9]{32}$/.test(db))throw Error('invalid_test_database');await admin.query(`DROP DATABASE ${db}`);await admin.end();if(dispatchError)throw dispatchError;
 }
 return {get app(){return app;},pool,authPool,config,queue,repository,training,dispatcher,controls,account,execute,close,database:db,
  async recreate(){await app.close();app=buildDevServer(pool,authPool,config,{root,serveWeb:options.serveWeb});await app.listen({host:'127.0.0.1',port});}};
}
