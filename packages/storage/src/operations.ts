import {createHash} from "node:crypto";
import type {Pool,PoolClient} from "pg";
import {ResourceError,type Principal} from "./repository.ts";
import {gateAssessment,type AssessmentOutput} from "@pi-interview/domain";
import {appendOperationEvent,loadAssessmentContext,assessmentContextHash} from "./training.ts";

export interface Operation {
 id:string; user_id:string; resource_id:string; kind:"update_target_focus"|"assess_answer"; session_id:string|null; session_version:number|null;
 expected_version:number; status:string; result:unknown; error_code:string|null;
 created_at:Date; completed_at:Date|null;
}
export interface QueueMessage {jobId:string;operationId:string;resourceVersion:number}
export interface Claim extends QueueMessage {
 userId:string; resourceId:string; attempt:number; fencingToken:number; focus:string;
 kind:Operation["kind"];sessionId:string|null;sessionVersion:number|null;
}
const publicColumns="o.id,o.user_id,o.resource_id,o.kind,o.session_id,o.session_version,o.expected_version,o.status,o.result,o.error_code,o.created_at,o.completed_at";
export class OperationRepository {
 private pool:Pool;
 constructor(pool:Pool){this.pool=pool;}
 private async transaction<T>(fn:(c:PoolClient)=>Promise<T>):Promise<T>{
  const c=await this.pool.connect();
  try{await c.query("BEGIN");const result=await fn(c);await c.query("COMMIT");return result;}
  catch(e){await c.query("ROLLBACK");throw e;}finally{c.release();}
 }
 async enqueue(p:Principal,resourceId:string,key:string,expectedVersion:number,focus:string){
  const hash=createHash("sha256").update(JSON.stringify([resourceId,expectedVersion,focus])).digest("hex");
  return this.transaction(async c=>{
   const target=await c.query("SELECT version FROM app.training_targets WHERE id=$1 AND user_id=$2 AND deleted_at IS NULL",[resourceId,p.userId]);
   if(!target.rows[0])throw new ResourceError("resource_unavailable",404);
   const replay=async()=>{
    const r=await c.query<Operation&{request_hash:string}>(`SELECT ${publicColumns},o.request_hash FROM app.operations o WHERE o.user_id=$1 AND o.kind='update_target_focus' AND o.request_id=$2`,[p.userId,key]);
    if(!r.rows[0])return null;
    if(r.rows[0].request_hash!==hash)throw new ResourceError("idempotency_conflict",409);
    const {request_hash:_,...operation}=r.rows[0];return {operation,created:false};
   };
   const existing=await replay();if(existing)return existing;
   if(target.rows[0].version!==expectedVersion)throw new ResourceError("version_conflict",409);
   const inserted=await c.query<{id:string}>("INSERT INTO app.operations(user_id,resource_id,kind,request_id,request_hash,expected_version,input_focus) VALUES($1,$2,'update_target_focus',$3,$4,$5,$6) ON CONFLICT(user_id,kind,request_id) DO NOTHING RETURNING id",[p.userId,resourceId,key,hash,expectedVersion,focus]);
   if(!inserted.rows[0]){const winner=await replay();if(!winner)throw Error("missing_operation_after_conflict");return winner;}
   const id=inserted.rows[0].id;
   const job=await c.query<{id:string}>("INSERT INTO app.background_jobs(operation_id) VALUES($1) RETURNING id",[id]);
   await c.query("INSERT INTO app.outbox_events(job_id) VALUES($1)",[job.rows[0].id]);
   await appendOperationEvent(c,id);
   const row=await c.query<Operation>(`SELECT ${publicColumns} FROM app.operations o WHERE o.id=$1`,[id]);
   return {operation:row.rows[0],created:true};
  });
 }
 async get(p:Principal,id:string){
  const r=await this.pool.query<Operation>(`SELECT ${publicColumns} FROM app.operations o JOIN app.training_targets t ON t.id=o.resource_id AND t.user_id=o.user_id WHERE o.id=$1 AND o.user_id=$2 AND t.deleted_at IS NULL AND (o.session_id IS NULL OR EXISTS(SELECT 1 FROM app.practice_sessions s WHERE s.id=o.session_id AND s.user_id=o.user_id AND s.deleted_at IS NULL))`,[id,p.userId]);
  if(!r.rows[0])throw new ResourceError("resource_unavailable",404);return r.rows[0];
 }
 async cancel(p:Principal,id:string){
  await this.transaction(async c=>{
   const r=await c.query("SELECT o.status FROM app.operations o JOIN app.training_targets t ON t.id=o.resource_id AND t.user_id=o.user_id WHERE o.id=$1 AND o.user_id=$2 AND t.deleted_at IS NULL AND (o.session_id IS NULL OR EXISTS(SELECT 1 FROM app.practice_sessions s WHERE s.id=o.session_id AND s.user_id=o.user_id AND s.deleted_at IS NULL)) FOR UPDATE OF o",[id,p.userId]);
   if(!r.rows[0])throw new ResourceError("resource_unavailable",404);
   if(!['PENDING','RUNNING','WAITING_CREDENTIALS'].includes(r.rows[0].status))return;
   await c.query("UPDATE app.operations SET status='CANCELLED',completed_at=clock_timestamp() WHERE id=$1",[id]);
   await c.query("UPDATE app.background_jobs SET status='CANCELLED',fencing_token=fencing_token+1,lease_until=NULL,updated_at=clock_timestamp() WHERE operation_id=$1",[id]);
   await c.query("UPDATE app.practice_sessions s SET status='CANCELLED',version=version+1 FROM app.operations o WHERE o.id=$1 AND s.id=o.session_id AND s.user_id=o.user_id AND s.version=o.session_version AND s.deleted_at IS NULL",[id]);
   await appendOperationEvent(c,id);
  });
  return this.get(p,id);
 }
 async pendingDispatch(recoveryAfterMs=5000){
  const r=await this.pool.query<QueueMessage&{outboxId:string}>(`SELECT e.id AS "outboxId",j.id AS "jobId",o.id AS "operationId",o.expected_version AS "resourceVersion"
   FROM app.outbox_events e JOIN app.background_jobs j ON j.id=e.job_id JOIN app.operations o ON o.id=j.operation_id
   WHERE o.status IN ('PENDING','RUNNING') AND (j.status='PENDING' OR (j.status='RUNNING' AND j.lease_until<=clock_timestamp()))
    AND (e.dispatched_at IS NULL OR e.dispatched_at<=clock_timestamp()-($1*interval '1 millisecond')) ORDER BY e.created_at LIMIT 100`,[recoveryAfterMs]);
  return r.rows;
 }
 async markDispatched(id:string){await this.pool.query("UPDATE app.outbox_events SET dispatched_at=clock_timestamp(),dispatch_count=dispatch_count+1 WHERE id=$1",[id]);}
 async claim(message:QueueMessage,leaseMs=30000):Promise<Claim|null>{
  return this.transaction(async c=>{
   const op=await c.query("SELECT * FROM app.operations WHERE id=$1 FOR UPDATE",[message.operationId]);
   const o=op.rows[0];if(!o||o.expected_version!==message.resourceVersion||!['PENDING','RUNNING'].includes(o.status))return null;
   const job=await c.query("SELECT *,lease_until>clock_timestamp() AS lease_active FROM app.background_jobs WHERE id=$1 AND operation_id=$2 FOR UPDATE",[message.jobId,message.operationId]);
   const j=job.rows[0];if(!j||!['PENDING','RUNNING'].includes(j.status)||(j.status==='RUNNING'&&j.lease_active))return null;
   const target=await c.query("SELECT id FROM app.training_targets WHERE id=$1 AND user_id=$2 AND version=$3 AND deleted_at IS NULL",[o.resource_id,o.user_id,o.expected_version]);
   const session=o.session_id?await c.query("SELECT id FROM app.practice_sessions WHERE id=$1 AND user_id=$2 AND target_id=$3 AND version=$4 AND status='EVALUATING' AND deleted_at IS NULL",[o.session_id,o.user_id,o.resource_id,o.session_version]):null;
   if(!target.rows[0]||(o.session_id&&!session?.rows[0])){
    await this.finishFailure(c,o.id,j.id,'resource_changed');return null;
   }
   const claimed=await c.query("UPDATE app.background_jobs SET status='RUNNING',attempt=attempt+1,fencing_token=fencing_token+1,lease_until=clock_timestamp()+($2*interval '1 millisecond'),updated_at=clock_timestamp() WHERE id=$1 RETURNING attempt,fencing_token",[j.id,leaseMs]);
   await c.query("UPDATE app.operations SET status='RUNNING',error_code=NULL WHERE id=$1",[o.id]);
   await appendOperationEvent(c,o.id);
   return {...message,userId:o.user_id,resourceId:o.resource_id,attempt:claimed.rows[0].attempt,fencingToken:claimed.rows[0].fencing_token,focus:o.input_focus,kind:o.kind,sessionId:o.session_id,sessionVersion:o.session_version};
  });
 }
 async heartbeat(claim:Claim,leaseMs=30000){
  const r=await this.pool.query(`UPDATE app.background_jobs j SET lease_until=clock_timestamp()+($6*interval '1 millisecond'),updated_at=clock_timestamp()
   FROM app.operations o JOIN app.training_targets t ON t.id=o.resource_id AND t.user_id=o.user_id
   WHERE j.id=$1 AND j.operation_id=$2 AND o.id=j.operation_id AND j.attempt=$3 AND j.fencing_token=$4 AND o.user_id=$5
    AND j.status='RUNNING' AND o.status='RUNNING' AND j.lease_until>clock_timestamp()
    AND t.id=$7 AND t.version=$8 AND t.deleted_at IS NULL
    AND (o.session_id IS NULL OR EXISTS(SELECT 1 FROM app.practice_sessions s WHERE s.id=o.session_id AND s.user_id=o.user_id AND s.version=o.session_version AND s.deleted_at IS NULL AND s.status='EVALUATING'))`,[claim.jobId,claim.operationId,claim.attempt,claim.fencingToken,claim.userId,leaseMs,claim.resourceId,claim.resourceVersion]);
  return r.rowCount===1;
 }
 private async ownsLease(c:PoolClient,claim:Claim){
  const op=await c.query("SELECT * FROM app.operations WHERE id=$1 FOR UPDATE",[claim.operationId]);
  const job=await c.query("SELECT *,lease_until>clock_timestamp() AS lease_active FROM app.background_jobs WHERE id=$1 AND operation_id=$2 FOR UPDATE",[claim.jobId,claim.operationId]);
  const o=op.rows[0],j=job.rows[0];
  return !!(o&&j&&o.user_id===claim.userId&&o.resource_id===claim.resourceId&&o.expected_version===claim.resourceVersion&&o.kind===claim.kind&&o.session_id===claim.sessionId&&o.session_version===claim.sessionVersion&&o.status==='RUNNING'&&j.status==='RUNNING'&&j.attempt===claim.attempt&&j.fencing_token===claim.fencingToken&&j.lease_active);
 }
 async complete(claim:Claim){
  return this.transaction(async c=>{
   if(!await this.ownsLease(c,claim))return false;
   // A target row lock can wait. Recheck the lease after obtaining it, not only before.
   await c.query("SELECT id FROM app.training_targets WHERE id=$1 FOR UPDATE",[claim.resourceId]);
   const lease=await c.query("SELECT lease_until>clock_timestamp() AS active FROM app.background_jobs WHERE id=$1",[claim.jobId]);
   if(!lease.rows[0]?.active)return false;
   if(claim.kind==='assess_answer')return this.completeAssessment(c,claim);
   // The business effect and terminal task state commit in the same SQL transaction.
   const target=await c.query("UPDATE app.training_targets SET focus=$4,version=version+1 WHERE id=$1 AND user_id=$2 AND version=$3 AND deleted_at IS NULL RETURNING id,focus,version,context_mode",[claim.resourceId,claim.userId,claim.resourceVersion,claim.focus]);
   if(!target.rows[0]){
    await this.finishFailure(c,claim.operationId,claim.jobId,'resource_changed');return false;
   }
   await c.query("UPDATE app.operations SET status='SUCCEEDED',result=$2::jsonb,completed_at=clock_timestamp() WHERE id=$1",[claim.operationId,JSON.stringify(target.rows[0])]);
   await c.query("UPDATE app.background_jobs SET status='SUCCEEDED',lease_until=NULL,updated_at=clock_timestamp() WHERE id=$1",[claim.jobId]);await appendOperationEvent(c,claim.operationId);return true;
  });
 }
 async releaseFailedAttempt(claim:Claim){
  if(claim.kind==='assess_answer')return this.failAssessment(claim,'assessment_execution_failed');
  return this.transaction(async c=>{
   if(!await this.ownsLease(c,claim))return false;
   const status=claim.attempt>=3?'FAILED':'PENDING';
   await c.query("UPDATE app.operations SET status=$2,error_code='worker_execution_failed',completed_at=CASE WHEN $2='FAILED' THEN clock_timestamp() ELSE NULL END WHERE id=$1",[claim.operationId,status]);
   await c.query("UPDATE app.background_jobs SET status=$2,lease_until=NULL,updated_at=clock_timestamp() WHERE id=$1",[claim.jobId,status]);await appendOperationEvent(c,claim.operationId);return true;
  });
 }
 private async finishFailure(c:PoolClient,operationId:string,jobId:string,code:string){
  await c.query("UPDATE app.operations SET status='FAILED',error_code=$2,completed_at=clock_timestamp() WHERE id=$1",[operationId,code]);
  await c.query("UPDATE app.background_jobs SET status='FAILED',lease_until=NULL,updated_at=clock_timestamp() WHERE id=$1",[jobId]);
  await c.query("UPDATE app.practice_sessions s SET status='FAILED',version=version+1 FROM app.operations o WHERE o.id=$1 AND s.id=o.session_id AND s.user_id=o.user_id AND s.version=o.session_version AND s.deleted_at IS NULL",[operationId]);
  await appendOperationEvent(c,operationId);
 }
 async assessmentContext(claim:Claim){return this.transaction(async c=>{if(!await this.ownsLease(c,claim))throw new ResourceError('execution_expired',409);return loadAssessmentContext(c,claim);});}
 async savedAssessment(claim:Claim):Promise<AssessmentOutput|null>{
  return this.transaction(async c=>{
   if(!await this.ownsLease(c,claim))throw new ResourceError('execution_expired',409);
   const context=await loadAssessmentContext(c,claim);
   const r=await c.query('SELECT input_hash,candidate,metadata FROM app.assessment_candidates WHERE operation_id=$1',[claim.operationId]);
   if(!r.rows[0])return null;
   if(r.rows[0].input_hash!==assessmentContextHash(context))throw Error('candidate_cache_mismatch');
   return {candidate:gateAssessment(context,r.rows[0].candidate),metadata:r.rows[0].metadata};
  });
 }
 async saveAssessment(claim:Claim,output:AssessmentOutput){
  return this.transaction(async c=>{
   if(!await this.ownsLease(c,claim))return false;
   const context=await loadAssessmentContext(c,claim),candidate=gateAssessment(context,output.candidate);
   await c.query('INSERT INTO app.assessment_candidates(operation_id,input_hash,candidate,metadata) VALUES($1,$2,$3::jsonb,$4::jsonb) ON CONFLICT(operation_id) DO NOTHING',[claim.operationId,assessmentContextHash(context),JSON.stringify(candidate),JSON.stringify(output.metadata)]);return true;
  });
 }
 private async completeAssessment(c:PoolClient,claim:Claim){
  await c.query('SELECT id FROM app.practice_sessions WHERE id=$1 FOR UPDATE',[claim.sessionId]);
  const lease=await c.query('SELECT lease_until>clock_timestamp() AS active FROM app.background_jobs WHERE id=$1',[claim.jobId]);if(!lease.rows[0]?.active)return false;
  let context;try{context=await loadAssessmentContext(c,claim);}catch{await this.finishFailure(c,claim.operationId,claim.jobId,'resource_changed');return false;}
  const r=await c.query('SELECT input_hash,candidate,metadata FROM app.assessment_candidates WHERE operation_id=$1',[claim.operationId]);
  if(!r.rows[0]||r.rows[0].input_hash!==assessmentContextHash(context))throw Error('candidate_cache_mismatch');
  const candidate=gateAssessment(context,r.rows[0].candidate);
  const inserted=await c.query('INSERT INTO app.assessment_revisions(user_id,session_id,operation_id,answer_id,candidate,metadata) VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb) RETURNING id',[claim.userId,claim.sessionId,claim.operationId,context.answerId,JSON.stringify(candidate),JSON.stringify(r.rows[0].metadata)]);
  await c.query("UPDATE app.practice_sessions SET status='COMPLETED',version=version+1 WHERE id=$1 AND user_id=$2 AND version=$3 AND deleted_at IS NULL",[claim.sessionId,claim.userId,claim.sessionVersion]);
  await c.query("UPDATE app.operations SET status='SUCCEEDED',result=$2::jsonb,completed_at=clock_timestamp() WHERE id=$1",[claim.operationId,JSON.stringify({session_id:claim.sessionId,assessment_revision_id:inserted.rows[0].id})]);
  await c.query("UPDATE app.background_jobs SET status='SUCCEEDED',lease_until=NULL,updated_at=clock_timestamp() WHERE id=$1",[claim.jobId]);await appendOperationEvent(c,claim.operationId);return true;
 }
 async failAssessment(claim:Claim,code:string,waiting=false){
  return this.transaction(async c=>{
   if(!await this.ownsLease(c,claim))return false;
   if(waiting){
    await c.query("UPDATE app.operations SET status='WAITING_CREDENTIALS',error_code=$2 WHERE id=$1",[claim.operationId,code]);
    await c.query("UPDATE app.background_jobs SET status='PENDING',lease_until=NULL,updated_at=clock_timestamp() WHERE id=$1",[claim.jobId]);await appendOperationEvent(c,claim.operationId);
   }else await this.finishFailure(c,claim.operationId,claim.jobId,code);return true;
  });
 }
 async resume(p:Principal,id:string){
  await this.get(p,id);
  await this.transaction(async c=>{
   const r=await c.query('SELECT status FROM app.operations WHERE id=$1 AND user_id=$2 FOR UPDATE',[id,p.userId]);
   if(r.rows[0]?.status!=='WAITING_CREDENTIALS')return;
   await c.query("UPDATE app.operations SET status='PENDING',error_code=NULL WHERE id=$1",[id]);
   await c.query("UPDATE app.background_jobs SET status='PENDING',lease_until=NULL,updated_at=clock_timestamp() WHERE operation_id=$1",[id]);await appendOperationEvent(c,id);
  });return this.get(p,id);
 }

}
