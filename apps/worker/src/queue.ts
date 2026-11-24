import {randomUUID} from "node:crypto";
import {Queue,Worker} from "bullmq";
import {Redis} from "ioredis";
import {OperationRepository,type QueueMessage,type Claim} from "@pi-interview/storage/operations";
import {AssessmentUnavailable} from "@pi-interview/domain";
import type {AssessmentExecutor} from "./assessment.ts";

export interface RedisConfig {host:string;port:number;password:string}
export function connectRedis(config:RedisConfig,worker=false){
 if(config.host!=="127.0.0.1"||config.port!==56379||!config.password)throw Error("unsupported_local_redis");
 return new Redis({...config,maxRetriesPerRequest:worker?null:1,connectTimeout:3000,retryStrategy:worker?(n=>Math.min(n*100,2000)):()=>null});
}
export function parseMessage(value:unknown):QueueMessage|null {
 if(!value||typeof value!=="object")return null;
 const v=value as Record<string,unknown>;
 const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
 if(Object.keys(v).sort().join(",")!=="jobId,operationId,resourceVersion"||typeof v.jobId!=="string"||!uuid.test(v.jobId)||typeof v.operationId!=="string"||!uuid.test(v.operationId)||!Number.isSafeInteger(v.resourceVersion)||Number(v.resourceVersion)<1)return null;
 return {jobId:v.jobId,operationId:v.operationId,resourceVersion:Number(v.resourceVersion)};
}
export class OutboxDispatcher {
 private repository:OperationRepository;
 private queue:Queue;
 constructor(repository:OperationRepository,queue:Queue){this.repository=repository;this.queue=queue;}
 async dispatch(recoveryAfterMs=5000){
  let count=0;
  for(const {outboxId,...message} of await this.repository.pendingDispatch(recoveryAfterMs)){
   // Publish first. A crash before SQL acknowledgement intentionally permits redelivery.
   await this.queue.add("execute",message,{jobId:outboxId+"-"+randomUUID(),removeOnComplete:true,removeOnFail:true});
   await this.repository.markDispatched(outboxId);count++;
  }
  return count;
 }
}
export function createTaskWorker(repository:OperationRepository,connection:Redis,queueName:string,options:{leaseMs?:number;concurrency?:number;assess?:AssessmentExecutor;prepare?:(claim:Claim,signal:AbortSignal)=>Promise<void>;onError?:(error:Error)=>void}={}){
 const leaseMs=options.leaseMs??30000;
 const worker=new Worker(queueName,async job=>{
  const message=parseMessage(job.data);if(!message)throw Error("invalid_queue_message");
  const claim=await repository.claim(message,leaseMs);if(!claim)return {committed:false};
  const controller=new AbortController();let renewing=false;
  const heartbeat=setInterval(()=>{
   if(renewing)return;renewing=true;
   void repository.heartbeat(claim,leaseMs).then(ok=>{if(!ok)controller.abort();},()=>controller.abort()).finally(()=>{renewing=false;});
  },Math.max(10,Math.floor(leaseMs/3)));
  try{
   if(claim.kind==='assess_answer'){
    const cached=await repository.savedAssessment(claim);
    if(!cached){
     if(!options.assess)throw new AssessmentUnavailable('model_not_enabled');
     const output=await options.assess(await repository.assessmentContext(claim),controller.signal);
     if(controller.signal.aborted||!await repository.saveAssessment(claim,output))return {committed:false};
    }
   }
   if(options.prepare)await options.prepare(claim,controller.signal);
   if(controller.signal.aborted)return {committed:false};
   return {committed:await repository.complete(claim)};
  }catch(error){
   if(claim.kind==='assess_answer'){
    await repository.failAssessment(claim,error instanceof AssessmentUnavailable?error.code:'assessment_execution_failed',error instanceof AssessmentUnavailable);return {committed:false};
   }
   await repository.releaseFailedAttempt(claim);throw error;
  }
  finally{clearInterval(heartbeat);}
 },{connection,concurrency:options.concurrency??2,autorun:true});
 worker.on("error",options.onError??(()=>console.error("worker_queue_error")));
 return worker;
}
