import {createHash} from "node:crypto";
import type {Pool,PoolClient} from "pg";
import {publicQuestion,feedbackView,type QuestionSnapshot,type AssessmentContext} from "@pi-interview/domain";
import {ResourceError,type Principal} from "./repository.ts";
import type {Claim,Operation} from "./operations.ts";
import type {PracticeSessionView} from "@pi-interview/contracts";

const hash=(value:unknown)=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
export const operationColumns='id,user_id,resource_id,kind,session_id,session_version,expected_version,status,result,error_code,created_at,completed_at';
export async function appendOperationEvent(c:PoolClient,id:string){await c.query('INSERT INTO app.business_events(operation_id,user_id,status,error_code) SELECT id,user_id,status,error_code FROM app.operations WHERE id=$1',[id]);}
export async function loadAssessmentContext(c:PoolClient,claim:Claim):Promise<AssessmentContext>{
 const r=await c.query(`SELECT s.*,a.id AS answer_id,a.original_text FROM app.practice_sessions s JOIN app.answer_attempts a ON a.session_id=s.id AND a.user_id=s.user_id
 JOIN app.training_targets t ON t.id=s.target_id AND t.user_id=s.user_id
 WHERE s.id=$1 AND s.user_id=$2 AND s.version=$3 AND s.target_id=$4 AND s.target_version=$5 AND t.version=$5
 AND s.status='EVALUATING' AND s.deleted_at IS NULL AND t.deleted_at IS NULL`,[claim.sessionId,claim.userId,claim.sessionVersion,claim.resourceId,claim.resourceVersion]);
 if(!r.rows[0])throw new ResourceError('resource_changed',409);
 const row=r.rows[0];return {userId:claim.userId,sessionId:row.id,sessionVersion:row.version,operationId:claim.operationId,answerId:row.answer_id,answerText:row.original_text,question:row.question_snapshot};
}
export const assessmentContextHash=(context:AssessmentContext)=>hash(context);
export class TrainingRepository {
 private pool:Pool;
 constructor(pool:Pool){this.pool=pool;}
 private async transaction<T>(fn:(c:PoolClient)=>Promise<T>):Promise<T>{const c=await this.pool.connect();try{await c.query('BEGIN');const r=await fn(c);await c.query('COMMIT');return r;}catch(e){await c.query('ROLLBACK');throw e;}finally{c.release();}}
 async create(p:Principal,targetId:string,key:string,question:QuestionSnapshot){
  return this.transaction(async c=>{
   const target=await c.query('SELECT version FROM app.training_targets WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL',[targetId,p.userId]);
   if(!target.rows[0])throw new ResourceError('resource_unavailable',404);
   const requestHash=hash([targetId,question.id]);
   const inserted=await c.query("INSERT INTO app.practice_sessions(user_id,target_id,target_version,request_id,request_hash,question_snapshot) VALUES($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT(user_id,request_id) DO NOTHING RETURNING id",[p.userId,targetId,target.rows[0].version,key,requestHash,JSON.stringify(question)]);
   if(inserted.rows[0])return {id:inserted.rows[0].id as string,created:true};
   const prior=await c.query('SELECT id,request_hash,deleted_at FROM app.practice_sessions WHERE user_id=$1 AND request_id=$2',[p.userId,key]);
   if(prior.rows[0]?.deleted_at)throw new ResourceError('resource_unavailable',404);
   if(prior.rows[0]?.request_hash!==requestHash)throw new ResourceError('idempotency_conflict',409);
   return {id:prior.rows[0].id as string,created:false};
  });
 }
 async list(p:Principal){
  const rows=await this.pool.query('SELECT s.id,s.status,s.version,s.created_at,s.question_snapshot FROM app.practice_sessions s JOIN app.training_targets t ON t.id=s.target_id AND t.user_id=s.user_id WHERE s.user_id=$1 AND s.deleted_at IS NULL AND t.deleted_at IS NULL ORDER BY s.created_at DESC LIMIT 50',[p.userId]);
  return rows.rows.map(({question_snapshot,...r})=>({...r,question:publicQuestion(question_snapshot)}));
 }
 async get(p:Principal,id:string):Promise<PracticeSessionView>{
  const r=await this.pool.query(`SELECT s.*,a.id AS answer_id,a.original_text,a.origin,ar.candidate,ar.metadata,o.id AS operation_id,o.status AS operation_status,o.error_code
 FROM app.practice_sessions s JOIN app.training_targets t ON t.id=s.target_id AND t.user_id=s.user_id
 LEFT JOIN app.answer_attempts a ON a.session_id=s.id AND a.user_id=s.user_id
 LEFT JOIN app.operations o ON o.session_id=s.id AND o.user_id=s.user_id
 LEFT JOIN app.assessment_revisions ar ON ar.session_id=s.id AND ar.user_id=s.user_id
 WHERE s.id=$1 AND s.user_id=$2 AND s.deleted_at IS NULL AND t.deleted_at IS NULL`,[id,p.userId]);
  const row=r.rows[0];if(!row)throw new ResourceError('resource_unavailable',404);
  const context:AssessmentContext={userId:p.userId,sessionId:id,sessionVersion:row.version,operationId:row.operation_id??'',answerId:row.answer_id??'',answerText:row.original_text??'',question:row.question_snapshot};
  return {id:row.id,version:row.version,status:row.status,mode:row.mode,context_mode:row.context_mode,target_id:row.target_id,question:publicQuestion(row.question_snapshot),created_at:row.created_at,
   answer:row.answer_id?{id:row.answer_id,text:row.original_text,origin:row.origin}:null,
   operation:row.operation_id?{id:row.operation_id,status:row.operation_status,error_code:row.error_code}:null,
   feedback:row.status==='COMPLETED'&&row.candidate?{...feedbackView(context,row.candidate),origin:row.metadata?.origin??'unknown',provider:row.metadata?.provider??null}:null};
 }
 async submit(p:Principal,id:string,key:string,expectedVersion:number,text:string){
  if(!text.trim()||text.length>2000||text.split(/\r?\n/).length>40)throw new ResourceError('invalid_answer',400);
  return this.transaction(async c=>{
   const r=await c.query(`SELECT s.*,t.version AS current_target_version FROM app.practice_sessions s JOIN app.training_targets t ON t.id=s.target_id AND t.user_id=s.user_id
 WHERE s.id=$1 AND s.user_id=$2 AND s.deleted_at IS NULL AND t.deleted_at IS NULL FOR UPDATE OF s`,[id,p.userId]);
   const s=r.rows[0];if(!s)throw new ResourceError('resource_unavailable',404);
   const requestHash=hash([id,expectedVersion,text]);
   const prior=await c.query('SELECT request_id,request_hash FROM app.answer_attempts WHERE session_id=$1 AND user_id=$2',[id,p.userId]);
   if(prior.rows[0]){
    if(prior.rows[0].request_id!==key||prior.rows[0].request_hash!==requestHash)throw new ResourceError('answer_already_submitted',409);
    return (await c.query<Operation>(`SELECT ${operationColumns} FROM app.operations WHERE session_id=$1 AND user_id=$2`,[id,p.userId])).rows[0];
   }
   if(s.status!=='WAITING_FOR_ANSWER'||s.version!==expectedVersion||s.target_version!==s.current_target_version)throw new ResourceError('version_conflict',409);
   await c.query('INSERT INTO app.answer_attempts(user_id,session_id,request_id,request_hash,original_text) VALUES($1,$2,$3,$4,$5)',[p.userId,id,key,requestHash,text]);
   await c.query("UPDATE app.practice_sessions SET status='EVALUATING',version=version+1 WHERE id=$1",[id]);
   const op=await c.query<Operation>(`INSERT INTO app.operations(user_id,resource_id,kind,request_id,request_hash,expected_version,session_id,session_version)
 VALUES($1,$2,'assess_answer',$3,$4,$5,$6,$7) ON CONFLICT(user_id,kind,request_id) DO NOTHING RETURNING ${operationColumns}`,[p.userId,s.target_id,key,requestHash,s.target_version,id,s.version+1]);
   if(!op.rows[0])throw new ResourceError('idempotency_conflict',409);
   const job=await c.query('INSERT INTO app.background_jobs(operation_id) VALUES($1) RETURNING id',[op.rows[0].id]);
   await c.query('INSERT INTO app.outbox_events(job_id) VALUES($1)',[job.rows[0].id]);
   await appendOperationEvent(c,op.rows[0].id);return op.rows[0];
  });
 }
 async events(p:Principal,operationId:string,after:string){
  return (await this.pool.query<{event_id:string;status:string;error_code:string|null}>('SELECT event_id::text,status,error_code FROM app.business_events WHERE user_id=$1 AND operation_id=$2 AND event_id>$3::bigint ORDER BY event_id LIMIT 100',[p.userId,operationId,after])).rows;
 }
}
