import Fastify, {type FastifyRequest} from "fastify";
import {fromNodeHeaders} from "better-auth/node";
import type {Pool} from "pg";
import {createDevAuth,assertDevelopmentConfig,type DevConfig} from "./auth.ts";
import {TargetRepository,ResourceError,type Principal,type TargetInput} from "@pi-interview/storage";
import {OperationRepository} from "@pi-interview/storage/operations";
import {TrainingRepository} from "@pi-interview/storage/training";
import {loadPracticeQuestions} from "@pi-interview/domain";
import {dirname,resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {registerTrainingRoutes} from "./training-routes.ts";
import {registerBuiltWeb} from "./web.ts";
declare module "fastify" {interface FastifyRequest {principal?:Principal}}
const idParams={type:"object",additionalProperties:false,required:["id"],properties:{id:{type:"string",pattern:"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$"}}};
const focus={type:"string",maxLength:120};
export function buildDevServer(pool:Pool,authPool:Pool,config:DevConfig,options:{root?:string;serveWeb?:boolean}={}) {
  assertDevelopmentConfig(config);
  const app=Fastify({logger:false,bodyLimit:16384,ajv:{customOptions:{removeAdditional:false,coerceTypes:false}}});
  const auth=createDevAuth(authPool,config),targets=new TargetRepository(pool),operations=new OperationRepository(pool);
  const root=options.root??resolve(dirname(fileURLToPath(import.meta.url)),"../../..");
  const training=new TrainingRepository(pool),questions=loadPracticeQuestions(root);
  if(options.serveWeb)registerBuiltWeb(app,root);
  app.setErrorHandler((error,_request,reply)=>{
    if(error instanceof ResourceError)return reply.code(error.status).send({code:error.message});
    if(error&&typeof error==="object"&&"validation" in error)return reply.code(400).send({code:"invalid_request"});
    return reply.code(500).send({code:"internal_error"});
  });
  app.get("/health",async()=>({status:"ok",mode:"local-development"}));
  app.route({method:["GET","POST"],url:"/api/auth/*",async handler(request,reply){
    const endpoint=request.method+":"+new URL(request.url,config.baseURL).pathname;
    if(!["GET:/api/auth/get-session","POST:/api/auth/sign-up/email","POST:/api/auth/sign-in/email","POST:/api/auth/sign-out"].includes(endpoint))return reply.code(404).send({code:"auth_endpoint_unavailable"});
    const response=await auth.handler(new Request(new URL(request.url,config.baseURL),{method:request.method,headers:fromNodeHeaders(request.headers),...(request.body?{body:JSON.stringify(request.body)}:{})}));
    response.headers.forEach((value,key)=>{if(key!=="set-cookie")reply.header(key,value);});
    const cookies=response.headers.getSetCookie();if(cookies.length)reply.header("set-cookie",cookies);
    return reply.code(response.status).send(response.body?await response.text():null);
  }});
  app.register(async protectedApp=>{
    protectedApp.addHook("preHandler",async(request,reply)=>{
      const session=await auth.api.getSession({headers:fromNodeHeaders(request.headers)});
      if(!session)return reply.code(401).send({code:"authentication_required"});
      request.principal={userId:session.user.id};
      if(["POST","PATCH","DELETE"].includes(request.method)&&request.headers.origin!==config.baseURL)return reply.code(403).send({code:"origin_not_allowed"});
    });
    const principal=(r:FastifyRequest)=>{if(!r.principal)throw new ResourceError("authentication_required",401);return r.principal;};
    registerTrainingRoutes(protectedApp,training,operations,questions,auth);
    protectedApp.post<{Body:TargetInput}>("/training-targets/",{schema:{body:{type:"object",additionalProperties:false,required:["role_package_id","level"],properties:{role_package_id:{const:"agent-intern"},level:{const:"intern"},focus}}}},async(request,reply)=>{
      const requestId=request.headers["idempotency-key"];
      if(typeof requestId!=="string"||!/^[a-zA-Z0-9_-]{8,128}$/.test(requestId))return reply.code(400).send({code:"idempotency_key_required"});
      const result=await targets.create(principal(request),requestId,{...request.body,focus:request.body.focus??""});
      return reply.code(result.created?201:200).send(result.target);
    });
    protectedApp.get("/training-targets/",async(request)=>({items:await targets.list(principal(request))}));
    protectedApp.get<{Params:{id:string}}>("/training-targets/:id",{schema:{params:idParams}},async(request)=>targets.get(principal(request),request.params.id));
    protectedApp.patch<{Params:{id:string};Body:{expected_version:number;focus:string}}>("/training-targets/:id",{schema:{params:idParams,body:{type:"object",additionalProperties:false,required:["expected_version","focus"],properties:{expected_version:{type:"integer",minimum:1},focus}}}},async(request)=>targets.updateFocus(principal(request),request.params.id,request.body.expected_version,request.body.focus));
    protectedApp.post<{Params:{id:string};Body:{expected_version:number;focus:string}}>("/training-targets/:id/focus-operations",{schema:{params:idParams,body:{type:"object",additionalProperties:false,required:["expected_version","focus"],properties:{expected_version:{type:"integer",minimum:1},focus}}}},async(request,reply)=>{
      const key=request.headers["idempotency-key"];
      if(typeof key!=="string"||!/^[a-zA-Z0-9_-]{8,128}$/.test(key))return reply.code(400).send({code:"idempotency_key_required"});
      const result=await operations.enqueue(principal(request),request.params.id,key,request.body.expected_version,request.body.focus);
      return reply.code(202).send(result.operation);
    });
    protectedApp.get<{Params:{id:string}}>("/operations/:id",{schema:{params:idParams}},async(request)=>operations.get(principal(request),request.params.id));
    protectedApp.post<{Params:{id:string}}>("/operations/:id/cancel",{schema:{params:idParams,body:{type:"object",additionalProperties:false,properties:{}}}},async(request)=>operations.cancel(principal(request),request.params.id));
  },{prefix:"/api"});
  return app;
}
