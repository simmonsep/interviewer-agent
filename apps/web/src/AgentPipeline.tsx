import {useEffect,useRef,useState} from 'react';
import {api,post,ApiError} from './api.ts';
import {AGENT_LABEL} from './AgentDock.tsx';

// B+ / F06F / S01: one click prepares a resume draft, then a *confirmed* plan opens a mock
// interview session. This is a front-end deterministic orchestration of REAL backend
// endpoints, not an LLM deciding what to do. The third step is a local-record summary, NOT
// an Agent and NOT a model verdict. A created-but-unanswered interview is never labelled as
// a finished interview.
//
// S01 adds the human confirmation: the interview step first creates a preparation, shows the
// frozen plan, and only creates a session after the user approves it. Nothing is created when
// the plan is rejected or stale, and the user explicitly chooses whether to open a session
// without a resume instead of being silently pushed into one.

interface GoalRef{id:string;version:number;status:string}
interface MaterialView{sources:{id:string}[];material_revision:number;
 runs:{id:string;status:string;origin:string|null}[];drafts:{id:string;run_id:string|null}[]}
interface BankSummary{bank_id:string;version:number;name:string;enabled:boolean}
interface Session{id:string;status:string;policy:{mode:string}}
interface WorkflowView{selected_job:{title:string}|null;
 interviews:{id:string;status:string}[];written_tests:unknown[]}
interface JobsView{selected:unknown|null;runs:unknown[]}
interface PlanView{version:string;role:string;mode:string;profile?:'quick'|'full'|null;max_questions:number;duration_seconds:number;
 target:{kind:string;title?:string;direction?:string};draft:{id:string;version:number;fact_count:number}|null;
 selection:{snapshot_id:string;carried_from_version:number|null}|null;
 generated_question_count?:number;
 wiki_question_count?:number;
 experience_search_status?:string;
 experience_question_count?:number;
 selected_bank_count?:number;
 jd_coverage?:{planned_sources:string[]}[];
 public_reference_count?:number;
 public_reference_status?:'not_requested'|'disabled'|'unavailable'|'no_passage'|'retrieved';
 frozen:{goal_version:number;material_revision:number}}
interface PrepView{id:string;status:string;version:number;goal_version:number;plan:PlanView|null;
 plan_sha256:string|null;decision:{decision:string}|null;session_id:string|null;error_code:string|null;
 role:'java-backend'|'ai-application';mode:'interview'|'coaching';profile:'quick'|'full'|null;
 max_questions:number;
 duration_seconds:number;use_resume:boolean;
 frozen_memory?:Array<{id:string;topic_id:string;label:string;summary:string}>}

type Phase='idle'|'running'|'done'|'waiting'|'failed'|'skipped';
type Key='rewrite'|'interview'|'review';
const keys:Key[]=['rewrite','interview','review'];
// S05 L3-6: a failure that hides its stable code cannot be diagnosed or shown honestly.
const codeNote=(code:string|null)=>code?`（${code}）`:'';
const meta:Record<Key,{agent:string;intro:string}> = {
 rewrite:{agent:'完善简历',
  intro:'基于你保存的真实材料生成可编辑初稿。由简历修改 Agent 执行。'},
 interview:{agent:'模拟面试',
  intro:'生成训练计划后进入真实作答，出题后需要你本人作答。由面试 Agent 执行。'},
 review:{agent:'复盘记录',
  intro:'整理本次准备过程与本机记录，不调用 Agent，也不给能力评价。'}
};
// A "done" phase only means the local step succeeded. Spell out what that means
// per step so that a PENDING interview session is never shown as "已完成面试".
const doneLabel:Record<Key,string>={
 rewrite:'初稿已产出',interview:'面试已创建 · 待作答',review:'记录已汇总'};
const phaseText:Record<Phase,string>={idle:'未开始',running:'正在执行',done:'已完成',
 waiting:'等你确认',failed:'未通过',skipped:'未执行'};
const shortStatus=(key:Key,phase:Phase):string=>{
 if(phase==='idle')return '';
 if(phase==='running')return '正在向本机服务提交请求…';
 if(phase==='waiting')return '计划已生成，等你确认；确认前不会创建面试。';
 if(phase==='failed')return '这一步未通过，原因见下方提示。';
 if(phase==='skipped')return '前置步骤未完成，这一步没有执行。';
 return {
  rewrite:'改写初稿已产出，可在「材料与简历」逐句审阅。',
  interview:'面试场次已创建、状态为待作答；这不是一场已完成的面试。',
  review:'本机记录已汇总，下面是可以核对的真实读数。'
 }[key];
};
const BUSY_RUN_STATES=new Set(['PENDING','RUNNING','QUEUED','WAITING_FOR_TOOL']);
const TERMINAL_PREP=new Set(['SUCCEEDED','FAILED','STALE','REJECTED','CANCELLED']);

function reason(error:unknown):string{
 if(error instanceof ApiError){
  if(error.code==='version_conflict')return '目标状态已变化，请刷新后再试；没有写入任何新记录。';
  if(error.code==='interview_in_progress'||error.code==='interview_session_limit')return '本机已有一场进行中的面试，请先答完或停止它；流水线不会重复开场。';
  if(error.code==='preparation_in_progress')return '本机已有一个待确认的训练计划，请先处理它。';
  if(error.code==='preparation_plan_changed')return '你看到的计划已经变化，请刷新后再确认；没有创建任何面试。';
  if(error.code==='wiki_source_withdrawn')return '本场拟用的 Wiki 题目已撤回，请重新准备。';
  if(error.code==='preparation_not_awaiting_confirmation')return '这个计划已经不在等待确认（可能已过期或已处理）。';
  if(error.code==='coordination_in_progress')return '协调器还在处理当前目标，等它结束再跑流水线。';
  if(error.serverMessage)return error.serverMessage;
  return `本机接口返回 ${error.code}（${error.status}），这一步没有写入记录。`;
 }
 return error instanceof Error?error.message:'本机服务未响应，这一步没有写入记录。';
}

const ROLE_LABEL:Record<string,string>={'java-backend':'Java 后端','ai-application':'AI 应用 / Agent'};
type PrepOptions={role?:'java-backend'|'ai-application';mode?:'interview'|'coaching';
 profile?:'quick'|'full';max_questions?:number;duration_seconds?:number;use_resume?:boolean};
const PREP_DEFAULTS={max_questions:4,duration_seconds:1200};

function planText(plan:PlanView):string{
 const target=plan.target?.title||plan.target?.direction||plan.target?.kind||'本机目标';
 const draft=plan.draft?`草稿 ${plan.draft.id.slice(0,8)} v${plan.draft.version}（${plan.draft.fact_count} 条事实）`:'不使用草稿';
 const kind=plan.profile==='full'?'完整模拟':'快练';
 const sources=[];
 if(plan.wiki_question_count)sources.push(`复用 Wiki ${plan.wiki_question_count} 道题`);
 if(plan.generated_question_count)sources.push(`按岗位生成 ${plan.generated_question_count} 道题`);
 if(plan.experience_question_count)sources.push(`${plan.experience_question_count} 道题参考公开面经`);
 if(plan.experience_search_status==='missing_credentials')sources.push('未配置面经搜索 Key');
 if(!sources.length&&plan.generated_question_count===0)sources.push('使用已有题源');
 if(plan.jd_coverage?.length){
  const matched=plan.jd_coverage.filter(item=>item.planned_sources.length).length;
  sources.push(`已匹配 ${matched}/${plan.jd_coverage.length} 个岗位考点`);
 }
 const source=sources.length?` · ${sources.join(' · ')}`:'';
 const referenceCount=plan.public_reference_count??0;
 const reference=referenceCount>0?` · ${referenceCount} 道题附官方资料`
   :plan.public_reference_status&&plan.public_reference_status!=='not_requested'?' · 本场未附公开资料':'';
 return `${kind} · ${ROLE_LABEL[plan.role]??plan.role} · ${plan.max_questions} 题 · ${Math.round(plan.duration_seconds/60)} 分钟 · ${target} · ${draft}${source}${reference}`;
}

export function AgentPipeline({goal,onOpenPane,next,nextDisabled,nextUseResume,onNext,onOpenSession}:{
 goal:GoalRef|null;onOpenPane:(pane:'interview'|'materials'|'research'|'artifacts')=>void;
 /** F07Z-142: the state-driven next step. Null keeps the legacy single-button behaviour. */
 next?:{label:string;description:string}|null;
 nextDisabled?:boolean;
 /** F07Z-166: the user's "no resume this session" choice also applies to the quick path. */
 nextUseResume?:boolean;
 onNext?:()=>void;
 /** F07Z-166: a recovered preparation already produced this session. */
 onOpenSession?:(id:string)=>void;
}){
 const [phase,setPhase]=useState<Record<Key,Phase>>({rewrite:'idle',interview:'idle',review:'idle'});
 const [detail,setDetail]=useState<Record<Key,string>>({rewrite:'',interview:'',review:''});
 const [summary,setSummary]=useState<{drafts:number;sessions:number;newSession:string|null;
  sessionStatus:string;selected:string;researchRuns:number;origins:string[]}|null>(null);
 const [prep,setPrep]=useState<PrepView|null>(null);
 // F07Z-166: only the legacy one-click flow keeps its own plan card confirm; a plan the
 // server prepared for the user's training turn has one confirm, the workbench primary.
 const [ownPlan,setOwnPlan]=useState(false);
 // F07Z-166: a preparation left by a previous visit is read back from the server, not from a
 // component-local id, so a reload can resume at the same step.
 const [recovered,setRecovered]=useState<PrepView|null>(null);
 const [offerNoResume,setOfferNoResume]=useState(false);
 const [error,setError]=useState('');
 const [busy,setBusy]=useState(false);
 const lock=useRef(false);
 // F07Z-166: only the JD quick path opens the new session automatically.
 const autoOpen=useRef(false);
 const current=useRef<Key>('rewrite');

 const put=(key:Key,next:Phase,text:string)=>{
  setPhase(previous=>({...previous,[key]:next}));
  setDetail(previous=>({...previous,[key]:text}));
 };
 const notify=()=>window.dispatchEvent(new CustomEvent('py-agent-result'));

 // F07Z-166: the workbench owns the single primary while a preparation waits or fails.
 const decideRef=useRef(decide);
 const quickRef=useRef(quickPrepare);
 decideRef.current=decide;
 quickRef.current=quickPrepare;
 useEffect(()=>{
  const confirm=()=>{autoOpen.current=true;void decideRef.current('approve');};
  const retry=()=>void (async()=>{
   // F07Z-166: a Java failure must not be retried as an AI session, and explicit choices
   // (questions, duration, no-resume) must survive the retry.
   let options:PrepOptions={};
   if(goal){
    try{
     const current=await api<PrepView>(`/api/career-goals/${goal.id}/interview-preparations/current`);
     options={role:current.role,mode:current.mode,profile:current.profile??undefined,
      max_questions:current.max_questions,
      duration_seconds:current.duration_seconds,use_resume:current.use_resume};
    }catch{/* no readable task: fall back to the defaults */}
   }
   await quickRef.current(options);
  })();
  window.addEventListener('py-prep-confirm',confirm);
  window.addEventListener('py-prep-retry',retry);
  return()=>{window.removeEventListener('py-prep-confirm',confirm);window.removeEventListener('py-prep-retry',retry);};
 },[]);

 useEffect(()=>{
  if(!goal){setPrep(null);setRecovered(null);return;}
  let alive=true,timer=0;
  // F07Z-166: the request that asks for training is sent before the task exists. While the
  // coordinator is still working, keep looking for the row the server is about to create.
  const coordinating=['PENDING','RUNNING'].includes(goal.status);
  const load=async()=>{
   try{
    const value=await api<PrepView>(`/api/career-goals/${goal.id}/interview-preparations/current`);
    if(!alive||lock.current)return;
    if(value.status==='PENDING'||value.status==='RUNNING'){
     setPrep(null);setRecovered(null);
     put('interview','running','正在准备本场计划…');
     timer=window.setTimeout(()=>void load(),900);
    }else if(value.status==='WAITING_FOR_USER'){
     setPrep(value);setOwnPlan(false);
     put('interview','waiting','已恢复上次生成的计划；确认前不会创建面试。');
    }else if(value.status==='SUCCEEDED'&&value.session_id){
     setPrep(null);setRecovered(value);
     put('interview','done',`本目标的准备已完成；场次 ${value.session_id.slice(0,8)} 已创建。`);
    }else if(['FAILED','STALE','REJECTED','CANCELLED'].includes(value.status)){
     setPrep(null);setRecovered(null);
     put('interview','failed',`上次准备未完成${codeNote(value.error_code)}；可以重新准备。`);
    }
   }catch(e){
    if(!alive)return;
    if(!(e instanceof ApiError&&e.status===404))return;
    if(coordinating)timer=window.setTimeout(()=>void load(),900);
   }
  };
  void load();
  return()=>{alive=false;if(timer)window.clearTimeout(timer);};
 },[goal?.id,goal?.status]);

 async function waitPrep(goalId:string,id:string,until:Set<string>):Promise<PrepView>{
  for(let i=0;i<90;i++){
   const value=await api<PrepView>(`/api/career-goals/${goalId}/interview-preparations/${id}`);
   if(until.has(value.status))return value;
   await new Promise(resolve=>window.setTimeout(resolve,400));
  }
  throw new Error('准备任务仍在排队；这不是失败，稍后刷新本页可继续查看。');
 }

 async function review(sessionId:string|null,sessionStatus:string){
  current.current='review';
  put('review','running','正在读取本机记录…');
  if(!goal)return;
  const [materials,sessions,workflow,jobs]=await Promise.all([
   api<MaterialView>(`/api/career-goals/${goal.id}/materials`),
   api<{items:Session[]}>(`/api/career-goals/${goal.id}/interviews`),
   api<WorkflowView>(`/api/career-goals/${goal.id}/workflow`),
   api<JobsView>(`/api/career-goals/${goal.id}/jobs`)
  ]);
  const origins=[...new Set((materials.runs??[]).map(item=>item.origin).filter((item):item is string=>Boolean(item)))];
  const value={drafts:materials.drafts?.length??0,sessions:sessions.items?.length??0,
   newSession:sessionId,sessionStatus,selected:workflow.selected_job?.title??'尚未选岗',
   researchRuns:jobs.runs?.length??0,origins};
  setSummary(value);
  put('review','done',`汇总自本机记录：${value.drafts} 版初稿 · ${value.sessions} 场面试 · ${value.researchRuns} 次研究；不是模型生成的评语。`);
  notify();
 }

 async function openSessionWithoutResume(){
  if(!goal||busy||lock.current)return;
  lock.current=true;setBusy(true);setError('');
  current.current='interview';
  try{
   const live=await api<{version:number;status:string}>(`/api/career-goals/${goal.id}`);
   put('interview','running','正在按你的选择直接开场（不使用简历）…');
   const session=await post<Session>(`/api/career-goals/${goal.id}/interviews`,{
    expected_version:live.version,mode:'interview',
    max_questions:PREP_DEFAULTS.max_questions,duration_seconds:PREP_DEFAULTS.duration_seconds
   },crypto.randomUUID());
   setOfferNoResume(false);setPrep(null);
   put('interview','done',`已按你的选择直接创建面试场次 ${session.id.slice(0,8)}（状态 ${session.status}）；等待你在「面试训练」作答，不冒充已答完。`);
   notify();
   await review(session.id,session.status);
  }catch(e){
   const text=reason(e);setError(text);put('interview','failed',text);
  }finally{lock.current=false;setBusy(false);}
 }

 async function run(){
  if(!goal||busy||lock.current)return;
  autoOpen.current=false;
  lock.current=true;setBusy(true);setError('');setSummary(null);setPrep(null);setOfferNoResume(false);
  setPhase({rewrite:'idle',interview:'idle',review:'idle'});
  setDetail({rewrite:'',interview:'',review:''});
  current.current='rewrite';
  try{
   // Read the authoritative goal state instead of trusting a possibly stale
   // React prop: the coordinator may have just finished its turn.
   const [live,view]=await Promise.all([
    api<{id:string;version:number;status:string}>(`/api/career-goals/${goal.id}`),
    api<MaterialView>(`/api/career-goals/${goal.id}/materials`)
   ]);
   if(['PENDING','RUNNING'].includes(live.status))throw new Error('协调器还在处理这句话，等它跑完再启动流水线。');
   if((view.sources?.length??0)===0)throw new Error('请先在「材料与简历」保存至少一段材料原文；流水线不会替你编造经历。');
   if((view.runs??[]).some(item=>BUSY_RUN_STATES.has(item.status)))throw new Error('本机已有一个材料任务在排队，等它产出结果再启动流水线。');

   // ---- phase 1: resume Agent, real rewrite endpoint --------------------
   current.current='rewrite';
   put('rewrite','running','正在提交改写任务…');
   const rewrite=await post<{run_id:string}>(`/api/career-goals/${goal.id}/materials/rewrite`,
    {expected_version:live.version,expected_revision:view.material_revision},crypto.randomUUID());
   put('rewrite','running',`已入队 run ${rewrite.run_id.slice(0,8)}；等本机材料 Worker 产出初稿`);
   let succeeded=false;
   for(let i=0;i<60;i++){
    const state=await api<MaterialView>(`/api/career-goals/${goal.id}/materials`);
    if((state.runs??[]).some(item=>item.id===rewrite.run_id&&item.status==='SUCCEEDED')){
     succeeded=true;break;
    }
    if((state.runs??[]).some(item=>item.id===rewrite.run_id&&['FAILED','CANCELLED'].includes(item.status)))break;
    await new Promise(resolve=>window.setTimeout(resolve,500));
   }
   put('rewrite',succeeded?'done':'waiting',succeeded
    ?`run ${rewrite.run_id.slice(0,8)} 已产出初稿；结果落在「材料与简历」，侧栏角标会显示版数。`
    :`run ${rewrite.run_id.slice(0,8)} 仍在排队或尚未产出初稿；不把排队说成已完成。`);
   notify();

   // ---- phase 2: preparation -> plan -> human confirmation --------------
   current.current='interview';
   put('interview','running','正在生成本次训练计划…');
   const created=await post<PrepView>(`/api/career-goals/${goal.id}/interview-preparations`,{
    expected_version:live.version,mode:'interview',
    max_questions:PREP_DEFAULTS.max_questions,duration_seconds:PREP_DEFAULTS.duration_seconds
   },crypto.randomUUID());
   const next=new Set(['WAITING_FOR_USER',...TERMINAL_PREP]);
   const planned=await waitPrep(goal.id,created.id,next);
   if(planned.status!=='WAITING_FOR_USER'){
    setOfferNoResume(true);
    throw new Error(planned.error_code==='context_changed'
     ?'材料或目标在准备期间发生变化，计划已过期；没有创建面试。'
     :`训练计划没有生成成功${codeNote(planned.error_code)}；没有创建面试。`);
   }
   setPrep(planned);setOwnPlan(true);
   put('interview','waiting','计划已生成，等你确认后才会开场；确认前不会创建任何面试。');
   notify();
  }catch(e){
   const text=reason(e);
   setError(text);
   put(current.current,'failed',text);
   for(const key of keys){
    if(key===current.current)continue;
    setPhase(previous=>previous[key]==='idle'?{...previous,[key]:'skipped'}:previous);
   }
  }finally{lock.current=false;setBusy(false);}
 }

 // F07Z-166 step 1: prepare from the current goal/JD without the resume-rewrite phase.
 async function quickPrepare(options:PrepOptions={}){
  if(!goal||busy||lock.current)return;
  autoOpen.current=true;
  lock.current=true;setBusy(true);setError('');
  current.current='interview';
  try{
   const live=await api<GoalRef>(`/api/career-goals/${goal.id}`);
   if(['PENDING','RUNNING'].includes(live.status))throw new Error('协调器还在处理这句话，等它跑完再准备这场。');
   if(live.status==='CANCELLED')throw new Error('当前目标已结束，请先新建或选择其他目标。');
   const useResume=options.use_resume??nextUseResume;
   const planned=await post<PrepView>(`/api/career-goals/${goal.id}/interview-preparations`,{
    expected_version:live.version,mode:options.mode??'interview',
    ...(options.profile?{profile:options.profile}:{}),
    max_questions:options.max_questions??PREP_DEFAULTS.max_questions,
    duration_seconds:options.duration_seconds??PREP_DEFAULTS.duration_seconds,
    ...(options.role?{role:options.role}:{}),
    ...(useResume===false?{use_resume:false}:{})},crypto.randomUUID());
   put('interview','running','已提交准备，等待本机生成计划…');
   // Let the workbench primary follow this task too, not only the plan card.
   notify();
   const ready=await waitPrep(goal.id,planned.id,new Set(['WAITING_FOR_USER',...TERMINAL_PREP]));
   if(ready.status!=='WAITING_FOR_USER'){
    throw new Error(ready.error_code==='context_changed'
     ?'目标或材料在准备期间发生变化，计划已过期；没有创建面试。'
     :`训练计划没有生成成功${codeNote(ready.error_code)}；没有创建面试。`);
   }
   setPrep(ready);setRecovered(null);setOwnPlan(false);
   put('interview','waiting','计划已生成，等你确认后才会开场；确认前不会创建任何面试。');
   notify();
  }catch(e){
   const text=reason(e);setError(text);put('interview','failed',text);
  }finally{lock.current=false;setBusy(false);}
 }
 async function reselect(){
  if(!goal||!prep||busy||lock.current)return;
  lock.current=true;setBusy(true);setError('');
  try{
   await post<PrepView>(`/api/career-goals/${goal.id}/interview-preparations/${prep.id}/confirm`,
    {expected_version:prep.version,plan_sha256:prep.plan_sha256,decision:'reject'},crypto.randomUUID());
   // The React prop may lag one turn; delete against the authoritative version.
   const live=await api<{version:number}>(`/api/career-goals/${goal.id}`);
   await api(`/api/career-goals/${goal.id}/selection?expected_version=${live.version}`,{method:'DELETE'});
   setPrep(null);setOfferNoResume(true);
   put('interview','skipped','已撤销本次沿用的选岗；请在岗位页重新选择后再准备。');
   onOpenPane?.('research');
   notify();
  }catch(e){
   const text=reason(e);setError(text);put('interview','failed',text);
  }finally{lock.current=false;setBusy(false);}
 }
 async function decide(decision:'approve'|'reject'){
  if(!goal||!prep||busy||lock.current)return;
  lock.current=true;setBusy(true);setError('');
  current.current='interview';
  try{
   await post<PrepView>(`/api/career-goals/${goal.id}/interview-preparations/${prep.id}/confirm`,
    {expected_version:prep.version,plan_sha256:prep.plan_sha256,decision},crypto.randomUUID());
   if(decision==='reject'){
    setPrep(null);setOfferNoResume(true);
    put('interview','skipped','你选择不开场：本机没有创建面试。需要的话可以「不带简历直接开场」。');
    notify();return;
   }
   put('interview','running','已确认，正在开场…');
   const done=await waitPrep(goal.id,prep.id,TERMINAL_PREP);
   if(done.status!=='SUCCEEDED'||!done.session_id){
    setPrep(null);setOfferNoResume(true);
    throw new Error(done.error_code==='context_changed'
     ?'确认期间材料或目标发生变化，本次没有开场；可以重新准备或直接无简历开场。'
     :`开场没有完成${codeNote(done.error_code)}；没有创建面试。`);
   }
   setPrep(null);
   put('interview','done',
    `你在计划上点了确认；面试场次 ${done.session_id.slice(0,8)} 已创建（状态待作答），等待你在「面试训练」作答，不冒充已答完。`);
   notify();
   await review(done.session_id,'PENDING');
   if(autoOpen.current){autoOpen.current=false;onOpenSession?.(done.session_id);}
  }catch(e){
   const text=reason(e);setError(text);put('interview','failed',text);
  }finally{lock.current=false;setBusy(false);}
 }

 // F07Z-143: once the state-driven 下一步 owns the primary, this one-click flow is an
 // option, not "the next step". Folding its own step list keeps the card from saying
 // "复盘记录 未开始" right above a 「查看本次复盘」 button.
 const steps=<ol className="d-pipeline-steps">
   {keys.map((key,index)=><li key={key} data-testid={`pipeline-step-${key}`} data-phase={phase[key]}>
    <span className="d-pipeline-index" aria-hidden="true">{index+1}</span>
    <div className="d-pipeline-body">
     <div className="d-pipeline-head"><strong>{meta[key].agent}</strong>
      <span className="tag d-pipeline-tag" data-phase={phase[key]}
       data-testid={`pipeline-tag-${key}`}>{phase[key]==='done'?doneLabel[key]:phaseText[phase[key]]}</span></div>
     <p className="fine-print">{meta[key].intro}</p>
     <p className="d-pipeline-status" data-testid={`pipeline-status-${key}`}
      title={detail[key]||undefined}>{shortStatus(key,phase[key])}</p>
    </div>
   </li>)}
  </ol>;
 return <section className="card d-card d-pipeline" data-testid="agent-pipeline" aria-label="一键准备简历与模拟面试">
  <div className="d-card-head d-pipeline-head">
   <div><h2>下一步</h2>
    {next&&goal
     ? <p className="d-pipeline-lede">{next.label}</p>
     : <p className="d-pipeline-lede">准备简历与模拟面试</p>}
    {next&&goal&&<p className="d-pipeline-why">{next.description}</p>}
    <span className="eyebrow">一次准备 · 简历 + 面试 + 汇总</span></div>
   <div className="d-pipeline-cta">
    {next&&goal
     ? <button type="button" className="primary" data-testid="workbench-next-action" data-workbench-primary-action
        disabled={busy||nextDisabled} onClick={onNext}>{next.label}</button>
     : <button type="button" className="primary" data-testid="pipeline-run" data-workbench-primary-action={goal?true:undefined}
        disabled={!goal||busy} onClick={()=>void run()}>{busy?'正在推进…':'开始准备'}</button>}
    {next&&goal&&<button type="button" className="text-button d-pipeline-run-alt" data-testid="pipeline-run"
      disabled={busy||nextDisabled} onClick={()=>void run()}>{busy?'正在推进…':'一次做完：简历 + 面试'}</button>}
    {goal&&<button type="button" className="secondary d-pipeline-quick" data-testid="pipeline-quick-prep"
      disabled={busy||nextDisabled||Boolean(prep)||Boolean(recovered?.session_id)}
      onClick={()=>void quickPrepare()}>按当前 JD 直接准备{nextUseResume===false?'（不用简历）':''}</button>}
    {goal&&<button type="button" className="secondary d-pipeline-full" data-testid="pipeline-full-sim"
      disabled={busy||nextDisabled||Boolean(prep)||Boolean(recovered?.session_id)}
      onClick={()=>void quickPrepare({profile:'full'})}>完整模拟（约 45 分钟，10–12 题）</button>}
   </div>
  </div>
  {!goal&&<p className="muted">先创建求职目标，下面两步才会绑定到本机记录。</p>}
  {error&&<p role="alert" className="error" data-testid="pipeline-error">{error}</p>}
  {prep&&<div className="d-prep-plan" data-testid="pipeline-plan">
   <h4>本次训练计划 · 待你确认</h4>
   <p data-testid="pipeline-plan-text">{planText(prep.plan!)}</p>
   <p className="fine-print">确认后只创建一场面试；驳回或让它过期都不会创建面试。
    计划的指纹 <code data-testid="pipeline-plan-sha">{prep.plan_sha256?.slice(0,12)}</code> 用来保证你确认的就是这一版。</p>
   <p className="fine-print" data-testid="pipeline-memory">本次准备可用的跨场证据：{(prep.frozen_memory??[]).length} 条{(prep.frozen_memory??[]).length>0?`（${(prep.frozen_memory??[]).map(item=>item.topic_id).join("、")}）`:""}。</p>
    <p className="fine-print" data-testid="pipeline-bank-source">
     {prep.plan?.selected_bank_count
      ?`题源包括角色默认题源和本次指定的 ${prep.plan.selected_bank_count} 个题库。`
      :'角色默认题源可用于补充本场题目。'}
    </p>
    {prep.plan?.selection?.carried_from_version!=null&&<p className="fine-print" data-testid="pipeline-carried">
     沿用上一版选岗与绑定简历（来自目标版本 {prep.plan.selection.carried_from_version}）。
     <button type="button" className="text-button" data-testid="pipeline-reselect" disabled={busy}
      onClick={()=>void reselect()}>重新选岗</button></p>}
    {nextUseResume===false&&<p className="fine-print" data-testid="pipeline-no-resume">本场按你的选择不使用简历。</p>}
   <div className="d-pipeline-links">
     {(!next||ownPlan)&&<button type="button" className={next?'secondary':'primary'} data-testid="pipeline-confirm" disabled={busy}
      onClick={()=>void decide('approve')}>确认并开场</button>}
    <button type="button" className="text-button" data-testid="pipeline-reject" disabled={busy}
     onClick={()=>void decide('reject')}>这次不开场</button>
   </div>
  </div>}
  {recovered?.session_id&&<div className="d-pipeline-noresume" data-testid="pipeline-recovered-session">
   <p className="fine-print">本目标的训练计划已经确认，面试场次 {recovered.session_id.slice(0,8)} 已创建；
    直接打开会回到那一场，不会重复开场。</p>
   {onOpenSession&&<button type="button" className="secondary" data-testid="pipeline-open-recovered"
    onClick={()=>onOpenSession(recovered.session_id!)}>打开这场面试</button>}
  </div>}
  {next&&goal
   ?<details className="d-pipeline-more" data-testid="pipeline-more">
     <summary>一次做完会做什么：简历初稿 + 模拟面试</summary>{steps}</details>
   :steps}
  {offerNoResume&&phase.interview!=='done'&&<div className="d-pipeline-noresume" data-testid="pipeline-no-resume">
   <p className="fine-print">这次没有生成可确认的计划。要不要不带简历直接开一场？这一步由你决定，不会自动开场。</p>
   <button type="button" className="text-button" data-testid="pipeline-open-no-resume" disabled={busy}
    onClick={()=>void openSessionWithoutResume()}>不带简历直接开场</button>
  </div>}
  {summary&&<div className="d-pipeline-summary" data-testid="pipeline-summary">
   <h3>本次执行摘要 · 本机记录汇总</h3>
   <div className="d-pipeline-numbers">
    <div><b className="d-num d-num-mid">{summary.drafts}</b><span>版改写初稿</span></div>
    <div><b className="d-num d-num-mid">{summary.sessions}</b><span>场面试会话</span></div>
    <div><b className="d-num d-num-mid">{summary.researchRuns}</b><span>次岗位研究</span></div>
   </div>
   <p className="fine-print">本场 {summary.newSession?.slice(0,8)} 状态 {summary.sessionStatus} · 选定岗位：{summary.selected}。这是记录汇总，不是能力判定或模型评语。</p>
   <div className="d-pipeline-links">
    <button type="button" className="text-button" onClick={()=>onOpenPane('materials')}>看改写初稿</button>
    <button type="button" className="text-button" onClick={()=>onOpenPane('interview')}>去答这一场</button>
    <button type="button" className="text-button" onClick={()=>onOpenPane('artifacts')}>看 Agent 产物</button>
   </div>
  </div>}
 </section>;
}
