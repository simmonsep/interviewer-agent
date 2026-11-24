// D-01-01 close-out: deterministic synthetic API for the browser regression when the
// real test stack (Docker PG 25432 / Redis 25380 / scripted Worker) is unavailable.
//
//   node apps/web/tests/d01-mock-api.mjs      # listens on 127.0.0.1:3100
//
// It implements ONLY the public shapes consumed by apps/web, keeps everything in
// memory, uses no database, no model provider and no credentials. Every response is
// synthetic; "scripted_protocol" here is a label, NOT a real Worker run.
import {createServer} from 'node:http';
import {randomUUID} from 'node:crypto';

const PORT=Number(process.env.D01_MOCK_PORT??3100);
const USER_ID='11111111-2222-3333-4444-555555555555';
const GOAL_ID='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
const BANK_ID='bbbbbbbb-1111-2222-3333-444444444444';
const Q1='请说明 LangGraph 中 state、checkpointer 与 store 的职责差异，以及进程重启后如何恢复会话。';
const Q2='请说明 interrupt 与 Command(resume=...) 的恢复语义，以及副作用幂等如何处理。';
const bank={bank_id:BANK_ID,version:1,origin:'private',enabled:true,revision:1,
 content_sha256:'synthetic-fixture',name:'Agent开发岗位试运行',item_count:2};
const goal={id:GOAL_ID,version:1,revision:1,status:'WAITING_FOR_TOOL',
 conditions:{direction:'AI 应用 / Agent 开发',cities:['上海'],city_policy:'preferred',auto_research:false},
 jd_text:'',model_requests:0,profile:null,action:null,steps:[]};
const sessions=new Map();
let loggedIn=false;
const user={id:USER_ID,name:'合成测试学员',email:'synthetic@example.com'};

function makeSession(){
 return {id:randomUUID(),version:1,revision:1,status:'WAITING_FOR_USER',goal_version:goal.version,
  policy:{role:'ai-application',mode:'interview',max_questions:2,duration_seconds:3600},
  pack_version:'v1',deadline_at:new Date(Date.now()+3_600_000).toISOString(),
  question_banks:[bank],
  events:[{id:randomUUID(),seq:1,kind:'question',payload:{topic_id:'langgraph-checkpointer',
   question_id:'q1',text:Q1,kind:'short_answer',pack_version:'v1',assisted:false}}],
  observations:[],report:null,
  run:{id:randomUUID(),status:'SUCCEEDED',error_code:null,model_requests:0,
   origin:'scripted_protocol',tool_log:[],diagnostic:null}};
}
function applyAnswer(session,body){
 const next=Math.max(...session.events.map(event=>event.seq),0)+1;
 const answerId=randomUUID();
 session.events.push({id:answerId,seq:next,kind:'answer',payload:{
  text:String(body.text??''),question_id:String(body.question_id??''),
  topic_id:'langgraph-checkpointer',assisted:false}});
 session.version+=1;
 const questionCount=session.events.filter(event=>event.kind==='question').length;
 if(questionCount<2){
  session.events.push({id:randomUUID(),seq:next+1,kind:'question',payload:{
   topic_id:'langgraph-interrupt',question_id:'q2',text:Q2,kind:'short_answer',
   pack_version:'v1',assisted:false}});
  session.status='WAITING_FOR_USER';
 }else{
  const answers=session.events.filter(event=>event.kind==='answer');
  const observations=answers.map((event,index)=>({
   id:randomUUID(),answer_id:event.id,topic_id:'langgraph',label:'supported_in_this_answer',
   summary:'【合成脚本观察】回答已按原文与来源关联，不代表真实模型评价。',
   answer_quote:event.payload.text.slice(0,80),evidence_id:`ev-${index+1}`,
   evidence_quote:'【合成参考摘录】确定性流程的状态持久化说明。',origin:'scripted_protocol',
   verification:'scripted',evidence_source:{id:'src-synthetic',
    url:'https://example.invalid/scripted-protocol',origin:'offline_fixture',version:'v1'},
   correction:null}));
  session.observations=observations;
  session.report={stop_reason:'question_limit',model_summary:null,questions:2,answers:2,
   assisted_answers:0,observations,semantic_scoring_eligible:false,
   unreviewed_answer_ids:[],next_actions:[]};
  session.status='COMPLETED';
 }
 return session;
}
function send(res,status,body){
 res.writeHead(status,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});
 res.end(JSON.stringify(body));
}
const server=createServer((req,res)=>{
 let raw='';
 req.on('data',chunk=>{raw+=chunk;});
 req.on('end',()=>{
  let body={};
  try{body=raw?JSON.parse(raw):{};}catch{}
  const path=new URL(req.url,'http://127.0.0.1:'+PORT).pathname;
  const method=req.method??'GET';
  if(path==='/__reset'&&method==='POST'){
   sessions.clear();loggedIn=false;goal.version=1;goal.status='WAITING_FOR_TOOL';
   user.name='合成测试学员';user.email='synthetic@example.com';
   return send(res,200,{ok:true});
  }
  if(path==='/health')return send(res,200,{status:'ok',service:'python-interview',synthetic:true});
  if(path==='/api/auth/get-session')return send(res,200,loggedIn?{user}:null);
  if(path==='/api/auth/sign-up/email'&&method==='POST'){
   user.name=String(body.name??user.name);user.email=String(body.email??user.email);
   loggedIn=true;return send(res,200,{user});
  }
  if(path==='/api/auth/sign-in/email'&&method==='POST'){loggedIn=true;return send(res,200,{user});}
  if(path==='/api/auth/sign-out'&&method==='POST'){loggedIn=false;return send(res,200,{});}
  if(path==='/api/catalog/questions')return send(res,200,{items:[],assessment_mode:'scripted'});
  if(path==='/api/practice-sessions')return send(res,200,{items:[]});
  if(path==='/api/question-banks')return send(res,200,{banks:[bank]});
  if(path==='/api/question-banks/imports'&&method==='POST')return send(res,200,{preview_id:'synthetic-preview'});
  if(/^\/api\/question-banks\/imports\/[^/]+\/commit$/.test(path)&&method==='POST')
   return send(res,200,{bank_id:BANK_ID,version:1});
  if(path==='/api/career-goals/'&&method==='POST')return send(res,200,goal);
  if(path==='/api/career-goals/')return send(res,200,{items:[GOAL_ID]});
  if(/^\/api\/career-goals\/[^/]+\/jobs$/.test(path))return send(res,200,
   {goal_version:goal.version,goal_status:goal.status,runs:[],items:[],selected:null,sources:[]});
  const interviewBase=/^\/api\/career-goals\/[^/]+\/interviews$/.test(path);
  const answerPath=/^\/api\/career-goals\/[^/]+\/interviews\/([^/]+)\/answers$/.exec(path);
  const detailPath=/^\/api\/career-goals\/[^/]+\/interviews\/([^/]+)$/.exec(path);
  if(interviewBase&&method==='POST'){
   const session=makeSession();sessions.set(session.id,session);
   return send(res,202,session);
  }
  if(interviewBase)return send(res,200,{items:[...sessions.values()]});
  if(answerPath&&method==='POST'){
   const session=sessions.get(answerPath[1]);
   if(!session)return send(res,404,{code:'resource_unavailable'});
   return send(res,202,applyAnswer(session,body));
  }
  if(detailPath){
   const session=sessions.get(detailPath[1]);
   return session?send(res,200,session):send(res,404,{code:'resource_unavailable'});
  }
  if(/^\/api\/career-goals\/[^/]+$/.test(path))return send(res,200,goal);
  return send(res,200,{items:[]});
 });
});
server.listen(PORT,'127.0.0.1',()=>{
 console.log(JSON.stringify({kind:'D-01-01 synthetic API',port:PORT,
  note:'in-memory only; no PG/Redis/Worker; scripted_protocol is a label'}));
});
