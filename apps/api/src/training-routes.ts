import type {FastifyInstance,FastifyRequest} from "fastify";
import {fromNodeHeaders} from "better-auth/node";
import {TrainingRepository} from "@pi-interview/storage/training";
import {OperationRepository} from "@pi-interview/storage/operations";
import {ResourceError} from "@pi-interview/storage";
import {publicQuestion,type QuestionSnapshot} from "@pi-interview/domain";
import type {createDevAuth} from "./auth.ts";

const idParams={type:'object',additionalProperties:false,required:['id'],properties:{id:{type:'string',format:'uuid'}}};
const principal=(r:FastifyRequest)=>{if(!r.principal)throw new ResourceError('authentication_required',401);return r.principal;};
function requestKey(r:FastifyRequest){const key=r.headers['idempotency-key'];if(typeof key!=='string'||!/^[a-zA-Z0-9_-]{8,128}$/.test(key))throw new ResourceError('idempotency_key_required',400);return key;}
export function registerTrainingRoutes(app:FastifyInstance,training:TrainingRepository,operations:OperationRepository,questions:QuestionSnapshot[],auth:ReturnType<typeof createDevAuth>){
 app.get('/catalog/questions',async()=>({role_id:'agent-intern',level:'intern',items:questions.map(publicQuestion)}));
 app.post<{Body:{target_id:string;question_id:string}}>('/practice-sessions',{schema:{body:{type:'object',additionalProperties:false,required:['target_id','question_id'],properties:{target_id:{type:'string',format:'uuid'},question_id:{type:'string',maxLength:80}}}}},async(req,reply)=>{
  const question=questions.find(q=>q.id===req.body.question_id);if(!question)throw new ResourceError('question_unavailable',400);
  const result=await training.create(principal(req),req.body.target_id,requestKey(req),question);return reply.code(result.created?201:200).send(await training.get(principal(req),result.id));
 });
 app.get('/practice-sessions',async req=>({items:await training.list(principal(req))}));
 app.get<{Params:{id:string}}>('/practice-sessions/:id',{schema:{params:idParams}},async req=>training.get(principal(req),req.params.id));
 app.post<{Params:{id:string};Body:{expected_version:number;text:string}}>('/practice-sessions/:id/answers',{schema:{params:idParams,body:{type:'object',additionalProperties:false,required:['expected_version','text'],properties:{expected_version:{type:'integer',minimum:1},text:{type:'string',minLength:1,maxLength:2000}}}}},async(req,reply)=>reply.code(202).send(await training.submit(principal(req),req.params.id,requestKey(req),req.body.expected_version,req.body.text)));
 app.post<{Params:{id:string}}>('/operations/:id/resume',{schema:{params:idParams,body:{type:'object',additionalProperties:false,properties:{}}}},async req=>operations.resume(principal(req),req.params.id));
 app.get<{Params:{id:string};Querystring:{after?:string}}>('/operations/:id/events',{schema:{params:idParams,querystring:{type:'object',additionalProperties:false,properties:{after:{type:'string',pattern:'^[0-9]{1,19}$'}}}}},async(req,reply)=>{
  const p=principal(req),id=req.params.id;
  await operations.get(p,id);
  let cursor=String(req.headers['last-event-id']??req.query.after??'0');
  if(!/^[0-9]{1,19}$/.test(cursor)||BigInt(cursor)>9223372036854775807n)throw new ResourceError('invalid_event_cursor',400);
  reply.hijack();reply.raw.writeHead(200,{'Content-Type':'text/event-stream; charset=utf-8','Cache-Control':'no-cache, no-transform','Connection':'keep-alive','X-Accel-Buffering':'no'});
  reply.raw.write('retry: 1500\n: connected\n\n');
  let closed=false,timer:ReturnType<typeof setTimeout>|undefined;
  const close=()=>{closed=true;clearTimeout(timer);};reply.raw.on('close',close);
  async function poll(){
   if(closed)return;
   try{
    // A long-lived connection must recheck logout/expiry as well as resource ownership.
    const session=await auth.api.getSession({headers:fromNodeHeaders(req.headers)});
    if(!session||session.user.id!==p.userId){reply.raw.end();close();return;}
    const operation=await operations.get(p,id),events=await training.events(p,id,cursor);
    for(const event of events){
     if(closed)return;
     if(!reply.raw.write(`id: ${event.event_id}\nevent: operation\ndata: ${JSON.stringify({operation_id:id,status:event.status,error_code:event.error_code})}\n\n`)){reply.raw.end();close();return;}
     cursor=event.event_id;
    }
    if(['SUCCEEDED','FAILED','CANCELLED','WAITING_CREDENTIALS'].includes(operation.status)&&events.length<100){reply.raw.end();close();return;}
    if(!closed){reply.raw.write(': heartbeat\n\n');timer=setTimeout(()=>void poll(),500);}
   }catch{reply.raw.end();close();}
  }
  void poll();
 });
}
